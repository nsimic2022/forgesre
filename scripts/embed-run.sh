#!/bin/sh
# Runs inside the embed container (llama.cpp server image): the local embedding server for
# ForgeAI document search on 127.0.0.1:8089. Waits until a .gguf is in /models (data/models/embed,
# downloaded by ./forgesre fetch-llm), then serves OpenAI-compatible /v1/embeddings.
MODELS="${EMBED_MODELS_DIR:-/models}"
THREADS="${EMBED_THREADS:-2}"
SERVER=/app/llama-server
[ -x "$SERVER" ] || SERVER="$(command -v llama-server)"

while true; do
  model=""
  for f in "$MODELS"/*.gguf; do
    [ -f "$f" ] && model="$f" && break
  done
  if [ -z "$model" ]; then
    echo "forgesre: no embedding model in $MODELS yet; run ./forgesre fetch-llm"
    sleep 30
    continue
  fi
  echo "forgesre: starting the embedding server with $(basename "$model")"
  # 2048 tokens per text and per batch: a document chunk is about 300 tokens.
  "$SERVER" -m "$model" --embedding --host 127.0.0.1 --port 8089 -c 2048 -b 2048 -ub 2048 -t "$THREADS"
  sleep 5
done
