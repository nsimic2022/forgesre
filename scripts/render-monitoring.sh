#!/usr/bin/env bash
# Render Prometheus, Alertmanager, and snmp_exporter config from templates.
# Safe on an existing VM: does not regenerate passwords. Do not run ./install.sh for this.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./install.sh once on a new VM, then use this command on updates."
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
if [[ -f secrets/secrets.env ]]; then
  # shellcheck disable=SC1091
  source secrets/secrets.env
fi
set +a

DATA_DIR="${FORGESRE_DATA:-./data}"
HTTP_PORT="${FORGESRE_HTTP_PORT:-8080}"
WEBHOOK="${ALERTMANAGER_WEBHOOK_TOKEN:-}"
COMMUNITY="${SNMP_COMMUNITY:-public}"
mkdir -p "$DATA_DIR/generated" secrets

ensure_kv() {
  local file="$1" key="$2" value="$3"
  if [[ ! -f "$file" ]]; then
    printf '%s=%s\n' "$key" "$value" > "$file"
    return 0
  fi
  if grep -q "^${key}=" "$file"; then
    return 0
  fi
  printf '%s=%s\n' "$key" "$value" >> "$file"
}

ensure_kv "$ROOT/secrets/secrets.env" SNMP_COMMUNITY "${COMMUNITY}"
chmod 600 "$ROOT/secrets/secrets.env" 2>/dev/null || true
ensure_kv "$ROOT/.env" SNMP_EXPORTER_CONFIG "${DATA_DIR}/generated/snmp.yml"
ensure_kv "$ROOT/.env" PROMETHEUS_CONFIG "${DATA_DIR}/generated/prometheus.yml"
ensure_kv "$ROOT/.env" ALERTMANAGER_CONFIG "${DATA_DIR}/generated/alertmanager.yml"
ensure_kv "$ROOT/.env" PROMETHEUS_ALERTS "${DATA_DIR}/generated/alerts.yml"
if grep -q '^FORGESRE_VERSION=' "$ROOT/.env"; then
  sed -i 's/^FORGESRE_VERSION=.*/FORGESRE_VERSION=0.6.0/' "$ROOT/.env"
else
  echo "FORGESRE_VERSION=0.6.0" >> "$ROOT/.env"
fi

if [[ -z "$WEBHOOK" ]]; then
  echo "ALERTMANAGER_WEBHOOK_TOKEN is empty. HTTP SD will 401 until secrets/secrets.env has the token."
fi

python3 - "$ROOT" "$DATA_DIR" "$HTTP_PORT" "$WEBHOOK" "$COMMUNITY" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
data = Path(sys.argv[2])
port = sys.argv[3]
token = sys.argv[4]
community = sys.argv[5].replace("\n", "").replace("\r", "")
out = data / "generated"
out.mkdir(parents=True, exist_ok=True)

def render(name: str, mapping: dict[str, str]) -> None:
    text = (root / "monitoring" / f"{name}.tpl").read_text()
    for key, value in mapping.items():
        text = text.replace(key, value)
    (out / name).write_text(text)

render(
    "prometheus.yml",
    {"__WEBHOOK_TOKEN__": token, "__CORE_PORT__": port},
)
render(
    "alertmanager.yml",
    {"__WEBHOOK_TOKEN__": token, "__CORE_PORT__": port},
)
render("snmp.yml", {"__SNMP_COMMUNITY__": community})

base = (root / "monitoring" / "alerts.yml").read_text()
local = root / "monitoring" / "alerts.local.yml"
extra = local.read_text() if local.exists() else ""
(out / "alerts.yml").write_text(base + ("\n" + extra if extra.strip() else ""))
print(f"Wrote {out}/prometheus.yml")
print(f"Wrote {out}/alertmanager.yml")
print(f"Wrote {out}/snmp.yml")
print(f"Wrote {out}/alerts.yml")
PY

echo "SNMP community is taken from SNMP_COMMUNITY in secrets/secrets.env (not printed)."
echo "Prometheus job forgesre-snmp scrapes snmp_exporter at 127.0.0.1:9116."
if curl -fsS -o /dev/null -X POST http://127.0.0.1:9090/-/reload 2>/dev/null; then
  echo "Prometheus reloaded."
else
  echo "Prometheus was not reloaded (not running yet, or lifecycle API off)."
  echo "Start/refresh the stack with: docker compose up -d"
fi
