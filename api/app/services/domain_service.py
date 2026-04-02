from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import dns.exception
import dns.resolver

from ..config import get_settings
from ..runtime_config import load_runtime_config
from .system_event_service import emit_system_event

HOST_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
ALLOWED_DOMAIN_STATUSES = {"pending", "active", "disabled"}


class DomainValidationError(ValueError):
    pass


@dataclass
class DomainCheckResult:
    ok: bool
    root_mx_hosts: list[str]
    wildcard_mx_hosts: list[str]
    expected_mx_host: str
    error: str | None = None


def _emit_system_event_safe(conn, **kwargs) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT managed_domain_event")
        emit_system_event(conn, **kwargs)
        with conn.cursor() as cur:
            cur.execute("RELEASE SAVEPOINT managed_domain_event")
    except Exception:
        try:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT managed_domain_event")
                cur.execute("RELEASE SAVEPOINT managed_domain_event")
        except Exception:
            pass


def normalize_hostname(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().rstrip(".")
    return normalized or None


def normalize_domain(value: str | None) -> str:
    return normalize_hostname(value) or ""


def validate_base_domain_name(domain: str) -> str:
    normalized = normalize_domain(domain)
    if not normalized:
        raise DomainValidationError("Base domain cannot be empty")
    if len(normalized) > 253:
        raise DomainValidationError("Base domain is too long")
    labels = normalized.split(".")
    if len(labels) < 2:
        raise DomainValidationError("Base domain must contain at least one dot")
    if any(not label for label in labels):
        raise DomainValidationError("Base domain is invalid")
    for label in labels:
        if len(label) > 63 or not HOST_LABEL_PATTERN.fullmatch(label):
            raise DomainValidationError("Base domain is invalid")
    return normalized


def normalize_domain_status(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in ALLOWED_DOMAIN_STATUSES:
        raise DomainValidationError(f"Unsupported domain status: {normalized}")
    return normalized


def _resolver(*, use_fallback: bool = False) -> dns.resolver.Resolver:
    settings = get_settings()
    custom_nameservers = settings.domain_dns_resolvers
    resolver = dns.resolver.Resolver(configure=not custom_nameservers and not use_fallback)
    if custom_nameservers:
        resolver.nameservers = custom_nameservers
    elif use_fallback:
        resolver.nameservers = ["1.1.1.1", "8.8.8.8"]
    resolver.timeout = settings.domain_dns_timeout_seconds
    resolver.lifetime = settings.domain_dns_timeout_seconds
    return resolver


def _resolve_mx_hosts(hostname: str) -> list[str]:
    last_error: Exception | None = None
    for use_fallback in (False, True):
        try:
            answers = _resolver(use_fallback=use_fallback).resolve(hostname, "MX")
            break
        except dns.resolver.NXDOMAIN:
            raise DomainValidationError(f"{hostname} does not exist in DNS") from None
        except dns.resolver.NoAnswer:
            raise DomainValidationError(f"{hostname} has no MX record") from None
        except dns.resolver.NoNameservers as exc:
            last_error = exc
            if use_fallback:
                raise DomainValidationError(f"{hostname} has no reachable nameserver: {exc}") from None
            continue
        except dns.exception.Timeout as exc:
            last_error = exc
            if use_fallback:
                raise DomainValidationError(f"DNS lookup timed out for {hostname}: {exc}") from None
            continue
        except dns.exception.DNSException as exc:
            last_error = exc
            if use_fallback:
                raise DomainValidationError(f"DNS lookup failed for {hostname}: {exc}") from None
            continue
    else:
        if last_error:
            raise DomainValidationError(f"DNS lookup failed for {hostname}: {last_error}") from None
        raise DomainValidationError(f"DNS lookup failed for {hostname}") from None

    hosts = sorted(
        {
            normalize_hostname(str(answer.exchange))
            for answer in answers
            if normalize_hostname(str(answer.exchange))
        }
    )
    if not hosts:
        raise DomainValidationError(f"{hostname} returned an empty MX response")
    return hosts


def verify_domain_routing(domain: str, *, expected_mx_host: str | None = None) -> DomainCheckResult:
    normalized_domain = validate_base_domain_name(domain)
    normalized_expected = normalize_hostname(expected_mx_host) or normalize_hostname(get_settings().smtp_hostname)
    if not normalized_expected:
        raise DomainValidationError("SMTP_HOSTNAME is not configured")

    root_mx_hosts = _resolve_mx_hosts(normalized_domain)
    # Use a normal hostname label for wildcard MX probing. Some DNS providers
    # behave poorly for MX lookups on underscore-prefixed labels and may return
    # misleading resolver errors even when wildcard MX is configured correctly.
    wildcard_probe_domain = f"tmprobe-wildcard-check.{normalized_domain}"
    wildcard_mx_hosts = _resolve_mx_hosts(wildcard_probe_domain)

    if normalized_expected not in root_mx_hosts:
        raise DomainValidationError(
            f"Root domain MX does not point to {normalized_expected}; found {', '.join(root_mx_hosts)}"
        )
    if normalized_expected not in wildcard_mx_hosts:
        raise DomainValidationError(
            f"Wildcard subdomain MX does not point to {normalized_expected}; found {', '.join(wildcard_mx_hosts)}"
        )

    return DomainCheckResult(
        ok=True,
        root_mx_hosts=root_mx_hosts,
        wildcard_mx_hosts=wildcard_mx_hosts,
        expected_mx_host=normalized_expected,
    )


def _fallback_base_domains() -> list[str]:
    settings = get_settings()
    return sorted({domain for domain in settings.base_domains if domain})


def list_active_base_domains(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT domain
            FROM managed_domains
            WHERE status = 'active'
            ORDER BY domain ASC
            """
        )
        items = [row["domain"] for row in cur.fetchall()]
        if items:
            return items

        cur.execute("SELECT COUNT(*) AS total FROM managed_domains")
        total = cur.fetchone()["total"]
    if total == 0:
        return _fallback_base_domains()
    return []


def resolve_default_base_domain(conn) -> str:
    settings = get_settings()
    active_domains = list_active_base_domains(conn)
    if not active_domains:
        raise DomainValidationError("No active base domain is available")
    if settings.default_base_domain and settings.default_base_domain in active_domains:
        return settings.default_base_domain
    return active_domains[0]


def resolve_matching_base_domain(conn, domain: str) -> str | None:
    normalized = normalize_domain(domain)
    for base_domain in sorted(list_active_base_domains(conn), key=len, reverse=True):
        if normalized == base_domain or normalized.endswith(f".{base_domain}"):
            return base_domain
    return None


def ensure_seeded_managed_domains(conn) -> None:
    settings = get_settings()
    expected_mx_host = normalize_hostname(settings.smtp_hostname)
    if not settings.base_domains:
        return
    with conn.cursor() as cur:
        for domain in settings.base_domains:
            cur.execute(
                """
                INSERT INTO managed_domains (
                    domain,
                    status,
                    source,
                    note,
                    expected_mx_host,
                    verified_at,
                    updated_by
                )
                VALUES (%s, 'active', 'system_seed', %s, %s, NOW(), 'system')
                ON CONFLICT ((lower(domain))) DO NOTHING
                """,
                (
                    domain,
                    "Seeded from BASE_DOMAINS on startup",
                    expected_mx_host,
                ),
            )


def list_managed_domains(
    conn,
    *,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    clauses = ["TRUE"]
    params: list[Any] = []
    if status and status != "all":
        clauses.append("status = %s")
        params.append(normalize_domain_status(status))

    where_sql = " AND ".join(clauses)
    bounded_limit = max(1, min(limit, 500))
    bounded_offset = max(0, offset)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM managed_domains WHERE {where_sql}", params)
        total = cur.fetchone()["total"]
        cur.execute(
            f"""
            SELECT
              id,
              domain,
              status,
              source,
              note,
              expected_mx_host,
              failure_count,
              last_error,
              root_mx_hosts,
              wildcard_mx_hosts,
              last_checked_at,
              verified_at,
              created_at,
              updated_at,
              updated_by
            FROM managed_domains
            WHERE {where_sql}
            ORDER BY
              CASE status
                WHEN 'pending' THEN 0
                WHEN 'active' THEN 1
                ELSE 2
              END,
              domain ASC
            LIMIT %s OFFSET %s
            """,
            [*params, bounded_limit, bounded_offset],
        )
        return {"total": total, "items": list(cur.fetchall())}


def get_managed_domain(conn, *, domain_id: UUID) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              id,
              domain,
              status,
              source,
              note,
              expected_mx_host,
              failure_count,
              last_error,
              root_mx_hosts,
              wildcard_mx_hosts,
              last_checked_at,
              verified_at,
              created_at,
              updated_at,
              updated_by
            FROM managed_domains
            WHERE id = %s
            LIMIT 1
            """,
            (domain_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError("Managed domain not found")
    return row


def create_managed_domain(conn, *, domain: str, note: str | None, admin_username: str) -> dict[str, Any]:
    normalized_domain = validate_base_domain_name(domain)
    normalized_note = str(note).strip() or None if note is not None else None
    expected_mx_host = normalize_hostname(get_settings().smtp_hostname)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO managed_domains (
                domain,
                status,
                source,
                note,
                expected_mx_host,
                updated_by
            )
            VALUES (%s, 'pending', 'admin', %s, %s, %s)
            ON CONFLICT ((lower(domain))) DO UPDATE
            SET
              status = CASE
                WHEN managed_domains.status = 'active' THEN 'active'
                ELSE 'pending'
              END,
              note = COALESCE(EXCLUDED.note, managed_domains.note),
              expected_mx_host = EXCLUDED.expected_mx_host,
              last_error = NULL,
              failure_count = 0,
              last_checked_at = CASE
                WHEN managed_domains.status = 'active' THEN managed_domains.last_checked_at
                ELSE NULL
              END,
              updated_by = EXCLUDED.updated_by,
              updated_at = NOW()
            RETURNING
              id,
              domain,
              status,
              source,
              note,
              expected_mx_host,
              failure_count,
              last_error,
              root_mx_hosts,
              wildcard_mx_hosts,
              last_checked_at,
              verified_at,
              created_at,
              updated_at,
              updated_by
            """,
            (normalized_domain, normalized_note, expected_mx_host, admin_username),
        )
        row = cur.fetchone()
    _emit_system_event_safe(
        conn,
        event_type="managed_domain_saved",
        source="admin",
        address=normalized_domain,
        summary=f"Managed domain queued for MX verification: {normalized_domain}",
        payload={"domain_id": str(row["id"]), "admin_username": admin_username},
    )
    return row


def request_domain_recheck(conn, *, domain_id: UUID, admin_username: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE managed_domains
            SET
              status = CASE WHEN status = 'active' THEN 'active' ELSE 'pending' END,
              last_checked_at = NULL,
              last_error = NULL,
              updated_by = %s,
              updated_at = NOW()
            WHERE id = %s
            RETURNING
              id,
              domain,
              status,
              source,
              note,
              expected_mx_host,
              failure_count,
              last_error,
              root_mx_hosts,
              wildcard_mx_hosts,
              last_checked_at,
              verified_at,
              created_at,
              updated_at,
              updated_by
            """,
            (admin_username, domain_id),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError("Managed domain not found")
    _emit_system_event_safe(
        conn,
        event_type="managed_domain_recheck_requested",
        source="admin",
        address=row["domain"],
        summary=f"Managed domain recheck requested for {row['domain']}",
        payload={"domain_id": str(row["id"]), "admin_username": admin_username},
    )
    return row


def _domain_due_condition(runtime_config) -> tuple[str, list[Any]]:
    return (
        """
        (
          status = 'pending'
          AND (last_checked_at IS NULL OR last_checked_at <= NOW() - make_interval(secs => %s))
        )
        OR (
          status = 'active'
          AND (last_checked_at IS NULL OR last_checked_at <= NOW() - make_interval(secs => %s))
        )
        OR (
          status = 'disabled'
          AND (last_checked_at IS NULL OR last_checked_at <= NOW() - make_interval(secs => %s))
        )
        """,
        [
            runtime_config.domain_verify_pending_interval_seconds,
            runtime_config.domain_verify_active_interval_seconds,
            runtime_config.domain_verify_disabled_interval_seconds,
        ],
    )


def _check_single_domain(conn, *, row: dict[str, Any]) -> dict[str, Any]:
    runtime_config = load_runtime_config(conn)
    try:
        result = verify_domain_routing(row["domain"], expected_mx_host=row["expected_mx_host"])
    except DomainValidationError as exc:
        next_failure_count = int(row["failure_count"] or 0) + 1
        next_status = row["status"]
        if row["status"] == "active" and next_failure_count >= runtime_config.domain_verify_failure_threshold:
            next_status = "disabled"
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE managed_domains
                SET
                  status = %s,
                  failure_count = %s,
                  last_error = %s,
                  last_checked_at = NOW(),
                  updated_at = NOW()
                WHERE id = %s
                RETURNING
                  id,
                  domain,
                  status,
                  source,
                  note,
                  expected_mx_host,
                  failure_count,
                  last_error,
                  root_mx_hosts,
                  wildcard_mx_hosts,
                  last_checked_at,
                  verified_at,
                  created_at,
                  updated_at,
                  updated_by
                """,
                (next_status, next_failure_count, str(exc), row["id"]),
            )
            updated = cur.fetchone()
        _emit_system_event_safe(
            conn,
            event_type="managed_domain_check_failed",
            source="domain_monitor",
            level="warning" if next_status == "disabled" else "info",
            address=row["domain"],
            summary=f"Managed domain MX check failed for {row['domain']}",
            payload={
                "domain_id": str(row["id"]),
                "status": next_status,
                "error": str(exc),
                "failure_count": next_failure_count,
            },
        )
        return updated

    event_type = "managed_domain_verified"
    summary = f"Managed domain verified for {row['domain']}"
    if row["status"] == "disabled":
        event_type = "managed_domain_reenabled"
        summary = f"Managed domain re-enabled for {row['domain']}"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE managed_domains
            SET
              status = 'active',
              failure_count = 0,
              last_error = NULL,
              root_mx_hosts = %s,
              wildcard_mx_hosts = %s,
              expected_mx_host = %s,
              last_checked_at = NOW(),
              verified_at = NOW(),
              updated_at = NOW()
            WHERE id = %s
            RETURNING
              id,
              domain,
              status,
              source,
              note,
              expected_mx_host,
              failure_count,
              last_error,
              root_mx_hosts,
              wildcard_mx_hosts,
              last_checked_at,
              verified_at,
              created_at,
              updated_at,
              updated_by
            """,
            (
                result.root_mx_hosts,
                result.wildcard_mx_hosts,
                result.expected_mx_host,
                row["id"],
            ),
        )
        updated = cur.fetchone()
    _emit_system_event_safe(
        conn,
        event_type=event_type,
        source="domain_monitor",
        address=row["domain"],
        summary=summary,
        payload={
            "domain_id": str(row["id"]),
            "root_mx_hosts": result.root_mx_hosts,
            "wildcard_mx_hosts": result.wildcard_mx_hosts,
            "expected_mx_host": result.expected_mx_host,
        },
    )
    return updated


def run_domain_checks(conn, *, limit: int = 20) -> list[dict[str, Any]]:
    runtime_config = load_runtime_config(conn)
    where_sql, params = _domain_due_condition(runtime_config)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              id,
              domain,
              status,
              source,
              note,
              expected_mx_host,
              failure_count,
              last_error,
              root_mx_hosts,
              wildcard_mx_hosts,
              last_checked_at,
              verified_at,
              created_at,
              updated_at,
              updated_by
            FROM managed_domains
            WHERE {where_sql}
            ORDER BY
              CASE status
                WHEN 'pending' THEN 0
                WHEN 'disabled' THEN 1
                ELSE 2
              END,
              COALESCE(last_checked_at, to_timestamp(0)) ASC,
              created_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            [*params, max(1, min(limit, 100))],
        )
        rows = list(cur.fetchall())
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(_check_single_domain(conn, row=row))
    return results
