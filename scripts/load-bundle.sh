#!/usr/bin/env bash
# Load a ForgeSRE bundle on a host without internet (built with ./forgesre bundle).
#
#   ./load-bundle.sh forgesre-bundle-<version>-<commit>.tar [target dir, default ./forgesre]
#
# Checks every file against SHA256SUMS, loads the images into Docker, unpacks the code into the
# target (a new install, or over an existing one: .env, secrets/, config/forgesre.yml and data/
# are not in the bundle and are kept), tags the Core image for the target's Compose project and
# unpacks models when present. Then: ./install.sh --offline (new) or ./forgesre update --offline.
set -euo pipefail
BUNDLE="${1:?usage: load-bundle.sh <bundle.tar> [target dir]}"
TARGET="${2:-$(pwd)/forgesre}"
if [[ -f "$(dirname "$0")/../docker-compose.yml" && -z "${2:-}" ]]; then
  TARGET="$(cd "$(dirname "$0")/.." && pwd)"  # run from scripts/ of an existing install
fi
if docker info >/dev/null 2>&1; then DK=(docker); else DK=(sudo docker); fi
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

tar -xf "$BUNDLE" -C "$WORK"
(cd "$WORK" && sha256sum -c --quiet SHA256SUMS) || { echo "Bundle is damaged: a checksum does not match."; exit 1; }
cat "$WORK/MANIFEST.txt"
echo "Loading images..."
gzip -dc "$WORK/images.tar.gz" | "${DK[@]}" load
echo "Unpacking the code into $TARGET..."
mkdir -p "$TARGET"
tar -xzf "$WORK/forgesre-src.tar.gz" -C "$TARGET" --strip-components=1
if [[ -f "$WORK/models.tar.gz" ]]; then
  mkdir -p "$TARGET/data"
  tar -xzf "$WORK/models.tar.gz" -C "$TARGET/data"
fi
# Compose names the built Core image after the project (the directory name): retag it if it differs.
FROM="$(awk '/^project:/{print $2}' "$WORK/MANIFEST.txt")"
TO="$(basename "$TARGET" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
for svc in core worker; do
  if [[ -n "$FROM" && "$FROM" != "$TO" ]] && "${DK[@]}" image inspect "${FROM}-${svc}" >/dev/null 2>&1; then
    "${DK[@]}" tag "${FROM}-${svc}" "${TO}-${svc}"
  fi
done
# Same Core inputs as the bundle: update and install use the loaded image instead of rebuilding.
mkdir -p "$TARGET/data"
awk '/^core_inputs:/{print $2}' "$WORK/MANIFEST.txt" > "$TARGET/data/.core-image.stamp"
touch "$TARGET/data/.offline-bundle"
echo
echo "Done. New appliance: cd $TARGET && ./install.sh --offline"
echo "Existing appliance:  cd $TARGET && ./forgesre update --offline"
