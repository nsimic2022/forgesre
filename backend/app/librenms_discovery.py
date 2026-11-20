"""Discovery comes from LibreNMS: ForgeSRE never sweeps a network itself.

LibreNMS finds the devices (its ``nets`` ranges with snmp-scan, LLDP/CDP/ARP neighbours of
devices it already polls). ``apply_devices`` turns LibreNMS's device list into ForgeSRE state:

- a device that matches an asset (serial, name, sysName, IP) gets what LibreNMS knows recorded
  on that asset through ``asset_sources``; nothing the operator typed is overwritten;
- a new device goes to the found list on Inventory › Add devices (``discovery.mode``
  semi-automatic or manual), or straight into the inventory (``discovery.mode: automatic``);
- an address an Engineer already ignored stays ignored.

The device list is read by the ``discovery_scan`` / ``topology_sync`` job (``topology.sync_librenms``),
never inside a web request. Setting LibreNMS's ranges and starting its scan run on the host
(``./forgesre discovery``), which can reach the LibreNMS container.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import DiscoveryCandidate

# LibreNMS device "type" → the ForgeSRE asset type proposed for it.
ROLE_BY_TYPE = {
    "network": "Network device",
    "wireless": "Network device",
    "firewall": "Firewall",
    "printer": "Printer",
    "storage": "Storage",
    "power": "Other",
    "environment": "Other",
    "appliance": "Web/appliance",
}


def not_ready() -> str:
    """Why discovery cannot read LibreNMS now, or '' when it can."""
    from app import librenms
    from app.settings import settings

    if not settings.librenms_enabled:
        return "LibreNMS is turned off (inventory.librenms.enabled); discovery reads LibreNMS"
    if not librenms.configured():
        return "LibreNMS has no API token yet (LIBRENMS_API_TOKEN); discovery reads LibreNMS"
    return ""


def role_for(device: dict[str, Any]) -> str:
    """The asset type to propose for one LibreNMS device. Servers say which exporter is not seen yet."""
    kind = str(device.get("type") or "").strip().lower()
    os_name = str(device.get("os") or "").strip().lower()
    if kind in ROLE_BY_TYPE:
        return ROLE_BY_TYPE[kind]
    if "windows" in os_name:
        return "Windows Server (no windows_exporter seen)"
    if kind in {"server", "workstation"} or os_name in {"linux", "freebsd", "openbsd", "netbsd", "solaris"}:
        return "Linux Server (no node_exporter seen)"
    return "Network device"  # LibreNMS only holds devices that answered SNMP


def _ip(device: dict[str, Any]) -> str:
    """LibreNMS keeps the address in ``ip``; older devices only have it as ``hostname``."""
    import ipaddress

    for key in ("ip", "hostname"):
        text = str(device.get(key) or "").strip()
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if not address.is_loopback:
            return text
    return ""


def apply_devices(db: Session, devices: list[dict[str, Any]], *, mode: str = "semi-automatic", enabled: bool = True) -> dict[str, int]:
    """Match LibreNMS devices to assets; list or add the new ones. Commits nothing (the caller does).

    Returns counts: known (matched an asset), found (new, waiting on Add devices), added
    (automatic mode), ignored (an Engineer said no before), skipped (no usable IP).
    """
    from app.asset_sources import SourceRecord, build_index, match, reconcile
    from app.inventory import approve_candidate, upsert_candidate

    index = build_index(db)
    counts = {"known": 0, "found": 0, "added": 0, "ignored": 0, "skipped": 0}
    for dev in devices:
        ip = _ip(dev)
        rec = SourceRecord(source="librenms", source_id=str(dev.get("device_id") or ""), name=str(dev.get("hostname") or ""),
                           sys_name=str(dev.get("sysName") or ""), ip=ip, serial=str(dev.get("serial") or ""))
        asset, disagreement = match(index, rec)
        if asset is not None:
            reconcile(db, index, asset, rec, disagreement=disagreement)
            counts["known"] += 1
            continue
        if not enabled:
            continue
        if not ip:
            counts["skipped"] += 1
            continue
        existing = db.query(DiscoveryCandidate).filter_by(ip=ip).first()
        if existing is not None and existing.status == "ignored":
            counts["ignored"] += 1
            continue
        row = upsert_candidate(db, ip, role_for(dev), [], source="librenms", snmp_ok=True)
        row.hostname = str(dev.get("sysName") or dev.get("hostname") or row.hostname or "")[:255]
        if row.hostname == ip:
            row.hostname = ""
        db.flush()
        if mode == "automatic" and row.status == "new":
            approve_candidate(db, row, actor="librenms")
            index = build_index(db)  # the new asset must match the next record with the same serial
            counts["added"] += 1
        else:
            counts["found"] += 1
    return counts


def summary(counts: dict[str, int], links: int | None = None) -> str:
    """One journal line for a LibreNMS read."""
    parts = [f"{counts.get('known', 0)} known", f"{counts.get('found', 0)} new in Add devices"]
    if counts.get("added"):
        parts.append(f"{counts['added']} added to the inventory")
    if counts.get("ignored"):
        parts.append(f"{counts['ignored']} ignored before")
    if counts.get("skipped"):
        parts.append(f"{counts['skipped']} without an IP")
    if links is not None:
        parts.append(f"{links} links")
    return "LibreNMS: " + ", ".join(parts)
