#!/usr/bin/env bash
# Download the local GGUF (not stored in git) and optionally enable llama.cpp.
# Safe on an existing install: does not regenerate secrets.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DOWNLOAD_ONLY=0
APPLY=1
OFFLINE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --download-only) DOWNLOAD_ONLY=1; APPLY=0; shift ;;
    --apply) APPLY=1; shift ;;
    --offline) OFFLINE=1; shift ;;
    -h|--help)
      cat <<'EOF'
Usage: ./forgesre fetch-llm [--download-only] [--offline]

Default download: Qwen2.5-14B-Instruct Q4_K_M (~9 GB)
  → $FORGESRE_DATA/models/model.gguf (gitignored).

Override URL with FORGESRE_LLM_URL. Lab (8 GB RAM), Qwen3-4B Q4_K_M:

  mkdir -p data/models
  wget -O data/models/model.gguf https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf
  ./forgesre fetch-llm --offline

  # or:
  FORGESRE_LLM_URL='https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf' ./forgesre fetch-llm

The file must be named model.gguf under data/models/ (not the clone root).

Without --download-only, also sets COMPOSE_PROFILES=ai, enables bundled LLM in
config/forgesre.yml, and starts the llama.cpp container.

Do not re-run ./install.sh just to add the model (that regenerates passwords).
Guide: docs/llm.md
EOF
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

DATA_DIR="${FORGESRE_DATA:-./data}"
if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  DATA_DIR="$(awk -F= '/^FORGESRE_DATA=/ {print $2}' "$ROOT/.env" | tail -1 | tr -d '"' )"
  DATA_DIR="${DATA_DIR:-./data}"
fi

MODEL_DIR="$DATA_DIR/models"
MODEL_PATH="$MODEL_DIR/model.gguf"
# Official Qwen GGUF, single-file Q4_K_M (~9 GB). Not committed to git.
DEFAULT_URL="https://huggingface.co/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q4_k_m.gguf"
URL="${FORGESRE_LLM_URL:-$DEFAULT_URL}"

mkdir -p "$MODEL_DIR"

have_model() {
  [[ -f "$MODEL_PATH" && "$(stat -c%s "$MODEL_PATH" 2>/dev/null || echo 0)" -gt 1000000000 ]]
}

download_model() {
  if have_model; then
    echo "GGUF already present: $MODEL_PATH ($(du -h "$MODEL_PATH" | awk '{print $1}'))"
    return 0
  fi
  if [[ "$OFFLINE" -eq 1 ]]; then
    echo "Offline: no GGUF at $MODEL_PATH" >&2
    echo "Copy a CPU Instruct GGUF to that path (named model.gguf) and re-run." >&2
    exit 1
  fi
  if ! command -v curl >/dev/null; then
    echo "curl is required to download the GGUF." >&2
    exit 1
  fi
  echo "Downloading local LLM (~9 GB). This is not stored in git."
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
  local dc=(docker compose)
  if ! docker info >/dev/null 2>&1; then
    if command -v sudo >/dev/null; then
      dc=(sudo docker compose)
    else
      echo "Docker daemon is not accessible; config is updated. Start later with:" >&2
      echo "  docker compose --profile ai up -d" >&2
      return 0
    fi
  fi
  set -a
  # shellcheck disable=SC1091
  [[ -f "$ROOT/.env" ]] && source "$ROOT/.env"
  set +a
  echo "Starting llama.cpp (profile ai) and reloading Core..."
  "${dc[@]}" --profile ai up -d llm
  "${dc[@]}" up -d --force-recreate core
}

download_model
if [[ "$APPLY" -eq 1 ]]; then
  enable_config
  start_llm
  echo
  echo "LLM enabled. Doctor should report llm: ok after llama.cpp finishes loading the GGUF."
  echo "Then: ./forgesre doctor && ./forgesre test"
  echo "Guide: docs/llm.md"
fi
