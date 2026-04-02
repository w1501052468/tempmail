import os
import time
from threading import Lock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from psycopg.types.json import Json

from .config import Settings, get_settings

_RUNTIME_CONFIG_LOCK = Lock()
_RUNTIME_CONFIG_CACHE: "RuntimeConfig | None" = None
_RUNTIME_CONFIG_CACHE_LOADED_AT = 0.0
_RUNTIME_CONFIG_CACHE_TTL_SECONDS = max(0.0, float(os.getenv("RUNTIME_CONFIG_CACHE_TTL_SECONDS", "5")))


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mailbox_default_ttl_minutes: int = Field(ge=1, le=10080)
    mailbox_min_ttl_minutes: int = Field(ge=1, le=10080)
    mailbox_max_ttl_minutes: int = Field(ge=1, le=10080)
    mailbox_local_part_min_length: int = Field(ge=4, le=32)
    mailbox_local_part_max_length: int = Field(ge=4, le=32)
    mailbox_subdomain_min_length: int = Field(ge=4, le=32)
    mailbox_subdomain_max_length: int = Field(ge=4, le=32)

    create_rate_limit_count: int = Field(ge=1, le=100000)
    create_rate_limit_window_seconds: int = Field(ge=1, le=86400)
    inbox_rate_limit_count: int = Field(ge=1, le=100000)
    inbox_rate_limit_window_seconds: int = Field(ge=1, le=86400)

    message_size_limit_bytes: int = Field(ge=1024, le=104857600)
    max_text_body_chars: int = Field(ge=1000, le=2000000)
    max_html_body_chars: int = Field(ge=1000, le=2000000)
    max_attachments_per_message: int = Field(ge=0, le=100)

    purge_grace_minutes: int = Field(ge=1, le=10080)
    access_event_retention_days: int = Field(ge=1, le=365)
    cleanup_batch_size: int = Field(ge=1, le=5000)
    domain_monitor_loop_seconds: int = Field(ge=5, le=3600)
    domain_verify_pending_interval_seconds: int = Field(ge=5, le=86400)
    domain_verify_active_interval_seconds: int = Field(ge=5, le=604800)
    domain_verify_disabled_interval_seconds: int = Field(ge=5, le=604800)
    domain_verify_failure_threshold: int = Field(ge=1, le=10)

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.mailbox_min_ttl_minutes > self.mailbox_default_ttl_minutes:
            raise ValueError("mailbox_min_ttl_minutes cannot exceed mailbox_default_ttl_minutes")
        if self.mailbox_default_ttl_minutes > self.mailbox_max_ttl_minutes:
            raise ValueError("mailbox_default_ttl_minutes cannot exceed mailbox_max_ttl_minutes")
        if self.mailbox_local_part_min_length > self.mailbox_local_part_max_length:
            raise ValueError("mailbox_local_part_min_length cannot exceed mailbox_local_part_max_length")
        if self.mailbox_subdomain_min_length > self.mailbox_subdomain_max_length:
            raise ValueError("mailbox_subdomain_min_length cannot exceed mailbox_subdomain_max_length")
        if self.domain_verify_pending_interval_seconds > self.domain_verify_active_interval_seconds:
            raise ValueError(
                "domain_verify_pending_interval_seconds cannot exceed domain_verify_active_interval_seconds"
            )
        return self


def runtime_config_defaults(settings: Settings | None = None) -> RuntimeConfig:
    active_settings = settings or get_settings()
    return RuntimeConfig(
        mailbox_default_ttl_minutes=active_settings.mailbox_default_ttl_minutes,
        mailbox_min_ttl_minutes=active_settings.mailbox_min_ttl_minutes,
        mailbox_max_ttl_minutes=active_settings.mailbox_max_ttl_minutes,
        mailbox_local_part_min_length=active_settings.effective_mailbox_local_part_min_length,
        mailbox_local_part_max_length=active_settings.effective_mailbox_local_part_max_length,
        mailbox_subdomain_min_length=active_settings.effective_mailbox_subdomain_min_length,
        mailbox_subdomain_max_length=active_settings.effective_mailbox_subdomain_max_length,
        create_rate_limit_count=active_settings.create_rate_limit_count,
        create_rate_limit_window_seconds=active_settings.create_rate_limit_window_seconds,
        inbox_rate_limit_count=active_settings.inbox_rate_limit_count,
        inbox_rate_limit_window_seconds=active_settings.inbox_rate_limit_window_seconds,
        message_size_limit_bytes=active_settings.message_size_limit_bytes,
        max_text_body_chars=active_settings.max_text_body_chars,
        max_html_body_chars=active_settings.max_html_body_chars,
        max_attachments_per_message=active_settings.max_attachments_per_message,
        purge_grace_minutes=active_settings.purge_grace_minutes,
        access_event_retention_days=active_settings.access_event_retention_days,
        cleanup_batch_size=active_settings.cleanup_batch_size,
        domain_monitor_loop_seconds=active_settings.domain_monitor_loop_seconds,
        domain_verify_pending_interval_seconds=active_settings.domain_verify_pending_interval_seconds,
        domain_verify_active_interval_seconds=active_settings.domain_verify_active_interval_seconds,
        domain_verify_disabled_interval_seconds=active_settings.domain_verify_disabled_interval_seconds,
        domain_verify_failure_threshold=active_settings.domain_verify_failure_threshold,
    )


def ensure_admin_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_runtime_config (
              singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
              config JSONB NOT NULL DEFAULT '{}'::jsonb,
              updated_by TEXT,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            INSERT INTO admin_runtime_config (singleton, config, updated_by)
            VALUES (TRUE, '{}'::jsonb, 'system')
            ON CONFLICT (singleton) DO NOTHING
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_logs (
              id BIGSERIAL PRIMARY KEY,
              action TEXT NOT NULL,
              admin_username TEXT NOT NULL,
              ip INET,
              metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created_at
            ON admin_audit_logs (created_at DESC)
            """
        )


def invalidate_runtime_config_cache() -> None:
    global _RUNTIME_CONFIG_CACHE, _RUNTIME_CONFIG_CACHE_LOADED_AT
    with _RUNTIME_CONFIG_LOCK:
        _RUNTIME_CONFIG_CACHE = None
        _RUNTIME_CONFIG_CACHE_LOADED_AT = 0.0


def _get_cached_runtime_config() -> RuntimeConfig | None:
    if _RUNTIME_CONFIG_CACHE_TTL_SECONDS <= 0:
        return None
    with _RUNTIME_CONFIG_LOCK:
        if _RUNTIME_CONFIG_CACHE is None:
            return None
        if (time.monotonic() - _RUNTIME_CONFIG_CACHE_LOADED_AT) > _RUNTIME_CONFIG_CACHE_TTL_SECONDS:
            return None
        return _RUNTIME_CONFIG_CACHE.model_copy(deep=True)


def _store_runtime_config_cache(runtime_config: RuntimeConfig) -> None:
    global _RUNTIME_CONFIG_CACHE, _RUNTIME_CONFIG_CACHE_LOADED_AT
    with _RUNTIME_CONFIG_LOCK:
        _RUNTIME_CONFIG_CACHE = runtime_config.model_copy(deep=True)
        _RUNTIME_CONFIG_CACHE_LOADED_AT = time.monotonic()


def load_runtime_config(conn, *, force_refresh: bool = False) -> RuntimeConfig:
    if not force_refresh:
        cached = _get_cached_runtime_config()
        if cached is not None:
            return cached

    defaults = runtime_config_defaults()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT config
            FROM admin_runtime_config
            WHERE singleton = TRUE
            LIMIT 1
            """
        )
        row = cur.fetchone()
    overrides = dict(row["config"] or {}) if row else {}
    legacy_local_part_length = overrides.pop("mailbox_local_part_length", None)
    legacy_subdomain_length = overrides.pop("mailbox_subdomain_length", None)
    if legacy_local_part_length is not None:
        overrides.setdefault("mailbox_local_part_min_length", legacy_local_part_length)
        overrides.setdefault("mailbox_local_part_max_length", legacy_local_part_length)
    if legacy_subdomain_length is not None:
        overrides.setdefault("mailbox_subdomain_min_length", legacy_subdomain_length)
        overrides.setdefault("mailbox_subdomain_max_length", legacy_subdomain_length)
    merged = {**defaults.model_dump(), **overrides}
    runtime_config = RuntimeConfig(**merged)
    _store_runtime_config_cache(runtime_config)
    return runtime_config.model_copy(deep=True)


def update_runtime_config(conn, *, updates: dict[str, Any], admin_username: str, client_ip: str | None):
    current = load_runtime_config(conn, force_refresh=True)
    next_config = RuntimeConfig(**{**current.model_dump(), **updates})
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_runtime_config
            SET config = %s, updated_by = %s, updated_at = NOW()
            WHERE singleton = TRUE
            """,
            (Json(next_config.model_dump()), admin_username),
        )
        cur.execute(
            """
            INSERT INTO admin_audit_logs (action, admin_username, ip, metadata)
            VALUES (%s, %s, %s, %s)
            """,
            (
                "update_runtime_config",
                admin_username,
                client_ip,
                Json(
                    {
                        "changed_keys": sorted(list(updates.keys())),
                        "config": next_config.model_dump(),
                    }
                ),
            ),
        )
    _store_runtime_config_cache(next_config)
    return next_config.model_copy(deep=True)


def deployment_config_snapshot(settings: Settings | None = None) -> dict[str, Any]:
    active_settings = settings or get_settings()
    return {
        "root_domain": active_settings.root_domain,
        "web_hostname": active_settings.web_hostname,
        "base_domains": active_settings.base_domains,
        "default_base_domain": active_settings.default_base_domain,
        "api_port": active_settings.api_port,
        "smtp_hostname": active_settings.smtp_hostname,
        "postfix_hostname": active_settings.postfix_hostname,
        "trust_proxy_headers": active_settings.trust_proxy_headers,
        "data_dir": active_settings.data_dir,
        "admin_username": active_settings.admin_username,
        "domain_monitor_loop_seconds": active_settings.domain_monitor_loop_seconds,
        "domain_verify_pending_interval_seconds": active_settings.domain_verify_pending_interval_seconds,
        "domain_verify_active_interval_seconds": active_settings.domain_verify_active_interval_seconds,
        "domain_verify_disabled_interval_seconds": active_settings.domain_verify_disabled_interval_seconds,
        "domain_verify_failure_threshold": active_settings.domain_verify_failure_threshold,
    }
