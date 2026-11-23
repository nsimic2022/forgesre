#!/usr/bin/env bash
# Run the bundled llama.cpp on an NVIDIA GPU (service llm-gpu, profile ai-gpu) or back on the CPU.
# Commands: status | on [LAYERS] | off
set -euo pipefail
trap 'echo "gpu.sh: failed at line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
ENV_FILE="${ROOT}/.env"
# shellcheck source=scripts/lib-profiles.sh
source "${ROOT}/scripts/lib-profiles.sh"
[[ -f "${ENV_FILE}" ]] || { echo "Missing .env. Run ./forgesre install first." >&2; exit 1; }
if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi
DC=("${DOCKER[@]}" compose)

usage() {
  cat <<'USAGE'
Usage: ./forgesre gpu [status | on [LAYERS] | off]

  status        GPU seen by the host and by Docker, which llama.cpp runs
  on [LAYERS]   use the CUDA build (service llm-gpu); LAYERS to offload, default 99 = all
  off           back to the CPU build (service llm)

Needs the NVIDIA driver (nvidia-smi) and the NVIDIA Container Toolkit (Docker runtime
"nvidia"). The model, context and port 8088 stay the same; Core needs no change.
USAGE
}

host_gpu() { command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L 2>/dev/null | grep -q GPU; }
docker_gpu() { "${DOCKER[@]}" info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; }

cmd="${1:-status}"
case "${cmd}" in
  status)
    if host_gpu; then nvidia-smi -L | sed 's/^/  /'; else echo "  no NVIDIA GPU visible (nvidia-smi)"; fi
    if docker_gpu; then echo "  Docker: nvidia runtime present"; else echo "  Docker: no nvidia runtime (install the NVIDIA Container Toolkit)"; fi
    if profile_on ai-gpu; then echo "llama.cpp: GPU (llm-gpu, ${FORGESRE_LLM_GPU_LAYERS:-$(env_get FORGESRE_LLM_GPU_LAYERS)} layers)"
    elif profile_on ai; then echo "llama.cpp: CPU (llm)"
    else echo "llama.cpp: off (./forgesre fetch-llm)"; fi
    ;;
  on)
    host_gpu || { echo "No NVIDIA GPU visible: nvidia-smi -L shows nothing. Install the driver first." >&2; exit 1; }
    docker_gpu || { echo "Docker has no nvidia runtime. Install the NVIDIA Container Toolkit and restart Docker." >&2; exit 1; }
    layers="${2:-99}"
    [[ "${layers}" =~ ^[0-9]+$ ]] || { echo "LAYERS must be a number." >&2; exit 1; }
    env_set FORGESRE_LLM_GPU_LAYERS "${layers}"
    profile_remove ai
    profile_add ai-gpu
    "${DC[@]}" stop llm >/dev/null 2>&1 || true
    "${DC[@]}" rm -f llm >/dev/null 2>&1 || true
    "${DC[@]}" up -d llm-gpu embed
    echo "llama.cpp now runs on the GPU (${layers} layers). Check: ./forgesre forgeai test"
    ;;
  off)
    profile_remove ai-gpu
    profile_add ai
    "${DC[@]}" stop llm-gpu >/dev/null 2>&1 || true
    "${DC[@]}" rm -f llm-gpu >/dev/null 2>&1 || true
    "${DC[@]}" up -d llm embed
    echo "llama.cpp back on the CPU. Check: ./forgesre forgeai test"
    ;;
  -h|--help|help) usage ;;
  *) usage; exit 1 ;;
esac
