from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from ..db import get_connection
from ..errors import (
    AuthenticationError,
    InvalidDomainError,
    InvalidMailboxAddressError,
    MailboxConflictError,
    MailboxCreationError,
    RateLimitExceededError,
)
from ..schemas import CreateMailboxRequest, MailboxCreateResponse, MailboxDisableResponse
from ..services.mailbox_service import (
    authenticate_mailbox,
    create_mailbox,
    disable_mailbox,
    record_access_event,
)
from ..services.system_event_service import emit_system_event
from .deps import get_client_ip, get_external_base_url, get_mailbox_token

router = APIRouter(prefix="/api/v1/mailboxes", tags=["mailboxes"])


@router.post("", response_model=MailboxCreateResponse, status_code=status.HTTP_201_CREATED)
def create_mailbox_route(request: Request, payload: CreateMailboxRequest | None = Body(default=None)):
    payload = payload or CreateMailboxRequest()
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent")
    with get_connection() as conn:
        try:
            mailbox = create_mailbox(
                conn,
                client_ip=client_ip,
                user_agent=user_agent,
                requested_address=payload.address,
                requested_domain=payload.domain,
                ttl_minutes=payload.ttl_minutes,
            )
        except (InvalidDomainError, InvalidMailboxAddressError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except MailboxConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RateLimitExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except MailboxCreationError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    base_url = get_external_base_url(request)
    return MailboxCreateResponse(
        address=mailbox["address"],
        token=mailbox["token"],
        created_at=mailbox["created_at"],
        expires_at=mailbox["expires_at"],
        list_messages_url=f"{base_url}/api/v1/inbox/messages",
        message_detail_url_template=f"{base_url}/api/v1/inbox/messages/{{message_id}}",
    )


@router.delete("/current", response_model=MailboxDisableResponse)
def disable_mailbox_route(
    request: Request,
    token: str = Depends(get_mailbox_token),
):
    client_ip = get_client_ip(request)
    with get_connection() as conn:
        try:
            mailbox, token_hash_value = authenticate_mailbox(conn, token=token)
            disabled = disable_mailbox(conn, mailbox_id=mailbox["id"])
            record_access_event(
                conn,
                action="disable_mailbox",
                ip=client_ip,
                mailbox_id=mailbox["id"],
                token_hash_value=token_hash_value,
            )
            emit_system_event(
                conn,
                event_type="mailbox_disabled",
                source="mailbox_api",
                mailbox_id=mailbox["id"],
                address=mailbox["address"],
                summary=f"Mailbox disabled for {mailbox['address']}",
                payload={
                    "client_ip": client_ip,
                    "disabled_at": disabled["disabled_at"].isoformat(),
                },
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    return MailboxDisableResponse(**disabled)
