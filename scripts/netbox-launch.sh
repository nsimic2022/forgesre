#!/bin/bash
# Granian bind for host-network NetBox. Core already owns :8080.
#
# docker-entrypoint creates SUPERUSER_API_TOKEN only when the superuser is
# first inserted. Later starts print "Superuser Already Exists" and skip the
# token, so Core's NETBOX_API_TOKEN 403s /api/dcim/devices/. Upsert a
# read-only token on every start. Does not touch database forgesre.
set -euo pipefail
PORT="${NETBOX_HTTP_PORT:-8001}"
HOST="${NETBOX_BIND_HOST:-0.0.0.0}"
# shellcheck disable=SC1091
source /opt/netbox/venv/bin/activate

ensure_core_api_token() {
  local token="${NETBOX_API_TOKEN:-${SUPERUSER_API_TOKEN:-}}"
  if [[ -z "${token}" ]]; then
    echo "forgesre: NETBOX_API_TOKEN empty — Core sync will get HTTP 403" >&2
    return 0
  fi
  (
    cd /opt/netbox/netbox
    FORGESRE_NB_TOKEN="${token}" \
    FORGESRE_NB_USER="${SUPERUSER_NAME:-admin}" \
    FORGESRE_NB_EMAIL="${SUPERUSER_EMAIL:-admin@forgesre.local}" \
    FORGESRE_NB_PASSWORD="${SUPERUSER_PASSWORD:-}" \
    python /opt/netbox/netbox/manage.py shell --no-startup --no-imports --interface python <<'PY'
import os
from django.contrib.auth import get_user_model
from users.models import Token

token_key = (os.environ.get("FORGESRE_NB_TOKEN") or "").strip()
username = (os.environ.get("FORGESRE_NB_USER") or "admin").strip()
email = (os.environ.get("FORGESRE_NB_EMAIL") or "admin@forgesre.local").strip()
password = os.environ.get("FORGESRE_NB_PASSWORD") or ""
if not token_key:
    raise SystemExit(0)
User = get_user_model()
user = User.objects.filter(username=username).first()
if user is None:
    if not password:
        print("forgesre: no NetBox superuser and SUPERUSER_PASSWORD empty")
        raise SystemExit(0)
    user = User.objects.create_superuser(username, email, password)
    print("forgesre: created NetBox superuser", username)
elif not user.is_active or not user.is_staff or not user.is_superuser:
    user.is_active = True
    user.is_staff = True
    user.is_superuser = True
    user.save()
row = Token.objects.filter(key=token_key).first()
if row is None:
    Token.objects.create(user=user, key=token_key, write_enabled=False)
    print("forgesre: created read-only API token for Core sync")
else:
    changed = False
    if row.user_id != user.id:
        row.user = user
        changed = True
    if getattr(row, "write_enabled", True):
        row.write_enabled = False
        changed = True
    if changed:
        row.save()
        print("forgesre: updated API token to read-only Core sync")
    else:
        print("forgesre: API token already present (read-only)")
PY
  ) || echo "forgesre: could not upsert NetBox API token (UI still starts)" >&2
}

ensure_core_api_token

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
