#!/bin/sh
set -eu

ROOT_DOMAIN="${ROOT_DOMAIN:-}"
WEB_HOSTNAME="${WEB_HOSTNAME:-}"
SMTP_HOSTNAME="${SMTP_HOSTNAME:-${POSTFIX_HOSTNAME:-}}"
if [ -z "$WEB_HOSTNAME" ] && [ -n "$ROOT_DOMAIN" ]; then
  WEB_HOSTNAME="mail.${ROOT_DOMAIN}"
fi
if [ -z "$SMTP_HOSTNAME" ] && [ -n "$ROOT_DOMAIN" ]; then
  SMTP_HOSTNAME="mx.${ROOT_DOMAIN}"
fi
WEB_HOSTNAME="${WEB_HOSTNAME:-mail.local}"
SMTP_HOSTNAME="${SMTP_HOSTNAME:-$WEB_HOSTNAME}"
ACME_HOSTS="$WEB_HOSTNAME"
if [ "$SMTP_HOSTNAME" != "$WEB_HOSTNAME" ]; then
  ACME_HOSTS="$ACME_HOSTS $SMTP_HOSTNAME"
fi

SIGNAL_FILE="/signals/certs.updated"
CERT_DIR="/certs"
EDGE_GENERAL_RATE="${EDGE_GENERAL_RATE:-30r/s}"
EDGE_GENERAL_BURST="${EDGE_GENERAL_BURST:-60}"
EDGE_CREATE_RATE="${EDGE_CREATE_RATE:-10r/m}"
EDGE_CREATE_BURST="${EDGE_CREATE_BURST:-20}"
EDGE_INBOX_RATE="${EDGE_INBOX_RATE:-120r/m}"
EDGE_INBOX_BURST="${EDGE_INBOX_BURST:-60}"

mkdir -p /var/www/acme /signals "$CERT_DIR"

if [ ! -s "$CERT_DIR/fullchain.pem" ] || [ ! -s "$CERT_DIR/privkey.pem" ]; then
  openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$CERT_DIR/privkey.pem" \
    -out "$CERT_DIR/fullchain.pem" \
    -days 3 \
    -subj "/CN=${WEB_HOSTNAME}" >/dev/null 2>&1
fi

export WEB_HOSTNAME SMTP_HOSTNAME ACME_HOSTS
export EDGE_GENERAL_RATE EDGE_GENERAL_BURST EDGE_CREATE_RATE EDGE_CREATE_BURST EDGE_INBOX_RATE EDGE_INBOX_BURST
envsubst '${EDGE_GENERAL_RATE} ${EDGE_CREATE_RATE} ${EDGE_INBOX_RATE}' < /etc/nginx/templates/nginx.conf.template > /etc/nginx/nginx.conf
envsubst '${WEB_HOSTNAME} ${SMTP_HOSTNAME} ${ACME_HOSTS} ${EDGE_GENERAL_BURST} ${EDGE_CREATE_BURST} ${EDGE_INBOX_BURST}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf

touch "$SIGNAL_FILE"

watch_and_reload() {
  last_state=""
  while true; do
    current_state="$(sha256sum "$CERT_DIR/fullchain.pem" "$CERT_DIR/privkey.pem" "$SIGNAL_FILE" 2>/dev/null | sha256sum | cut -d' ' -f1 || true)"
    if [ -n "$last_state" ] && [ "$current_state" != "$last_state" ]; then
      nginx -s reload >/dev/null 2>&1 || true
    fi
    last_state="$current_state"
    sleep 30
  done
}

watch_and_reload &

exec nginx -g 'daemon off;'
