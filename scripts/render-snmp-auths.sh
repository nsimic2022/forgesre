#!/usr/bin/env bash
# Write per-asset SNMP auths (Custom community / v3 from Assets) into data/generated/snmp.yml and
# reload snmp_exporter. Needs Core up. Never prints community strings or passwords.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./install.sh once on a new VM, then use ./forgesre update."
  exit 1
fi

# Export .env and secrets (FORGESRE_HTTP_PORT, ALERTMANAGER_WEBHOOK_TOKEN, FORGESRE_DATA)
# so render_snmp_auths.py can read them from its environment.
set -a
# shellcheck disable=SC1091
source .env
if [[ -f secrets/secrets.env ]]; then
  # shellcheck disable=SC1091
  source secrets/secrets.env
fi
set +a

exec python3 "$ROOT/scripts/render_snmp_auths.py" "${FORGESRE_DATA:-./data}/generated/snmp.yml"
