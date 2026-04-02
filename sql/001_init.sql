CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

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
  last_accessed_at TIMESTAMPTZ,
  CONSTRAINT chk_mailbox_status CHECK (status IN ('active', 'disabled', 'expired'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mailboxes_address_lower
  ON mailboxes ((lower(address)));

CREATE INDEX IF NOT EXISTS idx_mailboxes_address_trgm
  ON mailboxes
  USING gin (address gin_trgm_ops);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mailboxes_token_hash
  ON mailboxes (token_hash);

CREATE INDEX IF NOT EXISTS idx_mailboxes_status_expires_at
  ON mailboxes (status, expires_at);

CREATE INDEX IF NOT EXISTS idx_mailboxes_created_at
  ON mailboxes (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_mailboxes_cleanup_ready
  ON mailboxes (status, COALESCE(disabled_at, expires_at))
  WHERE status IN ('expired', 'disabled');

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
);

CREATE INDEX IF NOT EXISTS idx_messages_mailbox_received_at
  ON messages (mailbox_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_received_at
  ON messages (received_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_subject_trgm
  ON messages
  USING gin (subject gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_messages_from_header_trgm
  ON messages
  USING gin (from_header gin_trgm_ops);

CREATE TABLE IF NOT EXISTS attachments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  filename TEXT,
  content_type TEXT NOT NULL,
  size_bytes BIGINT NOT NULL,
  sha256 TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_attachments_message_id
  ON attachments (message_id);

CREATE TABLE IF NOT EXISTS access_events (
  id BIGSERIAL PRIMARY KEY,
  action TEXT NOT NULL,
  ip INET,
  mailbox_id UUID,
  token_hash TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_access_events_action_ip_created_at
  ON access_events (action, ip, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_access_events_action_token_created_at
  ON access_events (action, token_hash, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_access_events_created_at
  ON access_events (created_at DESC);
