#!/usr/bin/env bash
# ./forgesre update: doctor → backup → render-monitoring → compose pull/up → snmp-auths → doctor.
# Use after git pull on a live box. Never runs install.sh or rewrites passwords.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./install.sh first."
  exit 1
fi

# Same docker rights for the Postgres dump as compose pull/up. Probe the daemon
# (`docker info`), not `docker compose version` (that works without docker.sock).
if docker info >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi

# NetBox and LibreNMS are optional Compose profiles (netbox, librenms). An appliance installed
# before that keeps both: add them to COMPOSE_PROFILES once, before anything is pulled or started.
ENV_FILE="$ROOT/.env"
# shellcheck source=scripts/lib-profiles.sh
source "$ROOT/scripts/lib-profiles.sh"
if [[ -z "$(env_get FORGESRE_OPTIONAL_PROFILES)" ]]; then
  profile_add netbox
  profile_add librenms
  env_set FORGESRE_OPTIONAL_PROFILES 1
  echo "NetBox and LibreNMS are optional now; this appliance keeps both (./forgesre optional to change)."
fi
NETBOX_ON=0
LIBRENMS_ON=0
profile_on netbox && NETBOX_ON=1
profile_on librenms && LIBRENMS_ON=1
if [[ "${DC[0]}" == "sudo" ]]; then
  sudo -v || true
  export FORGESRE_COMPOSE="sudo -n docker compose"
else
  export FORGESRE_COMPOSE="docker compose"
fi

echo "Checking status..."
"$ROOT/scripts/doctor.sh" || echo "Continuing after doctor warnings."
echo "Creating backup..."
if "$ROOT/scripts/backup.sh"; then
  echo "Backup written under data/backups/."
else
  echo "Backup failed (see above). Continuing with render-monitoring and compose up."
  echo "Run ./forgesre backup later if you need an archive from before this update."
fi
echo "Rendering Prometheus / Alertmanager / snmp_exporter / Alloy / Loki config..."
# Secrets added after the first install: create them once so existing appliances get them too.
SECRETS_FILE="$ROOT/secrets/secrets.env"
for key in CHANGES_WEBHOOK_TOKEN MCP_TOKEN; do
  if [[ -f "$SECRETS_FILE" ]] && ! grep -q "^${key}=.\+" "$SECRETS_FILE"; then
    if { sed -i "/^${key}=/d" "$SECRETS_FILE" &&
         printf '%s=%s\n' "$key" "$(openssl rand -hex 24)" >> "$SECRETS_FILE"; } 2>/dev/null; then
      echo "Created ${key} in secrets/secrets.env."
    else
      echo "Could not write secrets/secrets.env; set it later: ./forgesre settings set-secret ${key}"
    fi
  fi
done

"$ROOT/scripts/render-monitoring.sh"
if [[ "$NETBOX_ON" -eq 1 ]]; then
  echo "Ensuring bundled NetBox secrets and data dirs..."
  "$ROOT/scripts/ensure-netbox-secrets.sh"
fi
if [[ "$LIBRENMS_ON" -eq 1 ]]; then
  echo "Ensuring bundled LibreNMS secrets and data dirs..."
  "$ROOT/scripts/ensure-librenms-secrets.sh"
fi

# snmp-exporter and blackbox-exporter always run; NetBox and LibreNMS only with their profile.
# OFFLINE=1 (--offline) skips docker compose pull.
OFFLINE=0
if [[ "${1:-}" == "--offline" ]]; then
  OFFLINE=1
fi

# One hash over every file baked into the Core image; unchanged hash = skip --build.
core_inputs_hash() {
  (
    cd "$ROOT"
    find backend/Dockerfile backend/requirements.txt backend/app agents frontend web postgres \
      \( -path web/node_modules -o -path web/dist \) -prune -o -type f -print 2>/dev/null \
      | sort \
      | xargs -r sha256sum
  ) | sha256sum | awk '{print $1}'
}

# Hash of every Compose `image:` pin. Unchanged pins = skip compose pull
# (llama.cpp :server used to move daily and re-download ~300MB).
compose_images_hash() {
  awk '/^[[:space:]]+image:/{print}' "$ROOT/docker-compose.yml" | sha256sum | awk '{print $1}'
}

# Hash of the NetBox launch / token helper inputs; a change forces NetBox + Core recreate.
netbox_launch_hash() {
  (
    cd "$ROOT"
    sha256sum scripts/netbox-launch.sh scripts/netbox-upsert-token.py docker-compose.yml scripts/ensure-netbox-secrets.sh
  ) | sha256sum | awk '{print $1}'
}

# STAMP = Core input hash from the last build; cur = hash now; need_build = 1 when they differ.
STAMP="$ROOT/data/.core-image.stamp"
mkdir -p "$ROOT/data"
cur="$(core_inputs_hash)"
need_build=1
if [[ -f "$STAMP" ]] && [[ "$(cat "$STAMP" 2>/dev/null || true)" == "$cur" ]]; then
  need_build=0
  echo "Core image inputs unchanged (Dockerfile/backend/agents/frontend/web); skipping --build."
fi

IMG_STAMP="$ROOT/data/.compose-images.stamp"
img_cur="$(compose_images_hash)"
need_pull=1
if [[ "$OFFLINE" -eq 1 ]]; then
  need_pull=0
  echo "Offline: skipping compose pull. Lab use: ./forgesre update --offline"
elif [[ -f "$IMG_STAMP" ]] && [[ "$(cat "$IMG_STAMP" 2>/dev/null || true)" == "$img_cur" ]]; then
  need_pull=0
  echo "Compose image pins unchanged; skipping compose pull."
fi
if [[ "$need_pull" -eq 1 ]]; then
  if "${DC[@]}" pull; then
    printf '%s\n' "$img_cur" > "$IMG_STAMP"
  else
    echo "compose pull failed; stack continues on local images. Next update will retry the pull."
  fi
fi

if [[ "$OFFLINE" -eq 1 && -f "$ROOT/data/.offline-bundle" ]]; then
  # Core image came from ./load-bundle.sh; building it would need pip and the internet.
  need_build=0
fi
if [[ "$need_build" -eq 1 ]]; then
  "${DC[@]}" up -d --build --pull never
  printf '%s\n' "$cur" > "$STAMP"
else
  if ! "${DC[@]}" up -d --pull never; then
    echo "compose up failed without --build; rebuilding Core."
    "${DC[@]}" up -d --build --pull never
    printf '%s\n' "$cur" > "$STAMP"
  fi
fi
# Named services start even when their profile is off, so the list follows the profiles.
# With both on this is: up -d snmp-exporter blackbox-exporter netbox-redis netbox librenms-db librenms-redis librenms librenms-dispatcher
extras=(snmp-exporter blackbox-exporter)
[[ "$NETBOX_ON" -eq 1 ]] && extras+=(netbox-redis netbox)
[[ "$LIBRENMS_ON" -eq 1 ]] && extras+=(librenms-db librenms-redis librenms librenms-dispatcher)
if ! "${DC[@]}" up -d "${extras[@]}"; then
  echo "SNMP / NetBox / LibreNMS extras failed to start. MariaDB logs (librenms-db / librenms-db-1):"
  [[ "$LIBRENMS_ON" -eq 1 ]] && "${DC[@]}" logs --tail=80 librenms-db || true
  echo "LibreNMS logs:"
  [[ "$LIBRENMS_ON" -eq 1 ]] && "${DC[@]}" logs --tail=80 librenms || true
  echo "If pull/start of mariadb:11.4.13 failed: docker compose pull librenms-db && docker compose logs --tail=80 librenms-db"
  echo "If lnms must not run as root: PUID/PGID must be 1000 (not 0), then ./forgesre update"
fi
if [[ "$NETBOX_ON" -eq 1 ]]; then
  # Same stamp idea as Core: NB_STAMP = last NetBox launch hash, nb_cur = now, need_nb = changed.
  NB_STAMP="$ROOT/data/.netbox-launch.stamp"
  nb_cur="$(netbox_launch_hash)"
  need_nb=1
  if [[ -f "$NB_STAMP" ]] && [[ "$(cat "$NB_STAMP" 2>/dev/null || true)" == "$nb_cur" ]]; then
    need_nb=0
  fi
  if [[ "$need_nb" -eq 1 ]]; then
    echo "NetBox launch script or compose token env changed; recreating netbox so the token helper runs."
    "${DC[@]}" up -d --no-deps --force-recreate netbox
    "${DC[@]}" up -d --no-deps --force-recreate core
    printf '%s\n' "$nb_cur" > "$NB_STAMP"
  else
    "${DC[@]}" up -d --no-deps --force-recreate netbox
  fi
fi
echo "Waiting for health..."
# Core and NetBox HTTP ports from .env (defaults 8080 / 8001).
HTTP_PORT=8080
NB_PORT=8001
if [[ -f .env ]]; then
  HTTP_PORT="$(awk -F= '/^[[:space:]]*FORGESRE_HTTP_PORT=/ {v=$2} END {print v}' .env | tr -d '"' | tr -d "'" | tr -d '\r' | awk '{print $1}' || true)"
  HTTP_PORT="${HTTP_PORT:-8080}"
  NB_PORT="$(awk -F= '/^NETBOX_PORT=/ {print $2}' .env | tail -1 | tr -d '"' || true)"
  NB_PORT="${NB_PORT:-8001}"
fi
echo "Waiting for Core on :${HTTP_PORT} (GET /api/v1/health, no token)..."
core_ok=0
# Poll Core liveness up to 30 × 2s.
for _i in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:${HTTP_PORT}/api/v1/health" >/dev/null 2>&1; then
    echo "Core is up."
    core_ok=1
    break
  fi
  sleep 2
done
if [[ "$core_ok" -eq 1 ]]; then
  "$ROOT/scripts/render-snmp-auths.sh" || echo "Per-asset SNMP auths not refreshed. Retry: ./forgesre snmp-auths"
fi
if [[ "$core_ok" -ne 1 ]]; then
  echo "Core is not answering GET /api/v1/health on :${HTTP_PORT}."
  echo "Logs: docker compose logs core --tail=80"
fi
if [[ "$NETBOX_ON" -eq 1 ]]; then
  echo "Waiting for NetBox on :${NB_PORT} (first boot can take several minutes)..."
  nb_ok=0
  # Poll NetBox /login/ up to 45 × 4s (first boot runs migrations).
  for _i in $(seq 1 45); do
    if curl -fsS -m 2 "http://127.0.0.1:${NB_PORT}/login/" >/dev/null 2>&1; then
      echo "NetBox is up."
      nb_ok=1
      break
    fi
    sleep 4
  done
  if [[ "$nb_ok" -ne 1 ]]; then
    echo "NetBox is still applying migrations or starting."
    echo "Doctor will stay yellow until http://127.0.0.1:${NB_PORT}/login/ answers."
    echo "Logs: docker compose logs --tail=80 netbox"
  fi
fi
if [[ "$LIBRENMS_ON" -eq 1 ]]; then
  echo "Waiting for LibreNMS on :8000 (first boot can take several minutes)..."
  ln_ok=0
  for _i in $(seq 1 45); do
    if curl -fsS -m 2 "http://127.0.0.1:8000/login" >/dev/null 2>&1; then
      echo "LibreNMS is up."
      ln_ok=1
      break
    fi
    sleep 4
  done
  if [[ "$ln_ok" -ne 1 ]]; then
    echo "LibreNMS is still applying migrations or starting."
    echo "Doctor will stay yellow until http://127.0.0.1:8000/login answers."
    echo "Logs: docker compose logs --tail=80 librenms"
  fi
  wrote_lnms=0
  if lnms_out="$("$ROOT/scripts/librenms-upsert-token.sh")"; then
    echo "$lnms_out"
    if [[ "$lnms_out" == *"token written"* ]]; then
      wrote_lnms=1
    fi
  else
    echo "LibreNMS token helper failed (see above). Create LIBRENMS_API_TOKEN in the UI."
  fi
  if [[ "$wrote_lnms" -eq 1 ]]; then
    echo "Recreating core so it reads the new LIBRENMS_API_TOKEN."
    "${DC[@]}" up -d --no-deps --force-recreate core || true
  fi
fi
# 2.0: ForgeAI document search needs the small embedding model next to the LLM (profile ai).
if [[ "$OFFLINE" -eq 0 ]] && grep -Eq '^COMPOSE_PROFILES=.*\bai\b' "$ROOT/.env" 2>/dev/null \
   && ! ls "${FORGESRE_DATA:-$ROOT/data}"/models/embed/*.gguf >/dev/null 2>&1; then
  echo "Fetching the embedding model for ForgeAI document search (~150 MB)..."
  "$ROOT/scripts/fetch-llm.sh" --download-only || echo "Embedding model download failed; retry later with ./forgesre fetch-llm"
  "${DC[@]}" up -d embed || true
fi
# 2.0: ForgeSRE no longer scans networks. Ranges once kept in discovery.cidrs go to LibreNMS.
old_cidrs="$(awk '/^discovery:/{d=1;next} /^[^ #]/{d=0} d && /^  cidrs:/{c=1;sub(/^  cidrs:[ ]*/,"");print;next} d && c && /^ *- /{sub(/^ *- */,"");print;next} {c=0}' \
  "$ROOT/config/forgesre.yml" 2>/dev/null | tr -d '[]"'"'"' ' | tr ',\n' '  ')"
if [[ -n "${old_cidrs// /}" ]]; then
  echo "discovery.cidrs (${old_cidrs}) is no longer used: LibreNMS discovers devices now."
  if "$ROOT/forgesre" discovery nets ${old_cidrs}; then
    sed -i '/^discovery:/,/^[^ #]/{/^  cidrs:/,/^  [a-z]/{/^  cidrs:/d;/^ *- /d}}' "$ROOT/config/forgesre.yml" || true
  else
    echo "Could not hand them to LibreNMS yet: ./forgesre discovery nets ${old_cidrs}"
  fi
fi
if grep -Eq '^COMPOSE_PROFILES=.*\bhttps\b' "$ROOT/.env" 2>/dev/null; then
  # the proxy has no build; a changed Caddyfile only needs a restart
  "${DC[@]}" up -d proxy && "${DC[@]}" restart proxy >/dev/null 2>&1 || echo "HTTPS proxy did not start: ./forgesre https status ; ./forgesre logs proxy"
else
  echo "HTTPS is off. Turn it on with: ./forgesre https on   (self-signed, or put your certificate in data/tls)"
fi
if [[ ! -f /etc/systemd/system/forgesre-backup.timer ]]; then
  echo "No nightly backup yet. Turn it on with: ./forgesre backup schedule on   (02:30, verify, keep 14)"
fi
"$ROOT/scripts/doctor.sh" || echo "Doctor still reports warnings (see above)."
echo "Update finished."
echo "SNMP:     ./forgesre snmp"
if [[ "$NETBOX_ON" -eq 1 ]]; then
  echo "NetBox:   http://127.0.0.1:${NB_PORT}  (admin / secrets/secrets.env NETBOX_SUPERUSER_*)"
fi
if [[ "$LIBRENMS_ON" -eq 1 ]]; then
  echo "LibreNMS: http://127.0.0.1:8000  (it discovers devices: ./forgesre discovery nets CIDR, then ./forgesre discovery)"
fi
