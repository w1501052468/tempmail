import asyncio
import json
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from .. import __version__
from ..config import get_settings
from ..db import get_connection
from ..errors import (
    InvalidDomainError,
    InvalidMailboxAddressError,
    MailboxConflictError,
    MailboxCreationError,
    MessageNotFoundError,
)
from ..schemas import (
    AdminManagedDomainListResponse,
    AdminDomainPolicyListResponse,
    AdminLoginRequest,
    AdminLoginResponse,
    AdminMailboxListResponse,
    AdminMessageListResponse,
    AdminRuntimeConfigResponse,
    AdminSystemEventListResponse,
    CreateMailboxRequest,
    DomainPolicyCreateRequest,
    DomainPolicyRecord,
    DomainPolicyUpdateRequest,
    MailboxCreateResponse,
    ManagedDomainCreateRequest,
    ManagedDomainRecord,
)
from ..security import create_admin_session, verify_admin_credentials
from ..services.admin_service import (
    create_admin_domain,
    create_admin_domain_policy,
    create_admin_mailbox,
    delete_admin_domain_policy,
    disable_admin_mailbox,
    get_admin_domain_detail,
    get_admin_config,
    get_admin_message_attachment,
    get_admin_mailbox_detail,
    get_admin_message_detail,
    get_admin_message_raw_path,
    get_admin_overview,
    list_access_events,
    list_admin_domains,
    list_admin_audit_logs,
    list_admin_domain_policies,
    list_admin_mailboxes,
    list_admin_messages,
    list_monitor_events,
    recheck_admin_domain,
    record_admin_audit,
    update_admin_domain_policy,
    update_admin_config,
)
from ..services.system_event_service import emit_system_event
from ..storage import resolve_relative_path
from .deps import get_client_ip, get_external_base_url, require_admin_session

router = APIRouter(tags=["admin"])
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ADMIN_HTML_PATH = STATIC_DIR / "admin.html"


def _admin_asset_version() -> str:
    latest_mtime = max(
        int((STATIC_DIR / asset_name).stat().st_mtime)
        for asset_name in ("admin.css", "admin.js")
    )
    return f"{__version__}-{latest_mtime}"


def _render_admin_html() -> str:
    return ADMIN_HTML_PATH.read_text(encoding="utf-8").replace("__ASSET_VERSION__", _admin_asset_version())


def _safe_record_admin_audit(*, action: str, admin_username: str, client_ip: str | None, metadata: dict | None = None) -> None:
    try:
        with get_connection() as conn:
            record_admin_audit(
                conn,
                action=action,
                admin_username=admin_username,
                client_ip=client_ip,
                metadata=metadata,
            )
    except Exception:
        # Admin audit logging should not block authentication flows.
        return


def _safe_emit_system_event(
    *,
    event_type: str,
    source: str,
    summary: str,
    level: str = "info",
    mailbox_id=None,
    message_id=None,
    address: str | None = None,
    payload: dict | None = None,
) -> None:
    try:
        with get_connection() as conn:
            emit_system_event(
                conn,
                event_type=event_type,
                source=source,
                summary=summary,
                level=level,
                mailbox_id=mailbox_id,
                message_id=message_id,
                address=address,
                payload=payload,
            )
    except Exception:
        return


@router.get("/admin")
def admin_page():
    return HTMLResponse(
        content=_render_admin_html(),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
        },
    )


@router.post("/api/v1/admin/login", response_model=AdminLoginResponse)
def admin_login(payload: AdminLoginRequest, request: Request, response: Response):
    username = payload.username.strip()
    client_ip = get_client_ip(request)
    if not verify_admin_credentials(username, payload.password):
        _safe_record_admin_audit(
            action="login_failed",
            admin_username=username or "<empty>",
            client_ip=client_ip,
            metadata={"reason": "invalid_credentials"},
        )
        _safe_emit_system_event(
            event_type="admin_login_failed",
            source="admin",
            level="warning",
            summary=f"Admin login failed for {username or '<empty>'}",
            payload={"client_ip": client_ip},
        )
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    session_token = create_admin_session(username)
    settings = get_settings()
    response.set_cookie(
        key="tempmail_admin_session",
        value=session_token,
        httponly=True,
        secure=(request.headers.get("x-forwarded-proto", request.url.scheme) == "https"),
        samesite="lax",
        max_age=settings.admin_session_hours * 3600,
        path="/",
    )
    _safe_record_admin_audit(
        action="login",
        admin_username=username,
        client_ip=client_ip,
    )
    _safe_emit_system_event(
        event_type="admin_login",
        source="admin",
        summary=f"Admin login for {username}",
        payload={"admin_username": username, "client_ip": client_ip},
    )
    return AdminLoginResponse(
        username=username,
        expires_in_hours=settings.admin_session_hours,
    )


@router.post("/api/v1/admin/logout")
def admin_logout(
    request: Request,
    response: Response,
    admin_username: str = Depends(require_admin_session),
):
    client_ip = get_client_ip(request)
    response.delete_cookie("tempmail_admin_session", path="/")
    _safe_record_admin_audit(
        action="logout",
        admin_username=admin_username,
        client_ip=client_ip,
    )
    _safe_emit_system_event(
        event_type="admin_logout",
        source="admin",
        summary=f"Admin logout for {admin_username}",
        payload={"admin_username": admin_username, "client_ip": client_ip},
    )
    return {"status": "ok"}


@router.get("/api/v1/admin/session")
def admin_session(admin_username: str = Depends(require_admin_session)):
    return {"username": admin_username}


@router.get("/api/v1/admin/overview")
def admin_overview(admin_username: str = Depends(require_admin_session)):
    with get_connection() as conn:
        return get_admin_overview(conn)


@router.get("/api/v1/admin/mailboxes", response_model=AdminMailboxListResponse)
def admin_mailboxes(
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        result = list_admin_mailboxes(conn, status=status, query=q, limit=limit, offset=offset)
    return AdminMailboxListResponse(**result)


@router.get("/api/v1/admin/messages", response_model=AdminMessageListResponse)
def admin_messages(
    q: str | None = None,
    limit: int = 25,
    offset: int = 0,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        result = list_admin_messages(conn, query=q, limit=limit, offset=offset)
    return AdminMessageListResponse(**result)


@router.post(
    "/api/v1/admin/mailboxes",
    response_model=MailboxCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def admin_mailbox_create(
    request: Request,
    payload: CreateMailboxRequest | None = Body(default=None),
    admin_username: str = Depends(require_admin_session),
):
    payload = payload or CreateMailboxRequest()
    with get_connection() as conn:
        try:
            mailbox = create_admin_mailbox(
                conn,
                payload=payload.model_dump(),
                admin_username=admin_username,
                client_ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
        except (InvalidDomainError, InvalidMailboxAddressError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except MailboxConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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


@router.get("/api/v1/admin/mailboxes/{mailbox_id}")
def admin_mailbox_detail(
    mailbox_id: UUID,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        try:
            return get_admin_mailbox_detail(conn, mailbox_id=mailbox_id)
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/v1/admin/mailboxes/{mailbox_id}/disable")
def admin_mailbox_disable(
    mailbox_id: UUID,
    request: Request,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        try:
            return disable_admin_mailbox(
                conn,
                mailbox_id=mailbox_id,
                admin_username=admin_username,
                client_ip=get_client_ip(request),
            )
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/admin/messages/{message_id}")
def admin_message_detail(
    message_id: UUID,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        try:
            return get_admin_message_detail(conn, message_id=message_id)
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/admin/messages/{message_id}/raw")
def admin_message_raw(
    message_id: UUID,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        try:
            raw_path = get_admin_message_raw_path(conn, message_id=message_id)
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    raw_path = resolve_relative_path(raw_path)
    if not raw_path.exists():
        raise HTTPException(status_code=404, detail="Raw message file not found")
    return FileResponse(raw_path, media_type="message/rfc822", filename=f"{message_id}.eml")


@router.get("/api/v1/admin/messages/{message_id}/attachments/{attachment_id}")
def admin_message_attachment(
    message_id: UUID,
    attachment_id: UUID,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        try:
            target = get_admin_message_attachment(conn, message_id=message_id, attachment_id=attachment_id)
        except MessageNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    file_path = resolve_relative_path(target["storage_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment file not found")
    return FileResponse(
        file_path,
        media_type=target["content_type"],
        filename=target["filename"] or f"{attachment_id}.bin",
    )


@router.get("/api/v1/admin/events")
def admin_events(
    limit: int = 100,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        return {"items": list_access_events(conn, limit=limit)}


@router.get("/api/v1/admin/audit")
def admin_audit(
    limit: int = 100,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        return {"items": list_admin_audit_logs(conn, limit=limit)}


@router.get("/api/v1/admin/domains", response_model=AdminManagedDomainListResponse)
def admin_domains(
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        return list_admin_domains(conn, status=status, limit=limit, offset=offset)


@router.get("/api/v1/admin/domains/{domain_id}", response_model=ManagedDomainRecord)
def admin_domain_detail(
    domain_id: UUID,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        try:
            return get_admin_domain_detail(conn, domain_id=domain_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/v1/admin/domains", response_model=ManagedDomainRecord)
def admin_domain_create(
    payload: ManagedDomainCreateRequest,
    request: Request,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        try:
            return create_admin_domain(
                conn,
                payload=payload.model_dump(),
                admin_username=admin_username,
                client_ip=get_client_ip(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/admin/domains/{domain_id}/recheck", response_model=ManagedDomainRecord)
def admin_domain_recheck(
    domain_id: UUID,
    request: Request,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        try:
            return recheck_admin_domain(
                conn,
                domain_id=domain_id,
                admin_username=admin_username,
                client_ip=get_client_ip(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/admin/policies", response_model=AdminDomainPolicyListResponse)
def admin_policies(
    scope: str | None = None,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        return list_admin_domain_policies(conn, scope=scope, status=status, limit=limit, offset=offset)


@router.post("/api/v1/admin/policies", response_model=DomainPolicyRecord)
def admin_policy_create(
    payload: DomainPolicyCreateRequest,
    request: Request,
    admin_username: str = Depends(require_admin_session),
):
    try:
        with get_connection() as conn:
            return create_admin_domain_policy(
                conn,
                payload=payload.model_dump(),
                admin_username=admin_username,
                client_ip=get_client_ip(request),
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/v1/admin/policies/{policy_id}", response_model=DomainPolicyRecord)
def admin_policy_update(
    policy_id: UUID,
    payload: DomainPolicyUpdateRequest,
    request: Request,
    admin_username: str = Depends(require_admin_session),
):
    try:
        with get_connection() as conn:
            return update_admin_domain_policy(
                conn,
                policy_id=policy_id,
                payload=payload.model_dump(),
                admin_username=admin_username,
                client_ip=get_client_ip(request),
            )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.delete("/api/v1/admin/policies/{policy_id}", response_model=DomainPolicyRecord)
def admin_policy_delete(
    policy_id: UUID,
    request: Request,
    admin_username: str = Depends(require_admin_session),
):
    try:
        with get_connection() as conn:
            return delete_admin_domain_policy(
                conn,
                policy_id=policy_id,
                admin_username=admin_username,
                client_ip=get_client_ip(request),
            )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.get("/api/v1/admin/monitor/events", response_model=AdminSystemEventListResponse)
def admin_monitor_events(
    limit: int = 100,
    event_type: str | None = None,
    source: str | None = None,
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        return {"items": list_monitor_events(conn, limit=limit, event_type=event_type, source=source)}


@router.get("/api/v1/admin/monitor/stream")
async def admin_monitor_stream(
    request: Request,
    after_id: int = 0,
    admin_username: str = Depends(require_admin_session),
):
    async def event_stream():
        cursor = max(0, int(after_id))
        heartbeat_counter = 0
        idle_polls = 0
        yield "event: ready\ndata: {\"status\":\"connected\"}\n\n"
        while True:
            if await request.is_disconnected():
                break

            with get_connection() as conn:
                items = list_monitor_events(conn, limit=50, after_id=cursor)

            if items:
                for item in items:
                    cursor = max(cursor, int(item["id"]))
                    yield f"event: system_event\ndata: {json.dumps(item, default=str)}\n\n"
                heartbeat_counter = 0
                idle_polls = 0
            else:
                idle_polls = min(idle_polls + 1, 6)
                heartbeat_counter += 1
                if heartbeat_counter >= 15:
                    heartbeat_counter = 0
                    yield "event: heartbeat\ndata: {}\n\n"

            await asyncio.sleep(1 if items else min(1 + idle_polls * 0.25, 2.5))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/v1/admin/config", response_model=AdminRuntimeConfigResponse)
def admin_config(
    admin_username: str = Depends(require_admin_session),
):
    with get_connection() as conn:
        result = get_admin_config(conn)
    return AdminRuntimeConfigResponse(**result)


@router.put("/api/v1/admin/config", response_model=AdminRuntimeConfigResponse)
def admin_config_update(
    payload: dict,
    request: Request,
    admin_username: str = Depends(require_admin_session),
):
    try:
        with get_connection() as conn:
            result = update_admin_config(
                conn,
                updates=payload,
                admin_username=admin_username,
                client_ip=get_client_ip(request),
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AdminRuntimeConfigResponse(**result)


