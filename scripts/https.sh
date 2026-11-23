#!/usr/bin/env bash
# HTTPS for the ForgeSRE UI and API: Caddy (compose profile "https") in front of Core.
# Commands: status | on | only | off | cert [HOSTNAME …]
set -euo pipefail
trap 'echo "https.sh: failed at line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
ENV_FILE="${ROOT}/.env"
# shellcheck source=scripts/lib-profiles.sh
source "${ROOT}/scripts/lib-profiles.sh"

usage() {
  cat <<'USAGE'
Usage: ./forgesre https [status|on|only|off|cert [NAME …]]

  status        profile, port, certificate subject and expiry
  on            HTTPS on FORGESRE_HTTPS_PORT (443); plain HTTP on FORGESRE_HTTP_PORT stays
                for agents, Alertmanager and webhooks
  only          like on, and Core listens on 127.0.0.1 only: every user goes through HTTPS
                and the session cookie is Secure
  off           stop the proxy; Core on plain HTTP as before
  cert NAME…    make a new self-signed certificate for these names / IPs (default: this
                host's name and addresses). To use your own, put forgesre.crt (with the
                chain) and forgesre.key in $FORGESRE_DATA/tls and run: ./forgesre https on
USAGE
}

[[ -f "${ENV_FILE}" ]] || { echo "Missing .env. Run ./forgesre install first." >&2; exit 1; }
if docker info >/dev/null 2>&1; then
  DC=(docker compose --env-file "${ENV_FILE}" -f "${ROOT}/docker-compose.yml")
else
  DC=(sudo docker compose --env-file "${ENV_FILE}" -f "${ROOT}/docker-compose.yml")
fi
DATA="$(env_get FORGESRE_DATA)"
DATA="${DATA:-./data}"
[[ "${DATA}" = /* ]] || DATA="${ROOT}/${DATA#./}"
TLS="${DATA}/tls"
PORT="$(env_get FORGESRE_HTTPS_PORT)"
PORT="${PORT:-443}"

# Self-signed certificate for the names given, or this host's name and IPv4 addresses. 825 days
# (the longest browsers accept); `./forgesre doctor` warns 30 days before it expires.
make_cert() {
  local names=("$@") san="" cn n
  if [[ ${#names[@]} -eq 0 ]]; then
    names=("$(hostname -f 2>/dev/null || hostname)" "$(hostname -s 2>/dev/null || hostname)" localhost)
    while read -r ip; do [[ -n "${ip}" ]] && names+=("${ip}"); done < <(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' || true)
    names+=(127.0.0.1)
  fi
  cn="${names[0]}"
  # X.509 limits the CN to 64 characters (long cloud FQDNs); browsers only read the SAN anyway
  [[ ${#cn} -le 64 ]] || cn="ForgeSRE"
  for n in "${names[@]}"; do
    [[ -z "${n}" || ",${san}," == *",DNS:${n},"* || ",${san}," == *",IP:${n},"* ]] && continue
    if [[ "${n}" =~ ^[0-9.]+$ ]]; then san+="${san:+,}IP:${n}"; else san+="${san:+,}DNS:${n}"; fi
  done
  mkdir -p "${TLS}"
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 825 \
    -keyout "${TLS}/forgesre.key" -out "${TLS}/forgesre.crt" \
    -subj "/CN=${cn}/O=ForgeSRE" -addext "subjectAltName=${san}" \
    -addext "extendedKeyUsage=serverAuth" >/dev/null 2>"${TLS}/.openssl.log" || { cat "${TLS}/.openssl.log" >&2; return 1; }
  rm -f "${TLS}/.openssl.log"
  chmod 600 "${TLS}/forgesre.key"
  chmod 644 "${TLS}/forgesre.crt"
  echo "Self-signed certificate for ${san} in ${TLS} (valid 825 days)."
  echo "Browsers warn until you trust it; better: put your company certificate there (forgesre.crt + forgesre.key)."
}

cert_info() {
  if [[ -f "${TLS}/forgesre.crt" ]]; then
    openssl x509 -in "${TLS}/forgesre.crt" -noout -subject -enddate -ext subjectAltName 2>/dev/null | sed 's/^/  /'
  else
    echo "  no certificate in ${TLS}"
  fi
}

cmd="${1:-status}"
shift || true
case "${cmd}" in
  status)
    if profile_on https; then echo "HTTPS: on, port ${PORT}"; else echo "HTTPS: off"; fi
    if [[ "$(env_get FORGESRE_BIND)" == "127.0.0.1" ]]; then echo "Mode: only (Core listens on 127.0.0.1)"; fi
    cert_info
    ;;
  on|only)
    [[ -f "${TLS}/forgesre.crt" && -f "${TLS}/forgesre.key" ]] || make_cert
    mkdir -p "${DATA}/caddy"
    profile_add https
    env_set FORGESRE_HTTPS on
    env_set FORGESRE_HTTPS_PORT "${PORT}"
    if [[ "${cmd}" == "only" ]]; then
      env_set FORGESRE_BIND 127.0.0.1
      env_set FORGESRE_COOKIE_SECURE true
    else
      env_unset FORGESRE_BIND
      env_unset FORGESRE_COOKIE_SECURE
    fi
    "${DC[@]}" up -d core
    "${DC[@]}" up -d proxy
    echo "HTTPS on: https://$(hostname -f 2>/dev/null || hostname):${PORT}/"
    if [[ "${cmd}" == "only" ]]; then echo "Plain HTTP now answers only on this host (127.0.0.1:$(env_get FORGESRE_HTTP_PORT))."; fi
    echo "Set system.public_url to the https:// address so links in mail and chat use it:"
    echo "  ./forgesre settings set system.public_url https://$(hostname -f 2>/dev/null || hostname)$([[ "${PORT}" == 443 ]] || echo ":${PORT}")"
    ;;
  off)
    profile_remove https
    env_set FORGESRE_HTTPS off
    env_unset FORGESRE_BIND
    env_unset FORGESRE_COOKIE_SECURE
    "${DC[@]}" stop proxy >/dev/null 2>&1 || true
    "${DC[@]}" rm -f proxy >/dev/null 2>&1 || true
    "${DC[@]}" up -d core
    echo "HTTPS off. UI on http://$(hostname -f 2>/dev/null || hostname):$(env_get FORGESRE_HTTP_PORT)/"
    ;;
  cert)
    make_cert "$@"
    if profile_on https; then "${DC[@]}" restart proxy; fi
    ;;
  -h|--help|help) usage ;;
  *) usage; exit 1 ;;
esac
