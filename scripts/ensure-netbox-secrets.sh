#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./install.sh first (new VM only)."
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a
DATA_DIR="${FORGESRE_DATA:-./data}"
mkdir -p secrets "${DATA_DIR}/netbox/media" "${DATA_DIR}/netbox/reports" "${DATA_DIR}/netbox/scripts" "${DATA_DIR}/netbox/redis"
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
nb_db="$(openssl rand -hex 16)"
nb_redis="$(openssl rand -hex 16)"
nb_secret="$(openssl rand -hex 32)"
nb_admin="$(openssl rand -hex 8)"
nb_token="$(openssl rand -hex 20)"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_DB_PASSWORD "$nb_db"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_REDIS_PASSWORD "$nb_redis"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_SECRET_KEY "$nb_secret"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_SUPERUSER_NAME "admin"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_SUPERUSER_EMAIL "admin@forgesre.local"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_SUPERUSER_PASSWORD "$nb_admin"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_API_TOKEN "$nb_token"
ensure_nonempty "$ROOT/.env" NETBOX_PORT "8001"
ensure_nonempty "$ROOT/.env" NETBOX_URL "http://127.0.0.1:8001"
ensure_nonempty "$ROOT/.env" NETBOX_DB_PASSWORD "$(awk -F= '/^NETBOX_DB_PASSWORD=/ {print $2}' "$ROOT/secrets/secrets.env" | tail -1)"
ensure_nonempty "$ROOT/.env" NETBOX_REDIS_PASSWORD "$(awk -F= '/^NETBOX_REDIS_PASSWORD=/ {print $2}' "$ROOT/secrets/secrets.env" | tail -1)"
ensure_nonempty "$ROOT/.env" NETBOX_SECRET_KEY "$(awk -F= '/^NETBOX_SECRET_KEY=/ {print $2}' "$ROOT/secrets/secrets.env" | tail -1)"
ensure_nonempty "$ROOT/.env" NETBOX_API_TOKEN "$(awk -F= '/^NETBOX_API_TOKEN=/ {print $2}' "$ROOT/secrets/secrets.env" | tail -1)"
ensure_nonempty "$ROOT/.env" NETBOX_SUPERUSER_NAME "admin"
ensure_nonempty "$ROOT/.env" NETBOX_SUPERUSER_EMAIL "admin@forgesre.local"
ensure_nonempty "$ROOT/.env" NETBOX_SUPERUSER_PASSWORD "$(awk -F= '/^NETBOX_SUPERUSER_PASSWORD=/ {print $2}' "$ROOT/secrets/secrets.env" | tail -1)"
chmod 600 "$ROOT/secrets/secrets.env" 2>/dev/null || true
