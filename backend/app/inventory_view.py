"""Inventory pages of the new UI: the asset library, the filtered table and the asset card.

Location (site, row, rack), customer and support fields live in ``assets.extras`` JSON, so
grouping and filtering happen in Python over one query. Fine up to a few thousand assets;
move to SQL JSON filters if an install grows far past that.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.asset_extras import EXTRA_FIELDS, EXTRA_LABELS, normalize_extras
from app.asset_types import ASSET_TYPE_GROUPS
from app.inventory import is_snmp_asset
from app.models import Asset, Incident, User
from app.noc import ACTIVE
from app.security import can

VIEWS = {
    "down": "Down now",
    "alarm": "With an open incident",
    "no_source": "No monitoring source",
    "no_owner": "No owner",
}


def asset_state(asset: Asset, open_incidents: int) -> str:
    """down (ICMP or exporter red), alarm (open incident), up (green), unknown (not probed)."""
    if asset.ping_status == "red" or asset.exporter_status == "red":
        return "down"
    if open_incidents:
        return "alarm"
    if asset.ping_status == "green" or asset.exporter_status == "green":
        return "up"
    return "unknown"


def asset_sources(asset: Asset) -> list[str]:
    """Where this asset is known or monitored from, in a fixed order."""
    out = []
    if asset.scrape_address:
        out.append("Prometheus")
    if is_snmp_asset(asset):
        out.append("SNMP")
    if asset.zabbix_hostid:
        out.append("Zabbix")
    if getattr(asset, "glpi_id", ""):
        out.append("GLPI")
    if asset.netbox_id:
        out.append("NetBox")
    if (asset.source or "").lower() == "librenms":
        out.append("LibreNMS")
    return out


def _open_counts(db: Session) -> dict[int, int]:
    rows = (
        db.query(Incident.asset_id, func.count(Incident.id))
        .filter(Incident.status.in_(ACTIVE), Incident.asset_id.is_not(None))
        .group_by(Incident.asset_id)
        .all()
    )
    return {int(asset_id): int(count) for asset_id, count in rows}


def _row(asset: Asset, extras: dict[str, str], open_count: int) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "number": asset.number,
        "hostname": asset.hostname,
        "ip": asset.ip,
        "type": asset.type,
        "environment": asset.environment or "",
        "site": extras.get("site", ""),
        "row": extras.get("row", ""),
        "rack": extras.get("rack", ""),
        "customer": extras.get("customer", ""),
        "owner": asset.owner or "",
        "state": asset_state(asset, open_count),
        "sources": asset_sources(asset),
        "open": open_count,
    }


def _matches(row: dict[str, Any], view: str) -> bool:
    if view == "down":
        return row["state"] == "down"
    if view == "alarm":
        return row["open"] > 0
    if view == "no_source":
        return not row["sources"]
    if view == "no_owner":
        return not row["owner"]
    return True


def inventory_page(
    db: Session,
    *,
    q: str = "",
    site: str = "",
    rack: str = "",
    type_: str = "",
    environment: str = "",
    customer: str = "",
    view: str = "",
    limit: int = 50,
    offset: int = 0,
    everything: bool = False,
) -> dict[str, Any]:
    """Library counts (unfiltered), then one page of assets matching every given filter (all of them with ``everything``)."""
    open_counts = _open_counts(db)
    rows = []
    for asset in db.query(Asset).order_by(Asset.hostname).all():
        rows.append(_row(asset, normalize_extras(asset.extras), open_counts.get(asset.id, 0)))

    sites: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        sites[row["site"] or "No site"][row["rack"] or "No rack"] += 1
    library = {
        "sites": [
            {"name": name, "count": sum(racks.values()), "racks": [{"name": r, "count": c} for r, c in sorted(racks.items())]}
            for name, racks in sorted(sites.items())
        ],
        "types": [{"name": n, "count": c} for n, c in sorted(Counter(r["type"] for r in rows).items())],
        "environments": [{"name": n, "count": c} for n, c in sorted(Counter(r["environment"] or "—" for r in rows).items())],
        "customers": [{"name": n, "count": c} for n, c in sorted(Counter(r["customer"] for r in rows if r["customer"]).items())],
        "views": [{"key": k, "name": v, "count": sum(1 for r in rows if _matches(r, k))} for k, v in VIEWS.items()],
    }

    text = q.strip().lower()

    def keep(row: dict[str, Any]) -> bool:
        if site and (row["site"] or "No site") != site:
            return False
        if rack and (row["rack"] or "No rack") != rack:
            return False
        if type_ and row["type"] != type_:
            return False
        if environment and (row["environment"] or "—") != environment:
            return False
        if customer and row["customer"] != customer:
            return False
        if view and not _matches(row, view):
            return False
        if text:
            hay = " ".join(str(row[k]) for k in ("hostname", "ip", "type", "owner", "site", "rack", "customer", "asset_id")).lower()
            return text in hay
        return True

    filtered = [row for row in rows if keep(row)]
    if everything:
        return {"rows": filtered}
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return {
        "library": library,
        "total": len(filtered),
        "all": len(rows),
        "offset": offset,
        "limit": limit,
        "rows": filtered[offset : offset + limit],
    }


def asset_card(db: Session, asset: Asset, user: User) -> dict[str, Any]:
    """The asset card: identity, location, owner and contacts, support, sources, recent incidents."""
    from app.api import _asset

    extras = normalize_extras(asset.extras)
    open_count = _open_counts(db).get(asset.id, 0)
    recent = (
        db.query(Incident).filter(Incident.asset_id == asset.id).order_by(Incident.started_at.desc()).limit(10).all()
    )
    data = _asset(asset)
    data.update(
        {
            "state": asset_state(asset, open_count),
            "sources": asset_sources(asset),
            "open": open_count,
            "details": [{"key": k, "label": EXTRA_LABELS.get(k, k), "value": v} for k, v in extras.items() if k in EXTRA_LABELS],
            "parent": asset.parent.hostname if asset.parent else "",
            **{key: extras.get(key, "") for key in ("site", "row", "rack", "customer")},
            "incidents": [
                {
                    "number": i.number,
                    "title": i.title,
                    "severity": (i.severity or "").upper(),
                    "status": i.status,
                    "started_at": i.started_at.isoformat() if i.started_at else None,
                }
                for i in recent
            ],
            "can_edit": can(user, "write_assets"),
            "snmp": _snmp_view(asset),
        }
    )
    return data


def _snmp_view(asset: Asset) -> dict[str, Any] | None:
    """SNMP version and which snmp_exporter auth is in use (``pending`` until snmp.yml has it). No secrets."""
    from app.asset_snmp import auth_status
    from app.inventory import is_snmp_asset

    return auth_status(asset) if is_snmp_asset(asset) else None


def asset_form_options(db: Session, user: User) -> dict[str, Any]:
    """Choices for the new UI's asset form: type groups, extra fields, known environments, sites, owners."""
    environments = {"Production", "Staging", "Test", "Development"}
    owners: set[str] = set()
    sites: set[str] = set()
    for asset in db.query(Asset).all():
        if asset.environment:
            environments.add(asset.environment)
        if asset.owner:
            owners.add(asset.owner)
        site = normalize_extras(asset.extras).get("site")
        if site:
            sites.add(site)
    return {
        "types": [{"group": group, "names": list(names)} for group, names in ASSET_TYPE_GROUPS],
        "extra_fields": [{"key": key, "label": label, "max": limit} for key, label, limit in EXTRA_FIELDS],
        "environments": sorted(environments),
        "owners": sorted(owners),
        "sites": sorted(sites),
        "can_edit": can(user, "write_assets"),
    }


def inventory_csv(db: Session, **filters: Any) -> str:
    """The filtered inventory as CSV with the same columns Import CSV reads, so a file can go back in."""
    import csv
    import io

    from app.asset_import import COLUMNS

    ids = [r["asset_id"] for r in inventory_page(db, everything=True, **filters)["rows"]]
    by_id = {a.asset_id: a for a in db.query(Asset).filter(Asset.asset_id.in_(ids)).all()} if ids else {}
    parents = {a.id: a.asset_id for a in db.query(Asset.id, Asset.asset_id).all()}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for asset_id in ids:
        a = by_id.get(asset_id)
        if a is None:
            continue
        extras = normalize_extras(a.extras)
        base = {"hostname": a.hostname, "ip": a.ip, "type": a.type, "environment": a.environment, "owner": a.owner,
                "contact_name": a.contact_name, "owner_email": a.owner_email, "owner_phone": a.owner_phone, "notes": a.notes,
                "monitoring_profile": a.monitoring_profile, "scrape_address": a.scrape_address, "asset_id": a.asset_id,
                "parent": parents.get(a.parent_id, "") if a.parent_id else ""}
        writer.writerow([base.get(c, extras.get(c, "")) or "" for c in COLUMNS])
    return "\ufeff" + out.getvalue()  # BOM: Excel opens UTF-8 with the right letters
