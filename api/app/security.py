import hashlib
import hmac
import json
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

from .config import get_settings

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_token() -> str:
    settings = get_settings()
    return f"{settings.token_prefix}{secrets.token_urlsafe(settings.token_bytes)}"


def hash_token(token: str) -> str:
    settings = get_settings()
    digest = hmac.new(
        settings.app_token_hash_secret.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()


def random_label(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def verify_admin_credentials(username: str, password: str) -> bool:
    settings = get_settings()
    return hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
        password, settings.admin_password
    )


def _sign_admin_session(payload_b64: str) -> str:
    settings = get_settings()
    return hmac.new(
        (settings.admin_session_secret or settings.app_token_hash_secret).encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_admin_session(username: str) -> str:
    settings = get_settings()
    payload = {
        "sub": username,
        "exp": int(time.time()) + settings.admin_session_hours * 3600,
        "nonce": secrets.token_urlsafe(12),
    }
    payload_b64 = urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("utf-8")
    signature = _sign_admin_session(payload_b64)
    return f"{payload_b64}.{signature}"


def verify_admin_session(token: str) -> str | None:
    try:
        payload_b64, signature = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign_admin_session(payload_b64), signature):
        return None
    try:
        payload = json.loads(urlsafe_b64decode(payload_b64.encode("utf-8")))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    username = str(payload.get("sub") or "").strip()
    settings = get_settings()
    if not username or not hmac.compare_digest(username, settings.admin_username):
        return None
    return username
