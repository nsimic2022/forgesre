"""Maintenance windows from the new UI. A window lives on the asset (extras maint_from / maint_until)
and is mirrored into maintenance_windows, exactly like the asset form.

While a window is open, alarms on that asset still open incidents but no mail or chat goes out.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.audit import audit
from app.dc_noc import MAINT_FROM, MAINT_UNTIL, maint_pair_error, parse_when, sync_asset_maintenance
from app.models import Asset


def find_assets(db: Session, refs: list[str]) -> tuple[list[Asset], list[str]]:
    """Assets by hostname, asset id or IP. Returns (found, refs that matched nothing)."""
    found: list[Asset] = []
    missing: list[str] = []
    for ref in [str(r).strip() for r in refs if str(r).strip()]:
        row = db.query(Asset).filter(or_(Asset.hostname == ref, Asset.asset_id == ref, Asset.ip == ref)).first()
        if row is None:
            missing.append(ref)
        elif row not in found:
            found.append(row)
    return found, missing


def set_window(db: Session, refs: list[str], starts_at: str, ends_at: str, actor: str) -> list[Asset]:
    """Put every asset in maintenance from ``starts_at`` to ``ends_at`` (ISO or datetime-local, appliance time).

    All or nothing: an unknown asset or a bad time pair raises ValueError and nothing is saved.
    """
    if not starts_at or not ends_at:
        raise ValueError("Maintenance needs both from and until.")
    problem = maint_pair_error(starts_at, ends_at)
    if problem:
        raise ValueError(problem)
    assets, missing = find_assets(db, refs)
    if missing:
        raise ValueError("No asset matches: " + ", ".join(missing))
    if not assets:
        raise ValueError("Pick at least one asset.")
    start, end = parse_when(starts_at), parse_when(ends_at)
    for asset in assets:
        extras = dict(asset.extras or {})
        extras[MAINT_FROM] = start.isoformat()
        extras[MAINT_UNTIL] = end.isoformat()
        asset.extras = extras
        sync_asset_maintenance(db, asset)
        audit(db, "maintenance.set", actor=actor, object_type="asset", object_id=asset.asset_id,
              data={"from": start.isoformat(), "until": end.isoformat()})
    db.commit()
    return assets


def clear_window(db: Session, asset_id: str, actor: str) -> dict[str, Any]:
    """End maintenance now for one asset: alarms notify again from the next one."""
    asset = db.query(Asset).filter_by(asset_id=asset_id).first()
    if asset is None:
        raise LookupError(asset_id)
    extras = {k: v for k, v in (asset.extras or {}).items() if k not in (MAINT_FROM, MAINT_UNTIL)}
    asset.extras = extras
    sync_asset_maintenance(db, asset)
    audit(db, "maintenance.clear", actor=actor, object_type="asset", object_id=asset.asset_id)
    db.commit()
    return {"asset_id": asset.asset_id, "cleared": True}
