#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT=8080
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  PORT="$(awk -F= '/FORGESRE_HTTP_PORT/ {print $2}' .env | tail -1 | tr -d '"' || true)"
  PORT="${PORT:-8080}"
fi

echo "ForgeSRE Health"
echo

ok() { printf "  %-16s ✓\n" "$1"; }
bad() { printf "  %-16s ✗  %s\n" "$1" "$2"; }

FAIL=0
if curl -fsS "http://127.0.0.1:${PORT}/api/v1/health" >/dev/null 2>&1; then
  ok "Core"
else
  bad "Core" "UI/API not reachable on port ${PORT}"
  FAIL=1
fi

TOKEN=""
if [[ -f secrets/secrets.env ]]; then
  # shellcheck disable=SC1091
  source secrets/secrets.env
  TOKEN="${ALERTMANAGER_WEBHOOK_TOKEN:-}"
fi

if ! curl -fsS -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${PORT}/api/v1/system/doctor" >/tmp/forgesre-doctor.json 2>/dev/null; then
  echo
  echo "Could not fetch /api/v1/system/doctor"
  echo "Why: Core is down, the port is wrong, or the webhook token is missing."
  echo "Test: curl -v http://127.0.0.1:${PORT}/api/v1/health"
  echo "Fix: docker compose logs core   and   ./forgesre secrets-check"
  exit 1
fi

python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path("/tmp/forgesre-doctor.json").read_text())
for name, item in data.get("components", {}).items():
    status = item.get("status")
    mark = "✓" if status in {"ok", "disabled"} else "✗"
    extra = ""
    if status not in {"ok", "disabled"}:
        extra = f"  {item.get('why','')}"
        if item.get("test"):
            extra += f"\n    Test: {item['test']}"
        if item.get("fix"):
            extra += f"\n    Fix:  {item['fix']}"
    print(f"  {name:<16} {mark}{extra}")
print()
print(f"Overall:\n  {data.get('overall')}")
raise SystemExit(0 if data.get("overall") == "HEALTHY" else 1)
PY
