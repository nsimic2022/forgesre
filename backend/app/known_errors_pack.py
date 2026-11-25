"""Starter pack of known errors for a data centre: the faults every NOC meets.

Added on request (Automation › Known errors › *Add the data-centre starter pack*), never on its
own. Each entry matches incident titles by words (``pattern``, alternatives split by ``|``), so
it works for Prometheus, Zabbix and LibreNMS names alike. The checks are read-only; fixes name
the usual remedy and leave the change to the operator. Edit or delete them like any other entry.
"""

from __future__ import annotations

from typing import Any

PACK: tuple[dict[str, str], ...] = (
    {
        "title": "RAID array degraded",
        "pattern": "raid|virtual disk|vdisk|logical drive",
        "symptoms": "The controller reports a degraded or rebuilding array; one physical disk failed, is missing or predicts failure. "
                    "Performance can drop during a rebuild; a second failure in RAID 5 loses data.",
        "workaround": "Read-only checks: the controller log (iDRAC / iLO / storcli / ssacli) for which slot failed; whether a hot spare "
                      "took over and the rebuild is running; that the last backup of this host is good.",
        "fix": "Replace the failed disk (same size or larger, hot-swap if the controller allows) and let the array rebuild; "
               "open a vendor case under warranty. Do not reboot during a rebuild.",
    },
    {
        "title": "Disk failing (SMART / predictive failure)",
        "pattern": "smart|predictive fail|disk fail|drive fail|media error",
        "symptoms": "SMART or the controller predicts failure: reallocated or pending sectors, media errors, a disk dropping out.",
        "workaround": "Read-only checks: smartctl -a on the disk or the controller's physical-disk view; whether it is part of a "
                      "redundant array; recent backups.",
        "fix": "Plan a replacement before it fails; on a RAID member replace it as for a degraded array.",
    },
    {
        "title": "Power supply failed or lost input",
        "pattern": "power supply|psu|redundancy lost|power redundancy",
        "symptoms": "One PSU of a server, switch or storage reports failed or no input; redundancy is lost but the device still runs.",
        "workaround": "Read-only checks: whether the PSU or its feed failed (iDRAC / iLO / show environment power); the PDU outlet and "
                      "the breaker for that feed; whether other devices on the same feed alarm too.",
        "fix": "If the feed is down, restore it (PDU, breaker, cable). If the PSU is dead, replace it hot-swap under warranty. "
               "Until then a second failure stops the device.",
    },
    {
        "title": "Temperature too high",
        "pattern": "temperature|overheat|too hot|thermal|inlet temp",
        "symptoms": "Inlet or component temperature above threshold; fans at high speed; the device may throttle or shut down.",
        "workaround": "Read-only checks: one device or the whole rack or row (cooling failure); CRAC / in-row unit alarms; blanking "
                      "panels, blocked airflow, a recently added hot device.",
        "fix": "Restore cooling (facilities); for one device clean filters, replace a failed fan, fix airflow. Move load if it keeps rising.",
    },
    {
        "title": "Fan failure",
        "pattern": "fan fail|fan speed|fan redundancy|fan status",
        "symptoms": "A fan stopped or runs out of range; temperature may follow.",
        "workaround": "Read-only checks: which fan module (hardware log); whether temperature is still normal.",
        "fix": "Replace the fan module (usually hot-swap) under warranty.",
    },
    {
        "title": "UPS on battery",
        "pattern": "on battery|onbattery|utility power fail|input power fail|mains fail",
        "symptoms": "The UPS lost mains and runs on battery; the runtime left counts down. Several UPS alarm at once when a feed failed.",
        "workaround": "Read-only checks: runtime left and load (NUT / SNMP); one UPS or all (utility or feed outage); generator start. "
                      "Tell facilities at once.",
        "fix": "Restore mains (facilities, generator). If the runtime gets short, shut down non-critical systems in order before the "
               "batteries run out. Afterwards test the batteries.",
    },
    {
        "title": "Network link down or flapping",
        "pattern": "flap|link down|link up|interface down|ifoperstatus",
        "symptoms": "An interface goes down and up repeatedly; errors and CRCs rise; hosts behind it lose connectivity in bursts.",
        "workaround": "Read-only checks: show interface counters (CRC, input errors) on both ends; the transceiver's light levels "
                      "(show interface transceiver); the log for err-disable; whether one port or many on the same switch.",
        "fix": "Replace the cable or transceiver, clean the fibre, or move to another port; if it is the far end, contact its owner.",
    },
    {
        "title": "Datastore or volume almost full",
        "pattern": "datastore|volume full|lun full|storage pool|thin provision",
        "symptoms": "A VMware datastore, LUN or pool is near full; snapshots or thin disks keep growing; VMs can pause when it fills.",
        "workaround": "Read-only checks: what grew (old snapshots, swap, logs, ISO files); free space trend in Grafana; thin-provisioning "
                      "overcommit.",
        "fix": "Consolidate or delete old snapshots, move VMs to another datastore, or grow the LUN. Do not delete files in the "
               "datastore browser by hand.",
    },
    {
        "title": "Filesystem full",
        "pattern": "disk full|filesystem|disk space|diskspace|no space left|free space",
        "symptoms": "A filesystem is almost full; applications fail to write, databases stop, logs stop.",
        "workaround": "Read-only checks: df -h and du -xh --max-depth=1 on the mount; what grew recently (logs, backups, core dumps).",
        "fix": "Remove or move what grew (rotate logs, old backups), then fix the cause (log rotation, backup retention) or grow the volume.",
    },
    {
        "title": "Clock drift (NTP)",
        "pattern": "ntp|clock skew|time drift|timeoffset|time offset|chrony",
        "symptoms": "A host's time differs from the reference; Kerberos / AD logins fail, logs and certificates look wrong.",
        "workaround": "Read-only checks: chronyc tracking or ntpq -p (offset, reachable servers); whether many hosts drift (the NTP "
                      "server itself); firewall to UDP 123.",
        "fix": "Point the host to a working NTP source and let it step once; fix the NTP server if many drift.",
    },
    {
        "title": "Certificate expiring or expired",
        "pattern": "certificate|ssl cert|tls cert|cert expir",
        "symptoms": "A TLS certificate expires soon or has expired; browsers and API clients refuse the connection.",
        "workaround": "Read-only checks: which name and service (the alert's labels); openssl s_client -connect host:443 | openssl x509 "
                      "-noout -dates; whether automatic renewal ran.",
        "fix": "Renew and install the certificate (ACME / internal CA), reload the service, and check the chain.",
    },
)


def add_pack(db: Any, actor: str) -> dict[str, Any]:
    """Add every entry whose title is not there yet. Returns what was added and skipped."""
    from app.known_errors import save_known_error
    from app.models import KnownError

    have = {t.lower() for (t,) in db.query(KnownError.title).all()}
    added, skipped = [], []
    for item in PACK:
        if item["title"].lower() in have:
            skipped.append(item["title"])
            continue
        save_known_error(db, {**item, "status": "open"}, actor)
        added.append(item["title"])
    return {"added": added, "skipped": skipped}
