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


def _fill_extras(asset: Asset, item: dict[str, Any]) -> bool:
    proposed = _item_extras(item)
    if not proposed:
        return False
    current = dict(asset.extras or {}) if isinstance(asset.extras, dict) else {}
    changed = False
    for key, value in proposed.items():
        if not str(current.get(key) or "").strip():
            current[key] = value
            changed = True
    if changed:
        asset.extras = current
    return changed


def sync_glpi(db: Session, *, force: bool = False) -> dict[str, Any]:
    """Computer / NetworkEquipment → assets. Never writes to GLPI.

    New item: type Auto, empty scrape, source=glpi, glpi_id set, no owner.
    Existing asset (matched by glpi_id, then IP, then hostname): only glpi_id,
    an empty IP, and empty customer/site extras are filled — owner, contact,
    email, phone, notes and other extras are never touched.
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

    rows = db.query(Asset).order_by(Asset.id).all()
    by_glpi: dict[str, Asset] = {}
    by_ip: dict[str, Asset] = {}
    by_name: dict[str, Asset] = {}
    for asset in rows:
        if is_lab_inventory_row(asset):
            continue
        if (getattr(asset, "glpi_id", "") or "").strip():
            by_glpi.setdefault(asset.glpi_id.strip(), asset)
        ip = (asset.ip or "").strip()
        if ip and not _loopback(ip):
            by_ip.setdefault(ip, asset)
        for key in (asset.hostname, asset.asset_id):
            if (key or "").strip():
                by_name.setdefault(key.strip().lower(), asset)

    created = linked = skipped = 0
    for item in items:
        glpi_id = str(item.get("glpi_id") or "").strip()
        ip = str(item.get("ip") or "").strip()
        name = str(item.get("name") or "").strip()
        asset = by_glpi.get(glpi_id) if glpi_id else None
        if asset is None and ip and not _loopback(ip):
            asset = by_ip.get(ip)
        if asset is None:
            for key in (name, glpi_asset_id(name, glpi_id)):
                asset = by_name.get(str(key or "").strip().lower())
                if asset is not None:
                    break
        if asset is not None and (getattr(asset, "glpi_id", "") or "").strip() and asset.glpi_id != glpi_id:
            skipped += 1
            continue
        if asset is None:
            asset = Asset(
                asset_id=_unique_asset_id(db, glpi_asset_id(name, glpi_id)),
                hostname=name,
                ip=ip,
                type=AUTO_ASSET_TYPE,
                environment="Production",
                status="offline" if item.get("offline") else "unknown",
                monitoring_profile="",
                owner="",
                source="glpi",
                glpi_id=glpi_id,
                scrape_address="",
                notes=glpi_notes(item),
                extras=_item_extras(item),
            )
            db.add(asset)
            db.flush()
            created += 1
        else:
            if glpi_id and not (getattr(asset, "glpi_id", "") or "").strip():
                asset.glpi_id = glpi_id
            if ip and not (asset.ip or "").strip():
                asset.ip = ip
            if not (asset.source or "").strip():
                asset.source = "glpi"
            _fill_extras(asset, item)
            linked += 1
        if glpi_id:
            by_glpi[glpi_id] = asset
        if ip and not _loopback(ip):
            by_ip.setdefault(ip, asset)
        if name:
            by_name.setdefault(name.lower(), asset)
    db.commit()
    report(
        db,
        "glpi",
        "sync",
        "ok",
        summary=f"GLPI sync: {created} new, {linked} linked, {skipped} skipped",
        detail=(
            "New items: type Auto, no scrape address, source=glpi. Owner, contact, email, phone, "
            "notes and filled extras on existing assets are never changed. Skipped = a second GLPI "
            "item with the IP/name of an asset already linked to another GLPI id."
        ),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return {"synced": created + linked, "created": created, "linked": linked, "skipped": skipped}


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
