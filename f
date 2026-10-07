#!/usr/bin/env bash
# Short alias for ./forgesre: ./f <command> runs the same scripts/forgesre.
set -euo pipefail
# Clone directory (where this file lives); commands expect to run from here.
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
exec "$ROOT/scripts/forgesre" "$@"
