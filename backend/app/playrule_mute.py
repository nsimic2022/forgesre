"""Per-asset Custom alarm ON/OFF.

OFF is a row in asset_playrule_mutes (asset id + playrule id). The playrule stays
in assets.playrule_ids. Match skips that id on this host only, including when the
same rule would have been the global Rules match. No row means ON: the asset's
custom alarms are tried first, then Rules.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, object_session

from app.asset_extras import asset_playrule_ids, normalize_playrule_ids
from app.models import Asset, AssetPlayruleMute


def muted_playrule_ids(asset: Asset | None, db: Session | None = None) -> list[int]:
    """Playrule ids this asset has switched OFF. Empty when the asset is missing."""
    if asset is None or not getattr(asset, "id", None):
        return []
    session = db or object_session(asset)
    if session is None:
        return []
    rows = session.query(AssetPlayruleMute.playrule_id).filter_by(asset_id=asset.id).all()
    found = {int(pk) for (pk,) in rows}
    # Keep the asset's saved order so the form and the match loop agree.
    ordered = [pk for pk in asset_playrule_ids(asset) if pk in found]
    extras = sorted(found.difference(ordered))
    return ordered + extras


def is_playrule_muted(asset: Asset | None, playrule_id: int, db: Session | None = None) -> bool:
    return int(playrule_id) in set(muted_playrule_ids(asset, db))


def playrule_off_from_form(
    switches: str,
    posted_ids: list[str] | None,
    on_ids: list[str] | None,
) -> list[int] | None:
    """OFF ids from the Custom alarms column.

    None when the form did not include the ON/OFF controls (older posts stay ON).
    A rule added from the dropdown is not in posted_ids, so it stays ON.
    """
    if not str(switches or "").strip():
        return None
    ids = normalize_playrule_ids(posted_ids)
    on = set(normalize_playrule_ids(on_ids))
    return [pk for pk in ids if pk not in on]


def replace_playrule_mutes(db: Session, asset: Asset, off_ids: list | None) -> None:
    """Replace OFF rows. Ids that are not on this asset's custom-alarm list are dropped."""
    if asset.id is None:
        db.flush()
    wanted: list[int] = []
    allowed = set(asset_playrule_ids(asset))
    for pk in normalize_playrule_ids(off_ids):
        if pk in allowed and pk not in wanted:
            wanted.append(pk)
    wanted_set = set(wanted)
    existing = db.query(AssetPlayruleMute).filter_by(asset_id=asset.id).all()
    have: set[int] = set()
    for row in existing:
        if row.playrule_id not in wanted_set:
            db.delete(row)
        else:
            have.add(int(row.playrule_id))
    db.flush()
    for pk in wanted:
        if pk not in have:
            db.add(AssetPlayruleMute(asset_id=asset.id, playrule_id=pk))


def prune_playrule_mutes(db: Session, asset: Asset) -> None:
    """Drop OFF rows for playrules no longer on this asset. Remaining OFF stays OFF."""
    if asset.id is None:
        return
    keep = set(asset_playrule_ids(asset))
    for row in db.query(AssetPlayruleMute).filter_by(asset_id=asset.id).all():
        if row.playrule_id not in keep:
            db.delete(row)


def store_playrule_mutes(
    db: Session,
    asset: Asset,
    off_ids: list | None,
    *,
    cloned_from: str = "",
    prune: bool = False,
) -> None:
    """Apply a posted OFF list, copy it from a clone source, or prune removed rules."""
    if off_ids is not None:
        replace_playrule_mutes(db, asset, off_ids)
        return
    if cloned_from:
        source = db.query(Asset).filter(Asset.asset_id == cloned_from).first()
        if source is not None and source.id != asset.id:
            keep = set(asset_playrule_ids(asset))
            replace_playrule_mutes(
                db,
                asset,
                [pk for pk in muted_playrule_ids(source, db) if pk in keep],
            )
        return
    if prune:
        prune_playrule_mutes(db, asset)
