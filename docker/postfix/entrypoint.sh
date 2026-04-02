#!/bin/sh
set -eu

ROOT_DOMAIN="${ROOT_DOMAIN:-}"
SMTP_HOSTNAME="${SMTP_HOSTNAME:-${POSTFIX_HOSTNAME:-}}"
if [ -z "$SMTP_HOSTNAME" ] && [ -n "$ROOT_DOMAIN" ]; then
  SMTP_HOSTNAME="mx.${ROOT_DOMAIN}"
fi
export POSTFIX_HOSTNAME="${SMTP_HOSTNAME:-mx1.mail.local}"
export POSTGRES_HOST="${POSTGRES_HOST:-db}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export POSTGRES_USER="${POSTGRES_USER:-tempmail}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-tempmail}"
export POSTGRES_DB="${POSTGRES_DB:-tempmail}"
export DATABASE_DSN="${DATABASE_DSN:-}"
export DATA_DIR="${DATA_DIR:-/data}"
export POSTFIX_CLIENT_CONNECTION_RATE_LIMIT="${POSTFIX_CLIENT_CONNECTION_RATE_LIMIT:-30}"
export POSTFIX_CLIENT_MESSAGE_RATE_LIMIT="${POSTFIX_CLIENT_MESSAGE_RATE_LIMIT:-60}"
export POSTFIX_CLIENT_RECIPIENT_RATE_LIMIT="${POSTFIX_CLIENT_RECIPIENT_RATE_LIMIT:-120}"
export MESSAGE_SIZE_LIMIT_BYTES="${MESSAGE_SIZE_LIMIT_BYTES:-10485760}"

mkdir -p /etc/postfix /data/raw /data/attachments /run/postfix /signals /certs
chown -R tempmail:tempmail /data

if [ ! -f /certs/fullchain.pem ] || [ ! -f /certs/privkey.pem ]; then
  openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout /certs/privkey.pem \
    -out /certs/fullchain.pem \
    -days 3 \
    -subj "/CN=${POSTFIX_HOSTNAME}" >/dev/null 2>&1
fi

export POSTFIX_TLS_CERT_FILE="/certs/fullchain.pem"
export POSTFIX_TLS_KEY_FILE="/certs/privkey.pem"

envsubst '${POSTFIX_HOSTNAME} ${POSTFIX_CLIENT_CONNECTION_RATE_LIMIT} ${POSTFIX_CLIENT_MESSAGE_RATE_LIMIT} ${POSTFIX_CLIENT_RECIPIENT_RATE_LIMIT} ${MESSAGE_SIZE_LIMIT_BYTES} ${POSTFIX_TLS_CERT_FILE} ${POSTFIX_TLS_KEY_FILE}' \
  < /config/postfix/main.cf.template > /etc/postfix/main.cf
cp /config/postfix/master.cf.template /etc/postfix/master.cf
envsubst '${POSTGRES_HOST} ${POSTGRES_PORT} ${POSTGRES_USER} ${POSTGRES_PASSWORD} ${POSTGRES_DB}' \
  < /config/postfix/pgsql-virtual-mailbox-domains.cf.template > /etc/postfix/pgsql-virtual-mailbox-domains.cf
envsubst '${POSTGRES_HOST} ${POSTGRES_PORT} ${POSTGRES_USER} ${POSTGRES_PASSWORD} ${POSTGRES_DB}' \
  < /config/postfix/pgsql-virtual-mailbox-maps.cf.template > /etc/postfix/pgsql-virtual-mailbox-maps.cf

chown root:postfix /etc/postfix/pgsql-virtual-mailbox-domains.cf
chmod 640 /etc/postfix/pgsql-virtual-mailbox-domains.cf
chown root:postfix /etc/postfix/pgsql-virtual-mailbox-maps.cf
chmod 640 /etc/postfix/pgsql-virtual-mailbox-maps.cf

cat > /usr/local/bin/tempmail-ingest <<EOF
#!/bin/sh
set -eu

cd /app
[ -f /etc/tempmail-ingest.env ] && . /etc/tempmail-ingest.env

exec /usr/local/bin/python -m tempmail.cli.ingest "\$@"
EOF

chmod 755 /usr/local/bin/tempmail-ingest

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

write_ingest_env_var() {
  name="$1"
  value="$2"
  printf 'export %s=%s\n' "$name" "$(shell_quote "$value")" >> /etc/tempmail-ingest.env
}

: > /etc/tempmail-ingest.env
chown root:tempmail /etc/tempmail-ingest.env
chmod 640 /etc/tempmail-ingest.env

write_ingest_env_var POSTGRES_HOST "${POSTGRES_HOST}"
write_ingest_env_var POSTGRES_PORT "${POSTGRES_PORT}"
write_ingest_env_var POSTGRES_USER "${POSTGRES_USER}"
write_ingest_env_var POSTGRES_PASSWORD "${POSTGRES_PASSWORD}"
write_ingest_env_var POSTGRES_DB "${POSTGRES_DB}"
write_ingest_env_var DATABASE_DSN "${DATABASE_DSN}"
write_ingest_env_var DATA_DIR "${DATA_DIR}"
write_ingest_env_var TEMPMAIL_SKIP_STARTUP_MIGRATIONS "1"
write_ingest_env_var ROOT_DOMAIN "${ROOT_DOMAIN:-}"
write_ingest_env_var WEB_HOSTNAME "${WEB_HOSTNAME:-}"
write_ingest_env_var SMTP_HOSTNAME "${SMTP_HOSTNAME:-}"
write_ingest_env_var POSTFIX_HOSTNAME "${POSTFIX_HOSTNAME:-}"
write_ingest_env_var BASE_DOMAINS "${BASE_DOMAINS:-}"
write_ingest_env_var DEFAULT_BASE_DOMAIN "${DEFAULT_BASE_DOMAIN:-}"
write_ingest_env_var MESSAGE_SIZE_LIMIT_BYTES "${MESSAGE_SIZE_LIMIT_BYTES:-}"
write_ingest_env_var MAX_TEXT_BODY_CHARS "${MAX_TEXT_BODY_CHARS:-}"
write_ingest_env_var MAX_HTML_BODY_CHARS "${MAX_HTML_BODY_CHARS:-}"
write_ingest_env_var MAX_ATTACHMENTS_PER_MESSAGE "${MAX_ATTACHMENTS_PER_MESSAGE:-}"
write_ingest_env_var PURGE_GRACE_MINUTES "${PURGE_GRACE_MINUTES:-}"
write_ingest_env_var ACCESS_EVENT_RETENTION_DAYS "${ACCESS_EVENT_RETENTION_DAYS:-}"
write_ingest_env_var CLEANUP_BATCH_SIZE "${CLEANUP_BATCH_SIZE:-}"

watch_and_reload() {
  last_state=""
  while true; do
    current_state="$(sha256sum /certs/fullchain.pem /certs/privkey.pem /signals/certs.updated 2>/dev/null | sha256sum | cut -d' ' -f1 || true)"
    if [ -n "$last_state" ] && [ "$current_state" != "$last_state" ]; then
      postfix reload >/dev/null 2>&1 || true
    fi
    last_state="$current_state"
    sleep 30
  done
}

touch /signals/certs.updated
watch_and_reload &

exec "$@"
