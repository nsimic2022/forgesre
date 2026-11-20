#!/usr/bin/env bash
# ./forgesre bundle: everything an appliance without internet needs, in one file.
#
# Run on a machine with internet and Docker, in a ForgeSRE checkout. Builds the Core image, pulls
# every image of every Compose profile, and writes data/bundles/forgesre-bundle-<version>-<commit>.tar:
#   forgesre-src.tar.gz     the code (git archive: no .env, secrets, config or data)
#   images.tar.gz           docker save of every image, Core included
#   models.tar.gz           optional (--include-models): data/models with the active GGUF
#   load-bundle.sh          the loader, also usable before the code is unpacked
#   MANIFEST.txt, SHA256SUMS
# On the offline host: ./load-bundle.sh forgesre-bundle-….tar (see docs/offline.md).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

INCLUDE_MODELS=0
OUT="$ROOT/data/bundles"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --include-models) INCLUDE_MODELS=1; shift ;;
    --out) OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done

if docker info >/dev/null 2>&1; then DC=(docker compose); DK=(docker); else DC=(sudo docker compose); DK=(sudo docker); fi
command -v git >/dev/null || { echo "git is needed to build a bundle"; exit 1; }

# Same hash as update.sh: on the offline host an unchanged hash means "use the loaded image, do not build".
core_inputs_hash() {
  (
    cd "$ROOT"
    find backend/Dockerfile backend/requirements.txt backend/app agents frontend web \
      \( -path web/node_modules -o -path web/dist \) -prune -o -type f -print 2>/dev/null \
      | sort \
      | xargs -r sha256sum
  ) | sha256sum | awk '{print $1}'
}

VERSION="$(grep -m1 -o 'version="[0-9.]*"' backend/app/main.py | cut -d'"' -f2)"
COMMIT="$(git rev-parse --short HEAD)"
PROJECT="$(basename "$ROOT" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
NAME="forgesre-bundle-${VERSION}-${COMMIT}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Every profile, so ForgeAI (llm), the worker and the mailbox images are in the bundle too.
PROFILES="$("${DC[@]}" config --profiles 2>/dev/null | paste -sd, -)"
export COMPOSE_PROFILES="$PROFILES"
echo "Building Core and pulling images (profiles: ${PROFILES:-none})..."
"${DC[@]}" build
"${DC[@]}" pull --ignore-buildable
mapfile -t IMAGES < <("${DC[@]}" config --images | sort -u)
echo "Saving ${#IMAGES[@]} images..."
"${DK[@]}" save "${IMAGES[@]}" | gzip -1 > "$WORK/images.tar.gz"
git archive --format=tar.gz --prefix=forgesre/ HEAD > "$WORK/forgesre-src.tar.gz"
if [[ "$INCLUDE_MODELS" -eq 1 && -d "${FORGESRE_DATA:-$ROOT/data}/models" ]]; then
  echo "Adding the ForgeAI models..."
  tar -C "${FORGESRE_DATA:-$ROOT/data}" -czf "$WORK/models.tar.gz" models
fi
cp "$ROOT/scripts/load-bundle.sh" "$WORK/load-bundle.sh"
{
  echo "ForgeSRE bundle ${VERSION} (commit ${COMMIT})"
  echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(hostname)"
  echo "project: ${PROJECT}"
  echo "core_inputs: $(core_inputs_hash)"
  echo "images:"
  printf '  %s\n' "${IMAGES[@]}"
} > "$WORK/MANIFEST.txt"
(cd "$WORK" && sha256sum ./*.tar.gz load-bundle.sh MANIFEST.txt > SHA256SUMS)
mkdir -p "$OUT"
tar -C "$WORK" -cf "$OUT/${NAME}.tar" .
echo "Bundle: $OUT/${NAME}.tar ($(du -h "$OUT/${NAME}.tar" | cut -f1))"
echo "Copy it to the appliance, then: tar -xf ${NAME}.tar load-bundle.sh && ./load-bundle.sh ${NAME}.tar"
