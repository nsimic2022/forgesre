#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./install.sh first."
  exit 1
fi

# Same docker rights for the Postgres dump as compose pull/up. Probe the daemon
# (`docker info`), not `docker compose version` (that works without docker.sock).
if docker info >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi
if [[ "${DC[0]}" == "sudo" ]]; then
  sudo -v || true
  export FORGESRE_COMPOSE="sudo -n docker compose"
else
  export FORGESRE_COMPOSE="docker compose"
fi

echo "Checking status..."
"$ROOT/scripts/doctor.sh" || echo "Continuing after doctor warnings."
echo "Creating backup..."
if "$ROOT/scripts/backup.sh"; then
  echo "Backup written under data/backups/."
else
  echo "Backup failed (see above). Continuing with render-monitoring and compose up."
  echo "Run ./forgesre backup later if you need an archive from before this update."
fi
echo "Rendering Prometheus / Alertmanager / snmp_exporter config..."
"$ROOT/scripts/render-monitoring.sh"
echo "Ensuring bundled NetBox secrets and data dirs..."
"$ROOT/scripts/ensure-netbox-secrets.sh"

# snmp-exporter and NetBox are default services (not Compose profiles).
if [[ "${1:-}" == "--offline" ]]; then
  "${DC[@]}" up -d --build --pull never
else
  "${DC[@]}" pull || true
  "${DC[@]}" up -d --build
fi
"${DC[@]}" up -d snmp-exporter netbox-redis netbox
echo "Waiting for health..."
sleep 5
NB_PORT=8001
if [[ -f .env ]]; then
  NB_PORT="$(awk -F= '/^NETBOX_PORT=/ {print $2}' .env | tail -1 | tr -d '"' || true)"
  NB_PORT="${NB_PORT:-8001}"
fi
echo "Waiting for NetBox on :${NB_PORT} (first boot can take several minutes)..."
nb_ok=0
for _i in $(seq 1 45); do
  if curl -fsS -m 2 "http://127.0.0.1:${NB_PORT}/login/" >/dev/null 2>&1; then
    echo "NetBox is up."
    nb_ok=1
    break
  fi
  sleep 4
done
if [[ "$nb_ok" -ne 1 ]]; then
  echo "NetBox is still applying migrations or starting."
  echo "Doctor will stay yellow until http://127.0.0.1:${NB_PORT}/login/ answers."
  echo "Logs: docker compose logs --tail=80 netbox"
fi
"$ROOT/scripts/doctor.sh" || echo "Doctor still reports warnings (see above)."
echo "Update finished."
echo "SNMP:   ./forgesre snmp"
echo "NetBox: http://127.0.0.1:${NB_PORT}  (admin / secrets/secrets.env NETBOX_SUPERUSER_*)"
