#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./install.sh first."
  exit 1
fi

echo "Checking status..."
"$ROOT/scripts/doctor.sh" || echo "Continuing after doctor warnings."
echo "Creating backup..."
"$ROOT/scripts/backup.sh"

if docker compose version >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi
if [[ "${1:-}" == "--offline" ]]; then
  "${DC[@]}" up -d --build --pull never
else
  "${DC[@]}" pull || true
  "${DC[@]}" up -d --build
fi
echo "Waiting for health..."
sleep 5
"$ROOT/scripts/doctor.sh"
echo "Update finished."
