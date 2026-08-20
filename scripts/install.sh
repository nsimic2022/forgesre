#!/usr/bin/env bash
# ForgeSRE installer. Host needs Docker, Compose, Bash, Git.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OFFLINE=0
NONINTERACTIVE=0
PROFILE="${FORGESRE_PROFILE:-standard}"
TIMEZONE="${FORGESRE_TIMEZONE:-Europe/Belgrade}"
DATA_DIR="${FORGESRE_DATA:-./data}"
HTTP_PORT="${FORGESRE_HTTP_PORT:-8080}"
BUNDLED_PROM="yes"
BUNDLED_GRAFANA="yes"
ENABLE_LOKI="yes"
ENABLE_AI="no"
ENABLE_DISCOVERY="yes"
DISCOVERY_CIDRS="${FORGESRE_DISCOVERY_CIDRS:-}"
ENABLE_NETBOX="no"
NETBOX_URL="${FORGESRE_NETBOX_URL:-}"
NETBOX_TOKEN="${NETBOX_API_TOKEN:-}"

usage() {
  cat <<'EOF'
Usage: ./install.sh [--offline] [--non-interactive] [options]

Options:
  --offline              Do not pull images
  --non-interactive      Use flags/defaults, no prompts
  --profile standard|full-ai   full-ai downloads ~9 GB GGUF (not stored in git)
  --timezone ZONE
  --data-dir PATH
  --port N
  --enable-ai yes|no     Download GGUF and start llama.cpp (yes). RCA works without it.
  --enable-discovery yes|no
  --discovery-cidrs CIDR[,CIDR]
  --netbox-url URL
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline) OFFLINE=1; shift ;;
    --non-interactive) NONINTERACTIVE=1; shift ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --port) HTTP_PORT="$2"; shift 2 ;;
    --enable-ai) ENABLE_AI="$2"; shift 2 ;;
    --enable-discovery) ENABLE_DISCOVERY="$2"; shift 2 ;;
    --discovery-cidrs) DISCOVERY_CIDRS="$2"; shift 2 ;;
    --netbox-url) NETBOX_URL="$2"; ENABLE_NETBOX="yes"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ "$PROFILE" == "3" || "$PROFILE" == "full-ai" || "$PROFILE" == "full_ai" ]]; then
  PROFILE="full-ai"
  ENABLE_AI="yes"
elif [[ "$PROFILE" == "2" || "$PROFILE" == "standard" ]]; then
  PROFILE="standard"
fi

dc() {
  if docker info >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v sudo >/dev/null; then
    sudo docker compose "$@"
  else
    echo "Docker daemon is not accessible." >&2
    exit 1
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

preflight() {
  echo
  echo "FORGESRE PREFLIGHT"
  local fail=0
  if need_cmd docker; then echo "Docker                ✓"; else echo "Docker                ✗"; fail=1; fi
  if docker compose version >/dev/null 2>&1 || sudo docker compose version >/dev/null 2>&1; then
    echo "Docker Compose        ✓"
  else
    echo "Docker Compose        ✗"; fail=1
  fi
  if docker info >/dev/null 2>&1 || sudo docker info >/dev/null 2>&1; then
    echo "Docker daemon         ✓"
  else
    echo "Docker daemon         ✗"; fail=1
  fi
  local cpus ram_gb disk_gb
  cpus="$(nproc)"
  ram_gb="$(awk '/MemTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)"
  disk_gb="$(df -BG --output=avail . | tail -1 | tr -dc 0-9)"
  if [[ "${cpus}" -ge 2 ]]; then echo "CPU                   ✓  (${cpus} cores)"; else echo "CPU                   ✗  need >= 2 cores"; fail=1; fi
  if [[ "${ram_gb}" -ge 4 ]]; then echo "Memory                ✓  (${ram_gb} GB)"; else echo "Memory                ✗  need >= 4 GB"; fail=1; fi
  local need_disk=10
  if [[ "$ENABLE_AI" == "yes" ]]; then need_disk=20; fi
  if [[ "${disk_gb}" -ge "$need_disk" ]]; then echo "Disk                  ✓  (${disk_gb} GB free)"; else echo "Disk                  ✗  need >= ${need_disk} GB"; fail=1; fi
  if command -v ss >/dev/null 2>&1 && ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE ":${HTTP_PORT}$"; then
    echo "Ports                 ✗  Port ${HTTP_PORT} is already in use."
    echo
    echo "ERROR"
    echo "Port ${HTTP_PORT} is already in use."
    echo "Run: ./doctor.sh"
    echo "or choose another port."
    fail=1
  else
    echo "Ports                 ✓  ${HTTP_PORT}"
  fi
  if [[ "$fail" -ne 0 ]]; then
    echo
    echo "Preflight failed."
    exit 1
  fi
  echo
  echo "Ready for installation."
}

explain() {
  local title="$1" why="$2" required="$3" skip="$4"
  echo
  echo "--- ${title} ---"
  echo "What: ${title}"
  echo "Why: ${why}"
  echo "Required: ${required}"
  echo "If skipped: ${skip}"
}

wizard() {
  echo "================================="
  echo "        ForgeSRE Installer"
  echo "================================="
  echo
  echo "Welcome."
  echo "This wizard will configure your ForgeSRE installation."
  echo
  echo "Installation profile:"
  echo "  1) Minimal   (not offered in V0.1)"
  echo "  2) Standard"
  echo "  3) Full AI"
  local choice
  read -r -p "Select [2-3]: " choice
  case "$choice" in
    3) PROFILE="full-ai"; ENABLE_AI="yes" ;;
    *) PROFILE="standard"; ENABLE_AI="no" ;;
  esac
  explain "Timezone" "Timestamps on incidents and logs." "yes" "Defaults to Europe/Belgrade."
  read -r -p "Timezone [${TIMEZONE}]: " ans || true
  TIMEZONE="${ans:-$TIMEZONE}"
  explain "Data directory" "Where Postgres, Prometheus, Loki and logs persist." "yes" "You must choose a writable path."
  read -r -p "Data directory [${DATA_DIR}]: " ans || true
  DATA_DIR="${ans:-$DATA_DIR}"
  explain "HTTP port" "ForgeSRE UI and API bind port on the host." "yes" "Change it if the port is busy."
  read -r -p "ForgeSRE HTTP port [${HTTP_PORT}]: " ans || true
  HTTP_PORT="${ans:-$HTTP_PORT}"
  explain "Prometheus" "Metrics engine. Bundled unless you already run Prometheus." "recommended" "Monitoring will be disabled (not supported in V0.1)."
  read -r -p "Use bundled Prometheus? [Y/n]: " ans || true
  [[ "${ans:-Y}" =~ ^[Nn] ]] && BUNDLED_PROM="no"
  explain "Grafana" "Deep dashboards. ForgeSRE UI stays a summary." "no" "Open Grafana link is hidden."
  read -r -p "Use bundled Grafana? [Y/n]: " ans || true
  [[ "${ans:-Y}" =~ ^[Nn] ]] && BUNDLED_GRAFANA="no"
  explain "Loki" "Local logs used as incident evidence." "no" "Incidents still work; log evidence degrades."
  read -r -p "Enable Loki? [Y/n]: " ans || true
  [[ "${ans:-Y}" =~ ^[Nn] ]] && ENABLE_LOKI="no"
  explain "AI / LLM" "Read-only wording on ForgeRCA. Downloads ~9 GB GGUF to data/models/ (not git)." "no" "Incidents and ForgeRCA still work without a model."
  if [[ "$PROFILE" == "full-ai" ]]; then ENABLE_AI="yes"; fi
  echo "Enable local LLM (downloads Qwen2.5-14B Instruct Q4_K_M, ~9 GB)?"
  echo "  1) Yes"
  echo "  2) No"
  read -r -p "Select [1-2] default $([[ $ENABLE_AI == yes ]] && echo 1 || echo 2): " ans || true
  case "${ans:-}" in
    1) ENABLE_AI="yes" ;;
    2) ENABLE_AI="no" ;;
  esac
  explain "Discovery" "TCP probe of 22/80/443/9100 plus SNMP GET on UDP/161. New hosts wait for Approve/Ignore. Not nmap." "no" "Inventory stays manual plus optional NetBox."
  read -r -p "Enable discovery? [Y/n]: " ans || true
  [[ "${ans:-Y}" =~ ^[Nn] ]] && ENABLE_DISCOVERY="no"
  if [[ "$ENABLE_DISCOVERY" == "yes" ]]; then
    read -r -p "CIDRs to scan, comma-separated [${DISCOVERY_CIDRS:-none}]: " ans || true
    DISCOVERY_CIDRS="${ans:-$DISCOVERY_CIDRS}"
  fi
  explain "NetBox" "Optional external inventory. ForgeSRE never bundles NetBox." "no" "Local inventory only."
  read -r -p "Connect an external NetBox? [y/N]: " ans || true
  if [[ "${ans:-N}" =~ ^[Yy] ]]; then
    ENABLE_NETBOX="yes"
    read -r -p "NetBox URL [${NETBOX_URL}]: " ans || true
    NETBOX_URL="${ans:-$NETBOX_URL}"
    read -r -p "NetBox API token (stored in secrets/secrets.env): " NETBOX_TOKEN || true
  fi
}

write_files() {
  mkdir -p "$DATA_DIR"/{postgres,prometheus,alertmanager,loki,grafana,logs,alloy,models,backups,generated} secrets config
  touch "$DATA_DIR/logs/forgesre.log"
  chmod 700 secrets || true
  chmod 750 "$DATA_DIR" 2>/dev/null || true
  chmod 700 "$DATA_DIR/backups" 2>/dev/null || true
  chmod 750 "$DATA_DIR/logs" 2>/dev/null || true
  chmod 770 "$DATA_DIR/postgres" "$DATA_DIR/prometheus" "$DATA_DIR/grafana" "$DATA_DIR/loki" "$DATA_DIR/alloy" 2>/dev/null || true
  sudo chown 70:70 "$DATA_DIR/postgres" 2>/dev/null || true
  local pg_pass admin_pass gf_pass webhook secret
  pg_pass="$(openssl rand -hex 12)"
  admin_pass="$(openssl rand -hex 8)"
  gf_pass="$(openssl rand -hex 8)"
  webhook="$(openssl rand -hex 16)"
  secret="$(openssl rand -hex 24)"

  umask 077
  cat > "$ROOT/secrets/secrets.env" <<EOF
POSTGRES_PASSWORD=${pg_pass}
FORGESRE_ADMIN_EMAIL=admin@forgesre.local
FORGESRE_ADMIN_PASSWORD=${admin_pass}
GRAFANA_ADMIN_PASSWORD=${gf_pass}
ALERTMANAGER_WEBHOOK_TOKEN=${webhook}
SECRET_KEY=${secret}
SMTP_USERNAME=
SMTP_PASSWORD=
NETBOX_API_TOKEN=${NETBOX_TOKEN}
SNMP_COMMUNITY=public
EOF
  chmod 600 "$ROOT/secrets/secrets.env"

  local compose_profiles="" llm_mode="disabled" ai_enabled="false" llm_threads="8"
  llm_threads="$(nproc 2>/dev/null || echo 8)"
  if [[ "$llm_threads" -gt 2 ]]; then llm_threads=$((llm_threads - 2)); fi
  [[ "$llm_threads" -lt 2 ]] && llm_threads=2
  if [[ "$ENABLE_AI" == "yes" ]]; then
    ai_enabled="true"
    llm_mode="disabled"
    if [[ -f "$DATA_DIR/models/model.gguf" ]]; then
      llm_mode="bundled"
      compose_profiles="ai"
      echo "Found $DATA_DIR/models/model.gguf — starting bundled llama.cpp."
    else
      echo "No GGUF at $DATA_DIR/models/model.gguf"
      echo "AI RCA still runs via the builtin analyst on real Prometheus/Loki evidence."
      echo "On an existing install run: ./forgesre fetch-llm"
    fi
  fi

  local discovery_enabled="true" netbox_enabled="false" cidrs_yaml="[]"
  [[ "$ENABLE_DISCOVERY" == "yes" ]] || discovery_enabled="false"
  if [[ -n "${NETBOX_URL}" ]]; then
    ENABLE_NETBOX="yes"
  fi
  [[ "$ENABLE_NETBOX" == "yes" ]] && netbox_enabled="true"
  if [[ -n "${DISCOVERY_CIDRS}" ]]; then
    cidrs_yaml="["
    local first=1 part
    IFS=',' read -ra _cidrs <<< "$DISCOVERY_CIDRS"
    for part in "${_cidrs[@]}"; do
      part="${part#"${part%%[![:space:]]*}"}"
      part="${part%"${part##*[![:space:]]}"}"
      [[ -z "$part" ]] && continue
      if [[ $first -eq 1 ]]; then first=0; else cidrs_yaml+=", "; fi
      cidrs_yaml+="\"${part}\""
    done
    cidrs_yaml+="]"
  fi

  cat > "$ROOT/.env" <<EOF
FORGESRE_VERSION=0.7.0
FORGESRE_DOMAIN=forgesre.local
FORGESRE_DATA=${DATA_DIR}
FORGESRE_TIMEZONE=${TIMEZONE}
FORGESRE_HTTP_PORT=${HTTP_PORT}
GRAFANA_PORT=3000
FORGESRE_PROFILE=${PROFILE}
COMPOSE_PROFILES=${compose_profiles}
FORGESRE_LLM_THREADS=${llm_threads}
POSTGRES_PASSWORD=${pg_pass}
GRAFANA_ADMIN_PASSWORD=${gf_pass}
ALERTMANAGER_CONFIG=${DATA_DIR}/generated/alertmanager.yml
PROMETHEUS_CONFIG=${DATA_DIR}/generated/prometheus.yml
PROMETHEUS_ALERTS=${DATA_DIR}/generated/alerts.yml
SNMP_EXPORTER_CONFIG=${DATA_DIR}/generated/snmp.yml
EOF

  local grafana_enabled="true" loki_enabled="true"
  [[ "$BUNDLED_GRAFANA" == "yes" ]] || grafana_enabled="false"
  [[ "$ENABLE_LOKI" == "yes" ]] || loki_enabled="false"

  cat > "$ROOT/config/forgesre.yml" <<EOF
schema_version: 1
system:
  mode: $([[ $OFFLINE -eq 1 ]] && echo offline || echo online)
  timezone: ${TIMEZONE}
  log_level: info
  cookie_secure: false
inventory:
  provider: $([[ $netbox_enabled == true ]] && echo netbox || echo local)
  netbox:
    enabled: ${netbox_enabled}
    mode: external
    url: "${NETBOX_URL}"
discovery:
  enabled: ${discovery_enabled}
  mode: semi-automatic
  cidrs: ${cidrs_yaml}
monitoring:
  prometheus:
    enabled: true
    mode: $([[ $BUNDLED_PROM == yes ]] && echo bundled || echo external)
    url: http://127.0.0.1:9090
  alertmanager:
    enabled: true
    mode: bundled
    url: http://127.0.0.1:9093
  snmp:
    enabled: true
    exporter_url: http://127.0.0.1:9116
    module: if_mib
logging:
  loki:
    enabled: ${loki_enabled}
    mode: bundled
    url: http://127.0.0.1:3100
  alloy:
    enabled: ${loki_enabled}
grafana:
  enabled: ${grafana_enabled}
  mode: bundled
  url: http://127.0.0.1:3000
ai:
  enabled: ${ai_enabled}
  provider: local
  llm:
    mode: ${llm_mode}
    url: http://127.0.0.1:8088/v1
    model: local
  rca:
    engine: forgerca
    window_minutes: 30
    max_log_lines: 20
    max_evidence: 40
notifications:
  email:
    enabled: false
    host: smtp.local
    port: 587
    from: forgesre@example.local
    tls: true
features:
  playrules: true
  playbooks: true
  escalation: true
EOF

  "$ROOT/scripts/render-monitoring.sh"

  cat > "$ROOT/installation-report.md" <<EOF
# ForgeSRE installation report

- Version: 0.7.0
- Profile: ${PROFILE}
- Timezone: ${TIMEZONE}
- Data: ${DATA_DIR}
- UI: http://127.0.0.1:${HTTP_PORT}
- Grafana: http://127.0.0.1:3000 (user admin)
- Admin email: admin@forgesre.local
- Admin password: ${admin_pass}
- Grafana password: ${gf_pass}
- AI enabled: ${ai_enabled}
- LLM mode: ${llm_mode}
- Loki enabled: ${loki_enabled}
- Discovery enabled: ${discovery_enabled}
- Discovery CIDRs: ${cidrs_yaml}
- NetBox enabled: ${netbox_enabled}

Keep secrets/secrets.env private (mode 600).

First-hour demo (nothing here is a real server):
1. Sign in at the UI and open Dashboard. The walkthrough box is at the top.
2. Open Assets → forge-demo-01 (owner contacts + a closed HighCPU history row).
3. Run ./forgesre demo (or Dashboard → Run demo workflow).
4. Open the new incident (Who to call), then Escalation (mail to platform@forgesre.local).
5. Discovery candidate 10.20.30.41 is on /discovery (Approve / Ignore).
6. Console (/journal) lists ok/error reports per module (install, seed, inventory, snmp, demo, …).
7. Network devices: Assets → type Network device + IP. Then ./forgesre snmp (UDP/161 via bundled snmp_exporter).
EOF
}

start_stack() {
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
  export DOCKER_BUILDKIT=0
  export COMPOSE_DOCKER_CLI_BUILD=0
  local pull_flag=()
  if [[ "$OFFLINE" -eq 1 ]]; then
    pull_flag=(--pull never)
  fi
  echo "Building and starting containers..."
  dc up -d --build "${pull_flag[@]}"
  echo "Waiting for Core..."
  local i
  for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${HTTP_PORT}/api/v1/health" >/dev/null 2>&1; then
      echo "Core is up."
      return 0
    fi
    sleep 2
  done
  echo "Core did not become healthy. Check: dc logs core"
  dc logs --tail 80 core || true
  exit 1
}

post_install_journal() {
  if [[ ! -f "$ROOT/secrets/secrets.env" ]]; then
    return 0
  fi
  # shellcheck disable=SC1091
  source "$ROOT/secrets/secrets.env"
  local jar
  jar="$(mktemp)"
  curl -fsS -c "$jar" -b "$jar" -X POST "http://127.0.0.1:${HTTP_PORT}/login" \
    -d "email=${FORGESRE_ADMIN_EMAIL}&password=${FORGESRE_ADMIN_PASSWORD}" >/dev/null || { rm -f "$jar"; return 0; }
  curl -fsS -c "$jar" -b "$jar" -X POST "http://127.0.0.1:${HTTP_PORT}/api/v1/journal" \
    -H "Content-Type: application/json" \
    -d "{\"module\":\"install\",\"action\":\"install\",\"status\":\"ok\",\"summary\":\"Install finished profile=${PROFILE} port=${HTTP_PORT}\",\"detail\":\"See installation-report.md. Open Dashboard, then Console.\"}" >/dev/null || true
  rm -f "$jar"
}

echo "================================="
echo "        ForgeSRE Installer"
echo "================================="
if [[ "$NONINTERACTIVE" -eq 0 ]]; then
  wizard
else
  echo "Non-interactive install profile=${PROFILE}"
fi
preflight
if [[ "$ENABLE_AI" == "yes" ]]; then
  fetch_args=(--download-only)
  [[ "$OFFLINE" -eq 1 ]] && fetch_args+=(--offline)
  FORGESRE_DATA="$DATA_DIR" "$ROOT/scripts/fetch-llm.sh" "${fetch_args[@]}"
fi
write_files
start_stack
"$ROOT/scripts/doctor.sh" || true
post_install_journal || true
echo
echo "Installation finished."
echo "UI:              http://127.0.0.1:${HTTP_PORT}"
echo "Admin:           admin@forgesre.local"
echo "Password:        (see installation-report.md or secrets/secrets.env)"
echo "Demo workflow:   ./forgesre demo"
echo "Network SNMP:    ./forgesre snmp"
echo "CLI help:        ./forgesre help"
echo "Then open:       Dashboard → forge-demo-01 → Incidents → Escalation → Console"
echo "Report:          installation-report.md"
