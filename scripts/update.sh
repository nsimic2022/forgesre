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

# snmp-exporter is a default service (not a Compose profile). :9116 connection
# refused means the container is not listening — start it with the stack.
if [[ "${1:-}" == "--offline" ]]; then
  "${DC[@]}" up -d --build --pull never
else
  "${DC[@]}" pull || true
  "${DC[@]}" up -d --build
fi
"${DC[@]}" up -d snmp-exporter
echo "Waiting for health..."
sleep 5
"$ROOT/scripts/doctor.sh" || echo "Doctor still reports warnings (see above)."
echo "Update finished."
echo "SNMP: ./forgesre snmp"
