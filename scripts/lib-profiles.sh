# shellcheck shell=bash
# Helpers for .env keys and COMPOSE_PROFILES, sourced by https.sh, optional.sh and update.sh.
# Needs ENV_FILE set to the clone's .env.

# Set KEY=value in .env, replacing an existing KEY line.
env_set() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}

# Remove KEY from .env (the compose / Core default applies again).
env_unset() {
  sed -i "/^$1=/d" "${ENV_FILE}"
}

# Value of KEY in .env (empty when missing).
env_get() {
  grep -E "^$1=" "${ENV_FILE}" 2>/dev/null | tail -n1 | cut -d= -f2- || true
}

# The profiles in COMPOSE_PROFILES, one per line, trimmed.
profiles_list() {
  local part
  IFS=',' read -ra _parts <<< "$(env_get COMPOSE_PROFILES)"
  for part in "${_parts[@]}"; do
    part="${part//[[:space:]]/}"
    [[ -n "${part}" ]] && echo "${part}"
  done
  return 0
}

# True when profile $1 is in COMPOSE_PROFILES. No grep -q: it stops reading early, the writer
# gets SIGPIPE and, under pipefail, a profile that is on would look off.
profile_on() {
  profiles_list | grep -x -- "$1" >/dev/null
}

# Add profile $1 once (order kept).
profile_add() {
  profile_on "$1" && return 0
  local now
  now="$(profiles_list | paste -sd, -)"
  env_set COMPOSE_PROFILES "${now:+${now},}$1"
}

# Remove profile $1.
profile_remove() {
  local next
  next="$(profiles_list | grep -vx "$1" | paste -sd, - || true)"
  env_set COMPOSE_PROFILES "${next}"
}
