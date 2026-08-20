#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

INCLUDE_SECRETS=1
if [[ "${1:-}" == "--no-secrets" ]]; then
  INCLUDE_SECRETS=0
fi

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

umask 077
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${FORGESRE_DATA}/backups/forgesre-${STAMP}"
mkdir -p "$DEST"
chmod 700 "${FORGESRE_DATA}/backups" 2>/dev/null || true

echo "Backing up PostgreSQL and configuration to $DEST"
if docker info >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi
"${DC[@]}" exec -T postgres pg_dump -U forgesre forgesre > "$DEST/forgesre.sql"
cp -a config/forgesre.yml "$DEST/"
cp -a .env "$DEST/"
if [[ "$INCLUDE_SECRETS" -eq 1 ]]; then
  cp -a secrets/secrets.env "$DEST/secrets.env"
  chmod 600 "$DEST/secrets.env"
else
  echo "secrets omitted (--no-secrets). Restore will need secrets/secrets.env from elsewhere."
fi
tar -C "${FORGESRE_DATA}/backups" -czf "${DEST}.tar.gz" "forgesre-${STAMP}"
chmod 600 "${DEST}.tar.gz"
rm -rf "$DEST"
echo "Wrote ${DEST}.tar.gz (mode 600)"
