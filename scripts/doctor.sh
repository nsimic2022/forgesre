#!/usr/bin/env bash
# ./forgesre doctor: Core liveness, then the /api/v1/system/doctor lights (same as /health-ui).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Core HTTP port from .env (last FORGESRE_HTTP_PORT=, quotes and CR stripped), default 8080.
PORT=8080
if [[ -f .env ]]; then
  _p="$(awk -F= '/^[[:space:]]*FORGESRE_HTTP_PORT=/ {v=$2} END {print v}' .env | tr -d '"' | tr -d "'" | tr -d '\r' | awk '{print $1}' || true)"
  PORT="${_p:-8080}"
fi

echo "ForgeSRE Health"
echo

# One aligned output row: ok = check, bad = cross plus the reason. Same width as the rows below.
ok() { printf "  %-20s  ✓\n" "$1"; }
bad() { printf "  %-20s  ✗  %s\n" "$1" "$2"; }

# Unauthenticated liveness. /api/v1/system/doctor needs a webhook token and must
# not be used to decide whether Core is up.
if curl -fsS "http://127.0.0.1:${PORT}/api/v1/health" >/dev/null 2>&1; then
  ok "Core API"
else
  bad "Core API" "UI/API not reachable on port ${PORT}"
  echo
  echo "Could not reach GET /api/v1/health (no login, no webhook token)."
  echo "Why: Core is down or the port is wrong — not a missing webhook token."
  echo "Test: curl -v http://127.0.0.1:${PORT}/api/v1/health"
  echo "Fix: docker compose logs core --tail=80"
  exit 1
fi

# Webhook bearer token for the SD and doctor endpoints (empty if secrets are unreadable).
# Parse KEY=value — never source secrets.env (LibreNMS tokens contain `|`).
TOKEN=""
if [[ -f secrets/secrets.env ]]; then
  # shellcheck source=dotenv_load.sh
  source "$ROOT/scripts/dotenv_load.sh"
  dotenv_load "$ROOT/secrets/secrets.env"
  TOKEN="${ALERTMANAGER_WEBHOOK_TOKEN:-}"
fi

# Bundled snmp-exporter is a default compose service (not Zabbix). Start it
# only when Core already has SNMP/network SD targets; otherwise doctor reports
# paused, not DOWN.
if curl -fsS -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${PORT}/api/v1/sd/snmp" 2>/dev/null | grep -q '"targets"'; then
  if ! curl -fsS -m 2 "http://127.0.0.1:9116/metrics" >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi
    "${DC[@]}" up -d snmp-exporter >/dev/null 2>&1 || true
    sleep 2
  fi
fi

if ! curl -fsS -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${PORT}/api/v1/system/doctor" >/tmp/forgesre-doctor.json 2>/dev/null; then
  echo
  echo "Could not fetch /api/v1/system/doctor"
  echo "Why: Core answered /api/v1/health, so the webhook token is missing or wrong."
  echo "Test: curl -v http://127.0.0.1:${PORT}/api/v1/health"
  echo "Fix: ./forgesre secrets-check"
  exit 1
fi

# Read-only host security summary (SSH, firewall, ports, file modes, updates); also refreshes
# the report Settings › Security shows. Never fails doctor: it is one more row.
SECURITY_LINE="$(PYTHONPATH="$ROOT/backend:$ROOT/agents" timeout 60 python3 -m app.cli_ops "$PORT" security --summary 2>/dev/null | tail -n 1 || true)"
export SECURITY_LINE

python3 - <<'PY'
import json
import os
from pathlib import Path
data = json.loads(Path("/tmp/forgesre-doctor.json").read_text())
# Statuses that print a check mark; anything else prints a cross with why / test / fix.
ok_status = {"ok", "disabled", "paused", "warn", "warning", "starting"}
# One row per doctor component (Core API, Prometheus, snmp, NetBox, …), marks in one column:
# ✓ ok · – off (not set up) · ! warning, with the reason · ✗ problem, with why / test / fix.
rows = data.get("components", {})
width = max([20] + [len(str(i.get("label") or n)) for n, i in rows.items()])
for name, item in rows.items():
    status = item.get("status")
    label = str(item.get("label") or name)
    extra = ""
    if status == "disabled":
        mark = "–"
        extra = "  off"
    elif status in {"warn", "warning", "starting"}:
        mark = "!"
        extra = f"  {item.get('why', status)}"
    elif status in ok_status:
        mark = "✓"
    else:
        mark = "✗"
    if mark == "✗":
        extra = f"  {item.get('why','')}"
        if item.get("test"):
            extra += f"\n    Test: {item['test']}"
        if item.get("fix"):
            extra += f"\n    Fix:  {item['fix']}"
    print(f"  {label:<{width}}  {mark}{extra}")
if os.environ.get("SECURITY_LINE"):
    print(f"  {'Security':<{width}}  {os.environ['SECURITY_LINE']}")
print()
print(f"Overall:\n  {data.get('overall')}")
raise SystemExit(0 if data.get("overall") == "HEALTHY" else 1)
PY
