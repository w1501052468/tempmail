from datetime import datetime, timedelta, timezone
import re
import secrets

from psycopg.types.json import Json

from ..errors import (
    AuthenticationError,
    InvalidDomainError,
    InvalidMailboxAddressError,
    MailboxConflictError,
    MailboxCreationError,
    MessageNotFoundError,
    RateLimitExceededError,
)
from ..mail_parser import parse_raw_email
from ..runtime_config import load_runtime_config
from ..security import generate_token, hash_token, random_label
from ..storage import remove_relative_path, resolve_relative_path
from .domain_service import (
    DomainValidationError,
    list_active_base_domains,
    resolve_matching_base_domain,
)
from .system_event_service import emit_system_event

LOCAL_PART_ALLOWED_CHARS = re.compile(r"^[a-z0-9._+-]+$")
HOST_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def active_mailbox_base_domain_exists_sql(*, mailbox_alias: str = "mailboxes") -> str:
    return (
        "EXISTS ("
        "SELECT 1 FROM managed_domains d "
        f"WHERE d.status = 'active' AND lower(d.domain) = lower({mailbox_alias}.base_domain)"
        ")"
    )


def record_access_event(
    conn,
    *,
    action: str,
    ip: str | None = None,
    mailbox_id=None,
    token_hash_value: str | None = None,
    metadata: dict | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO access_events (action, ip, mailbox_id, token_hash, metadata)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (action, ip, mailbox_id, token_hash_value, Json(metadata or {})),
        )


def enforce_rate_limit(
    conn,
    *,
    action: str,
    limit: int,
    window_seconds: int,
    ip: str | None = None,
    token_hash_value: str | None = None,
) -> None:
    if not ip and not token_hash_value:
        return
    with conn.cursor() as cur:
        clauses = [
            "action = %s",
            "created_at >= NOW() - make_interval(secs => %s)",
        ]
        params: list[object] = [action, window_seconds]
        if ip:
            clauses.append("ip = %s")
            params.append(ip)
        if token_hash_value:
            clauses.append("token_hash = %s")
            params.append(token_hash_value)
        query = f"SELECT COUNT(*) AS count FROM access_events WHERE {' AND '.join(clauses)}"
        cur.execute(query, params)
        row = cur.fetchone()
        if row and row["count"] >= limit:
            raise RateLimitExceededError(f"Rate limit exceeded for action {action}")


def _normalize_domain(domain: str | None) -> str:
    return (domain or "").strip().lower()


def _validate_host(value: str, *, field_name: str) -> None:
    if not value:
        raise InvalidMailboxAddressError(f"{field_name} cannot be empty")
    if len(value) > 253:
        raise InvalidMailboxAddressError(f"{field_name} is too long")

    labels = value.split(".")
    if any(not label for label in labels):
        raise InvalidMailboxAddressError(f"{field_name} is invalid")
    for label in labels:
        if len(label) > 63 or not HOST_LABEL_PATTERN.fullmatch(label):
            raise InvalidMailboxAddressError(f"{field_name} is invalid")


def _validate_local_part(local_part: str) -> None:
    if not local_part:
        raise InvalidMailboxAddressError("Requested mailbox local part cannot be empty")
    if len(local_part) > 64:
        raise InvalidMailboxAddressError("Requested mailbox local part is too long")
    if local_part.startswith(".") or local_part.endswith(".") or ".." in local_part:
        raise InvalidMailboxAddressError("Requested mailbox local part is invalid")
    if not LOCAL_PART_ALLOWED_CHARS.fullmatch(local_part):
        raise InvalidMailboxAddressError(
            "Requested mailbox local part may only contain lowercase letters, digits, ., _, +, or -"
        )


def _parse_requested_mailbox(conn, requested_address: str | None, requested_domain: str | None) -> dict[str, str] | None:
    normalized_address = str(requested_address or "").strip().lower()
    if not normalized_address:
        return None
    if requested_domain:
        raise InvalidMailboxAddressError("Cannot specify both address and domain when creating a custom mailbox")
    if normalized_address.count("@") != 1:
        raise InvalidMailboxAddressError("Requested mailbox address must contain a single @")

    local_part, domain = normalized_address.split("@", 1)
    _validate_local_part(local_part)
    _validate_host(domain, field_name="Requested mailbox domain")

    base_domain = resolve_matching_base_domain(conn, domain)
    if not base_domain:
        raise InvalidDomainError("Requested domain is not active or not allowed")
    suffix = f".{base_domain}"
    subdomain = domain[: -len(suffix)] if domain.endswith(suffix) else ""
    if subdomain:
        _validate_host(subdomain, field_name="Requested mailbox subdomain")

    return {
        "address": normalized_address,
        "base_domain": base_domain,
        "subdomain": subdomain,
        "local_part": local_part,
    }


def _validate_ttl(ttl_minutes: int | None, runtime_config) -> int:
    settings = runtime_config
    ttl = ttl_minutes or settings.mailbox_default_ttl_minutes
    ttl = max(ttl, settings.mailbox_min_ttl_minutes)
    ttl = min(ttl, settings.mailbox_max_ttl_minutes)
    return ttl


def _random_length(min_length: int, max_length: int) -> int:
    if min_length >= max_length:
        return min_length
    return min_length + secrets.randbelow((max_length - min_length) + 1)


def _persist_mailbox(
    conn,
    *,
    client_ip: str | None,
    user_agent: str | None,
    address: str,
    base_domain: str,
    subdomain: str,
    local_part: str,
    expires_at: datetime,
    access_action: str,
    event_source: str,
    requested_address: bool,
    reclaimed: bool,
) -> dict | None:
    token = generate_token()
    token_hash_value = hash_token(token)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mailboxes (
                address,
                base_domain,
                subdomain,
                local_part,
                token_hash,
                created_ip,
                created_user_agent,
                expires_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id, address, created_at, expires_at
            """,
            (
                address,
                base_domain,
                subdomain,
                local_part,
                token_hash_value,
                client_ip,
                user_agent,
                expires_at,
            ),
        )
        row = cur.fetchone()

    if not row:
        return None

    metadata = {
        "address": address,
        "requested_address": requested_address,
        "reclaimed": reclaimed,
    }
    record_access_event(
        conn,
        action=access_action,
        ip=client_ip,
        mailbox_id=row["id"],
        token_hash_value=token_hash_value,
        metadata=metadata,
    )
    emit_system_event(
        conn,
        event_type="mailbox_created",
        source=event_source,
        mailbox_id=row["id"],
        address=address,
        summary=f"Mailbox {'recreated' if reclaimed else 'created'} for {address}",
        payload={
            **metadata,
            "created_ip": client_ip,
            "expires_at": row["expires_at"].isoformat(),
        },
    )
    return {
        "address": row["address"],
        "token": token,
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


def _remove_mailbox_for_reuse(conn, *, mailbox_id) -> None:
    purge_mailbox_messages(conn, mailbox_id=mailbox_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM mailboxes
            WHERE id = %s
            """,
            (mailbox_id,),
        )


def _prepare_requested_mailbox_slot(conn, *, address: str, allow_disabled_recreate: bool) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, expires_at
            FROM mailboxes
            WHERE lower(address) = lower(%s)
            LIMIT 1
            FOR UPDATE
            """,
            (address,),
        )
        row = cur.fetchone()

    if not row:
        return False

    is_still_active = row["status"] == "active" and row["expires_at"] and row["expires_at"] > datetime.now(timezone.utc)
    if is_still_active:
        raise MailboxConflictError("Requested mailbox address is already active")
    if row["status"] == "disabled" and not allow_disabled_recreate:
        raise MailboxConflictError("Requested mailbox address is disabled and cannot be recreated yet")

    _remove_mailbox_for_reuse(conn, mailbox_id=row["id"])
    return True


def create_mailbox(
    conn,
    *,
    client_ip: str | None,
    user_agent: str | None,
    requested_domain: str | None,
    ttl_minutes: int | None,
    requested_address: str | None = None,
    skip_rate_limit: bool = False,
    access_action: str = "create_mailbox",
    event_source: str = "mailbox_api",
    allow_disabled_recreate: bool = False,
) -> dict:
    settings = load_runtime_config(conn)
    ttl = _validate_ttl(ttl_minutes, settings)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl)
    if not skip_rate_limit:
        enforce_rate_limit(
            conn,
            action=access_action,
            limit=settings.create_rate_limit_count,
            window_seconds=settings.create_rate_limit_window_seconds,
            ip=client_ip,
        )

    requested_mailbox = _parse_requested_mailbox(conn, requested_address, requested_domain)
    if requested_mailbox:
        reclaimed = _prepare_requested_mailbox_slot(
            conn,
            address=requested_mailbox["address"],
            allow_disabled_recreate=allow_disabled_recreate,
        )
        created = _persist_mailbox(
            conn,
            client_ip=client_ip,
            user_agent=user_agent,
            address=requested_mailbox["address"],
            base_domain=requested_mailbox["base_domain"],
            subdomain=requested_mailbox["subdomain"],
            local_part=requested_mailbox["local_part"],
            expires_at=expires_at,
            access_action=access_action,
            event_source=event_source,
            requested_address=True,
            reclaimed=reclaimed,
        )
        if created:
            return created
        raise MailboxConflictError("Requested mailbox address is already in use")

    try:
        if requested_domain:
            normalized_domain = _normalize_domain(requested_domain)
            matched_domain = resolve_matching_base_domain(conn, normalized_domain)
            if matched_domain != normalized_domain:
                raise InvalidDomainError("Requested domain is not active or not allowed")
            domain = normalized_domain
        else:
            active_domains = list_active_base_domains(conn)
            if not active_domains:
                raise InvalidDomainError("No active base domain is available")
            domain = secrets.choice(active_domains)
    except DomainValidationError as exc:
        raise InvalidDomainError(str(exc)) from exc
    for _ in range(30):
        local_part = random_label(
            _random_length(settings.mailbox_local_part_min_length, settings.mailbox_local_part_max_length)
        )
        subdomain = random_label(
            _random_length(settings.mailbox_subdomain_min_length, settings.mailbox_subdomain_max_length)
        )
        address = f"{local_part}@{subdomain}.{domain}"
        created = _persist_mailbox(
            conn,
            client_ip=client_ip,
            user_agent=user_agent,
            address=address,
            base_domain=domain,
            subdomain=subdomain,
            local_part=local_part,
            expires_at=expires_at,
            access_action=access_action,
            event_source=event_source,
            requested_address=False,
            reclaimed=False,
        )
        if created:
            return created

    raise MailboxCreationError("Unable to allocate a unique mailbox")


def authenticate_mailbox(conn, *, token: str) -> tuple[dict, str]:
    token_hash_value = hash_token(token)
    domain_active_sql = active_mailbox_base_domain_exists_sql(mailbox_alias="mailboxes")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, address, status, created_at, expires_at, disabled_at
            FROM mailboxes
            WHERE token_hash = %s
              AND status = 'active'
              AND expires_at > NOW()
              AND {domain_active_sql}
            LIMIT 1
            """,
            (token_hash_value,),
        )
        mailbox = cur.fetchone()
    if not mailbox:
        raise AuthenticationError("Token is invalid or expired")
    return mailbox, token_hash_value


def note_inbox_access(
    conn,
    *,
    mailbox_id,
    token_hash_value: str,
    client_ip: str | None,
    action: str,
) -> None:
    settings = load_runtime_config(conn)
    enforce_rate_limit(
        conn,
        action=action,
        limit=settings.inbox_rate_limit_count,
        window_seconds=settings.inbox_rate_limit_window_seconds,
        ip=client_ip,
        token_hash_value=token_hash_value,
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mailboxes
            SET last_accessed_at = NOW(), last_access_ip = %s
            WHERE id = %s
            """,
            (client_ip, mailbox_id),
        )
    record_access_event(
        conn,
        action=action,
        ip=client_ip,
        mailbox_id=mailbox_id,
        token_hash_value=token_hash_value,
    )


def list_messages(conn, *, mailbox_id, limit: int = 20, offset: int = 0) -> list[dict]:
    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              id,
              from_header,
              subject,
              received_at,
              size_bytes,
              attachment_count,
              LEFT(COALESCE(text_body, html_body, ''), 200) AS preview
            FROM messages
            WHERE mailbox_id = %s
            ORDER BY received_at DESC
            LIMIT %s OFFSET %s
            """,
            (mailbox_id, bounded_limit, bounded_offset),
        )
        return list(cur.fetchall())


def hydrate_message_bodies(conn, message: dict) -> dict:
    hydrated = dict(message)
    if hydrated.get("text_body") and hydrated.get("html_body"):
        return hydrated

    raw_path = hydrated.get("raw_path")
    if not raw_path:
        return hydrated

    try:
        parsed = parse_raw_email(resolve_relative_path(raw_path).read_bytes())
    except Exception:
        return hydrated

    updates: dict[str, str] = {}
    if not hydrated.get("text_body") and parsed.text_body:
        hydrated["text_body"] = parsed.text_body
        updates["text_body"] = parsed.text_body
    if not hydrated.get("html_body") and parsed.html_body:
        hydrated["html_body"] = parsed.html_body
        updates["html_body"] = parsed.html_body

    if updates:
        with conn.cursor() as cur:
            assignments = ", ".join(f"{column} = %s" for column in updates)
            cur.execute(
                f"""
                UPDATE messages
                SET {assignments}
                WHERE id = %s
                """,
                [*updates.values(), hydrated["id"]],
            )
    return hydrated


def get_message(conn, *, mailbox_id, message_id) -> tuple[dict, list[dict]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              id,
              envelope_from,
              envelope_to,
              subject,
              message_id,
              from_header,
              to_header,
              reply_to,
              date_header,
              received_at,
              text_body,
              html_body,
              headers_json,
              raw_path
            FROM messages
            WHERE mailbox_id = %s AND id = %s
            LIMIT 1
            """,
            (mailbox_id, message_id),
        )
        message = cur.fetchone()
        if not message:
            raise MessageNotFoundError("Message not found")
        message = hydrate_message_bodies(conn, message)
        cur.execute(
            """
            SELECT id, filename, content_type, size_bytes, storage_path
            FROM attachments
            WHERE message_id = %s
            ORDER BY created_at ASC
            """,
            (message_id,),
        )
        attachments = list(cur.fetchall())
    return message, attachments


def get_message_raw_path(conn, *, mailbox_id, message_id) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_path
            FROM messages
            WHERE mailbox_id = %s AND id = %s
            LIMIT 1
            """,
            (mailbox_id, message_id),
        )
        row = cur.fetchone()
    if not row:
        raise MessageNotFoundError("Message not found")
    return row["raw_path"]


def get_message_attachment(conn, *, mailbox_id, message_id, attachment_id) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              a.id,
              a.filename,
              a.content_type,
              a.size_bytes,
              a.storage_path
            FROM attachments a
            JOIN messages m ON m.id = a.message_id
            WHERE m.mailbox_id = %s
              AND m.id = %s
              AND a.id = %s
            LIMIT 1
            """,
            (mailbox_id, message_id, attachment_id),
        )
        row = cur.fetchone()
    if not row:
        raise MessageNotFoundError("Attachment not found")
    return row


def get_latest_message(conn, *, mailbox_id) -> tuple[dict, list[dict]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM messages
            WHERE mailbox_id = %s
            ORDER BY received_at DESC, created_at DESC
            LIMIT 1
            """,
            (mailbox_id,),
        )
        row = cur.fetchone()
    if not row:
        raise MessageNotFoundError("Message not found")
    return get_message(conn, mailbox_id=mailbox_id, message_id=row["id"])


def delete_message(conn, *, mailbox_id, message_id) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, subject, raw_path
            FROM messages
            WHERE mailbox_id = %s AND id = %s
            LIMIT 1
            """,
            (mailbox_id, message_id),
        )
        message = cur.fetchone()
        if not message:
            raise MessageNotFoundError("Message not found")

        cur.execute(
            """
            SELECT id, storage_path
            FROM attachments
            WHERE message_id = %s
            ORDER BY created_at ASC
            """,
            (message_id,),
        )
        attachments = list(cur.fetchall())

        cur.execute(
            """
            DELETE FROM messages
            WHERE mailbox_id = %s AND id = %s
            """,
            (mailbox_id, message_id),
        )

    deleted_files = 0
    paths = [message["raw_path"], *[row["storage_path"] for row in attachments]]
    for relative_path in paths:
        if not relative_path:
            continue
        remove_relative_path(relative_path)
        deleted_files += 1

    return {
        "id": message["id"],
        "subject": message["subject"],
        "attachment_count": len(attachments),
        "deleted_files": deleted_files,
    }


def purge_mailbox_messages(conn, *, mailbox_id) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, raw_path
            FROM messages
            WHERE mailbox_id = %s
            ORDER BY received_at DESC
            """,
            (mailbox_id,),
        )
        messages = list(cur.fetchall())
        message_ids = [row["id"] for row in messages]

        attachments: list[dict] = []
        if message_ids:
            cur.execute(
                """
                SELECT id, storage_path
                FROM attachments
                WHERE message_id = ANY(%s)
                ORDER BY created_at ASC
                """,
                (message_ids,),
            )
            attachments = list(cur.fetchall())

            cur.execute(
                """
                DELETE FROM messages
                WHERE mailbox_id = %s
                """,
                (mailbox_id,),
            )

    deleted_files = 0
    paths = [*[row["raw_path"] for row in messages], *[row["storage_path"] for row in attachments]]
    for relative_path in paths:
        if not relative_path:
            continue
        remove_relative_path(relative_path)
        deleted_files += 1

    return {
        "deleted_messages": len(messages),
        "deleted_attachments": len(attachments),
        "deleted_files": deleted_files,
    }


def disable_mailbox(conn, *, mailbox_id) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mailboxes
            SET status = 'disabled', disabled_at = NOW()
            WHERE id = %s
            RETURNING address, status, disabled_at
            """,
            (mailbox_id,),
        )
        row = cur.fetchone()
    if not row:
        raise AuthenticationError("Mailbox no longer exists")
    return row
