"""Rack elevation: assets placed by site, row, rack and U position (asset extras).

Read-only view for Inventory › Racks. Positions come from extras ``rack_u`` (lowest U) and
``u_height`` (default 1). Two devices on the same U are reported as overlaps, not hidden.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from app.asset_extras import normalize_extras
from app.models import Asset, Incident
from app.settings import settings

OPEN = ("OPEN", "INVESTIGATING", "ESCALATED")


def rack_units() -> int:
    raw = ((settings.yaml or {}).get("inventory") or {}).get("rack_units")
    try:
        return max(10, min(60, int(raw)))
    except (TypeError, ValueError):
        return 42


def racks(db: Session, site: str = "") -> dict[str, Any]:
    """Racks with their devices top-down data, unplaced devices per rack and U overlaps."""
    height = rack_units()
    burning: dict[int, str] = {}
    for inc in db.query(Incident).filter(Incident.status.in_(OPEN)).all():
        if inc.asset_id is not None:
            sev = str(inc.severity or "").upper()
            if burning.get(inc.asset_id) != "CRITICAL":
                burning[inc.asset_id] = sev
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for asset in db.query(Asset).order_by(Asset.asset_id).all():
        extras = normalize_extras(asset.extras)
        rack = extras.get("rack", "")
        if not rack:
            continue
        where = (extras.get("site", ""), extras.get("row", ""), rack)
        if site and where[0] != site:
            continue
        u = int(extras["rack_u"]) if extras.get("rack_u") else None
        size = int(extras.get("u_height") or 1)
        grouped[where].append({
            "asset_id": asset.asset_id, "hostname": asset.hostname, "type": asset.type, "u": u, "height": size,
            "status": asset.status, "incident": burning.get(asset.id, ""),
        })
    out = []
    for (site_name, row, rack), devices in sorted(grouped.items()):
        placed = sorted((d for d in devices if d["u"] is not None), key=lambda d: -d["u"])
        taken: dict[int, str] = {}
        overlaps: list[str] = []
        for d in placed:
            for unit in range(d["u"], d["u"] + d["height"]):
                if unit in taken:
                    overlaps.append(f"U{unit}: {taken[unit]} and {d['asset_id']}")
                taken[unit] = d["asset_id"]
        out.append({
            "site": site_name, "row": row, "rack": rack, "units": height,
            "devices": placed, "unplaced": [d for d in devices if d["u"] is None],
            "used_u": len([u for u in taken if u <= height]), "overlaps": overlaps,
            "too_high": [d["asset_id"] for d in placed if d["u"] + d["height"] - 1 > height],
        })
    return {"racks": out, "units": height}
