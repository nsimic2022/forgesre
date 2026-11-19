"""What changed before an incident.

Two kinds of change are merged on the incident page:

- ChangeEvent rows: deploys, config pushes and change tickets posted by CI/CD, scripts or
  GLPI to POST /api/v1/changes, or typed in by an Engineer.
- ForgeSRE's own audit log: edits to the asset, its parent, maintenance, rules, playbooks,
  escalation and noise filters, i.e. what people changed here.

"What changed?" is the first question on most incidents; showing it next to the alarm saves
a search through chat and tickets.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Asset, AuditLog, ChangeEvent, Incident, utcnow

WINDOW_HOURS = 24
KINDS = ("deploy", "config", "change", "ticket", "maintenance", "other")
# Audit actions that change how alarms are handled, shown even without an asset.
GLOBAL_AUDIT = ("playrule.", "playbook.", "escalation.", "noise_filter.", "noise.", "settings.")
ASSET_AUDIT = ("asset.", "maintenance.", "assets.")


def _aware(value: datetime) -> datetime:
    from datetime import timezone

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def record(db: Session, *, title: str, source: str = "manual", kind: str = "change", detail: str = "", url: str = "",
           actor: str = "", asset_ref: str = "", at: datetime | None = None) -> ChangeEvent:
    """Store one change. ``asset_ref`` is a hostname, asset id or IP; unknown ones keep the change global."""
    title = " ".join(str(title or "").split())[:255]
    if not title:
        raise ValueError("title is required")
    kind = kind if kind in KINDS else "other"
    if url and not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    asset = None
    ref = str(asset_ref or "").strip()
    if ref:
        asset = db.query(Asset).filter(or_(Asset.hostname == ref, Asset.asset_id == ref, Asset.ip == ref)).first()
        if asset is None:
            detail = (f"asset: {ref}\n" + (detail or "")).strip()
    row = ChangeEvent(title=title, source=str(source or "manual")[:32], kind=kind, detail=str(detail or "")[:4000],
                      url=str(url or "")[:500], actor=str(actor or "")[:255], asset_id=asset.id if asset else None,
                      at=_aware(at) if at else utcnow())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _event_row(row: ChangeEvent, start: datetime, hostnames: dict[int, str]) -> dict[str, Any]:
    return {
        "at": _aware(row.at).isoformat(), "before_min": int((start - _aware(row.at)).total_seconds() // 60),
        "source": row.source, "kind": row.kind, "title": row.title, "detail": row.detail, "url": row.url,
        "actor": row.actor, "asset": hostnames.get(row.asset_id or 0, ""),
    }


def _audit_title(row: AuditLog) -> str:
    action = row.action.replace("_", " ").replace(".", " ")
    return f"{action} {row.object_id}".strip()


def changes_before(db: Session, incident: Incident, hours: int = WINDOW_HOURS, limit: int = 20) -> list[dict[str, Any]]:
    """Changes on this asset, its parent, or global ones, in the ``hours`` before the incident (newest first)."""
    start = _aware(incident.started_at or utcnow())
    since = start - timedelta(hours=hours)
    asset = incident.asset
    ids = [a for a in (asset.id if asset else None, asset.parent_id if asset else None) if a]
    hostnames = {a.id: a.hostname for a in db.query(Asset).filter(Asset.id.in_(ids)).all()} if ids else {}
    events = (
        db.query(ChangeEvent)
        .filter(ChangeEvent.at >= since, ChangeEvent.at <= start + timedelta(minutes=1),
                or_(ChangeEvent.asset_id.is_(None), ChangeEvent.asset_id.in_(ids or [-1])))
        .order_by(ChangeEvent.at.desc()).limit(limit).all()
    )
    out = [_event_row(e, start, hostnames) for e in events]
    refs = [r for r in ((asset.asset_id if asset else ""), (asset.parent.asset_id if asset and asset.parent else "")) if r]
    audits = (
        db.query(AuditLog)
        .filter(AuditLog.at >= since, AuditLog.at <= start + timedelta(minutes=1))
        .order_by(AuditLog.at.desc()).limit(400).all()
    )
    for row in audits:
        on_asset = row.object_id in refs and row.action.startswith(ASSET_AUDIT)
        global_rule = row.action.startswith(GLOBAL_AUDIT)
        if not (on_asset or global_rule):
            continue
        out.append({
            "at": _aware(row.at).isoformat(), "before_min": int((start - _aware(row.at)).total_seconds() // 60),
            "source": "forgesre", "kind": "config", "title": _audit_title(row), "detail": "", "url": "",
            "actor": row.actor, "asset": row.object_id if on_asset else "",
        })
    out.sort(key=lambda r: r["at"], reverse=True)
    return out[:limit]


def recent(db: Session, limit: int = 50) -> list[dict[str, Any]]:
    """Latest posted changes for the Changes list."""
    rows = db.query(ChangeEvent).order_by(ChangeEvent.at.desc()).limit(limit).all()
    names = {a.id: a.hostname for a in db.query(Asset).filter(Asset.id.in_([r.asset_id for r in rows if r.asset_id] or [-1])).all()}
    now = utcnow()
    return [{**_event_row(r, now, names), "id": r.id} for r in rows]
