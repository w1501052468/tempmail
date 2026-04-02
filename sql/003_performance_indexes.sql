CREATE INDEX IF NOT EXISTS idx_messages_mailbox_id
  ON messages (mailbox_id);

CREATE INDEX IF NOT EXISTS idx_messages_mailbox_created_at
  ON messages (mailbox_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_access_events_created_at_desc
  ON access_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created_at_desc
  ON admin_audit_logs (created_at DESC);
