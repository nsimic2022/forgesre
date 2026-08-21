#!/usr/bin/env bash
# Live appliance test. Writes a detailed Markdown + JSON report.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python3 "$ROOT/scripts/appliance_test.py" "$@"
