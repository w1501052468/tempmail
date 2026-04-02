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

ACME_EMAIL="${ACME_EMAIL:?ACME_EMAIL is required}"
ACME_SERVER="${ACME_SERVER:-letsencrypt}"
ACME_KEYLENGTH="${ACME_KEYLENGTH:-ec-256}"

mkdir -p /acme.sh /acme-web/.well-known/acme-challenge /certs /signals
touch /signals/certs.updated
echo ok > /acme-web/.well-known/acme-challenge/ping

acme_cert_args() {
  if printf '%s' "$ACME_KEYLENGTH" | grep -q '^ec-'; then
    printf '%s' "--ecc"
  fi
}

has_acme_cert() {
  acme.sh --home /acme.sh --list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fx "$WEB_HOSTNAME" >/dev/null 2>&1
}

acme_domain_args() {
  printf '%s' "-d $WEB_HOSTNAME"
  if [ "$SMTP_HOSTNAME" != "$WEB_HOSTNAME" ]; then
    printf '%s' " -d $SMTP_HOSTNAME"
  fi
}

install_cert() {
  acme.sh --home /acme.sh --server "$ACME_SERVER" --install-cert -d "$WEB_HOSTNAME" $(acme_cert_args) \
    --key-file /certs/privkey.pem \
    --fullchain-file /certs/fullchain.pem \
    --reloadcmd "touch /signals/certs.updated"
}

wait_for_http() {
  count=0
  until [ "$(wget -q -O - "http://edge/.well-known/acme-challenge/ping" 2>/dev/null || true)" = "ok" ]; do
    count=$((count + 1))
    if [ "$count" -gt 60 ]; then
      echo "edge HTTP challenge path was not reachable in time" >&2
      break
    fi
    sleep 5
  done
}

register_account() {
  acme.sh --home /acme.sh --server "$ACME_SERVER" --register-account -m "$ACME_EMAIL" >/dev/null 2>&1 || true
}

issue_cert() {
  # shellcheck disable=SC2086
  acme.sh --home /acme.sh --server "$ACME_SERVER" --issue $(acme_domain_args) -w /acme-web --keylength "$ACME_KEYLENGTH"
}

ensure_cert_installed() {
  while true; do
    if has_acme_cert && install_cert; then
      return 0
    fi
    echo "Certificate was not ready to install yet, trying to issue or renew it..." >&2
    if issue_cert && install_cert; then
      return 0
    fi
    echo "Certificate issue/install failed, retrying in 5 minutes..." >&2
    sleep 300
  done
}

wait_for_http
register_account
ensure_cert_installed

while true; do
  acme.sh --cron --home /acme.sh --server "$ACME_SERVER" >/dev/null 2>&1 || true
  install_cert >/dev/null 2>&1 || true
  sleep 12h
done
