#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ $# -eq 0 ]]; then
  echo "usage: ./forgesre restore ARCHIVE.tar.gz [--yes]"
  echo "Without --yes this only prints the plan and exits 1."
  echo "Stop Core first, then restore, then update:"
  echo "  docker compose stop core"
  echo "  ./forgesre restore data/backups/forgesre-YYYYMMDDTHHMMSSZ.tar.gz --yes"
  echo "  ./forgesre update"
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./install.sh first."
  exit 1
fi

# shellcheck disable=SC1091
set -a
source .env
if [[ -f secrets/secrets.env ]]; then
  # shellcheck disable=SC1091
  source secrets/secrets.env
fi
set +a

if [[ -z "${DATABASE_URL:-}" && -n "${POSTGRES_PASSWORD:-}" ]]; then
  export DATABASE_URL="postgresql+psycopg2://forgesre:${POSTGRES_PASSWORD}@127.0.0.1:5432/forgesre"
fi

export PYTHONPATH="${ROOT}/backend${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m app.backup restore "$@"
