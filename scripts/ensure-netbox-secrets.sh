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
# NetBox v4.5+ API_TOKEN_PEPPERS: pepper must be ≥50 chars (hex-32 = 64).
nb_pepper="$(openssl rand -hex 32)"
secret_val() {
  awk -F= -v k="$1" '$0 ~ "^"k"=" {print substr($0, index($0,"=")+1)}' "$ROOT/secrets/secrets.env" | tail -1
}
# Prefer an existing ForgeSRE or docker-native pepper so we do not rotate hashes.
existing_pepper="$(secret_val NETBOX_API_TOKEN_PEPPER)"
if [[ ${#existing_pepper} -lt 50 ]]; then
  existing_pepper="$(secret_val API_TOKEN_PEPPER_1)"
fi
if [[ ${#existing_pepper} -ge 50 ]]; then
  nb_pepper="$existing_pepper"
fi
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_DB_PASSWORD "$nb_db"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_REDIS_PASSWORD "$nb_redis"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_SECRET_KEY "$nb_secret"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_SUPERUSER_NAME "admin"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_SUPERUSER_EMAIL "admin@forgesre.local"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_SUPERUSER_PASSWORD "$nb_admin"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_API_TOKEN "$nb_token"
ensure_nonempty "$ROOT/secrets/secrets.env" NETBOX_API_TOKEN_PEPPER "$nb_pepper"
# Official image reads API_TOKEN_PEPPER_1 via env_file; keep it aligned.
nb_pepper_value="$(secret_val NETBOX_API_TOKEN_PEPPER)"
if [[ -n "${nb_pepper_value}" ]]; then
  ensure_nonempty "$ROOT/secrets/secrets.env" API_TOKEN_PEPPER_1 "$nb_pepper_value"
  if grep -qE "^API_TOKEN_PEPPER_1=" "$ROOT/secrets/secrets.env"; then
    sed -i "s|^API_TOKEN_PEPPER_1=.*|API_TOKEN_PEPPER_1=${nb_pepper_value}|" "$ROOT/secrets/secrets.env"
  fi
fi
ensure_nonempty "$ROOT/.env" NETBOX_PORT "8001"
ensure_nonempty "$ROOT/.env" NETBOX_URL "http://127.0.0.1:8001"
ensure_nonempty "$ROOT/.env" NETBOX_DB_PASSWORD "$(awk -F= '/^NETBOX_DB_PASSWORD=/ {print $2}' "$ROOT/secrets/secrets.env" | tail -1)"
ensure_nonempty "$ROOT/.env" NETBOX_REDIS_PASSWORD "$(awk -F= '/^NETBOX_REDIS_PASSWORD=/ {print $2}' "$ROOT/secrets/secrets.env" | tail -1)"
ensure_nonempty "$ROOT/.env" NETBOX_SECRET_KEY "$(awk -F= '/^NETBOX_SECRET_KEY=/ {print $2}' "$ROOT/secrets/secrets.env" | tail -1)"
# Compose interpolates ${NETBOX_API_TOKEN_PEPPER} → container API_TOKEN_PEPPER_1.
nb_pepper_value="$(awk -F= '/^NETBOX_API_TOKEN_PEPPER=/ {print substr($0, index($0,"=")+1)}' "$ROOT/secrets/secrets.env" | tail -1)"
if [[ -n "${nb_pepper_value}" ]]; then
  ensure_nonempty "$ROOT/.env" NETBOX_API_TOKEN_PEPPER "$nb_pepper_value"
  if grep -qE "^NETBOX_API_TOKEN_PEPPER=" "$ROOT/.env"; then
    sed -i "s|^NETBOX_API_TOKEN_PEPPER=.*|NETBOX_API_TOKEN_PEPPER=${nb_pepper_value}|" "$ROOT/.env"
  fi
fi
# Compose interpolates ${NETBOX_API_TOKEN} from .env; Core also reads secrets.env.
# Keep .env aligned with secrets so the bundled token is the same string NetBox upserts.
nb_token_value="$(awk -F= '/^NETBOX_API_TOKEN=/ {print substr($0, index($0,"=")+1)}' "$ROOT/secrets/secrets.env" | tail -1)"
if [[ -n "${nb_token_value}" ]]; then
  ensure_nonempty "$ROOT/.env" NETBOX_API_TOKEN "$nb_token_value"
  if grep -qE "^NETBOX_API_TOKEN=" "$ROOT/.env"; then
    sed -i "s|^NETBOX_API_TOKEN=.*|NETBOX_API_TOKEN=${nb_token_value}|" "$ROOT/.env"
  fi
fi
ensure_nonempty "$ROOT/.env" NETBOX_SUPERUSER_NAME "admin"
ensure_nonempty "$ROOT/.env" NETBOX_SUPERUSER_EMAIL "admin@forgesre.local"
ensure_nonempty "$ROOT/.env" NETBOX_SUPERUSER_PASSWORD "$(awk -F= '/^NETBOX_SUPERUSER_PASSWORD=/ {print $2}' "$ROOT/secrets/secrets.env" | tail -1)"
chmod 600 "$ROOT/secrets/secrets.env" 2>/dev/null || true
