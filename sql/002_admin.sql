CREATE TABLE IF NOT EXISTS admin_runtime_config (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_by TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO admin_runtime_config (singleton, config, updated_by)
VALUES (TRUE, '{}'::jsonb, 'system')
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS admin_audit_logs (
  id BIGSERIAL PRIMARY KEY,
  action TEXT NOT NULL,
  admin_username TEXT NOT NULL,
  ip INET,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created_at
  ON admin_audit_logs (created_at DESC);
