#!/usr/bin/env bash
# Enable the self-hosted mailbox (docker-mailserver + Roundcube) and point
# ForgeSRE SMTP at it. Not a Gmail relay and not a fake catcher (Mailpit).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

DMS_IMAGE="ghcr.io/docker-mailserver/docker-mailserver:15.1.0"

usage() {
  cat <<'EOF'
Usage: ./forgesre mailbox [--reset]

Starts docker-mailserver (Postfix + Dovecot) and Roundcube on this host.
Creates forgesre@<domain>, a domain catch-all, and writes SMTP settings so
Core sends through localhost:587.

  --reset   Recreate the forgesre mailbox password and rewrite SMTP secrets

ForgeSRE still only sends. Roundcube on :8081 is the email client (read/reply).
EOF
}

RESET=0
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "${1:-}" == "--reset" ]]; then
  RESET=1
fi

log() { echo ">> $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
  DC=(docker compose --env-file "${ROOT}/.env" -f "${ROOT}/docker-compose.yml")
elif command -v sudo >/dev/null; then
  DOCKER=(sudo docker)
  DC=(sudo docker compose --env-file "${ROOT}/.env" -f "${ROOT}/docker-compose.yml")
else
  fail "Docker daemon is not accessible."
fi

ENV_FILE="${ROOT}/.env"
SECRETS="${ROOT}/secrets/secrets.env"
YML="${ROOT}/config/forgesre.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
  fail "Missing ${ENV_FILE}. Copy .env.example first (or ./forgesre install on a new VM)."
fi
# shellcheck disable=SC1090
set -a
source "${ENV_FILE}"
set +a

MAIL_DOMAIN="${MAIL_DOMAIN:-${FORGESRE_DOMAIN:-forgesre.local}}"
MAIL_ACCOUNT="forgesre@${MAIL_DOMAIN}"
MAIL_PASSWORD="${MAIL_PASSWORD:-}"
if [[ -z "${MAIL_PASSWORD}" && -f "${SECRETS}" ]]; then
  MAIL_PASSWORD="$(grep -E '^SMTP_PASSWORD=' "${SECRETS}" | tail -n1 | cut -d= -f2- || true)"
fi
if [[ -z "${MAIL_PASSWORD}" || "${RESET}" == "1" ]]; then
  MAIL_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
fi

ROUNDCUBE_PORT="${ROUNDCUBE_PORT:-8081}"
DES_KEY="${ROUNDCUBEMAIL_DES_KEY:-}"
if [[ -z "${DES_KEY}" ]]; then
  DES_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
fi

mkdir -p "${ROOT}/data/dms/mail-data" "${ROOT}/data/dms/mail-state" \
  "${ROOT}/data/dms/mail-logs" "${ROOT}/data/dms/config" \
  "${ROOT}/data/roundcube/db" "${ROOT}/secrets"

set_env_key() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}

merge_compose_profiles() {
  local current add drop next part
  current="$(grep -E '^COMPOSE_PROFILES=' "${ENV_FILE}" | tail -n1 | cut -d= -f2- || true)"
  add="$1"
  drop="$2"
  next=""
  IFS=',' read -ra parts <<< "${current}"
  for part in "${parts[@]}"; do
    part="${part#"${part%%[![:space:]]*}"}"
    part="${part%"${part##*[![:space:]]}"}"
    [[ -z "${part}" || "${part}" == "${drop}" || "${part}" == "${add}" ]] && continue
    if [[ -n "${next}" ]]; then next+=","; fi
    next+="${part}"
  done
  if [[ -n "${next}" ]]; then next+=","; fi
  next+="${add}"
  set_env_key COMPOSE_PROFILES "${next}"
}

set_env_key MAIL_DOMAIN "${MAIL_DOMAIN}"
set_env_key ROUNDCUBE_PORT "${ROUNDCUBE_PORT}"
set_env_key ROUNDCUBEMAIL_DES_KEY "${DES_KEY}"
if grep -qE '^COMPOSE_PROFILES=' "${ENV_FILE}"; then
  merge_compose_profiles mailbox mail
else
  printf 'COMPOSE_PROFILES=mailbox\n' >> "${ENV_FILE}"
fi

# Re-read .env so compose interpolation sees MAIL_DOMAIN / ROUNDCUBE_PORT.
set -a
source "${ENV_FILE}"
set +a

ACCOUNTS_FILE="${ROOT}/data/dms/config/postfix-accounts.cf"
account_in_config() {
  [[ -f "${ACCOUNTS_FILE}" ]] && grep -qE "^${MAIL_ACCOUNT}\\|" "${ACCOUNTS_FILE}"
}

dms_setup_volume() {
  "${DOCKER[@]}" run --rm \
    -e OVERRIDE_HOSTNAME="mail.${MAIL_DOMAIN}" \
    -v "${ROOT}/data/dms/config:/tmp/docker-mailserver" \
    "${DMS_IMAGE}" \
    setup "$@"
}

mailserver_running() {
  local id
  id="$("${DC[@]}" ps -q mailserver 2>/dev/null || true)"
  [[ -n "${id}" ]]
}

ensure_account() {
  if account_in_config && [[ "${RESET}" != "1" ]]; then
    log "Mailbox ${MAIL_ACCOUNT} already exists (password unchanged)."
    return
  fi
  if mailserver_running; then
    if account_in_config && [[ "${RESET}" == "1" ]]; then
      "${DC[@]}" exec -T mailserver setup email update "${MAIL_ACCOUNT}" "${MAIL_PASSWORD}" >/dev/null
      log "Updated password for ${MAIL_ACCOUNT}"
    else
      "${DC[@]}" exec -T mailserver setup email add "${MAIL_ACCOUNT}" "${MAIL_PASSWORD}"
      log "Created mailbox ${MAIL_ACCOUNT}"
    fi
  else
    if account_in_config && [[ "${RESET}" == "1" ]]; then
      dms_setup_volume email update "${MAIL_ACCOUNT}" "${MAIL_PASSWORD}"
      log "Updated password for ${MAIL_ACCOUNT}"
    else
      dms_setup_volume email add "${MAIL_ACCOUNT}" "${MAIL_PASSWORD}"
      log "Created mailbox ${MAIL_ACCOUNT}"
    fi
  fi
}

ensure_catchall() {
  local aliases="${ROOT}/data/dms/config/postfix-virtual.cf"
  if [[ -f "${aliases}" ]] && grep -qE "^@${MAIL_DOMAIN}[[:space:]]" "${aliases}"; then
    return
  fi
  if mailserver_running; then
    "${DC[@]}" exec -T mailserver setup alias add "@${MAIL_DOMAIN}" "${MAIL_ACCOUNT}" || true
  else
    dms_setup_volume alias add "@${MAIL_DOMAIN}" "${MAIL_ACCOUNT}" || true
  fi
}

log "Pulling mailbox images…"
"${DC[@]}" --profile mailbox pull mailserver roundcube

ensure_account
ensure_catchall

log "Starting mailserver + Roundcube for ${MAIL_DOMAIN}…"
"${DC[@]}" --profile mailbox up -d mailserver roundcube

log "Waiting for mailserver SMTP…"
ready=0
for _ in $(seq 1 90); do
  if "${DC[@]}" exec -T mailserver sh -c "ss -lnt 2>/dev/null | grep -qE ':25|:587'" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "${ready}" != "1" ]]; then
  fail "mailserver did not open ports 25/587. Check: docker compose --profile mailbox logs mailserver"
fi

# Running container may have started before the volume account existed.
if mailserver_running && ! "${DC[@]}" exec -T mailserver setup email list 2>/dev/null | grep -qF "${MAIL_ACCOUNT}"; then
  ensure_account
  ensure_catchall
fi

umask 077
touch "${SECRETS}"
python3 - "${SECRETS}" "${MAIL_ACCOUNT}" "${MAIL_PASSWORD}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
account, password = sys.argv[2], sys.argv[3]
keys = {
    "SMTP_HOST": "127.0.0.1",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": account,
    "SMTP_PASSWORD": password,
    "SMTP_FROM": account,
    "SMTP_USE_TLS": "true",
}
text = path.read_text(encoding="utf-8") if path.exists() else ""
lines = text.splitlines()
out = []
seen = set()
for line in lines:
    key = line.split("=", 1)[0] if "=" in line and not line.strip().startswith("#") else ""
    if key in keys:
        out.append(f"{key}={keys[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in keys.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY

python3 - "${YML}" "${MAIL_ACCOUNT}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
account = sys.argv[2]
wanted = {
    "enabled": "true",
    "host": '"127.0.0.1"',
    "port": "587",
    "from": f'"{account}"',
    "tls": "true",
}
text = path.read_text(encoding="utf-8") if path.exists() else ""
lines = text.splitlines()
out: list[str] = []
in_email = False
seen: set[str] = set()

def flush_missing() -> None:
    for key, value in wanted.items():
        if key not in seen:
            out.append(f"    {key}: {value}")

for line in lines:
    stripped = line.strip()
    if in_email:
        indent = len(line) - len(line.lstrip(" "))
        if stripped and not stripped.startswith("#") and indent <= 2:
            flush_missing()
            in_email = False
            out.append(line)
            continue
        key = stripped.split(":", 1)[0] if ":" in stripped and not stripped.startswith("#") else ""
        if indent == 4 and key in wanted:
            out.append(f"    {key}: {wanted[key]}")
            seen.add(key)
            continue
        out.append(line)
        continue
    out.append(line)
    if stripped == "email:" and (line.startswith("  email:") or line.startswith("email:")):
        in_email = True
        seen = set()

if in_email:
    flush_missing()

path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY

if "${DC[@]}" ps -q core >/dev/null 2>&1 && [[ -n "$("${DC[@]}" ps -q core 2>/dev/null || true)" ]]; then
  log "Restarting core so it picks up SMTP secrets…"
  "${DC[@]}" up -d --force-recreate core
fi

echo
echo "Mailbox is yours (not Gmail, not Mailpit)."
echo "  Domain:     ${MAIL_DOMAIN}"
echo "  Account:    ${MAIL_ACCOUNT}"
echo "  Password:   ${MAIL_PASSWORD}"
echo "  Webmail:    http://<this-host>:${ROUNDCUBE_PORT}   (Roundcube — the email client)"
echo "  IMAP:       <this-host>:993"
echo "  Submission: 127.0.0.1:587  (ForgeSRE sends here)"
echo "  Inbound MX: port 25 on this host"
echo
echo "Roundcube is the email client. ForgeSRE only sends; you read and reply in Roundcube."
echo "Internet receive needs a real domain + MX pointing here, and TCP/25 open."
echo "LAN-only (${MAIL_DOMAIN}) works between mailboxes on this box without Gmail."
echo "Add more people:"
echo "  docker compose --profile mailbox exec mailserver setup email add you@${MAIL_DOMAIN} 'password'"
echo "Password is also in secrets/secrets.env (SMTP_PASSWORD)."
