"""Dependency map: who sits behind whom.

The parent pointer on each asset drives parent-down suppression (one incident for the switch,
the servers behind it stay quiet). This module helps fill those pointers in:

- ``sync_librenms`` (background jobs ``topology_sync`` and ``discovery_scan``) reads LibreNMS:
  its devices go through ``librenms_discovery.apply_devices`` (known assets updated, new ones
  listed in Add devices), and LLDP/CDP neighbours go into topology_links, both ends matched to
  assets by hostname, sysName or IP.
- ``suggestions`` proposes a parent where exactly one end of a link is a network device: the
  server or appliance gets the switch as parent. Switch-to-switch links are listed but not
  guessed, because the direction towards the core is not known.
- ``tree`` is the map the new UI draws: roots, children, and how many assets each one hides.

Nothing is changed without an Engineer accepting it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import Asset, Incident, TopologyLink, utcnow

NETWORK_TYPES = {"Network device", "Switch", "Router", "Firewall"}
SOURCE = "librenms"


def is_network(asset: Asset | None) -> bool:
    return asset is not None and (asset.type or "") in NETWORK_TYPES


def _short(name: str) -> str:
    return (name or "").strip().lower().split(".")[0]


def _asset_index(db: Session) -> dict[str, Asset]:
    index: dict[str, Asset] = {}
    for asset in db.query(Asset).all():
        for key in (asset.hostname, asset.ip, asset.asset_id):
            if key:
                index.setdefault(str(key).strip().lower(), asset)
                index.setdefault(_short(str(key)), asset)
    return index


def _match(index: dict[str, Asset], *names: Any) -> Asset | None:
    for name in names:
        text = str(name or "").strip().lower()
        if text and (text in index or _short(text) in index):
            return index.get(text) or index.get(_short(text))
    return None


def sync_librenms(db: Session) -> dict[str, int]:
    """Replace LibreNMS links with what LibreNMS sees now. Raises on a LibreNMS error (the job records it)."""
    from app import librenms
    from app.journal import report

    devices = librenms.run_sync(librenms.list_devices())
    try:
        payload = librenms.run_sync(librenms._call("GET", "/resources/links"))
        links = [row for row in (payload.get("links") or []) if isinstance(row, dict)]
    except librenms.LibreNmsError as exc:
        if "do not exist" not in str(exc).lower():
            raise
        links = []  # LibreNMS answers an error when it has no LLDP/CDP data yet
    index = _asset_index(db)
    by_device: dict[str, Asset | None] = {}
    names: dict[str, str] = {}
    for dev in devices:
        key = str(dev.get("device_id") or "")
        by_device[key] = _match(index, dev.get("hostname"), dev.get("sysName"), dev.get("ip"))
        names[key] = str(dev.get("sysName") or dev.get("hostname") or key)
    db.query(TopologyLink).filter(TopologyLink.source == SOURCE).delete(synchronize_session=False)
    matched = 0
    for link in links:
        local = by_device.get(str(link.get("local_device_id") or ""))
        remote = by_device.get(str(link.get("remote_device_id") or "")) or _match(index, link.get("remote_hostname"))
        if local is not None and remote is not None:
            matched += 1
        db.add(TopologyLink(
            source=SOURCE, local_asset_id=local.id if local else None, remote_asset_id=remote.id if remote else None,
            local_name=names.get(str(link.get("local_device_id") or ""), "")[:255],
            remote_name=str(link.get("remote_hostname") or "")[:255], local_port=str(link.get("local_port_id") or "")[:128],
            remote_port=str(link.get("remote_port") or "")[:128], protocol=str(link.get("protocol") or "")[:16], seen_at=utcnow(),
        ))
    from app.librenms_discovery import apply_devices, summary
    from app.settings import settings

    counts = apply_devices(db, devices, mode=settings.discovery_mode, enabled=settings.discovery_enabled)
    report(db, "discovery", "librenms", "ok", summary=summary(counts, links=len(links)),
           detail=f"{len(devices)} devices, {matched} links between known assets")
    db.commit()
    return {"links": len(links), "matched": matched, "devices": len(devices), **counts}


def suggestions(db: Session) -> list[dict[str, Any]]:
    """Parent proposals from links: the non-network end gets the network end as parent."""
    out: dict[int, dict[str, Any]] = {}
    for link in db.query(TopologyLink).all():
        a = db.get(Asset, link.local_asset_id) if link.local_asset_id else None
        b = db.get(Asset, link.remote_asset_id) if link.remote_asset_id else None
        if a is None or b is None or is_network(a) == is_network(b):
            continue
        child, parent = (b, a) if is_network(a) else (a, b)
        if child.parent_id == parent.id or child.id in out:
            continue
        out[child.id] = {
            "asset_id": child.asset_id, "hostname": child.hostname, "parent_asset_id": parent.asset_id,
            "parent_hostname": parent.hostname, "current_parent": child.parent.hostname if child.parent else "",
            "protocol": link.protocol, "port": link.remote_port if child is b else link.local_port,
        }
    return sorted(out.values(), key=lambda s: s["hostname"])


def _down_ids(db: Session) -> set[int]:
    from app.services import ACTIVE_INCIDENT_STATUSES

    rows = db.query(Incident.asset_id).filter(Incident.status.in_(ACTIVE_INCIDENT_STATUSES), Incident.asset_id.isnot(None)).all()
    return {r[0] for r in rows}


def tree(db: Session) -> dict[str, Any]:
    """Assets as a forest by parent pointer, with the number of assets behind each node."""
    assets = db.query(Asset).order_by(Asset.hostname).all()
    alarms = _down_ids(db)
    kids: dict[int | None, list[Asset]] = {}
    for asset in assets:
        kids.setdefault(asset.parent_id, []).append(asset)

    def node(asset: Asset, depth: int = 0) -> dict[str, Any]:
        children = [node(c, depth + 1) for c in kids.get(asset.id, [])] if depth < 12 else []
        return {
            "asset_id": asset.asset_id, "hostname": asset.hostname, "type": asset.type or "", "ip": asset.ip or "",
            "alarm": asset.id in alarms, "network": is_network(asset),
            "behind": sum(1 + c["behind"] for c in children), "children": children,
        }

    roots = [node(a) for a in kids.get(None, [])]
    with_children = [r for r in roots if r["children"]]
    alone = [r for r in roots if not r["children"]]
    last = db.query(TopologyLink).order_by(TopologyLink.seen_at.desc()).first()
    return {
        "roots": sorted(with_children, key=lambda r: -r["behind"]),
        "unlinked": alone,
        "assets": len(assets),
        "with_parent": sum(1 for a in assets if a.parent_id),
        "links": db.query(TopologyLink).count(),
        "last_sync": last.seen_at.isoformat() if last else None,
    }
