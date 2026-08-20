#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./install.sh first."
  exit 1
fi
# shellcheck disable=SC1091
set -a
source .env
set +a
# shellcheck disable=SC1091
source secrets/secrets.env

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${FORGESRE_DATA}/backups/forgesre-${STAMP}"
mkdir -p "$DEST"

echo "Backing up PostgreSQL and configuration to $DEST"
if docker info >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi
"${DC[@]}" exec -T postgres pg_dump -U forgesre forgesre > "$DEST/forgesre.sql"
cp -a config/forgesre.yml "$DEST/"
cp -a .env "$DEST/"
cp -a secrets/secrets.env "$DEST/secrets.env"
chmod 600 "$DEST/secrets.env"
tar -C "${FORGESRE_DATA}/backups" -czf "${DEST}.tar.gz" "forgesre-${STAMP}"
echo "Wrote ${DEST}.tar.gz"
