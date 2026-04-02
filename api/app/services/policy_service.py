from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any
from uuid import UUID

from .domain_service import resolve_matching_base_domain

ALLOWED_SCOPES = {"recipient_base_domain", "sender_domain"}
ALLOWED_ACTIONS = {"allow", "reject", "discard"}
ALLOWED_STATUSES = {"active", "disabled"}


@dataclass
class PolicyDecision:
    matched: bool
    action: str = "allow"
    policy: dict[str, Any] | None = None
    recipient_base_domain: str | None = None
    sender_domain: str | None = None


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_lower(value: str) -> str:
    return str(value or "").strip().lower()


def normalize_policy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    scope = _normalize_lower(payload.get("scope"))
    action = _normalize_lower(payload.get("action"))
    status = _normalize_lower(payload.get("status") or "active")
    pattern = _normalize_lower(payload.get("pattern"))
    note = _normalize_optional_text(payload.get("note"))
    priority = int(payload.get("priority", 100))

    if scope not in ALLOWED_SCOPES:
        raise ValueError(f"Unsupported policy scope: {scope}")
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"Unsupported policy action: {action}")
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Unsupported policy status: {status}")
    if not pattern:
        raise ValueError("Policy pattern cannot be empty")
    if priority < 0 or priority > 100000:
        raise ValueError("Policy priority must be between 0 and 100000")

    return {
        "scope": scope,
        "pattern": pattern,
        "action": action,
        "priority": priority,
        "status": status,
        "note": note,
    }


def resolve_recipient_base_domain(conn, recipient: str) -> str | None:
    normalized = _normalize_lower(recipient)
    if "@" not in normalized:
        return None
    _, _, domain = normalized.partition("@")
    if not domain:
        return None
    return resolve_matching_base_domain(conn, domain)


def extract_sender_domain(sender: str | None) -> str | None:
    normalized = _normalize_lower(sender or "")
    if not normalized or normalized == "<>":
        return None
    if normalized.startswith("<") and normalized.endswith(">"):
        normalized = normalized[1:-1].strip()
    if "@" not in normalized:
        return None
    _, _, domain = normalized.rpartition("@")
    return domain or None


def _pattern_matches(value: str | None, pattern: str) -> bool:
    if not value:
        return False
    target = _normalize_lower(value)
    normalized_pattern = _normalize_lower(pattern)
    if normalized_pattern == "*":
        return True
    if normalized_pattern.startswith("*."):
        suffix = normalized_pattern[2:]
        return target == suffix or target.endswith(f".{suffix}")
    return fnmatchcase(target, normalized_pattern)


def _evaluation_scopes(recipient_base_domain: str | None, sender_domain: str | None) -> list[str]:
    scopes: list[str] = []
    if recipient_base_domain:
        scopes.append("recipient_base_domain")
    if sender_domain:
        scopes.append("sender_domain")
    return scopes


def list_domain_policies(
    conn,
    *,
    scope: str | None = None,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    clauses = ["TRUE"]
    params: list[Any] = []
    normalized_scope = _normalize_lower(scope) if scope else None
    normalized_status = _normalize_lower(status) if status else None

    if normalized_scope:
        clauses.append("scope = %s")
        params.append(normalized_scope)
    if normalized_status and normalized_status != "all":
        clauses.append("status = %s")
        params.append(normalized_status)

    where_sql = " AND ".join(clauses)
    bounded_limit = max(1, min(limit, 500))
    bounded_offset = max(0, offset)

    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM domain_policies WHERE {where_sql}", params)
        total = cur.fetchone()["total"]
        cur.execute(
            f"""
            SELECT
              id,
              scope,
              pattern,
              action,
              priority,
              status,
              note,
              match_count,
              last_matched_at,
              updated_by,
              created_at,
              updated_at
            FROM domain_policies
            WHERE {where_sql}
            ORDER BY priority ASC, created_at ASC
            LIMIT %s OFFSET %s
            """,
            [*params, bounded_limit, bounded_offset],
        )
        items = list(cur.fetchall())
    return {"total": total, "items": items}


def create_domain_policy(conn, *, payload: dict[str, Any], admin_username: str) -> dict[str, Any]:
    normalized = normalize_policy_payload(payload)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO domain_policies (
                scope,
                pattern,
                action,
                priority,
                status,
                note,
                updated_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING
              id,
              scope,
              pattern,
              action,
              priority,
              status,
              note,
              match_count,
              last_matched_at,
              updated_by,
              created_at,
              updated_at
            """,
            (
                normalized["scope"],
                normalized["pattern"],
                normalized["action"],
                normalized["priority"],
                normalized["status"],
                normalized["note"],
                admin_username,
            ),
        )
        return cur.fetchone()


def update_domain_policy(conn, *, policy_id: UUID, payload: dict[str, Any], admin_username: str) -> dict[str, Any]:
    normalized = normalize_policy_payload(payload)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE domain_policies
            SET
              scope = %s,
              pattern = %s,
              action = %s,
              priority = %s,
              status = %s,
              note = %s,
              updated_by = %s,
              updated_at = NOW()
            WHERE id = %s
            RETURNING
              id,
              scope,
              pattern,
              action,
              priority,
              status,
              note,
              match_count,
              last_matched_at,
              updated_by,
              created_at,
              updated_at
            """,
            (
                normalized["scope"],
                normalized["pattern"],
                normalized["action"],
                normalized["priority"],
                normalized["status"],
                normalized["note"],
                admin_username,
                policy_id,
            ),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError("Policy not found")
    return row


def delete_domain_policy(conn, *, policy_id: UUID) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM domain_policies
            WHERE id = %s
            RETURNING
              id,
              scope,
              pattern,
              action,
              priority,
              status,
              note,
              match_count,
              last_matched_at,
              updated_by,
              created_at,
              updated_at
            """,
            (policy_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError("Policy not found")
    return row


def ensure_default_allow_all_policy(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM domain_policies
            WHERE scope = 'recipient_base_domain'
              AND pattern = '*'
              AND action = 'allow'
              AND status = 'active'
              AND priority = 0
            ORDER BY created_at ASC
            LIMIT 1
            """
        )
        existing = cur.fetchone()
        if existing:
            return

        cur.execute(
            """
            INSERT INTO domain_policies (
                scope,
                pattern,
                action,
                priority,
                status,
                note,
                updated_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "recipient_base_domain",
                "*",
                "allow",
                0,
                "active",
                "System default: allow all inbound mail for configured base domains",
                "system",
            ),
        )


def evaluate_domain_policies(conn, *, recipient: str, sender: str | None) -> PolicyDecision:
    recipient_base_domain = resolve_recipient_base_domain(conn, recipient)
    sender_domain = extract_sender_domain(sender)
    scopes = _evaluation_scopes(recipient_base_domain, sender_domain)

    if not scopes:
        return PolicyDecision(
            matched=False,
            action="allow",
            policy=None,
            recipient_base_domain=recipient_base_domain,
            sender_domain=sender_domain,
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              id,
              scope,
              pattern,
              action,
              priority,
              status,
              note,
              match_count,
              last_matched_at,
              updated_by,
              created_at,
              updated_at
            FROM domain_policies
            WHERE status = 'active'
              AND scope = ANY(%s)
            ORDER BY priority ASC, created_at ASC
            """,
            (scopes,),
        )
        policies = list(cur.fetchall())

        # Recipient policies take precedence over sender policies so a default
        # allow rule for configured base domains cannot be shadowed by a broad
        # sender-domain discard/reject rule.
        for scope_name, candidate_value in (
            ("recipient_base_domain", recipient_base_domain),
            ("sender_domain", sender_domain),
        ):
            for policy in policies:
                if policy["scope"] != scope_name:
                    continue
                if not _pattern_matches(candidate_value, policy["pattern"]):
                    continue

                cur.execute(
                    """
                    UPDATE domain_policies
                    SET match_count = match_count + 1, last_matched_at = NOW()
                    WHERE id = %s
                    RETURNING
                      id,
                      scope,
                      pattern,
                      action,
                      priority,
                      status,
                      note,
                      match_count,
                      last_matched_at,
                      updated_by,
                      created_at,
                      updated_at
                    """,
                    (policy["id"],),
                )
                refreshed = cur.fetchone()
                return PolicyDecision(
                    matched=True,
                    action=refreshed["action"],
                    policy=refreshed,
                    recipient_base_domain=recipient_base_domain,
                    sender_domain=sender_domain,
                )

    return PolicyDecision(
        matched=False,
        action="allow",
        policy=None,
        recipient_base_domain=recipient_base_domain,
        sender_domain=sender_domain,
    )
