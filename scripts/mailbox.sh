#!/usr/bin/env bash
# Optional Compose profile "mailbox": docker-mailserver + Roundcube.
# Does not change Core SMTP (Gmail / Outlook / whatever is already in YAML)
# unless you pass --bind-core later.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

DMS_IMAGE="ghcr.io/docker-mailserver/docker-mailserver:15.1.0"

usage() {
  cat <<'EOF'
Usage: ./forgesre mailbox [--reset] [--bind-core]

Starts docker-mailserver (Postfix + Dovecot) and Roundcube. Off at install.
Core keep sending through Gmail / Outlook / current YAML. This only adds the
optional on-box server + webmail for when you own a domain.

  --reset       Recreate the forgesre@ mailbox password (MAILBOX_PASSWORD)
  --bind-core   Also point Core SMTP at 127.0.0.1:587 (replaces Gmail/Outlook)

ForgeSRE still only sends. Roundcube on :8081 is the email client (read/reply).
EOF
}

RESET=0
BIND_CORE=0
for arg in "$@"; do
  case "${arg}" in
    -h|--help) usage; exit 0 ;;
    --reset) RESET=1 ;;
    --bind-core) BIND_CORE=1 ;;
    "") ;;
    *) echo "Unknown argument: ${arg}" >&2; usage; exit 1 ;;
  esac
done

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
  MAIL_PASSWORD="$(grep -E '^MAILBOX_PASSWORD=' "${SECRETS}" | tail -n1 | cut -d= -f2- || true)"
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
python3 - "${SECRETS}" "${MAIL_ACCOUNT}" "${MAIL_PASSWORD}" "${MAIL_DOMAIN}" "${BIND_CORE}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
account, password, domain, bind_core = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
keys = {
    "MAILBOX_DOMAIN": domain,
    "MAILBOX_USERNAME": account,
    "MAILBOX_PASSWORD": password,
}
if bind_core == "1":
    keys.update(
        {
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": account,
            "SMTP_PASSWORD": password,
            "SMTP_FROM": account,
            "SMTP_USE_TLS": "true",
        }
    )
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

if [[ "${BIND_CORE}" == "1" ]]; then
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
  if [[ -n "$("${DC[@]}" ps -q core 2>/dev/null || true)" ]]; then
    log "Restarting core so it sends through the on-box mailbox…"
    "${DC[@]}" up -d --force-recreate core
  fi
else
  log "Core SMTP unchanged (Gmail / Outlook / current YAML)."
fi

echo
echo "Optional mailbox profile is up. Core send path was not rewritten."
if [[ "${BIND_CORE}" == "1" ]]; then
  echo "  Core SMTP:  127.0.0.1:587  (bind-core)"
else
  echo "  Core SMTP:  unchanged — keep using Gmail or Outlook in config/forgesre.yml"
fi
echo "  Domain:     ${MAIL_DOMAIN}"
echo "  Account:    ${MAIL_ACCOUNT}"
echo "  Password:   ${MAIL_PASSWORD}"
echo "  Webmail:    http://<this-host>:${ROUNDCUBE_PORT}   (Roundcube)"
echo "  IMAP:       <this-host>:993"
echo "  Inbound MX: port 25 on this host (needs a real domain later)"
echo
echo "ForgeSRE still only sends. Read and reply in Roundcube or in Gmail/Outlook."
echo "Internet receive needs a real domain + MX pointing here, and TCP/25 open."
echo "Add more people:"
echo "  docker compose --profile mailbox exec mailserver setup email add you@${MAIL_DOMAIN} 'password'"
echo "Mailbox password is in secrets/secrets.env (MAILBOX_PASSWORD), not SMTP_PASSWORD."
