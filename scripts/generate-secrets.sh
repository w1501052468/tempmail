#!/bin/sh
set -eu

echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
echo "APP_TOKEN_HASH_SECRET=$(openssl rand -hex 32)"
echo "ADMIN_SESSION_SECRET=$(openssl rand -hex 32)"
