#!/bin/sh
# Runs inside the llm container (llama.cpp server image). Starts llama-server with the model
# named in /models/active (written by Core from Settings › ForgeAI models), else model.gguf,
# else the first .gguf. Checks every 5 s and restarts llama-server when the choice changes,
# so a model can be switched from the UI without a shell. Split models: the -00001-of- part.
MODELS="${LLM_MODELS_DIR:-/models}"
CTX="${LLM_CTX:-8192}"
THREADS="${LLM_THREADS:-8}"
SERVER=/app/llama-server
[ -x "$SERVER" ] || SERVER="$(command -v llama-server)"

pick() {
  if [ -s "$MODELS/active" ]; then
    name="$(head -n 1 "$MODELS/active" | tr -d '\r\n')"
    if [ -n "$name" ] && [ -f "$MODELS/$name" ]; then echo "$name"; return; fi
  fi
  if [ -f "$MODELS/model.gguf" ]; then echo "model.gguf"; return; fi
  for f in "$MODELS"/*.gguf; do
    [ -f "$f" ] || continue
    case "$f" in *-0000[2-9]-of-*|*-000[1-9][0-9]-of-*) continue ;; esac
    basename "$f"; return
  done
}

# Tests: print the choice and stop.
if [ "${LLM_PICK_ONLY:-}" = "1" ]; then pick; exit 0; fi

while true; do
  model="$(pick)"
  if [ -z "$model" ]; then
    echo "forgesre: no model in $MODELS yet; add one in Settings › RCA / AI or run ./forgesre fetch-llm"
    sleep 15
    continue
  fi
  echo "forgesre: starting llama-server with $model"
  "$SERVER" -m "$MODELS/$model" --host 127.0.0.1 --port 8088 -c "$CTX" -t "$THREADS" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 5
    now="$(pick)"
    if [ "$now" != "$model" ]; then
      echo "forgesre: model changed to ${now:-none}; restarting llama-server"
      kill "$pid" 2>/dev/null
      break
    fi
  done
  wait "$pid" 2>/dev/null
  sleep 2
done
