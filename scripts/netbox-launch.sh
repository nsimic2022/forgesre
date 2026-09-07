#!/bin/bash
# Granian bind for host-network NetBox. Core already owns :8080.
#
# docker-entrypoint creates a superuser only on first insert. netbox-docker
# 5.0.2 / NetBox v4.6 will not mint SUPERUSER_API_TOKEN unless SUPERUSER_API_KEY
# is also set (v2 tokens). Later starts print "already exists" and skip the
# token, so Core's NETBOX_API_TOKEN 403s /api/dcim/devices/.
#
# NetBox v4.6 Token model: default version is v2. `key` is a 12-char public id,
# NOT the secret (HMAC with API_TOKEN_PEPPERS). The UI shows the full secret
# once: nbt_<key>.<secret>. REST wants Authorization: Bearer nbt_….
#
# Prefer that full v2 string in NETBOX_API_TOKEN. scripts/netbox-upsert-token.py
# skips v1 create when the secret looks like v2 — do not overwrite N's UI token.
# A legacy 40-char v1 value is still upserted as plaintext (write_enabled=False,
# SUPERUSER_NAME / NETBOX_SUPERUSER_NAME, dcim.view_device). Core is not a NetBox UI login.
# A token on a non-superuser without DCIM view is HTTP 403 even when the secret
# is valid. Does not touch database forgesre.
#
# NetBox 4.5+ User has no is_staff. The previous inline shell touched is_staff
# and AttributeError was swallowed as "could not upsert" while Granian still
# started — Discovery stayed HTTP 403. The helper logs exception type + message
# (never the token). UI start is not blocked if upsert fails; doctor stays honest.
#
# UI token create (v2) still needs API_TOKEN_PEPPERS (≥50 chars). Compose sets
# API_TOKEN_PEPPER_1 from NETBOX_API_TOKEN_PEPPER in secrets/secrets.env.
set -euo pipefail
PORT="${NETBOX_HTTP_PORT:-8001}"
HOST="${NETBOX_BIND_HOST:-0.0.0.0}"
# shellcheck disable=SC1091
source /opt/netbox/venv/bin/activate

# Last KEY= in a dotenv file. Prints the value to stdout for capture; caller must not log it.
_dotenv_value() {
  local file="$1" key="$2" found="" line name value
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    name="${line%%=*}"
    value="${line#*=}"
    name="${name%"${name##*[![:space:]]}"}"
    name="${name#"${name%%[![:space:]]*}"}"
    if [[ "$name" == "$key" ]]; then
      value="${value%\"}"
      value="${value#\"}"
      value="${value%\'}"
      value="${value#\'}"
      found="$value"
    fi
  done < "$file"
  printf '%s' "$found"
}

ensure_core_api_token() {
  local token="${NETBOX_API_TOKEN:-${SUPERUSER_API_TOKEN:-}}"
  if [[ -z "${token}" && -f /run/secrets/forgesre-secrets.env ]]; then
    token="$(_dotenv_value /run/secrets/forgesre-secrets.env NETBOX_API_TOKEN)"
    if [[ -z "${token}" ]]; then
      token="$(_dotenv_value /run/secrets/forgesre-secrets.env SUPERUSER_API_TOKEN)"
    fi
  fi
  if [[ -z "${token}" ]]; then
    echo "forgesre: NETBOX_API_TOKEN empty — Core sync will get HTTP 403" >&2
    return 0
  fi
  local upsert_py="${FORGESRE_UPSERT_TOKEN_PY:-/opt/netbox/forgesre-upsert-token.py}"
  if [[ ! -f "${upsert_py}" ]]; then
    echo "forgesre: upsert script missing at ${upsert_py} (UI still starts)" >&2
    echo "forgesre: that means the token never landed in the NetBox DB; Discovery stays HTTP 403" >&2
    return 0
  fi
  (
    cd /opt/netbox/netbox
    FORGESRE_NB_TOKEN="${token}" \
    FORGESRE_NB_USER="${SUPERUSER_NAME:-admin}" \
    FORGESRE_NB_EMAIL="${SUPERUSER_EMAIL:-admin@forgesre.local}" \
    FORGESRE_NB_PASSWORD="${SUPERUSER_PASSWORD:-}" \
    python "${upsert_py}"
  ) || {
    echo "forgesre: could not upsert NetBox API token (UI still starts)" >&2
    echo "forgesre: that means the token never landed in the NetBox DB; Discovery stays HTTP 403" >&2
  }
}

ensure_core_api_token

exec granian \
  --host "${HOST}" \
  --port "${PORT}" \
  --interface "wsgi" \
  --no-ws \
  --workers "${GRANIAN_WORKERS:-2}" \
  --respawn-failed-workers \
  --backpressure "${GRANIAN_BACKPRESSURE:-${GRANIAN_WORKERS:-2}}" \
  --loop "uvloop" \
  --log \
  --log-level "info" \
  --access-log \
  --working-dir "/opt/netbox/netbox/" \
  --static-path-route "/static" \
  --static-path-mount "/opt/netbox/netbox/static/" \
  --static-path-dir-to-file index.html \
  --pid-file "/tmp/granian.pid" \
  "netbox.granian:application"
