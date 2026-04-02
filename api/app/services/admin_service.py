from typing import Any
from uuid import UUID

from psycopg.types.json import Json

from .. import __version__
from ..config import get_settings
from ..errors import MessageNotFoundError
from ..runtime_config import deployment_config_snapshot, load_runtime_config, update_runtime_config
from .domain_service import (
    create_managed_domain,
    get_managed_domain,
    list_active_base_domains,
    list_managed_domains,
    request_domain_recheck,
)
from .mailbox_service import active_mailbox_base_domain_exists_sql, create_mailbox, hydrate_message_bodies
from .policy_service import (
    create_domain_policy,
    delete_domain_policy,
    list_domain_policies,
    update_domain_policy,
)
from .system_event_service import emit_system_event, list_system_events


def record_admin_audit(conn, *, action: str, admin_username: str, client_ip: str | None, metadata: dict | None = None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_audit_logs (action, admin_username, ip, metadata)
            VALUES (%s, %s, %s, %s)
            """,
            (action, admin_username, client_ip, Json(metadata or {})),
        )


def _effective_mailbox_status_case(*, mailbox_alias: str = "mailboxes") -> str:
    domain_active_sql = active_mailbox_base_domain_exists_sql(mailbox_alias=mailbox_alias)
    return (
        "CASE "
        f"WHEN {mailbox_alias}.status = 'active' AND NOT {domain_active_sql} THEN 'disabled' "
        f"WHEN {mailbox_alias}.status = 'active' AND {mailbox_alias}.expires_at <= NOW() THEN 'expired' "
        f"ELSE {mailbox_alias}.status "
        "END"
    )


def get_admin_overview(conn) -> dict[str, Any]:
    settings = get_settings()
    runtime_config = load_runtime_config(conn)
    active_base_domains = list_active_base_domains(conn)
    effective_status_sql = _effective_mailbox_status_case(mailbox_alias="mailboxes")
    recent_status_sql = _effective_mailbox_status_case(mailbox_alias="m")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH mailbox_stats AS (
              SELECT
                COUNT(*) AS total_mailboxes,
                COUNT(*) FILTER (WHERE {effective_status_sql} = 'active') AS active_mailboxes,
                COUNT(*) FILTER (WHERE {effective_status_sql} = 'disabled') AS disabled_mailboxes,
                COUNT(*) FILTER (WHERE {effective_status_sql} = 'expired') AS expired_mailboxes
              FROM mailboxes
            ),
            message_stats AS (
              SELECT
                COUNT(*) AS total_messages,
                COUNT(*) FILTER (WHERE received_at >= NOW() - INTERVAL '24 hours') AS messages_last_24h,
                COALESCE(SUM(attachment_count), 0) AS total_attachments
              FROM messages
            ),
            domain_stats AS (
              SELECT
                COUNT(*) AS total_domains,
                COUNT(*) FILTER (WHERE status = 'active') AS active_domains,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending_domains,
                COUNT(*) FILTER (WHERE status = 'disabled') AS disabled_domains
              FROM managed_domains
            ),
            access_stats AS (
              SELECT COUNT(*) AS total_access_events
              FROM access_events
              WHERE created_at >= NOW() - INTERVAL '24 hours'
            )
            SELECT
              mailbox_stats.total_mailboxes,
              mailbox_stats.active_mailboxes,
              mailbox_stats.disabled_mailboxes,
              mailbox_stats.expired_mailboxes,
              message_stats.total_messages,
              message_stats.messages_last_24h,
              message_stats.total_attachments,
              domain_stats.total_domains,
              domain_stats.active_domains,
              domain_stats.pending_domains,
              domain_stats.disabled_domains,
              access_stats.total_access_events
            FROM mailbox_stats, message_stats, domain_stats, access_stats
            """,
        )
        stats = cur.fetchone()

        cur.execute(
            f"""
            SELECT
              id,
              address,
              {recent_status_sql} AS status,
              created_at,
              expires_at
            FROM mailboxes m
            ORDER BY m.created_at DESC
            LIMIT 10
            """,
        )
        recent_mailboxes = list(cur.fetchall())

        cur.execute(
            """
            SELECT m.id, mb.address, m.subject, m.from_header, m.received_at, m.size_bytes
            FROM messages m
            JOIN mailboxes mb ON mb.id = m.mailbox_id
            ORDER BY m.received_at DESC
            LIMIT 10
            """
        )
        recent_messages = list(cur.fetchall())

    return {
        "stats": dict(stats),
        "runtime_config": runtime_config.model_dump(),
        "deployment": {
            **deployment_config_snapshot(settings),
            "base_domains": active_base_domains,
            "configured_base_domains": settings.base_domains,
            "service_name": settings.app_name,
            "version": __version__,
        },
        "recent_mailboxes": recent_mailboxes,
        "recent_messages": recent_messages,
    }


def list_admin_mailboxes(conn, *, status: str | None, query: str | None, limit: int, offset: int) -> dict[str, Any]:
    clauses = ["TRUE"]
    params: list[Any] = []
    effective_status_sql = _effective_mailbox_status_case(mailbox_alias="mailboxes")
    if status and status != "all":
        normalized_status = status.strip().lower()
        clauses.append(f"{effective_status_sql} = %s")
        params.append(normalized_status)
    if query:
        normalized_query = query.strip()
        clauses.append("address ILIKE %s")
        params.append(f"%{normalized_query}%")

    bounded_limit = max(1, min(limit, 200))
    bounded_offset = max(0, offset)

    where_sql = " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM mailboxes WHERE {where_sql}", params)
        total = cur.fetchone()["total"]
        cur.execute(
            f"""
            WITH page AS (
              SELECT
                id,
                address,
                {_effective_mailbox_status_case(mailbox_alias='mailboxes')} AS status,
                created_at,
                expires_at,
                disabled_at,
                last_accessed_at
              FROM mailboxes
              WHERE {where_sql}
              ORDER BY created_at DESC
              LIMIT %s OFFSET %s
            )
            SELECT
              page.id,
              page.address,
              page.status,
              page.created_at,
              page.expires_at,
              page.disabled_at,
              page.last_accessed_at,
              COALESCE(message_counts.message_count, 0) AS message_count
            FROM page
            LEFT JOIN (
              SELECT mailbox_id, COUNT(*) AS message_count
              FROM messages
              WHERE mailbox_id IN (SELECT id FROM page)
              GROUP BY mailbox_id
            ) AS message_counts
              ON message_counts.mailbox_id = page.id
            ORDER BY page.created_at DESC
            """,
            [*params, bounded_limit, bounded_offset],
        )
        items = list(cur.fetchall())
    return {"total": total, "items": items}


def list_admin_messages(conn, *, query: str | None, limit: int, offset: int) -> dict[str, Any]:
    clauses = ["TRUE"]
    params: list[Any] = []
    if query:
        normalized_query = query.strip()
        clauses.append(
            """
            (
              mb.address ILIKE %s
              OR m.subject ILIKE %s
              OR m.from_header ILIKE %s
            )
            """
        )
        wildcard_query = f"%{normalized_query}%"
        params.extend([wildcard_query, wildcard_query, wildcard_query])

    bounded_limit = max(1, min(limit, 100))
    bounded_offset = max(0, offset)
    where_sql = " AND ".join(clauses)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM messages m
            JOIN mailboxes mb ON mb.id = m.mailbox_id
            WHERE {where_sql}
            """,
            params,
        )
        total = cur.fetchone()["total"]
        cur.execute(
            f"""
            SELECT
              m.id,
              m.mailbox_id,
              mb.address AS mailbox_address,
              m.subject,
              m.from_header,
              m.received_at,
              m.size_bytes,
              m.attachment_count
            FROM messages m
            JOIN mailboxes mb ON mb.id = m.mailbox_id
            WHERE {where_sql}
            ORDER BY m.received_at DESC, m.created_at DESC
            LIMIT %s OFFSET %s
            """,
            [*params, bounded_limit, bounded_offset],
        )
        items = list(cur.fetchall())

    return {"total": total, "items": items}


def get_admin_mailbox_detail(conn, *, mailbox_id: UUID) -> dict[str, Any]:
    effective_status_sql = _effective_mailbox_status_case(mailbox_alias="mailboxes")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              id,
              address,
              base_domain,
              subdomain,
              local_part,
              {effective_status_sql} AS status,
              created_ip,
              created_user_agent,
              last_access_ip,
              created_at,
              expires_at,
              disabled_at,
              last_accessed_at
            FROM mailboxes
            WHERE id = %s
            LIMIT 1
            """,
            (mailbox_id,),
        )
        mailbox = cur.fetchone()
        if not mailbox:
            raise MessageNotFoundError("Mailbox not found")
        cur.execute(
            """
            SELECT
              id,
              subject,
              from_header,
              received_at,
              size_bytes,
              attachment_count
            FROM messages
            WHERE mailbox_id = %s
            ORDER BY received_at DESC
            LIMIT 50
            """,
            (mailbox_id,),
        )
        messages = list(cur.fetchall())
    return {"mailbox": mailbox, "messages": messages}


def get_admin_message_detail(conn, *, message_id: UUID) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              m.id,
              mb.address AS mailbox_address,
              m.mailbox_id,
              m.envelope_from,
              m.envelope_to,
              m.subject,
              m.message_id,
              m.from_header,
              m.to_header,
              m.reply_to,
              m.date_header,
              m.received_at,
              m.size_bytes,
              m.text_body,
              m.html_body,
              m.headers_json,
              m.raw_path
            FROM messages m
            JOIN mailboxes mb ON mb.id = m.mailbox_id
            WHERE m.id = %s
            LIMIT 1
            """,
            (message_id,),
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
    return {"message": message, "attachments": attachments}


def get_admin_message_raw_path(conn, *, message_id: UUID) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_path
            FROM messages
            WHERE id = %s
            LIMIT 1
            """,
            (message_id,),
        )
        row = cur.fetchone()
    if not row:
        raise MessageNotFoundError("Message not found")
    return row["raw_path"]


def get_admin_message_attachment(conn, *, message_id: UUID, attachment_id: UUID) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              id,
              filename,
              content_type,
              size_bytes,
              storage_path
            FROM attachments
            WHERE message_id = %s AND id = %s
            LIMIT 1
            """,
            (message_id, attachment_id),
        )
        row = cur.fetchone()
    if not row:
        raise MessageNotFoundError("Attachment not found")
    return row


def disable_admin_mailbox(conn, *, mailbox_id: UUID, admin_username: str, client_ip: str | None) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mailboxes
            SET status = 'disabled', disabled_at = NOW()
            WHERE id = %s
            RETURNING id, address, status, disabled_at
            """,
            (mailbox_id,),
        )
        row = cur.fetchone()
    if not row:
        raise MessageNotFoundError("Mailbox not found")
    record_admin_audit(
        conn,
        action="disable_mailbox",
        admin_username=admin_username,
        client_ip=client_ip,
        metadata={"mailbox_id": str(row["id"]), "address": row["address"]},
    )
    emit_system_event(
        conn,
        event_type="mailbox_disabled",
        source="admin",
        mailbox_id=row["id"],
        address=row["address"],
        summary=f"Admin disabled mailbox {row['address']}",
        payload={"admin_username": admin_username, "client_ip": client_ip},
    )
    return row


def create_admin_mailbox(
    conn,
    *,
    payload: dict[str, Any],
    admin_username: str,
    client_ip: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    mailbox = create_mailbox(
        conn,
        client_ip=client_ip,
        user_agent=user_agent,
        requested_address=payload.get("address"),
        requested_domain=payload.get("domain"),
        ttl_minutes=payload.get("ttl_minutes"),
        skip_rate_limit=True,
        access_action="admin_create_mailbox",
        event_source="admin",
        allow_disabled_recreate=True,
    )
    record_admin_audit(
        conn,
        action="admin_create_mailbox",
        admin_username=admin_username,
        client_ip=client_ip,
        metadata={
            "address": mailbox["address"],
            "requested_address": bool(str(payload.get("address") or "").strip()),
            "expires_at": mailbox["expires_at"].isoformat(),
        },
    )
    return mailbox


def list_access_events(conn, *, limit: int = 100) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 500))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, action, ip, mailbox_id, metadata, created_at
            FROM access_events
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (bounded_limit,),
        )
        return list(cur.fetchall())


def list_admin_audit_logs(conn, *, limit: int = 100) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 500))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, action, admin_username, ip, metadata, created_at
            FROM admin_audit_logs
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (bounded_limit,),
        )
        return list(cur.fetchall())


def list_monitor_events(
    conn,
    *,
    limit: int = 100,
    after_id: int | None = None,
    event_type: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    return list_system_events(
        conn,
        limit=limit,
        after_id=after_id,
        event_type=event_type,
        source=source,
    )


def list_admin_domain_policies(
    conn,
    *,
    scope: str | None = None,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    return list_domain_policies(conn, scope=scope, status=status, limit=limit, offset=offset)


def list_admin_domains(
    conn,
    *,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    return list_managed_domains(conn, status=status, limit=limit, offset=offset)


def create_admin_domain(
    conn,
    *,
    payload: dict[str, Any],
    admin_username: str,
    client_ip: str | None,
) -> dict[str, Any]:
    row = create_managed_domain(
        conn,
        domain=payload.get("domain", ""),
        note=payload.get("note"),
        admin_username=admin_username,
    )
    record_admin_audit(
        conn,
        action="create_managed_domain",
        admin_username=admin_username,
        client_ip=client_ip,
        metadata={"domain_id": str(row["id"]), "domain": row["domain"], "status": row["status"]},
    )
    return row


def recheck_admin_domain(
    conn,
    *,
    domain_id: UUID,
    admin_username: str,
    client_ip: str | None,
) -> dict[str, Any]:
    row = request_domain_recheck(conn, domain_id=domain_id, admin_username=admin_username)
    record_admin_audit(
        conn,
        action="recheck_managed_domain",
        admin_username=admin_username,
        client_ip=client_ip,
        metadata={"domain_id": str(row["id"]), "domain": row["domain"], "status": row["status"]},
    )
    return row


def get_admin_domain_detail(conn, *, domain_id: UUID) -> dict[str, Any]:
    return get_managed_domain(conn, domain_id=domain_id)


def create_admin_domain_policy(
    conn,
    *,
    payload: dict[str, Any],
    admin_username: str,
    client_ip: str | None,
) -> dict[str, Any]:
    row = create_domain_policy(conn, payload=payload, admin_username=admin_username)
    record_admin_audit(
        conn,
        action="create_domain_policy",
        admin_username=admin_username,
        client_ip=client_ip,
        metadata={"policy_id": str(row["id"]), "pattern": row["pattern"], "action": row["action"]},
    )
    emit_system_event(
        conn,
        event_type="domain_policy_created",
        source="admin",
        summary=f"Created domain policy {row['scope']}:{row['pattern']} -> {row['action']}",
        payload={"admin_username": admin_username, "client_ip": client_ip, "policy_id": str(row["id"])},
    )
    return row


def update_admin_domain_policy(
    conn,
    *,
    policy_id: UUID,
    payload: dict[str, Any],
    admin_username: str,
    client_ip: str | None,
) -> dict[str, Any]:
    row = update_domain_policy(conn, policy_id=policy_id, payload=payload, admin_username=admin_username)
    record_admin_audit(
        conn,
        action="update_domain_policy",
        admin_username=admin_username,
        client_ip=client_ip,
        metadata={"policy_id": str(row["id"]), "pattern": row["pattern"], "action": row["action"]},
    )
    emit_system_event(
        conn,
        event_type="domain_policy_updated",
        source="admin",
        summary=f"Updated domain policy {row['scope']}:{row['pattern']} -> {row['action']}",
        payload={"admin_username": admin_username, "client_ip": client_ip, "policy_id": str(row["id"])},
    )
    return row


def delete_admin_domain_policy(
    conn,
    *,
    policy_id: UUID,
    admin_username: str,
    client_ip: str | None,
) -> dict[str, Any]:
    row = delete_domain_policy(conn, policy_id=policy_id)
    record_admin_audit(
        conn,
        action="delete_domain_policy",
        admin_username=admin_username,
        client_ip=client_ip,
        metadata={"policy_id": str(row["id"]), "pattern": row["pattern"], "action": row["action"]},
    )
    emit_system_event(
        conn,
        event_type="domain_policy_deleted",
        source="admin",
        summary=f"Deleted domain policy {row['scope']}:{row['pattern']}",
        payload={"admin_username": admin_username, "client_ip": client_ip, "policy_id": str(row["id"])},
    )
    return row


def get_admin_config(conn) -> dict[str, Any]:
    return {
        "runtime": load_runtime_config(conn).model_dump(),
        "deployment": {
            **deployment_config_snapshot(),
            "base_domains": list_active_base_domains(conn),
            "configured_base_domains": get_settings().base_domains,
        },
    }


def update_admin_config(conn, *, updates: dict[str, Any], admin_username: str, client_ip: str | None) -> dict[str, Any]:
    runtime = update_runtime_config(
        conn,
        updates=updates,
        admin_username=admin_username,
        client_ip=client_ip,
    )
    emit_system_event(
        conn,
        event_type="admin_config_updated",
        source="admin",
        summary=f"Admin updated runtime config ({len(updates)} keys)",
        payload={
            "admin_username": admin_username,
            "client_ip": client_ip,
            "changed_keys": sorted(list(updates.keys())),
        },
    )
    return {
        "runtime": runtime.model_dump(),
        "deployment": {
            **deployment_config_snapshot(),
            "base_domains": list_active_base_domains(conn),
            "configured_base_domains": get_settings().base_domains,
        },
    }


