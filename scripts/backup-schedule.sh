#!/usr/bin/env bash
# Nightly ForgeSRE backup as a systemd timer: backup, off-box copy, verify, keep the newest N.
# Commands: status | on [HH:MM] | off | run
set -euo pipefail
trap 'echo "backup-schedule.sh: failed at line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=forgesre-backup
UNIT_DIR="${FORGESRE_SYSTEMD_DIR:-/etc/systemd/system}"
SUDO=()
[[ "$(id -u)" -eq 0 || -w "${UNIT_DIR}" ]] || SUDO=(sudo)

usage() {
  cat <<'USAGE'
Usage: ./forgesre backup schedule [status | on [HH:MM] | off | run]

  status        timer on or off, next and last run, result of the last check
  on [HH:MM]    nightly backup at HH:MM local time (default 02:30): backup, off-box copy when
                FORGESRE_OFFBOX is set, verify, then keep the newest FORGESRE_BACKUP_KEEP (14)
  off           remove the timer (backups already made stay)
  run           run the scheduled backup now, the same way the timer does
USAGE
}

write_units() {
  local when="$1"
  "${SUDO[@]}" tee "${UNIT_DIR}/${UNIT}.service" >/dev/null <<UNITEOF
[Unit]
Description=ForgeSRE nightly backup (./forgesre backup --scheduled)
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
WorkingDirectory=${ROOT}
ExecStart=${ROOT}/forgesre backup --scheduled
# a hung dump or a slow NFS / S3 copy must not run forever
TimeoutStartSec=2h
Nice=10
IOSchedulingClass=idle
UNITEOF
  "${SUDO[@]}" tee "${UNIT_DIR}/${UNIT}.timer" >/dev/null <<UNITEOF
[Unit]
Description=ForgeSRE nightly backup at ${when}

[Timer]
OnCalendar=*-*-* ${when}:00
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
UNITEOF
}

cmd="${1:-status}"
case "${cmd}" in
  status)
    if [[ -f "${UNIT_DIR}/${UNIT}.timer" ]]; then
      grep -h '^OnCalendar=' "${UNIT_DIR}/${UNIT}.timer" | sed 's/^OnCalendar=\*-\*-\* /Nightly at /; s/:00$//'
      systemctl list-timers "${UNIT}.timer" --no-pager 2>/dev/null | sed -n '1,2p' || true
      systemctl status "${UNIT}.service" --no-pager 2>/dev/null | grep -E 'Active:|Main PID|status=' | head -3 || true
    else
      echo "Nightly backup: off (./forgesre backup schedule on)"
    fi
    ;;
  on)
    when="${2:-02:30}"
    [[ "${when}" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "Time must be HH:MM (24 h), e.g. 02:30." >&2; exit 1; }
    write_units "${when}"
    "${SUDO[@]}" systemctl daemon-reload
    "${SUDO[@]}" systemctl enable --now "${UNIT}.timer" >/dev/null
    echo "Nightly backup at ${when}. Next run: $(systemctl show "${UNIT}.timer" -p NextElapseUSecRealtime --value 2>/dev/null || echo '?')"
    echo "Logs: journalctl -u ${UNIT} ; run now: ./forgesre backup schedule run"
    ;;
  off)
    "${SUDO[@]}" systemctl disable --now "${UNIT}.timer" >/dev/null 2>&1 || true
    "${SUDO[@]}" rm -f "${UNIT_DIR}/${UNIT}.timer" "${UNIT_DIR}/${UNIT}.service"
    "${SUDO[@]}" systemctl daemon-reload || true
    echo "Nightly backup off. Existing backups stay in data/backups."
    ;;
  run)
    exec "${ROOT}/forgesre" backup --scheduled
    ;;
  -h|--help|help) usage ;;
  *) usage; exit 1 ;;
esac
