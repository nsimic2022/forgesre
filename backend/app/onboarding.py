"""Add devices from the new UI: identify one address, add it, then prove that monitoring works.

1. ``identify`` (job ``onboard_identify``): TCP ports, node/windows exporter /metrics, and an SNMP
   GET of sysDescr / sysName with the community the operator gave (v1/v2c). It suggests hostname,
   type, vendor and how ForgeSRE will watch it. Nothing is saved.
2. The UI adds the asset with the normal asset API; SNMP custom community / v3 credentials are
   stored on the asset and ``snmp_apply`` writes them into snmp.yml and reloads snmp_exporter.
3. ``verify`` (job ``asset_verify``) runs the live chain (address → exporter or SNMP → Prometheus
   SD and target → series → Alertmanager → Core) and returns it as a checklist with fixes.

Every probe has a short timeout and runs in the jobs worker, never in a web request.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

from sqlalchemy.orm import Session

IDENTIFY_KIND = "onboard_identify"
VERIFY_KIND = "asset_verify"


def _check_ip(ip: str) -> str:
    text = (ip or "").strip()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError as exc:
        raise ValueError("enter one IPv4 or IPv6 address") from exc


def _reverse_dns(ip: str) -> str:
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(1.0)
    try:
        return socket.gethostbyaddr(ip)[0].split(".")[0]
    except OSError:
        return ""
    finally:
        socket.setdefaulttimeout(previous)


def identify(ip: str, snmp: dict[str, Any] | None = None, snmp_port: int = 161) -> dict[str, Any]:
    """What answers at ``ip`` and what ForgeSRE suggests for it. Never raises for an unreachable host."""
    from discovery import probe_host

    from app.inventory import candidate_inventory_guess
    from app.models import DiscoveryCandidate
    from app.snmp_get import guess_type, system_info

    ip = _check_ip(ip)
    snmp = snmp or {}
    tcp = probe_host(ip, timeout=0.5)
    version = str(snmp.get("snmp_version") or "v2c")
    sys: dict[str, Any] = {}
    snmp_note = ""
    if version in {"v1", "v2c"}:
        custom = snmp.get("snmp_community_mode") == "custom" and snmp.get("snmp_community")
        community = str(snmp.get("snmp_community") if custom else os.environ.get("SNMP_COMMUNITY") or "public")
        sys = system_info(ip, community, version=version, port=int(snmp_port or 161))
        if sys.get("error"):
            snmp_note = str(sys.pop("error"))
            sys = {}
    else:
        snmp_note = "SNMPv3 is checked after the device is added, through snmp_exporter with the user you gave."
    candidate = DiscoveryCandidate(
        ip=ip, proposed_role=tcp.get("proposed_role") or "", open_ports=tcp.get("open_ports") or [],
        snmp_ok=bool(sys) or bool(tcp.get("snmp_ok")),
        node_exporter=tcp.get("exporter_kind") == "linux", windows_exporter=tcp.get("exporter_kind") == "windows",
    )
    atype, profile, scrape = candidate_inventory_guess(candidate)
    vendor, snmp_type = guess_type(str(sys.get("sys_descr") or ""))
    if sys and atype in {"Network device", "Web/appliance", "Unknown"} and snmp_type:
        atype = snmp_type
    hostname = str(sys.get("sys_name") or "").split(".")[0] or _reverse_dns(ip)
    watch = (
        f"Prometheus scrapes {scrape}" if scrape
        else "snmp_exporter walks it over SNMP" if sys or candidate.snmp_ok or version == "v3"
        else "Nothing to scrape yet: install node_exporter (Linux) or windows_exporter (Windows), or enable SNMP"
    )
    return {
        "ip": ip,
        "alive": bool(tcp.get("alive")) or bool(sys),
        "open_ports": sorted(int(p) for p in tcp.get("open_ports") or []),
        "exporter": tcp.get("exporter_kind") or "",
        "detect_message": tcp.get("detect_message") or "",
        "snmp": sys,
        "snmp_note": snmp_note,
        "suggested": {"hostname": hostname, "type": atype or "Other", "monitoring_profile": profile,
                      "scrape_address": scrape, "vendor": vendor, "location": str(sys.get("sys_location") or "")},
        "watch": watch,
    }


# Verify keys in the order an operator follows them, with what each proves and how to fix it.
STEPS: list[tuple[str, str, str]] = [
    ("icmp", "The address answers", "Check the IP, routing and firewall from the ForgeSRE VM: ./forgesre ping <host>."),
    ("port", "The exporter or SNMP port answers", "Linux: node_exporter on 9100. Windows: windows_exporter on 9182. Network: SNMP on UDP/161 allowed from this VM."),
    ("family", "The right metrics come back", "The exporter answers but with other metrics; check the type, or the SNMP community / v3 user."),
    ("prom", "Prometheus knows the target", "Prometheus reads targets from ForgeSRE every 30 s; wait a minute, then check Settings › Integrations › Prometheus."),
    ("target", "Prometheus scrapes it (up = 1)", "Open Prometheus targets: the error there (timeout, 401, connection refused) is the fix."),
    ("series", "Samples are stored", "Prometheus scrapes but stores nothing yet; check again in a minute."),
    ("am", "Alertmanager is up", "Without Alertmanager alarms cannot reach ForgeSRE: ./forgesre logs alertmanager."),
    ("core", "Alarms reach ForgeSRE", "No alarm from this asset yet; that is fine until something fires. Try Automation › Try an alarm."),
]


def checklist(report: dict[str, Any]) -> dict[str, Any]:
    """A verify report as ordered steps {key, title, state: ok|fail|wait|skip, detail, fix}."""
    steps = []
    for key, title, fix in STEPS:
        check = report.get(key) or {}
        ok = check.get("ok")
        state = "ok" if ok is True else "fail" if ok is False else "skip"
        if key in {"prom", "target", "series"} and ok is None:
            state = "wait"
        steps.append({"key": key, "title": title, "state": state, "detail": check.get("detail") or "", "fix": fix if state in {"fail", "wait"} else ""})
    return {
        "asset_id": report.get("asset_id"), "overall": report.get("overall") or "", "reason": report.get("overall_reason") or "",
        "hint": report.get("hint") or "", "class": report.get("class") or "", "steps": steps,
    }


def verify(db: Session, asset_id: str) -> dict[str, Any]:
    """Jobs worker: the live chain for one asset, as a checklist. Probes time out after 3 s."""
    from app.api import verify_assets
    from app.models import Asset

    asset = db.query(Asset).filter_by(asset_id=asset_id).first()
    if asset is None:
        raise LookupError(f"asset {asset_id} not found")
    return checklist(verify_assets(db, [asset], timeout=3.0)[0])
