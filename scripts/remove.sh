#!/usr/bin/env bash
# ./forgesre remove backup: delete one backup_* folder after a typed yes or --yes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./install.sh first."
  exit 1
fi

# Export .env and secrets so app.backup resolves FORGESRE_DATA the same way as backup.
# shellcheck disable=SC1091
set -a
source .env
if [[ -f secrets/secrets.env ]]; then
  # shellcheck disable=SC1091
  source secrets/secrets.env
fi
set +a

# Host Python has no sqlalchemy. remove only deletes one folder under
# data/backups/ (tar + MANIFEST) or one legacy root tar — never data/.
export PYTHONPATH="${ROOT}/backend${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m app.backup remove "$@"
