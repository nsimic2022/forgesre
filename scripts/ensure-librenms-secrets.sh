#!/usr/bin/env bash
# Fill missing LibreNMS DB / Redis / APP_KEY values. Never rotates a valid key.
# Quotes base64 APP_KEY and repairs orphan dotenv lines (Compose + Laravel phpdotenv).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DATA_DIR="${FORGESRE_DATA:-./data}"
mkdir -p secrets "${DATA_DIR}/librenms/mysql" "${DATA_DIR}/librenms/redis" "${DATA_DIR}/librenms/data"
chmod 700 secrets 2>/dev/null || true
touch secrets/secrets.env
chmod 600 secrets/secrets.env 2>/dev/null || true

export FORGESRE_ROOT="$ROOT"
export FORGESRE_DATA="$DATA_DIR"
python3 "$ROOT/scripts/librenms_dotenv.py"
chmod 600 secrets/secrets.env "${DATA_DIR}/librenms/data/.env" 2>/dev/null || true
