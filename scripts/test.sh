#!/usr/bin/env bash
# ./forgesre test: live appliance test. Writes a detailed Markdown + JSON report
# to data/reports/ (logic in appliance_test.py).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python3 "$ROOT/scripts/appliance_test.py" "$@"
