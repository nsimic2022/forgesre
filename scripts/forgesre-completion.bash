# bash completion for ./forgesre and ./f
# Login shell (optional, once):
#   source /path/to/forgesre/scripts/forgesre-completion.bash
#
# Do not `complete -I` here — that would steal TAB for every command in the
# operator's shell. The interactive forgesre> prompt enables -I itself.

_forgesre_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_forgesre_cmds="help shell quit exit install doctor test ping probe verify status logs config assets inventory snmp render-monitoring journal demo demo-rca demo-reset jobs incidents history login logout whoami sd version secrets-check fetch-llm backup restore import remove update mailbox tls completion"
_forgesre_services="core postgres prometheus alertmanager snmp-exporter loki alloy grafana llm mailserver roundcube mailpit"
_forgesre_journal="install core seed inventory discovery incident rca escalation notification demo netbox snmp jobs backup"
_forgesre_status="OPEN INVESTIGATING ESCALATED RESOLVED CLOSED"

_forgesre_is_wrapper() {
  local name="${1##*/}"
  [[ "$name" == "forgesre" || "$name" == "f" ]]
}

_forgesre_incident_ids() {
  local port="8080" cache now age
  cache="${_forgesre_root}/data/.cli-complete-incidents"
  mkdir -p "${_forgesre_root}/data" 2>/dev/null || true
  now="$(date +%s)"
  if [[ -f "$cache" ]]; then
    age=$((now - $(stat -c %Y "$cache" 2>/dev/null || echo 0)))
    if [[ "$age" -ge 0 && "$age" -lt 30 ]]; then
      cat "$cache"
      return
    fi
  fi
  if [[ -f "${_forgesre_root}/.env" ]]; then
    port="$(awk -F= '/FORGESRE_HTTP_PORT/ {print $2}' "${_forgesre_root}/.env" | tail -1 | tr -d '"')"
    port="${port:-8080}"
  fi
  PYTHONPATH="${_forgesre_root}/backend" python3 -m app.cli_ops "$port" numbers 2>/dev/null | tee "$cache" || true
}

_forgesre_complete() {
  local cur="${COMP_WORDS[COMP_CWORD]-}"
  local first="${COMP_WORDS[0]-}"
  local cmd=""

  if _forgesre_is_wrapper "$first"; then
    if [[ ${COMP_CWORD} -eq 1 ]]; then
      COMPREPLY=($(compgen -W "${_forgesre_cmds}" -- "$cur"))
      return
    fi
    cmd="${COMP_WORDS[1]-}"
  else
    if [[ ${COMP_CWORD} -eq 0 ]]; then
      COMPREPLY=($(compgen -W "${_forgesre_cmds} quit exit" -- "$cur"))
      return
    fi
    cmd="${COMP_WORDS[0]-}"
  fi

  case "$cmd" in
    help)
      COMPREPLY=($(compgen -W "${_forgesre_cmds}" -- "$cur"))
      ;;
    logs)
      COMPREPLY=($(compgen -W "${_forgesre_services}" -- "$cur"))
      ;;
    journal)
      COMPREPLY=($(compgen -W "${_forgesre_journal}" -- "$cur"))
      ;;
    backup)
      COMPREPLY=($(compgen -W "--no-secrets --include-models" -- "$cur"))
      ;;
    restore|import)
      COMPREPLY=($(compgen -W "backup --yes" -- "$cur"))
      ;;
    remove)
      COMPREPLY=($(compgen -W "backup --yes" -- "$cur"))
      ;;
    update)
      COMPREPLY=($(compgen -W "--offline" -- "$cur"))
      ;;
    fetch-llm)
      COMPREPLY=($(compgen -W "--download-only --offline" -- "$cur"))
      ;;
    test)
      COMPREPLY=($(compgen -W "--json --quiet --out" -- "$cur"))
      ;;
    mailbox)
      COMPREPLY=($(compgen -W "--reset --bind-core" -- "$cur"))
      ;;
    install)
      COMPREPLY=($(compgen -W "--non-interactive --profile --port" -- "$cur"))
      ;;
    history)
      COMPREPLY=($(compgen -W "--days --status --asset ${_forgesre_status} $(_forgesre_incident_ids)" -- "$cur"))
      ;;
    incidents)
      COMPREPLY=($(compgen -W "$(_forgesre_incident_ids)" -- "$cur"))
      ;;
    ping|probe|verify)
      COMPREPLY=($(compgen -W "--timeout --demo" -- "$cur"))
      ;;
    *)
      COMPREPLY=()
      ;;
  esac
}

# ':' is in default COMP_WORDBREAKS and would split 09:13 during TAB.
COMP_WORDBREAKS="${COMP_WORDBREAKS//:}"
complete -F _forgesre_complete forgesre f ./forgesre ./f
