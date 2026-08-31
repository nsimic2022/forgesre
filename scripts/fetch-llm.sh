#!/usr/bin/env bash
# Download a catalog GGUF (not stored in git) and optionally enable llama.cpp.
# Safe on an existing install: does not regenerate secrets.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CATALOG_PY="$ROOT/backend/app/llm_catalog.py"

DOWNLOAD_ONLY=0
APPLY=1
OFFLINE=0
ACTION="fetch"
MODEL_ID=""
USE_AFTER=0

usage() {
  cat <<'EOF'
Usage:
  ./forgesre fetch-llm [--download-only] [--offline]
  ./forgesre fetch-llm --list
  ./forgesre fetch-llm --model ID [--download-only] [--offline]
  ./forgesre fetch-llm use ID [--offline]
  ./forgesre fetch-llm switch ID     (alias of use)

Catalog (llama.cpp only; Ollama is not the product default):

  qwen2.5-14b     Qwen2.5-14B-Instruct Q4_K_M   → model.gguf          (~8.4 GB, default)
  qwen3-1.7b      Qwen3-1.7B Q4_K_M             → Qwen3-1.7B-Q4_K_M.gguf
  qwen2.5-1.5b    Qwen2.5-1.5B-Instruct Q4_K_M  → qwen2.5-1.5b-instruct-q4_k_m.gguf
  qwen3-4b        Qwen3-4B Q4_K_M               → Qwen3-4B-Q4_K_M.gguf

Unset FORGESRE_LLM_GGUF still loads /models/model.gguf (same as the
compose screenshot). Default ctx stays 8192 for that file. Light models
set FORGESRE_LLM_CTX=4096. Switch restarts the llm container.

  ./forgesre fetch-llm --list
  ./forgesre fetch-llm --model qwen3-1.7b
  ./forgesre fetch-llm use qwen2.5-1.5b
  ./forgesre fetch-llm use qwen2.5-14b

Override any URL with FORGESRE_LLM_URL. FORGESRE_LLM_GGUF must be a
basename (no slashes), ending in .gguf.

Do not re-run ./install.sh just to add or switch a model (that regenerates
passwords). Guide: docs/llm.md
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --download-only) DOWNLOAD_ONLY=1; APPLY=0; shift ;;
    --apply) APPLY=1; shift ;;
    --offline) OFFLINE=1; shift ;;
    --list|list) ACTION="list"; shift ;;
    --model)
      [[ $# -ge 2 ]] || { echo "fetch-llm --model needs an id" >&2; exit 1; }
      MODEL_ID="$2"
      USE_AFTER=1
      shift 2
      ;;
    --use) USE_AFTER=1; shift ;;
    use|switch)
      ACTION="use"
      [[ $# -ge 2 ]] || { echo "fetch-llm $1 needs a catalog id" >&2; exit 1; }
      MODEL_ID="$2"
      USE_AFTER=1
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

DATA_DIR="${FORGESRE_DATA:-./data}"
if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  DATA_DIR="$(awk -F= '/^FORGESRE_DATA=/ {print $2}' "$ROOT/.env" | tail -1 | tr -d '"' )"
  DATA_DIR="${DATA_DIR:-./data}"
fi

MODEL_DIR="$DATA_DIR/models"
mkdir -p "$MODEL_DIR"

catalog_cmd() {
  python3 "$CATALOG_PY" "$@" --models-dir "$MODEL_DIR" --dotenv "$ROOT/.env"
}

if [[ "$ACTION" == "list" ]]; then
  catalog_cmd list
  exit 0
fi

resolve_entry() {
  python3 "$CATALOG_PY" resolve "$1"
}

ENTRY_JSON="$(resolve_entry "${MODEL_ID}")"
ENTRY_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$ENTRY_JSON")"
ENTRY_NAME="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' <<<"$ENTRY_JSON")"
ENTRY_FILE="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["filename"])' <<<"$ENTRY_JSON")"
ENTRY_URL="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])' <<<"$ENTRY_JSON")"
ENTRY_CTX="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["ctx"])' <<<"$ENTRY_JSON")"
ENTRY_MIN="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["min_bytes"])' <<<"$ENTRY_JSON")"
ENTRY_SIZE="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["size_hint"])' <<<"$ENTRY_JSON")"

if [[ ! "$ENTRY_FILE" =~ ^[A-Za-z0-9._-]+\.gguf$ ]]; then
  echo "Refusing GGUF filename $ENTRY_FILE (basename .gguf only)." >&2
  exit 1
fi

MODEL_PATH="$MODEL_DIR/$ENTRY_FILE"
URL="${FORGESRE_LLM_URL:-$ENTRY_URL}"

have_model() {
  local path="${1:-$MODEL_PATH}"
  local min="${2:-$ENTRY_MIN}"
  [[ -f "$path" && "$(stat -c%s "$path" 2>/dev/null || echo 0)" -gt "$min" ]]
}

download_model() {
  if have_model; then
    echo "GGUF already present: $MODEL_PATH ($(du -h "$MODEL_PATH" | awk '{print $1}'))"
    return 0
  fi
  if [[ "$OFFLINE" -eq 1 ]]; then
    echo "Offline: no usable GGUF at $MODEL_PATH" >&2
    echo "Copy the Instruct GGUF to that path and re-run, or drop --offline." >&2
    exit 1
  fi
  if ! command -v curl >/dev/null; then
    echo "curl is required to download the GGUF." >&2
    exit 1
  fi
  echo "Downloading $ENTRY_NAME ($ENTRY_SIZE). This is not stored in git."
  echo "Catalog id: $ENTRY_ID"
  echo "URL: $URL"
  echo "Dest: $MODEL_PATH"
  local tmp="${MODEL_PATH}.partial"
  curl -fL --retry 5 --retry-delay 4 -C - \
    -A "forgesre-fetch-llm" \
    --progress-bar \
    -o "$tmp" \
    "$URL"
  mv "$tmp" "$MODEL_PATH"
  if ! have_model; then
    echo "Download looks too small; delete $MODEL_PATH and retry." >&2
    exit 1
  fi
  echo "Saved $MODEL_PATH ($(du -h "$MODEL_PATH" | awk '{print $1}'))"
}

upsert_env() {
  local key="$1" val="$2" file="$ROOT/.env"
  [[ -f "$file" ]] || return 0
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$file"
  else
    echo "${key}=${val}" >> "$file"
  fi
}

switch_env() {
  export FORGESRE_LLM_GGUF="$ENTRY_FILE"
  export FORGESRE_LLM_CTX="$ENTRY_CTX"
  upsert_env FORGESRE_LLM_GGUF "$ENTRY_FILE"
  upsert_env FORGESRE_LLM_CTX "$ENTRY_CTX"
  echo "Active GGUF: FORGESRE_LLM_GGUF=$ENTRY_FILE  FORGESRE_LLM_CTX=$ENTRY_CTX"
}

enable_config() {
  if [[ -f "$ROOT/.env" ]]; then
    if grep -q '^COMPOSE_PROFILES=' "$ROOT/.env"; then
      current="$(grep -E '^COMPOSE_PROFILES=' "$ROOT/.env" | tail -n1 | cut -d= -f2-)"
      next=""
      IFS=',' read -ra parts <<< "${current}"
      for part in "${parts[@]}"; do
        part="${part#"${part%%[![:space:]]*}"}"
        part="${part%"${part##*[![:space:]]}"}"
        [[ -z "${part}" || "${part}" == "ai" ]] && continue
        if [[ -n "${next}" ]]; then next+=","; fi
        next+="${part}"
      done
      if [[ -n "${next}" ]]; then next="ai,${next}"; else next="ai"; fi
      sed -i "s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=${next}|" "$ROOT/.env"
    else
      echo 'COMPOSE_PROFILES=ai' >> "$ROOT/.env"
    fi
    if grep -q '^FORGESRE_LLM_THREADS=' "$ROOT/.env"; then
      true
    else
      local n
      n="$(nproc 2>/dev/null || echo 8)"
      if [[ "$n" -gt 2 ]]; then n=$((n - 2)); fi
      [[ "$n" -lt 2 ]] && n=2
      echo "FORGESRE_LLM_THREADS=${n}" >> "$ROOT/.env"
    fi
  fi
  if [[ -f "$ROOT/config/forgesre.yml" ]]; then
    python3 - "$ROOT/config/forgesre.yml" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
out = []
in_ai = False
in_llm = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("ai:") and not line.startswith(" "):
        in_ai = True
        in_llm = False
        out.append(line)
        continue
    if in_ai and line and not line.startswith(" ") and not line.startswith("\t") and not stripped.startswith("#"):
        in_ai = False
        in_llm = False
    if in_ai and stripped.startswith("enabled:"):
        out.append("  enabled: true")
        continue
    if in_ai and stripped == "llm:":
        in_llm = True
        out.append(line)
        continue
    if in_llm and stripped.startswith("mode:"):
        out.append("    mode: bundled")
        in_llm = False
        continue
    out.append(line)
path.write_text("\n".join(out) + "\n")
PY
  fi
}

start_llm() {
  local recreate_core="${1:-1}"
  local dc=(docker compose)
  if ! docker info >/dev/null 2>&1; then
    if command -v sudo >/dev/null; then
      dc=(sudo docker compose)
    else
      echo "Docker daemon is not accessible; config is updated. Start later with:" >&2
      echo "  docker compose --profile ai up -d --force-recreate llm" >&2
      return 0
    fi
  fi
  set -a
  # shellcheck disable=SC1091
  [[ -f "$ROOT/.env" ]] && source "$ROOT/.env"
  set +a
  export FORGESRE_LLM_GGUF="${FORGESRE_LLM_GGUF:-$ENTRY_FILE}"
  export FORGESRE_LLM_CTX="${FORGESRE_LLM_CTX:-$ENTRY_CTX}"
  echo "Starting llama.cpp (profile ai) with -m /models/${FORGESRE_LLM_GGUF} -c ${FORGESRE_LLM_CTX} and reloading…"
  "${dc[@]}" --profile ai up -d --force-recreate llm
  if [[ "$recreate_core" -eq 1 ]]; then
    "${dc[@]}" up -d --force-recreate core
  fi
}

download_model
if [[ "$USE_AFTER" -eq 1 ]]; then
  switch_env
fi
if [[ "$APPLY" -eq 1 ]]; then
  enable_config
  start_llm 1
  echo
  echo "LLM: $ENTRY_NAME  file=$ENTRY_FILE  ctx=$ENTRY_CTX"
  echo "Doctor should report llm: ok after llama.cpp finishes loading the GGUF."
  echo "Then: ./forgesre doctor && ./forgesre test"
  echo "List / switch: ./forgesre fetch-llm --list"
  echo "Guide: docs/llm.md"
fi
