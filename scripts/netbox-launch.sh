#!/bin/bash
# Granian bind for host-network NetBox. Core already owns :8080.
set -euo pipefail
PORT="${NETBOX_HTTP_PORT:-8001}"
HOST="${NETBOX_BIND_HOST:-0.0.0.0}"
# shellcheck disable=SC1091
source /opt/netbox/venv/bin/activate
exec granian \
  --host "${HOST}" \
  --port "${PORT}" \
  --interface "wsgi" \
  --no-ws \
  --workers "${GRANIAN_WORKERS:-2}" \
  --respawn-failed-workers \
  --backpressure "${GRANIAN_BACKPRESSURE:-${GRANIAN_WORKERS:-2}}" \
  --loop "uvloop" \
  --log \
  --log-level "info" \
  --access-log \
  --working-dir "/opt/netbox/netbox/" \
  --static-path-route "/static" \
  --static-path-mount "/opt/netbox/netbox/static/" \
  --static-path-dir-to-file index.html \
  --pid-file "/tmp/granian.pid" \
  "netbox.granian:application"
