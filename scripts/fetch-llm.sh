#!/usr/bin/env bash
# Download the local GGUF (not stored in git) and optionally enable llama.cpp.
# Safe on an existing install: does not regenerate secrets.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Flags: DOWNLOAD_ONLY = fetch the file only; APPLY = also enable + start llama.cpp;
# OFFLINE = never download (file must already be in data/models/).
DOWNLOAD_ONLY=0
APPLY=1
OFFLINE=0
# MODEL_ID = one of the catalog models below (same list as Settings › RCA / AI).
MODEL_ID="qwen3-4b"
MODEL_SET=0
# Parse command-line flags one at a time.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --download-only) DOWNLOAD_ONLY=1; APPLY=0; shift ;;
    --apply) APPLY=1; shift ;;
    --offline) OFFLINE=1; shift ;;
    --model) MODEL_ID="${2:-}"; MODEL_SET=1; shift 2 ;;
    --model=*) MODEL_ID="${1#--model=}"; MODEL_SET=1; shift ;;
    -h|--help)
      cat <<'EOF'
Usage: ./forgesre fetch-llm [--model ID] [--download-only] [--offline]

Models (the same list as Settings › RCA / AI, where you can also switch):
  qwen3-4b      Qwen3 4B Q4_K_M, 2.5 GB, 6 GB RAM, 30-90 s per answer (default)
  qwen3-8b      Qwen3 8B Q4_K_M, 5 GB, 10 GB RAM
  llama3.1-8b   Llama 3.1 8B Instruct Q4_K_M, 4.9 GB, 10 GB RAM
  qwen2.5-7b    Qwen2.5 7B Instruct Q4_K_M, 4.7 GB in 2 files, 10 GB RAM
  qwen2.5-14b   Qwen2.5 14B Instruct Q4_K_M, 9 GB in 3 files, 16 GB RAM, minutes per answer

Files go to $FORGESRE_DATA/models/ (gitignored) and data/models/active names the one to run.
The embedding model for document search (nomic-embed-text, ~150 MB) goes to data/models/embed/.

Any other single-file GGUF with FORGESRE_LLM_URL (saved as model.gguf), or by hand:

  mkdir -p data/models
  wget -O data/models/model.gguf https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf
  ./forgesre fetch-llm --offline

  # or:
  FORGESRE_LLM_URL='https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf' ./forgesre fetch-llm

A file you copy yourself must be named model.gguf under data/models/ (not the clone root),
or be picked in Settings.

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

# Data dir from .env (FORGESRE_DATA), default ./data.
DATA_DIR="${FORGESRE_DATA:-./data}"
if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  DATA_DIR="$(awk -F= '/^FORGESRE_DATA=/ {print $2}' "$ROOT/.env" | tail -1 | tr -d '"' )"
  DATA_DIR="${DATA_DIR:-./data}"
fi

MODEL_DIR="$DATA_DIR/models"
MODEL_PATH="$MODEL_DIR/model.gguf"
HF="https://huggingface.co"
# Catalog: file URLs per model (checked on huggingface.co). Split models list every part.
case "$MODEL_ID" in
  qwen3-4b) FILES=("$HF/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf") ;;
  qwen3-8b) FILES=("$HF/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf") ;;
  llama3.1-8b) FILES=("$HF/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf") ;;
  qwen2.5-7b) FILES=("$HF/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"
                     "$HF/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf") ;;
  qwen2.5-14b) FILES=("$HF/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q4_k_m-00001-of-00003.gguf"
                      "$HF/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q4_k_m-00002-of-00003.gguf"
                      "$HF/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q4_k_m-00003-of-00003.gguf") ;;
  *) echo "Unknown --model $MODEL_ID (see ./forgesre fetch-llm --help)" >&2; exit 1 ;;
esac
URL="${FORGESRE_LLM_URL:-}"

mkdir -p "$MODEL_DIR"

# A real GGUF is > 1 GB; smaller means a failed or partial download.
big() { [[ -f "$1" && "$(stat -c%s "$1" 2>/dev/null || echo 0)" -gt 1000000000 ]]; }
have_model() {
  # No --model: any model already there (model.gguf or the active one) is enough.
  if [[ -z "$URL" && "$OFFLINE" -eq 0 && "$MODEL_SET" -eq 1 ]]; then
    big "$MODEL_DIR/$(basename "${FILES[0]}")"
  else
    big "$MODEL_PATH" || { [[ -s "$MODEL_DIR/active" ]] && big "$MODEL_DIR/$(head -n1 "$MODEL_DIR/active")"; }
  fi
}

# One file: download to .partial (curl -C - resumes it), rename only when complete.
fetch_one() {
  local url="$1" dest="$2"
  local tmp="${dest}.partial"
  curl -fL --retry 5 --retry-delay 4 -C - -A "forgesre-fetch-llm" --progress-bar -o "$tmp" "$url"
  mv "$tmp" "$dest"
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
  if [[ -n "$URL" ]]; then
    echo "Downloading $URL"
    echo "Dest: $MODEL_PATH"
    fetch_one "$URL" "$MODEL_PATH"
    if ! big "$MODEL_PATH"; then
      echo "Download looks too small; delete $MODEL_PATH and retry." >&2
      exit 1
    fi
    printf 'model.gguf\n' > "$MODEL_DIR/active"
  else
    echo "Downloading $MODEL_ID (${#FILES[@]} file(s)). Not stored in git."
    local f
    for f in "${FILES[@]}"; do
      echo "  $f"
      fetch_one "$f" "$MODEL_DIR/$(basename "$f")"
    done
    MODEL_PATH="$MODEL_DIR/$(basename "${FILES[0]}")"
    printf '%s\n' "$(basename "${FILES[0]}")" > "$MODEL_DIR/active"
  fi
  echo "Saved $MODEL_PATH ($(du -h "$MODEL_PATH" | awk '{print $1}'))"
}

# Turn on Compose profile "ai", pick llama.cpp threads, and set ai.llm.mode: bundled in YAML.
enable_config() {
  if [[ -f "$ROOT/.env" ]]; then
    if grep -q '^COMPOSE_PROFILES=' "$ROOT/.env"; then
      # current = existing profile list; next = rebuilt list with "ai" first, no duplicates.
      current="$(grep -E '^COMPOSE_PROFILES=' "$ROOT/.env" | tail -n1 | cut -d= -f2-)"
      next=""
      IFS=',' read -ra parts <<< "${current}"
      # Copy every other profile (trimmed, empties and "ai" dropped) into next.
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
      # Threads = CPU count minus 2 (leave room for Core / Postgres), at least 2.
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
# in_ai / in_llm track whether the current line is inside the top-level ai: / ai.llm: block.
in_ai = False
in_llm = False
# Copy the YAML line by line, rewriting ai.enabled and ai.llm.mode in place.
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

# Start the llm container and recreate Core so it picks up the bundled LLM setting.
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
  "${dc[@]}" --profile ai up -d llm embed
  "${dc[@]}" up -d --force-recreate core
}

# The small embedding model for ForgeAI document search (nomic-embed-text v1.5, Q8_0, ~146 MB).
EMBED_URL="${FORGESRE_EMBED_URL:-https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf}"
download_embed() {
  local dir="$MODEL_DIR/embed" dest
  dest="$dir/$(basename "$EMBED_URL")"
  mkdir -p "$dir"
  if [[ -f "$dest" && "$(stat -c%s "$dest" 2>/dev/null || echo 0)" -gt 20000000 ]]; then
    echo "Embedding model already present: $dest"
    return 0
  fi
  if [[ "$OFFLINE" -eq 1 ]]; then
    echo "Offline: no embedding model in $dir (document search stays off until one is copied there)."
    return 0
  fi
  echo "Downloading the embedding model for document search (~150 MB)."
  fetch_one "$EMBED_URL" "$dest" || { echo "Embedding model download failed; document search stays off. Retry: ./forgesre fetch-llm" >&2; return 0; }
}

download_model
download_embed
if [[ "$APPLY" -eq 1 ]]; then
  enable_config
  start_llm
  echo
  echo "LLM enabled. Doctor should report llm: ok after llama.cpp finishes loading the GGUF."
  echo "Then: ./forgesre doctor && ./forgesre test"
  echo "Guide: docs/llm.md"
fi
