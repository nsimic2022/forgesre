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
if [[ -f secrets/secrets.env ]]; then
  # shellcheck disable=SC1091
  source secrets/secrets.env
fi
set +a

if [[ -z "${DATABASE_URL:-}" && -n "${POSTGRES_PASSWORD:-}" ]]; then
  export DATABASE_URL="postgresql+psycopg2://forgesre:${POSTGRES_PASSWORD}@127.0.0.1:5432/forgesre"
fi

# Host Python has no sqlalchemy. Restore applies db.json via docker compose
# exec postgres when sqlalchemy is missing. Same docker rights as update.
if [[ -z "${FORGESRE_COMPOSE:-}" ]]; then
  if docker info >/dev/null 2>&1; then
    export FORGESRE_COMPOSE="docker compose"
  else
    sudo -v >/dev/null 2>&1 || true
    export FORGESRE_COMPOSE="sudo -n docker compose"
  fi
fi
export PYTHONPATH="${ROOT}/backend${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m app.backup restore "$@"
