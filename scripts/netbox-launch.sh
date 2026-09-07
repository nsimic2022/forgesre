#!/bin/bash
# Granian bind for host-network NetBox. Core already owns :8080.
#
# docker-entrypoint creates a superuser only on first insert. netbox-docker
# 5.0.2 / NetBox v4.6 will not mint SUPERUSER_API_TOKEN unless SUPERUSER_API_KEY
# is also set (v2 tokens). Later starts print "already exists" and skip the
# token, so Core's NETBOX_API_TOKEN 403s /api/dcim/devices/.
#
# NetBox v4.6 Token model: default version is v2. `key` is a 12-char public id;
# the secret is HMAC'd with API_TOKEN_PEPPERS and never stored. Creating with
# key=<40-char NETBOX_API_TOKEN> fails the version check constraint (or stores a
# hash the REST API does not accept as Authorization: Token …).
#
# Upsert a legacy v1 token on every start: plaintext = NETBOX_API_TOKEN (40 hex
# chars), assigned to the superuser, write_enabled=False. Core already sends
# Authorization: Token <NETBOX_API_TOKEN>. Do not copy a second token from the
# NetBox UI. Does not touch database forgesre.
#
# UI token create (v2) still needs API_TOKEN_PEPPERS (≥50 chars). Compose sets
# API_TOKEN_PEPPER_1 from NETBOX_API_TOKEN_PEPPER in secrets/secrets.env.
set -euo pipefail
PORT="${NETBOX_HTTP_PORT:-8001}"
HOST="${NETBOX_BIND_HOST:-0.0.0.0}"
# shellcheck disable=SC1091
source /opt/netbox/venv/bin/activate

# Last KEY= in a dotenv file. Prints the value to stdout for capture; caller must not log it.
_dotenv_value() {
  local file="$1" key="$2" found="" line name value
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    name="${line%%=*}"
    value="${line#*=}"
    name="${name%"${name##*[![:space:]]}"}"
    name="${name#"${name%%[![:space:]]*}"}"
    if [[ "$name" == "$key" ]]; then
      value="${value%\"}"
      value="${value#\"}"
      value="${value%\'}"
      value="${value#\'}"
      found="$value"
    fi
  done < "$file"
  printf '%s' "$found"
}

ensure_core_api_token() {
  local token="${NETBOX_API_TOKEN:-${SUPERUSER_API_TOKEN:-}}"
  if [[ -z "${token}" && -f /run/secrets/forgesre-secrets.env ]]; then
    token="$(_dotenv_value /run/secrets/forgesre-secrets.env NETBOX_API_TOKEN)"
    if [[ -z "${token}" ]]; then
      token="$(_dotenv_value /run/secrets/forgesre-secrets.env SUPERUSER_API_TOKEN)"
    fi
  fi
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
from users.choices import TokenVersionChoices
from users.models import Token

token_key = (os.environ.get("FORGESRE_NB_TOKEN") or "").strip()
username = (os.environ.get("FORGESRE_NB_USER") or "admin").strip()
email = (os.environ.get("FORGESRE_NB_EMAIL") or "admin@forgesre.local").strip()
password = os.environ.get("FORGESRE_NB_PASSWORD") or ""
if not token_key:
    raise SystemExit(0)
if len(token_key) != 40:
    print(
        "forgesre: NETBOX_API_TOKEN length",
        len(token_key),
        "(v1 plaintext must be 40 characters; openssl rand -hex 20)",
    )
    raise SystemExit(1)

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

# Previous upsert wrote the 40-char secret into v2 `key` (max 12). Delete it.
stale = Token.objects.filter(key=token_key)
if stale.exists():
    n = stale.count()
    stale.delete()
    print("forgesre: removed", n, "invalid token(s) that stored the secret in key")

row = Token.objects.filter(version=TokenVersionChoices.V1, plaintext=token_key).first()
if row is None:
    row = Token(
        user=user,
        version=TokenVersionChoices.V1,
        write_enabled=False,
        enabled=True,
        description="ForgeSRE Core read-sync",
        key=None,
        pepper_id=None,
        hmac_digest=None,
        token=token_key,
    )
    row.save()
    print("forgesre: created read-only v1 API token for Core sync")
else:
    changed = False
    if row.user_id != user.id:
        row.user = user
        changed = True
    if getattr(row, "write_enabled", True):
        row.write_enabled = False
        changed = True
    if not getattr(row, "enabled", True):
        row.enabled = True
        changed = True
    if changed:
        row.save()
        print("forgesre: updated v1 API token to read-only Core sync")
    else:
        print("forgesre: API token already present (read-only v1)")

ready = Token.objects.filter(
    version=TokenVersionChoices.V1, plaintext=token_key, enabled=True
).exists()
if not ready:
    print("forgesre: v1 token upsert did not persist")
    raise SystemExit(1)
print("forgesre: v1 token ready for GET /api/dcim/devices/")

# Superuser already lists devices. Attach ObjectPermission too so a later
# non-superuser assignment still GETs /api/dcim/devices/.
try:
    from core.models import ObjectType
    from users.models import ObjectPermission

    ot = ObjectType.objects.filter(app_label="dcim", model="device").first()
    if ot is not None:
        perm = ObjectPermission.objects.filter(name="ForgeSRE Core read devices").first()
        if perm is None:
            perm = ObjectPermission(name="ForgeSRE Core read devices", enabled=True, actions=["view"])
            perm.save()
        elif list(perm.actions) != ["view"] or not perm.enabled:
            perm.actions = ["view"]
            perm.enabled = True
            perm.save()
        perm.users.add(user)
        perm.object_types.add(ot)
except Exception as exc:
    print("forgesre: dcim.view_device object permission skipped:", exc)
PY
  ) || {
    echo "forgesre: could not upsert NetBox API token (UI still starts)" >&2
  }
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
