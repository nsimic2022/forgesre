#!/usr/bin/env bash
# Host apply helper: applies .env changes asked for in Settings › Server and restarts only the
# containers that use them. Core cannot edit .env or run Docker; it drops a request file in
# data/apply/requests/, a systemd path unit starts "apply-env.sh run", and this script checks every
# key and value against scripts/env-keys.tsv before it writes .env and runs docker compose.
# Commands: status | on | off | run
set -euo pipefail
trap 'echo "apply-env.sh: failed at line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=forgesre-apply
UNIT_DIR="${FORGESRE_SYSTEMD_DIR:-/etc/systemd/system}"
ENV_FILE="${FORGESRE_ENV_FILE:-${ROOT}/.env}"
KEYS="${ROOT}/scripts/env-keys.tsv"
# shellcheck source=lib-profiles.sh
source "${ROOT}/scripts/lib-profiles.sh"
data_dir() {
  local d
  d="$(env_get FORGESRE_DATA)"
  d="${d:-./data}"
  [[ "${d}" == /* ]] || d="${ROOT}/${d#./}"
  echo "${d}"
}
APPLY="${FORGESRE_APPLY_DIR:-$(data_dir)/apply}"
SUDO=()
[[ "$(id -u)" -eq 0 || -w "${UNIT_DIR}" ]] || SUDO=(sudo)
DC=(docker compose)
[[ -n "${FORGESRE_DOCKER_CMD:-}" ]] && read -ra DC <<< "${FORGESRE_DOCKER_CMD}"

usage() {
  cat <<'USAGE'
Usage: ./forgesre apply-helper [status | on | off | run]

  status   helper on or off, requests waiting, last results
  on       install the systemd path unit: Settings › Server can change .env and restart containers
  off      remove it (Settings › Server then only shows values; edit .env by hand)
  run      apply waiting requests now, the same way the path unit does
USAGE
}

# Check one KEY=VALUE against env-keys.tsv; prints the restart list on success.
check() {
  local key="$1" value="$2" k kind lo hi choices restart
  while IFS=$'\t' read -r k kind lo hi choices restart; do
    [[ "${k}" == "${key}" ]] || continue
    [[ "${lo}" == "." ]] && lo=""; [[ "${hi}" == "." ]] && hi=""; [[ "${restart}" == "." ]] && restart=""
    [[ "${value}" =~ ^[A-Za-z0-9._:/@+,-]*$ ]] || { echo "bad characters"; return 1; }
    case "${kind}" in
      int|port)
        [[ "${value}" =~ ^[0-9]+$ ]] || { echo "not a number"; return 1; }
        [[ -z "${lo}" || "${value}" -ge "${lo}" ]] || { echo "below ${lo}"; return 1; }
        [[ -z "${hi}" || "${value}" -le "${hi}" ]] || { echo "above ${hi}"; return 1; } ;;
      enum)
        local ok=0 c
        IFS='|' read -ra _cs <<< "${choices}"
        for c in "${_cs[@]}"; do [[ "${c}" == "-" ]] && c=""; [[ "${c}" == "${value}" ]] && ok=1; done
        [[ "${ok}" -eq 1 ]] || { echo "not one of ${choices}"; return 1; } ;;
      path)
        [[ -z "${value}" || "${value}" == /* ]] || { echo "not an absolute path"; return 1; } ;;
      url)
        [[ -z "${value}" || "${value}" =~ ^https?:// ]] || { echo "not an http(s) URL"; return 1; } ;;
      text)
        if [[ "${key}" == "FORGESRE_TIMEZONE" && ! -f "/usr/share/zoneinfo/${value}" ]]; then echo "unknown time zone"; return 1; fi ;;
    esac
    echo "OK ${restart}"
    return 0
  done < <(grep -v '^#' "${KEYS}")
  echo "not allowed from the UI"
  return 1
}

# Apply one request file; writes results/<id>.result and removes the request.
apply_one() {
  local req="$1" id actor line key value why restarts="" svc active
  id="$(basename "${req}" .req)"
  actor="$(sed -n 's/^#actor=//p' "${req}" | head -1)"
  local out="${APPLY}/results/${id}.result"
  mkdir -p "${APPLY}/results"
  local -a pairs=()
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    key="${line%%=*}"; value="${line#*=}"
    if ! why="$(check "${key}" "${value}")"; then
      printf 'status=error\nmessage=%s: %s\nactor=%s\nat=%s\n' "${key}" "${why}" "${actor}" "$(date -u +%FT%TZ)" > "${out}"
      rm -f "${req}"
      echo "request ${id}: refused (${key}: ${why})"
      return 0
    fi
    restarts+=" ${why#OK }"
    pairs+=("${key}=${value}")
  done < "${req}"
  local p
  for p in "${pairs[@]}"; do env_set "${p%%=*}" "${p#*=}"; done
  # only containers of the active profiles: naming a profile-off service would start it
  active=" $("${DC[@]}" --project-directory "${ROOT}" config --services 2>/dev/null | tr '\n' ' ') "
  local -a todo=()
  for svc in $(echo "${restarts//,/ }" | tr ' ' '\n' | sort -u); do
    [[ "${active}" == *" ${svc} "* ]] && todo+=("${svc}")
  done
  local msg="${#pairs[@]} value(s) written to .env"
  if [[ "${#todo[@]}" -gt 0 ]]; then
    if "${DC[@]}" --project-directory "${ROOT}" up -d --no-deps "${todo[@]}" >/dev/null 2>&1; then
      msg+="; restarted ${todo[*]}"
    else
      printf 'status=error\nmessage=%s; docker compose up -d %s failed (see journalctl -u %s)\nrestarted=\nactor=%s\nat=%s\n' \
        "${msg}" "${todo[*]}" "${UNIT}" "${actor}" "$(date -u +%FT%TZ)" > "${out}"
      rm -f "${req}"
      return 0
    fi
  fi
  printf 'status=ok\nmessage=%s\nrestarted=%s\nchanged=%s\nactor=%s\nat=%s\n' "${msg}" "${todo[*]:-}" "${pairs[*]%%=*}" "${actor}" "$(date -u +%FT%TZ)" > "${out}"
  rm -f "${req}"
  echo "request ${id}: ${msg}"
}

cmd="${1:-status}"
case "${cmd}" in
  run)
    mkdir -p "${APPLY}/requests" "${APPLY}/results"
    shopt -s nullglob
    for req in "${APPLY}/requests/"*.req; do apply_one "${req}"; done
    # keep the newest 50 results
    ls -1t "${APPLY}/results/"*.result 2>/dev/null | tail -n +51 | xargs -r rm -f
    ;;
  on)
    mkdir -p "${APPLY}/requests" "${APPLY}/results"
    "${SUDO[@]}" mkdir -p "${UNIT_DIR}"
    "${SUDO[@]}" tee "${UNIT_DIR}/${UNIT}.service" >/dev/null <<UNITEOF
[Unit]
Description=ForgeSRE apply helper: .env changes from Settings › Server
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=${ROOT}
ExecStart=${ROOT}/scripts/apply-env.sh run
TimeoutStartSec=10min
UNITEOF
    "${SUDO[@]}" tee "${UNIT_DIR}/${UNIT}.path" >/dev/null <<UNITEOF
[Unit]
Description=ForgeSRE apply helper: watch for change requests

[Path]
PathExistsGlob=${APPLY}/requests/*.req
MakeDirectory=yes

[Install]
WantedBy=multi-user.target
UNITEOF
    if [[ -z "${FORGESRE_SYSTEMD_DIR:-}" ]]; then
      "${SUDO[@]}" systemctl daemon-reload
      "${SUDO[@]}" systemctl enable --now "${UNIT}.path"
    fi
    date -u +%FT%TZ > "${APPLY}/installed"
    echo "Apply helper on: Settings › Server can change .env values and restart the containers that use them."
    ;;
  off)
    if [[ -z "${FORGESRE_SYSTEMD_DIR:-}" ]]; then "${SUDO[@]}" systemctl disable --now "${UNIT}.path" 2>/dev/null || true; fi
    "${SUDO[@]}" rm -f "${UNIT_DIR}/${UNIT}.path" "${UNIT_DIR}/${UNIT}.service"
    [[ -z "${FORGESRE_SYSTEMD_DIR:-}" ]] && "${SUDO[@]}" systemctl daemon-reload || true
    rm -f "${APPLY}/installed"
    echo "Apply helper off: Settings › Server only shows values; edit .env and run ./forgesre update."
    ;;
  status)
    if [[ -f "${UNIT_DIR}/${UNIT}.path" ]]; then echo "Apply helper: on"; else echo "Apply helper: off (./forgesre apply-helper on)"; fi
    shopt -s nullglob
    reqs=("${APPLY}/requests/"*.req)
    echo "Waiting requests: ${#reqs[@]}"
    for r in $(ls -1t "${APPLY}/results/"*.result 2>/dev/null | head -5); do
      echo "  $(basename "${r}" .result): $(sed -n 's/^status=//p' "${r}") — $(sed -n 's/^message=//p' "${r}")"
    done
    ;;
  -h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
