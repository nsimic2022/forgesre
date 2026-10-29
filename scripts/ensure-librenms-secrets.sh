#!/usr/bin/env bash
# Fill missing LibreNMS DB / Redis / APP_KEY values. Never rotates a non-empty key.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DATA_DIR="${FORGESRE_DATA:-./data}"
mkdir -p secrets "${DATA_DIR}/librenms/mysql" "${DATA_DIR}/librenms/redis" "${DATA_DIR}/librenms/data"
chmod 700 secrets 2>/dev/null || true
touch secrets/secrets.env
chmod 600 secrets/secrets.env 2>/dev/null || true

ensure_nonempty() {
  local file="$1" key="$2" value="$3"
  if [[ ! -f "$file" ]]; then
    printf '%s=%s\n' "$key" "$value" > "$file"
    return 0
  fi
  if grep -qE "^${key}=.+" "$file"; then
    return 0
  fi
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    return 0
  fi
  printf '%s=%s\n' "$key" "$value" >> "$file"
}

ln_db="$(openssl rand -hex 16)"
ln_root="$(openssl rand -hex 16)"
ln_redis="$(openssl rand -hex 16)"
# Laravel APP_KEY: base64: + 32 random bytes.
ln_app="base64:$(openssl rand -base64 32 | tr -d '\n')"

ln_admin="$(openssl rand -hex 12)"

ensure_nonempty "$ROOT/secrets/secrets.env" LIBRENMS_DB_PASSWORD "$ln_db"
ensure_nonempty "$ROOT/secrets/secrets.env" LIBRENMS_DB_ROOT_PASSWORD "$ln_root"
ensure_nonempty "$ROOT/secrets/secrets.env" LIBRENMS_REDIS_PASSWORD "$ln_redis"
ensure_nonempty "$ROOT/secrets/secrets.env" LIBRENMS_APP_KEY "$ln_app"
ensure_nonempty "$ROOT/secrets/secrets.env" LIBRENMS_ADMIN_PASSWORD "$ln_admin"

secret_val() {
  awk -F= -v k="$1" '$0 ~ "^"k"=" {print substr($0, index($0,"=")+1)}' "$ROOT/secrets/secrets.env" | tail -1
}

if [[ -f "$ROOT/.env" ]]; then
  ensure_nonempty "$ROOT/.env" LIBRENMS_URL "http://127.0.0.1:8000"
  ensure_nonempty "$ROOT/.env" LIBRENMS_DB_PASSWORD "$(secret_val LIBRENMS_DB_PASSWORD)"
  ensure_nonempty "$ROOT/.env" LIBRENMS_DB_ROOT_PASSWORD "$(secret_val LIBRENMS_DB_ROOT_PASSWORD)"
  ensure_nonempty "$ROOT/.env" LIBRENMS_REDIS_PASSWORD "$(secret_val LIBRENMS_REDIS_PASSWORD)"
  ensure_nonempty "$ROOT/.env" LIBRENMS_APP_KEY "$(secret_val LIBRENMS_APP_KEY)"
fi
echo "LibreNMS secrets present (empty keys filled, existing values kept)."
