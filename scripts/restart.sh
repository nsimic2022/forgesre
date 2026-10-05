#!/usr/bin/env bash
# Bounce the running appliance stack: same containers, new processes, volumes kept.
# Never runs install.sh, git pull, render-monitoring, or writes .env / secrets/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env || ! -f secrets/secrets.env ]]; then
  echo "This command needs an installed appliance (.env and secrets/secrets.env)."
  echo "New VM: ./forgesre install"
  exit 1
fi

if docker info >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi

# Postgres first (Core and NetBox depend on it), Core last so it reconnects to fresh deps.
ORDER=(postgres netbox-redis prometheus alertmanager snmp-exporter loki alloy grafana llm netbox mailserver roundcube core)
REQUIRED=" postgres core "
# One-shot init container (restart: "no"); bouncing it would only re-run the init.
SKIP=" netbox-db-init "

label() {
  case "$1" in
    core) echo "Core" ;;
    postgres) echo "Postgres" ;;
    prometheus) echo "Prometheus" ;;
    alertmanager) echo "Alertmanager" ;;
    snmp-exporter) echo "snmp_exporter" ;;
    loki) echo "Loki" ;;
    alloy) echo "Alloy" ;;
    grafana) echo "Grafana" ;;
    netbox-redis) echo "NetBox Redis" ;;
    netbox) echo "NetBox" ;;
    llm) echo "LLM (llama.cpp)" ;;
    mailserver) echo "Mail server" ;;
    roundcube) echo "Roundcube" ;;
    *) echo "$1" ;;
  esac
}

echo "Restarting ForgeSRE stack (no git pull, no install.sh, .env and secrets/ untouched)..."
if ! present_raw="$("${DC[@]}" ps --all --services 2>&1)"; then
  echo "docker compose ps failed:"
  echo "$present_raw"
  exit 1
fi
present=" $(echo "$present_raw" | tr '\n' ' ') "

if [[ "$present" != *" postgres "* || "$present" != *" core "* ]]; then
  echo "Postgres or Core has no container yet. Bring the stack up first: ./forgesre update"
  exit 1
fi

# Services with a container that are not in ORDER (future additions) still get restarted.
targets=("${ORDER[@]}")
for svc in $present; do
  [[ " ${ORDER[*]} " == *" $svc "* ]] && continue
  [[ "$SKIP" == *" $svc "* ]] && continue
  targets=("${targets[@]:0:${#targets[@]}-1}" "$svc" core)
done

hard_fail=0
warn=0
for svc in "${targets[@]}"; do
  name="$(label "$svc")"
  if [[ "$present" != *" $svc "* ]]; then
    echo "Skipping ${name} (${svc}): no container (profile off or not created)."
    continue
  fi
  echo "Restarting ${name} (${svc})..."
  if ! "${DC[@]}" restart "$svc"; then
    if [[ "$REQUIRED" == *" $svc "* ]]; then
      echo "FAIL: could not restart ${name}. Logs: docker compose logs --tail=80 ${svc}"
      hard_fail=1
      [[ "$svc" == "postgres" ]] && exit 1
    else
      echo "WARN: could not restart ${name}; continuing. Logs: docker compose logs --tail=80 ${svc}"
      warn=1
    fi
    continue
  fi
  if [[ "$svc" == "postgres" ]]; then
    echo "Waiting for Postgres (pg_isready)..."
    pg_ok=0
    for _i in $(seq 1 30); do
      if "${DC[@]}" exec -T postgres pg_isready -h 127.0.0.1 -U forgesre >/dev/null 2>&1; then
        pg_ok=1
        break
      fi
      sleep 2
    done
    if [[ "$pg_ok" -ne 1 ]]; then
      echo "FAIL: Postgres did not become ready. Logs: docker compose logs --tail=80 postgres"
      exit 1
    fi
    echo "Postgres is ready."
  fi
done

[[ "$hard_fail" -ne 0 ]] && exit 1

HTTP_PORT="$(awk -F= '/^[[:space:]]*FORGESRE_HTTP_PORT=/ {v=$2} END {print v}' .env | tr -d '"' | tr -d "'" | tr -d '\r' | awk '{print $1}' || true)"
HTTP_PORT="${HTTP_PORT:-8080}"
echo "Waiting for Core on :${HTTP_PORT} (GET /api/v1/health, no token)..."
core_ok=0
for _i in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:${HTTP_PORT}/api/v1/health" >/dev/null 2>&1; then
    core_ok=1
    break
  fi
  sleep 2
done
if [[ "$core_ok" -ne 1 ]]; then
  echo "FAIL: Core is not answering GET /api/v1/health on :${HTTP_PORT}."
  echo "Logs: docker compose logs core --tail=80"
  exit 1
fi
echo "Core is up."
if [[ "$warn" -ne 0 ]]; then
  echo "Restart finished with warnings (see above). Check: ./forgesre doctor"
else
  echo "Restart finished. Check: ./forgesre doctor"
fi
