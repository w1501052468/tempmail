from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ..db import get_connection
from ..errors import AuthenticationError, MessageNotFoundError, RateLimitExceededError
from ..schemas import (
    AttachmentInfo,
    InboxListResponse,
    MailboxPurgeResponse,
    MessageDeleteResponse,
    MessageDetailResponse,
    MessageSummary,
)
from ..services.mailbox_service import (
    authenticate_mailbox,
    delete_message,
    get_latest_message,
    get_message_attachment,
    get_message,
    get_message_raw_path,
    list_messages,
    note_inbox_access,
    purge_mailbox_messages,
)
from ..services.system_event_service import emit_system_event
from ..storage import resolve_relative_path
from .deps import get_client_ip, get_external_base_url, get_mailbox_token

router = APIRouter(prefix="/api/v1/inbox", tags=["inbox"])


def _authenticate_request(conn, *, token: str, client_ip: str | None, action: str) -> tuple[dict, str]:
    mailbox, token_hash_value = authenticate_mailbox(conn, token=token)
    note_inbox_access(
        conn,
        mailbox_id=mailbox["id"],
        token_hash_value=token_hash_value,
        client_ip=client_ip,
        action=action,
    )
    return mailbox, token_hash_value


def _build_message_detail_response(request: Request, *, mailbox: dict, message: dict, attachments: list[dict]) -> MessageDetailResponse:
    base_url = get_external_base_url(request)
    message_id = message["id"]
    return MessageDetailResponse(
        **message,
        mailbox_address=mailbox["address"],
        raw_url=f"{base_url}/api/v1/inbox/messages/{message_id}/raw",
        attachments=[
            AttachmentInfo(
                id=row["id"],
                filename=row["filename"],
                content_type=row["content_type"],
                size_bytes=row["size_bytes"],
                download_url=f"{base_url}/api/v1/inbox/messages/{message_id}/attachments/{row['id']}",
            )
            for row in attachments
        ],
    )


@router.get("/messages", response_model=InboxListResponse)
def list_messages_route(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    token: str = Depends(get_mailbox_token),
):
    client_ip = get_client_ip(request)
    with get_connection() as conn:
        try:
            mailbox, _ = _authenticate_request(
                conn,
                token=token,
                client_ip=client_ip,
                action="inbox_list",
            )
            messages = list_messages(conn, mailbox_id=mailbox["id"], limit=limit, offset=offset)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RateLimitExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc

    return InboxListResponse(
        mailbox_address=mailbox["address"],
        expires_at=mailbox["expires_at"],
        items=[MessageSummary(**row) for row in messages],
    )


@router.get("/messages/latest", response_model=MessageDetailResponse)
def get_latest_message_route(
    request: Request,
    token: str = Depends(get_mailbox_token),
):
    client_ip = get_client_ip(request)
    with get_connection() as conn:
        try:
            mailbox, _ = _authenticate_request(
                conn,
                token=token,
                client_ip=client_ip,
                action="inbox_get_latest_message",
            )
            message, attachments = get_latest_message(conn, mailbox_id=mailbox["id"])
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RateLimitExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _build_message_detail_response(request, mailbox=mailbox, message=message, attachments=attachments)


@router.delete("/messages", response_model=MailboxPurgeResponse)
def purge_mailbox_messages_route(
    request: Request,
    token: str = Depends(get_mailbox_token),
):
    client_ip = get_client_ip(request)
    with get_connection() as conn:
        try:
            mailbox, _ = _authenticate_request(
                conn,
                token=token,
                client_ip=client_ip,
                action="inbox_purge_messages",
            )
            result = purge_mailbox_messages(conn, mailbox_id=mailbox["id"])
            emit_system_event(
                conn,
                event_type="mailbox_messages_purged",
                source="inbox_api",
                mailbox_id=mailbox["id"],
                address=mailbox["address"],
                summary=f"Purged {result['deleted_messages']} messages from {mailbox['address']}",
                payload={
                    "deleted_messages": result["deleted_messages"],
                    "deleted_attachments": result["deleted_attachments"],
                    "deleted_files": result["deleted_files"],
                    "client_ip": client_ip,
                },
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RateLimitExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc

    return MailboxPurgeResponse(mailbox_address=mailbox["address"], **result)


@router.get("/messages/{message_id}", response_model=MessageDetailResponse)
def get_message_route(
    request: Request,
    message_id: UUID,
    token: str = Depends(get_mailbox_token),
):
    client_ip = get_client_ip(request)
    with get_connection() as conn:
        try:
            mailbox, _ = _authenticate_request(
                conn,
                token=token,
                client_ip=client_ip,
                action="inbox_get_message",
            )
            message, attachments = get_message(conn, mailbox_id=mailbox["id"], message_id=message_id)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RateLimitExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _build_message_detail_response(request, mailbox=mailbox, message=message, attachments=attachments)


@router.delete("/messages/{message_id}", response_model=MessageDeleteResponse)
def delete_message_route(
    request: Request,
    message_id: UUID,
    token: str = Depends(get_mailbox_token),
):
    client_ip = get_client_ip(request)
    with get_connection() as conn:
        try:
            mailbox, _ = _authenticate_request(
                conn,
                token=token,
                client_ip=client_ip,
                action="inbox_delete_message",
            )
            deleted = delete_message(conn, mailbox_id=mailbox["id"], message_id=message_id)
            emit_system_event(
                conn,
                event_type="message_deleted",
                source="inbox_api",
                mailbox_id=mailbox["id"],
                message_id=deleted["id"],
                address=mailbox["address"],
                summary=f"Deleted one message from {mailbox['address']}",
                payload={
                    "subject": deleted["subject"],
                    "attachment_count": deleted["attachment_count"],
                    "deleted_files": deleted["deleted_files"],
                    "client_ip": client_ip,
                },
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RateLimitExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return MessageDeleteResponse(mailbox_address=mailbox["address"], **deleted)


@router.get("/messages/{message_id}/raw")
def download_raw_route(
    request: Request,
    message_id: UUID,
    token: str = Depends(get_mailbox_token),
):
    client_ip = get_client_ip(request)
    with get_connection() as conn:
        try:
            mailbox, _ = _authenticate_request(
                conn,
                token=token,
                client_ip=client_ip,
                action="inbox_download_raw",
            )
            raw_path = get_message_raw_path(conn, mailbox_id=mailbox["id"], message_id=message_id)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RateLimitExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    raw_path = resolve_relative_path(raw_path)
    if not raw_path.exists():
        raise HTTPException(status_code=404, detail="Raw message file not found")
    return FileResponse(
        path=raw_path,
        media_type="message/rfc822",
        filename=f"{message_id}.eml",
    )


@router.get("/messages/{message_id}/attachments/{attachment_id}")
def download_attachment_route(
    request: Request,
    message_id: UUID,
    attachment_id: UUID,
    token: str = Depends(get_mailbox_token),
):
    client_ip = get_client_ip(request)
    with get_connection() as conn:
        try:
            mailbox, _ = _authenticate_request(
                conn,
                token=token,
                client_ip=client_ip,
                action="inbox_download_attachment",
            )
            attachment = get_message_attachment(
                conn,
                mailbox_id=mailbox["id"],
                message_id=message_id,
                attachment_id=attachment_id,
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RateLimitExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    attachment_path = resolve_relative_path(attachment["storage_path"])
    if not attachment_path.exists():
        raise HTTPException(status_code=404, detail="Attachment file not found")
    return FileResponse(
        path=attachment_path,
        media_type=attachment["content_type"],
        filename=attachment["filename"] or f"{attachment_id}.bin",
    )
