"""Core-side GLPI work: Computer / NetworkEquipment import.

Read-only toward GLPI (every call goes through app.glpi, which only allows
GET). Runs inside Core: Discovery button and the 6 h discovery loop (when
inventory.glpi.auto_sync is true). No sidecar container, no webhook.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from sqlalchemy.orm import Session

from app.demo_ids import is_lab_inventory_row
from app.exporter_detect import AUTO_ASSET_TYPE
from app.glpi import GlpiError, GlpiUnavailable, glpi_status, list_inventory
from app.journal import report
from app.models import Asset, JournalEntry
from app.settings import settings

log = logging.getLogger("forgesre")


def _loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip in {"::1", "0.0.0.0"}


def glpi_asset_id(name: str, glpi_id: str = "") -> str:
    """ForgeSRE asset_id from a GLPI item name (lowercase, a-z0-9-, ≤63)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:63].strip("-")
    if not slug:
        tail = re.sub(r"[^a-z0-9]", "", str(glpi_id).lower()) or "item"
        slug = f"glpi-{tail}"[:63].strip("-")
    return slug


def _unique_asset_id(db: Session, base: str) -> str:
    candidate = base
    n = 2
    while db.query(Asset.id).filter(Asset.asset_id == candidate).first() is not None:
        suffix = f"-{n}"
        candidate = f"{base[: 63 - len(suffix)].rstrip('-')}{suffix}"
        n += 1
    return candidate


def glpi_notes(item: dict[str, Any]) -> str:
    """Snapshot for a newly imported asset. Never rewritten on later syncs."""
    parts = []
    if item.get("itemtype"):
        parts.append(f"type={item['itemtype']}")
    if item.get("serial"):
        parts.append(f"serial={item['serial']}")
    if item.get("location"):
        parts.append(f"location={item['location']}")
    if item.get("entity"):
        parts.append(f"entity={item['entity']}")
    head = f"Imported from GLPI ({'; '.join(parts)})." if parts else "Imported from GLPI."
    return f"{head} Type is Auto and nothing is scraped yet — use Detect or Edit to pick the exporter."


def _item_extras(item: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    if item.get("location"):
        out["site"] = str(item["location"])[:255]
    if item.get("entity"):
        out["customer"] = str(item["entity"])[:255]
    return out


def sync_glpi(db: Session, *, force: bool = False) -> dict[str, Any]:
    """Computer / NetworkEquipment → assets. Never writes to GLPI.

    New item: type Auto, empty scrape, source=glpi, glpi_id set, no owner (or, with
    inventory.new_from_sources: review, a Discovery row instead). Existing asset is matched
    by GLPI id, serial, MAC, name, then IP (app.asset_sources); empty IP / site / customer /
    contract are filled, GLPI-owned fields follow GLPI until a person changes them, every other
    difference becomes a conflict to review. Owner, contact, email, phone and notes are never touched.
    """
    if not settings.glpi_enabled:
        return {"synced": 0, "skipped": True}
    started = time.monotonic()
    try:
        items = list_inventory(
            settings.glpi_url,
            settings.glpi_app_token,
            settings.glpi_user_token,
            timeout=settings.glpi_timeout,
            force=force,
        )
    except GlpiError as exc:
        detail = str(exc)
        summary = "GLPI sync failed: unreachable" if isinstance(exc, GlpiUnavailable) else "GLPI sync failed"
        log.warning("glpi sync failed: %s", detail)
        last = db.query(JournalEntry).filter_by(module="glpi", action="sync").order_by(JournalEntry.id.desc()).first()
        if not (last is not None and last.status == "error" and last.summary == summary and last.detail == detail):
            report(db, "glpi", "sync", "error", summary=summary, detail=detail)
        return {"synced": 0, "error": detail}

    from app.asset_sources import SourceRecord, build_index, hold_for_review, match, new_device_mode, reconcile

    index = build_index(db)
    review = new_device_mode() == "review"
    created = linked = skipped = held = conflicts = 0
    for item in items:
        glpi_id = str(item.get("glpi_id") or "").strip()
        ip = str(item.get("ip") or "").strip()
        name = str(item.get("name") or "").strip()
        rec = SourceRecord(source="glpi", source_id=glpi_id, name=name, ip="" if _loopback(ip) else ip,
                           serial=str(item.get("serial") or ""), fields=_item_extras(item))
        asset, disagreement = match(index, rec)
        if asset is not None and (getattr(asset, "glpi_id", "") or "").strip() and glpi_id and asset.glpi_id != glpi_id:
            # Same IP / name / serial as an asset already linked to another GLPI item: review, never merge.
            from app.asset_sources import open_conflict

            if open_conflict(db, None, rec, "match", detail=f"looks like {asset.asset_id}, which is already GLPI {asset.glpi_id}"):
                conflicts += 1
            skipped += 1
            continue
        if asset is None:
            if review and hold_for_review(db, rec, f"From GLPI: {name or glpi_id}"):
                held += 1
                continue
            asset = Asset(
                asset_id=_unique_asset_id(db, glpi_asset_id(name, glpi_id)),
                hostname=name,
                ip=rec.ip,
                type=AUTO_ASSET_TYPE,
                environment="Production",
                status="offline" if item.get("offline") else "unknown",
                monitoring_profile="",
                owner="",
                source="glpi",
                glpi_id=glpi_id,
                scrape_address="",
                notes=glpi_notes(item),
                extras={},
            )
            db.add(asset)
            db.flush()
            created += 1
        else:
            if not (asset.source or "").strip():
                asset.source = "glpi"
            linked += 1
        conflicts += reconcile(db, index, asset, rec, disagreement=disagreement)["conflicts"]
    db.commit()
    report(
        db,
        "glpi",
        "sync",
        "ok",
        summary=f"GLPI sync: {created} new, {linked} linked, {skipped} skipped"
                + (f", {held} waiting in Discovery" if held else "") + (f", {conflicts} to review" if conflicts else ""),
        detail=(
            "New items: type Auto, no scrape address, source=glpi. Owner, contact, email, phone, "
            "notes and filled extras on existing assets are never changed. Skipped = a second GLPI "
            "item with the IP/name of an asset already linked to another GLPI id."
        ),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return {"synced": created + linked, "created": created, "linked": linked, "skipped": skipped, "held": held, "conflicts": conflicts}


def last_sync_entry(db: Session) -> JournalEntry | None:
    return db.query(JournalEntry).filter_by(module="glpi", action="sync").order_by(JournalEntry.id.desc()).first()


def note_health(db: Session, status: dict[str, Any]) -> None:
    """One journal line when GLPI goes down (or comes back). Repeats are skipped."""
    if not status.get("configured"):
        return
    healthy = bool(status.get("ok"))
    last = db.query(JournalEntry).filter_by(module="glpi", action="health").order_by(JournalEntry.id.desc()).first()
    if healthy:
        if last is None or last.status == "ok":
            return
        report(db, "glpi", "health", "ok", summary="GLPI API reachable again", detail=str(status.get("why") or ""))
        return
    if last is not None and last.status == "warn":
        return
    report(
        db,
        "glpi",
        "health",
        "warn",
        summary=f"GLPI {str(status.get('label') or 'unreachable').lower()} — Core keeps working",
        detail=(
            f"{status.get('why') or ''} Incidents, mail and the UI are unaffected; "
            "GLPI calls back off for 2 minutes instead of retrying."
        ).strip(),
    )


def current_status(*, force: bool = False) -> dict[str, Any]:
    return glpi_status(
        settings.glpi_url,
        settings.glpi_app_token,
        settings.glpi_user_token,
        timeout=settings.glpi_timeout,
        force=force,
    )
