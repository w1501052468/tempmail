from fastapi import Cookie, Header, HTTPException, Request

from ..config import get_settings
from ..security import verify_admin_session


def get_client_ip(request: Request) -> str | None:
    settings = get_settings()
    if settings.trust_proxy_headers:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    if request.client:
        return request.client.host
    return None


def get_external_base_url(request: Request) -> str:
    settings = get_settings()
    if settings.trust_proxy_headers:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        if proto and host:
            return f"{proto}://{host}"
    return str(request.base_url).rstrip("/")


def get_mailbox_token(
    authorization: str | None = Header(default=None),
    x_mailbox_token: str | None = Header(default=None),
) -> str:
    if x_mailbox_token:
        return x_mailbox_token.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    raise HTTPException(status_code=401, detail="Missing mailbox token")


def require_admin_session(
    tempmail_admin_session: str | None = Cookie(default=None),
) -> str:
    if not tempmail_admin_session:
        raise HTTPException(status_code=401, detail="Admin login required")
    username = verify_admin_session(tempmail_admin_session)
    if not username:
        raise HTTPException(status_code=401, detail="Admin session is invalid or expired")
    return username
