#!/usr/bin/env bash
# Optional parts of the stack (Compose profiles): list, enable or disable NetBox and LibreNMS.
# Data stays in data/ when a part is disabled; enable brings it back as it was.
set -euo pipefail
trap 'echo "optional.sh: failed at line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
ENV_FILE="${ROOT}/.env"
# shellcheck source=scripts/lib-profiles.sh
source "${ROOT}/scripts/lib-profiles.sh"
[[ -f "${ENV_FILE}" ]] || { echo "Missing .env. Run ./forgesre install first." >&2; exit 1; }
if docker info >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi

usage() {
  cat <<'USAGE'
Usage: ./forgesre optional [list | enable NAME | disable NAME]

  list              every optional part and whether it is on
  enable netbox     bundled NetBox (DCIM / IPAM, Core read-syncs devices)
  enable librenms   bundled LibreNMS (discovery, ports, neighbours)
  disable NAME      stop it, keep its data in data/, turn the integration off in Settings

Other optional parts have their own command: https (./forgesre https), ai (./forgesre fetch-llm),
mailbox (./forgesre mailbox), worker (docs/high-availability.md).
USAGE
}

declare -A SERVICES=(
  [netbox]="netbox-redis netbox"
  [librenms]="librenms-db librenms-redis librenms librenms-dispatcher"
)
declare -A SETTING=([netbox]="inventory.netbox.enabled" [librenms]="inventory.librenms.enabled")
declare -A WHAT=(
  [netbox]="NetBox (DCIM/IPAM)" [librenms]="LibreNMS (discovery)" [https]="HTTPS proxy"
  [ai]="Local model (llama.cpp)" [mailbox]="Mail server + Roundcube" [worker]="Separate worker"
)

# Turn the integration on or off in Core; Core may still be starting, so say how to do it later.
core_setting() {
  "${ROOT}/forgesre" settings set "$1" "$2" >/dev/null 2>&1 \
    || echo "Could not reach Core to set $1=$2; run later: ./forgesre settings set $1 $2"
}

cmd="${1:-list}"
name="${2:-}"
case "${cmd}" in
  list)
    for part in netbox librenms https ai mailbox worker; do
      if profile_on "${part}"; then state="on "; else state="off"; fi
      printf '  %-9s %s  %s\n' "${part}" "${state}" "${WHAT[${part}]}"
    done
    ;;
  enable|disable)
    [[ -n "${SERVICES[${name}]:-}" ]] || { echo "Choose netbox or librenms." >&2; usage; exit 1; }
    if [[ "${cmd}" == "enable" ]]; then
      profile_add "${name}"
      env_set FORGESRE_OPTIONAL_PROFILES 1
      "${ROOT}/scripts/ensure-${name}-secrets.sh"
      # shellcheck disable=SC2086
      "${DC[@]}" up -d ${SERVICES[${name}]}
      core_setting "${SETTING[${name}]}" true
      echo "${WHAT[${name}]} enabled. First start can take several minutes; then ./forgesre doctor."
      if [[ "${name}" == "librenms" ]]; then echo "LibreNMS API token: ./forgesre update creates it once LibreNMS answers."; fi
    else
      profile_remove "${name}"
      env_set FORGESRE_OPTIONAL_PROFILES 1
      core_setting "${SETTING[${name}]}" false
      # shellcheck disable=SC2086
      "${DC[@]}" stop ${SERVICES[${name}]} >/dev/null 2>&1 || true
      # shellcheck disable=SC2086
      "${DC[@]}" rm -f ${SERVICES[${name}]} >/dev/null 2>&1 || true
      echo "${WHAT[${name}]} disabled; its data stays in data/. Back on: ./forgesre optional enable ${name}"
    fi
    ;;
  -h|--help|help) usage ;;
  *) usage; exit 1 ;;
esac
