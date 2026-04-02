import os
from contextlib import contextmanager
from threading import Lock

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings
from .runtime_config import ensure_admin_schema
from .services.domain_service import ensure_seeded_managed_domains
from .services.policy_service import ensure_default_allow_all_policy

_DB_POOL: ConnectionPool | None = None
_DB_POOL_LOCK = Lock()


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _database_conninfo() -> str:
    settings = get_settings()
    if any(
        os.getenv(name)
        for name in ["POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"]
    ):
        return make_conninfo(
            host=os.getenv("POSTGRES_HOST", "db"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            dbname=os.getenv("POSTGRES_DB", "tempmail"),
            user=os.getenv("POSTGRES_USER", "tempmail"),
            password=os.getenv("POSTGRES_PASSWORD", "tempmail"),
        )
    return settings.database_dsn


def _connection_kwargs() -> dict[str, object]:
    return {"row_factory": dict_row}


def ensure_core_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mailboxes (
              id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
              address TEXT NOT NULL,
              base_domain TEXT NOT NULL,
              subdomain TEXT NOT NULL,
              local_part TEXT NOT NULL,
              token_hash TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active',
              created_ip INET,
              created_user_agent TEXT,
              last_access_ip INET,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              expires_at TIMESTAMPTZ NOT NULL,
              disabled_at TIMESTAMPTZ,
              last_accessed_at TIMESTAMPTZ
            )
            """
        )
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS address TEXT")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS base_domain TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS subdomain TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS local_part TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS token_hash TEXT")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS created_ip INET")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS created_user_agent TEXT")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS last_access_ip INET")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ")
        cur.execute(
            """
            UPDATE mailboxes
            SET local_part = split_part(address, '@', 1)
            WHERE COALESCE(local_part, '') = ''
              AND position('@' in address) > 0
            """
        )
        cur.execute(
            """
            UPDATE mailboxes
            SET base_domain = split_part(address, '@', 2)
            WHERE COALESCE(base_domain, '') = ''
              AND position('@' in address) > 0
            """
        )
        cur.execute(
            """
            UPDATE mailboxes
            SET subdomain = ''
            WHERE subdomain IS NULL
            """
        )
        cur.execute(
            """
            UPDATE mailboxes
            SET status = 'active'
            WHERE status IS NULL
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_mailboxes_address_lower
              ON mailboxes ((lower(address)))
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mailboxes_address_trgm
              ON mailboxes
              USING gin (address gin_trgm_ops)
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_mailboxes_token_hash
              ON mailboxes (token_hash)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mailboxes_status_expires_at
              ON mailboxes (status, expires_at)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mailboxes_created_at
              ON mailboxes (created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mailboxes_cleanup_ready
              ON mailboxes (status, COALESCE(disabled_at, expires_at))
              WHERE status IN ('expired', 'disabled')
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
              id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
              mailbox_id UUID NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
              envelope_from TEXT,
              envelope_to TEXT NOT NULL,
              helo_name TEXT,
              client_address INET,
              subject TEXT,
              message_id TEXT,
              date_header TIMESTAMPTZ,
              from_header TEXT,
              to_header TEXT,
              reply_to TEXT,
              text_body TEXT,
              html_body TEXT,
              headers_json JSONB NOT NULL DEFAULT '{}'::jsonb,
              raw_path TEXT NOT NULL,
              raw_sha256 TEXT NOT NULL,
              size_bytes BIGINT NOT NULL,
              attachment_count INTEGER NOT NULL DEFAULT 0,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS mailbox_id UUID")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS envelope_from TEXT")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS envelope_to TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS helo_name TEXT")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS client_address INET")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS subject TEXT")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_id TEXT")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS date_header TIMESTAMPTZ")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS from_header TEXT")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS to_header TEXT")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS reply_to TEXT")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS text_body TEXT")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS html_body TEXT")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS headers_json JSONB NOT NULL DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS raw_path TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS raw_sha256 TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS size_bytes BIGINT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS attachment_count INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_mailbox_received_at
              ON messages (mailbox_id, received_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_received_at
              ON messages (received_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_subject_trgm
              ON messages
              USING gin (subject gin_trgm_ops)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_from_header_trgm
              ON messages
              USING gin (from_header gin_trgm_ops)
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS attachments (
              id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
              message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
              filename TEXT,
              content_type TEXT NOT NULL,
              size_bytes BIGINT NOT NULL,
              sha256 TEXT NOT NULL,
              storage_path TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("ALTER TABLE attachments ADD COLUMN IF NOT EXISTS message_id UUID")
        cur.execute("ALTER TABLE attachments ADD COLUMN IF NOT EXISTS filename TEXT")
        cur.execute("ALTER TABLE attachments ADD COLUMN IF NOT EXISTS content_type TEXT NOT NULL DEFAULT 'application/octet-stream'")
        cur.execute("ALTER TABLE attachments ADD COLUMN IF NOT EXISTS size_bytes BIGINT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE attachments ADD COLUMN IF NOT EXISTS sha256 TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE attachments ADD COLUMN IF NOT EXISTS storage_path TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE attachments ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_attachments_message_id
              ON attachments (message_id)
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS access_events (
              id BIGSERIAL PRIMARY KEY,
              action TEXT NOT NULL,
              ip INET,
              mailbox_id UUID,
              token_hash TEXT,
              metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("ALTER TABLE access_events ADD COLUMN IF NOT EXISTS action TEXT NOT NULL DEFAULT 'unknown'")
        cur.execute("ALTER TABLE access_events ADD COLUMN IF NOT EXISTS ip INET")
        cur.execute("ALTER TABLE access_events ADD COLUMN IF NOT EXISTS mailbox_id UUID")
        cur.execute("ALTER TABLE access_events ADD COLUMN IF NOT EXISTS token_hash TEXT")
        cur.execute("ALTER TABLE access_events ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE access_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_access_events_action_ip_created_at
              ON access_events (action, ip, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_access_events_action_token_created_at
              ON access_events (action, token_hash, created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_access_events_created_at
              ON access_events (created_at DESC)
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS system_events (
              id BIGSERIAL PRIMARY KEY,
              event_type TEXT NOT NULL,
              level TEXT NOT NULL DEFAULT 'info',
              source TEXT NOT NULL,
              mailbox_id UUID,
              message_id UUID,
              address TEXT,
              summary TEXT NOT NULL,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("ALTER TABLE system_events ADD COLUMN IF NOT EXISTS event_type TEXT NOT NULL DEFAULT 'unknown'")
        cur.execute("ALTER TABLE system_events ADD COLUMN IF NOT EXISTS level TEXT NOT NULL DEFAULT 'info'")
        cur.execute("ALTER TABLE system_events ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'system'")
        cur.execute("ALTER TABLE system_events ADD COLUMN IF NOT EXISTS mailbox_id UUID")
        cur.execute("ALTER TABLE system_events ADD COLUMN IF NOT EXISTS message_id UUID")
        cur.execute("ALTER TABLE system_events ADD COLUMN IF NOT EXISTS address TEXT")
        cur.execute("ALTER TABLE system_events ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE system_events ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE system_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_system_events_created_at
              ON system_events (created_at DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_system_events_type_created_at
              ON system_events (event_type, created_at DESC)
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS domain_policies (
              id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
              scope TEXT NOT NULL,
              pattern TEXT NOT NULL,
              action TEXT NOT NULL,
              priority INTEGER NOT NULL DEFAULT 100,
              status TEXT NOT NULL DEFAULT 'active',
              note TEXT,
              match_count BIGINT NOT NULL DEFAULT 0,
              last_matched_at TIMESTAMPTZ,
              updated_by TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'recipient_base_domain'")
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS pattern TEXT NOT NULL DEFAULT '*'")
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS action TEXT NOT NULL DEFAULT 'allow'")
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 100")
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS note TEXT")
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS match_count BIGINT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS last_matched_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS updated_by TEXT")
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute("ALTER TABLE domain_policies ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_domain_policies_scope_status_priority
              ON domain_policies (scope, status, priority ASC, created_at ASC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_domain_policies_status_priority
              ON domain_policies (status, priority ASC, created_at ASC)
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS managed_domains (
              id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
              domain TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              source TEXT NOT NULL DEFAULT 'admin',
              note TEXT,
              expected_mx_host TEXT,
              failure_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              root_mx_hosts TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
              wildcard_mx_hosts TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
              last_checked_at TIMESTAMPTZ,
              verified_at TIMESTAMPTZ,
              updated_by TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS domain TEXT")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'admin'")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS note TEXT")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS expected_mx_host TEXT")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS last_error TEXT")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS root_mx_hosts TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]")
        cur.execute(
            "ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS wildcard_mx_hosts TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]"
        )
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS updated_by TEXT")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute("ALTER TABLE managed_domains ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_managed_domains_domain_lower
              ON managed_domains ((lower(domain)))
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_managed_domains_status_domain
              ON managed_domains (status, domain ASC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_managed_domains_status_last_checked_at
              ON managed_domains (status, last_checked_at ASC NULLS FIRST)
            """
        )


def connect():
    return psycopg.connect(_database_conninfo(), **_connection_kwargs())


def get_db_pool() -> ConnectionPool:
    global _DB_POOL
    if _DB_POOL is not None:
        return _DB_POOL

    with _DB_POOL_LOCK:
        if _DB_POOL is None:
            min_size = _env_int("DB_POOL_MIN_SIZE", 1)
            max_size = _env_int("DB_POOL_MAX_SIZE", 10, minimum=min_size)
            timeout = _env_int("DB_POOL_TIMEOUT_SECONDS", 30)
            pool = ConnectionPool(
                conninfo=_database_conninfo(),
                min_size=min_size,
                max_size=max_size,
                timeout=timeout,
                kwargs=_connection_kwargs(),
                open=True,
            )
            pool.wait()
            _DB_POOL = pool

    return _DB_POOL


def open_db_pool() -> ConnectionPool:
    return get_db_pool()


def close_db_pool() -> None:
    global _DB_POOL
    with _DB_POOL_LOCK:
        if _DB_POOL is not None:
            _DB_POOL.close()
            _DB_POOL = None


@contextmanager
def get_connection():
    pool = get_db_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def run_startup_migrations() -> None:
    with get_connection() as conn:
        ensure_core_schema(conn)
        ensure_admin_schema(conn)
        ensure_seeded_managed_domains(conn)
        ensure_default_allow_all_policy(conn)
