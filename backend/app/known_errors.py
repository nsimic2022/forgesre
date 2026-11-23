"""Saved list views (per person or shared with a team) and the known-errors knowledge base."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Incident, KnownError, SavedView, User
from app.security import can

VIEW_PAGES = ("incidents",)
VIEW_FILTERS = ("tab", "severity", "q", "team", "date_from", "date_to")
KE_STATUSES = ("open", "fixed")


# --- saved views -------------------------------------------------------------------------

def view_dict(row: SavedView, user: User) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "page": row.page, "filters": row.filters or {}, "team": row.team,
            "owner": row.owner, "mine": row.owner == user.email,
            "can_delete": row.owner == user.email or can(user, "write_play")}


def list_views(db: Session, user: User, page: str) -> list[dict[str, Any]]:
    """Your own views first, then views shared with a team, by team and name."""
    rows = (db.query(SavedView).filter(SavedView.page == page)
            .filter((SavedView.owner == user.email) | (SavedView.team != "")).all())
    rows.sort(key=lambda r: (r.owner != user.email, r.team.lower(), r.name.lower()))
    return [view_dict(r, user) for r in rows]


def save_view(db: Session, user: User, page: str, name: str, filters: dict[str, Any], team: str = "") -> SavedView:
    """Store one view. Personal views need Analyst; sharing with a team needs Engineer."""
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise ValueError("name is required")
    if page not in VIEW_PAGES:
        raise ValueError("unknown page")
    team = str(team or "").strip()[:255]
    if team and not can(user, "write_play"):
        raise PermissionError("sharing a view with a team needs the Engineer role")
    clean = {k: str(v)[:200] for k, v in (filters or {}).items() if k in VIEW_FILTERS and str(v or "").strip()}
    row = SavedView(page=page, name=name, filters=clean, owner=user.email, team=team)
    db.add(row)
    db.commit()
    return row


def delete_view(db: Session, user: User, view_id: int) -> None:
    row = db.get(SavedView, view_id)
    if row is None:
        raise LookupError("view not found")
    if row.owner != user.email and not can(user, "write_play"):
        raise PermissionError("only the owner or an Engineer can delete this view")
    db.delete(row)
    db.commit()


# --- known errors ------------------------------------------------------------------------

def ke_dict(row: KnownError) -> dict[str, Any]:
    return {"id": row.id, "title": row.title, "alertname": row.alertname, "pattern": row.pattern, "symptoms": row.symptoms,
            "workaround": row.workaround, "fix": row.fix, "link": row.link, "status": row.status, "created_by": row.created_by,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def save_known_error(db: Session, data: dict[str, Any], actor: str, ke_id: int | None = None) -> KnownError:
    """Create or update one entry; it needs a title and at least an alertname or a title pattern."""
    title = " ".join(str(data.get("title") or "").split())[:200]
    alertname = str(data.get("alertname") or "").strip()[:200]
    pattern = str(data.get("pattern") or "").strip()[:200]
    if not title:
        raise ValueError("title is required")
    if not alertname and not pattern:
        raise ValueError("set an alertname or a text the incident title contains, or it never matches")
    link = str(data.get("link") or "").strip()[:500]
    if link and not link.lower().startswith(("http://", "https://")):
        raise ValueError("link must start with http:// or https://")
    status = str(data.get("status") or "open").strip().lower()
    if status not in KE_STATUSES:
        raise ValueError("status is open or fixed")
    row = db.get(KnownError, ke_id) if ke_id else KnownError(created_by=actor)
    if row is None:
        raise LookupError("known error not found")
    row.title, row.alertname, row.pattern, row.link, row.status = title, alertname, pattern, link, status
    row.symptoms = str(data.get("symptoms") or "")[:4000]
    row.workaround = str(data.get("workaround") or "")[:4000]
    row.fix = str(data.get("fix") or "")[:4000]
    db.add(row)
    db.commit()
    return row


def matches(row: KnownError, alertname: str, title: str) -> bool:
    """Alertname equal (case-insensitive) and/or pattern contained in the title; both when both are set."""
    ok_name = not row.alertname or row.alertname.lower() == (alertname or "").lower()
    ok_text = not row.pattern or row.pattern.lower() in (title or "").lower()
    return bool(row.alertname or row.pattern) and ok_name and ok_text


def known_errors_for(db: Session, incident: Incident) -> list[dict[str, Any]]:
    """Known errors that match this incident, open ones first."""
    from app.services import incident_alertname

    name = incident_alertname(incident)
    query = db.query(KnownError)
    if name:
        query = query.filter((KnownError.alertname == "") | (func.lower(KnownError.alertname) == name.lower()))
    else:
        query = query.filter(KnownError.alertname == "")
    hits = [r for r in query.all() if matches(r, name, incident.title)]
    hits.sort(key=lambda r: (r.status != "open", r.title.lower()))
    return [ke_dict(r) for r in hits[:5]]


def search_known_errors(db: Session, text: str = "", limit: int = 100) -> list[dict[str, Any]]:
    """All entries, or the ones whose title, alertname, pattern, symptoms or fix mention ``text``."""
    query = db.query(KnownError)
    needle = str(text or "").strip()
    if needle:
        like = f"%{needle}%"
        query = query.filter(KnownError.title.ilike(like) | KnownError.alertname.ilike(like) | KnownError.pattern.ilike(like)
                             | KnownError.symptoms.ilike(like) | KnownError.fix.ilike(like) | KnownError.workaround.ilike(like))
    return [ke_dict(r) for r in query.order_by(KnownError.status, KnownError.title).limit(max(1, min(limit, 500))).all()]
