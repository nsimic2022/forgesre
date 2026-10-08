"""Incident history: 90-day lists, mail outbox, audit, operator notes.

Reads existing Postgres tables. Does not replace Incidents / Escalation / Journal.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session, joinedload

from app.audit import audit
from app.models import Asset, AuditLog, Incident, IncidentNote, Notification, utcnow

DEFAULT_DAYS = 90
MAX_DAYS = 3660
LIST_LIMIT = 200
PAGE_SIZE = 10
PAGE_SIZE_CHOICES = (10, 20, 50, 100)
MAX_PAGE_SIZE = PAGE_SIZE_CHOICES[-1]
NOTE_MAX = 4000


def parse_per_page(raw: Any, default: int = PAGE_SIZE) -> int:
    """Rows per page from ?per_page=. Junk or <1 → default; above 100 → 100; in-between snaps up to 10/20/50/100."""
    try:
        size = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if size < 1:
        return default
    for choice in PAGE_SIZE_CHOICES:
        if size <= choice:
            return choice
    return MAX_PAGE_SIZE


def per_page_param(page_param: str = "page") -> str:
    """Query key for the rows-per-page select that pairs with a page key (page → per_page, audit_page → audit_per_page)."""
    if page_param == "page":
        return "per_page"
    base = page_param[: -len("_page")] if page_param.endswith("_page") else page_param
    return f"{base}_per_page"


def parse_page(raw: Any, *, total: int, size: int = PAGE_SIZE) -> tuple[int, int, int]:
    """1-based page, page count, offset. Clamped to the last page when `raw` is past the end."""
    try:
        page = int(raw)
    except (TypeError, ValueError):
        page = 1
    size = max(1, int(size or PAGE_SIZE))
    total = max(0, int(total or 0))
    pages = max(1, (total + size - 1) // size) if total else 1
    page = max(1, min(page, pages))
    return page, pages, (page - 1) * size


def page_numbers(page: int, pages: int) -> list[int | None]:
    """Numbered tabs with None as a gap (render as …)."""
    pages = max(1, int(pages or 1))
    page = max(1, min(int(page or 1), pages))
    if pages <= 7:
        return list(range(1, pages + 1))
    keep = {1, pages, page}
    for delta in (1, 2):
        if 1 < page - delta:
            keep.add(page - delta)
        if page + delta < pages:
            keep.add(page + delta)
    ordered = sorted(keep)
    out: list[int | None] = []
    prev = 0
    for n in ordered:
        if prev and n - prev > 1:
            out.append(None)
        out.append(n)
        prev = n
    return out


def pager_state(
    raw: Any,
    *,
    total: int,
    size: Any = PAGE_SIZE,
    param: str = "page",
    fragment: str = "",
) -> dict[str, Any]:
    size = parse_per_page(size)
    page, pages, offset = parse_page(raw, total=total, size=size)
    start = (offset + 1) if total else 0
    end = min(offset + size, total) if total else 0
    return {
        "page": page,
        "pages": pages,
        "offset": offset,
        "total": total,
        "size": size,
        "default_size": PAGE_SIZE,
        "choices": PAGE_SIZE_CHOICES,
        "size_param": per_page_param(param),
        "show_size": total > PAGE_SIZE_CHOICES[0],
        "start": start,
        "end": end,
        "param": param,
        "hash": fragment,
        "numbers": page_numbers(page, pages),
        "has_prev": page > 1,
        "has_next": page < pages,
        "prev": page - 1,
        "next": page + 1,
    }


def paginate(
    items: list,
    raw: Any,
    *,
    size: Any = PAGE_SIZE,
    param: str = "page",
    fragment: str = "",
) -> tuple[list, dict[str, Any]]:
    total = len(items)
    state = pager_state(raw, total=total, size=size, param=param, fragment=fragment)
    return items[state["offset"] : state["offset"] + state["size"]], state


def paginate_query(
    query: Any,
    raw: Any,
    *,
    size: Any = PAGE_SIZE,
    param: str = "page",
    fragment: str = "",
) -> tuple[list, dict[str, Any]]:
    """Same pager as paginate(), but COUNT + LIMIT/OFFSET in SQL so off-page rows are never loaded."""
    total = query.order_by(None).count()
    state = pager_state(raw, total=total, size=size, param=param, fragment=fragment)
    return query.offset(state["offset"]).limit(state["size"]).all(), state


def clamp_days(raw: Any, default: int = DEFAULT_DAYS) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = default
    return max(1, min(days, MAX_DAYS))


def cutoff_since(days: int) -> datetime:
    return utcnow() - timedelta(days=days)


# Incidents list presets. Custom from/to dates are calendar days in the appliance timezone.
INCIDENT_WINDOWS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _filter_zone() -> ZoneInfo | timezone:
    from app.settings import settings

    try:
        return ZoneInfo(settings.timezone or "UTC")
    except Exception:
        return timezone.utc


def parse_filter_date(raw: str) -> date | None:
    text = (raw or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def local_day_start(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=_filter_zone()).astimezone(timezone.utc)


CRITICAL_SEVERITIES = ("CRITICAL", "CRIT", "FATAL", "EMERGENCY")
DONE_STATUSES = ("RESOLVED", "CLOSED")
ACTIVE_STATUSES = ("OPEN", "INVESTIGATING", "ESCALATED")
UNACKED_GROUP = "unacked"


def asset_pks_by_extras(db: Session, *, site: str = "", customer: str = "") -> list[int]:
    """Assets whose Site / Customer extras match exactly (case-insensitive), the same rule as the Assets filters."""
    from app.inventory import assets_matching

    return [row.id for row in assets_matching(db.query(Asset).all(), site=site, customer=customer)]


def incident_query(
    db: Session,
    *,
    days: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    status: str = "",
    open_only: bool = False,
    closed_only: bool = False,
    critical_only: bool = False,
    unacked_only: bool = False,
    site: str = "",
    customer: str = "",
    q: str = "",
):
    """Single filter used by the Incidents list and the Dashboard tiles (tile count = rows behind the click).

    ``since`` / ``until`` (until is exclusive) win over ``days``. Presets and custom ranges set those.
    ``q`` matches the incident number, title, or the host's id / hostname / IP.
    """
    query = db.query(Incident)
    if (site or "").strip() or (customer or "").strip():
        query = query.filter(Incident.asset_id.in_(asset_pks_by_extras(db, site=site, customer=customer)))
    if since is not None:
        query = query.filter(Incident.started_at >= since)
    elif days is not None:
        query = query.filter(Incident.started_at >= cutoff_since(clamp_days(days)))
    if until is not None:
        query = query.filter(Incident.started_at < until)
    if unacked_only:
        query = query.filter(Incident.status.in_(ACTIVE_STATUSES), Incident.ack_at.is_(None))
    if open_only:
        query = query.filter(Incident.status.notin_(DONE_STATUSES))
    if closed_only:
        query = query.filter(Incident.status.in_(DONE_STATUSES))
    if critical_only:
        query = query.filter(func.upper(Incident.severity).in_(CRITICAL_SEVERITIES))
    status = (status or "").strip().upper()
    if status:
        query = query.filter(Incident.status == status)
    needle_text = " ".join((q or "").split())
    if needle_text:
        needle = f"%{needle_text}%"
        query = query.filter(
            or_(
                Incident.number.ilike(needle),
                Incident.title.ilike(needle),
                exists().where(
                    Asset.id == Incident.asset_id,
                    or_(
                        Asset.asset_id.ilike(needle),
                        Asset.hostname.ilike(needle),
                        Asset.ip.ilike(needle),
                    ),
                ),
            )
        )
    return query


INCIDENT_STATUSES = ("OPEN", "INVESTIGATING", "ESCALATED", "RESOLVED", "CLOSED")


def incident_list_filters(
    *,
    status: str = "",
    severity: str = "",
    open_filter: str = "",
    days: str = "",
    window: str = "",
    started_from: str = "",
    started_to: str = "",
    site: str = "",
    customer: str = "",
    q: str = "",
) -> dict[str, Any]:
    """The /incidents filter from its query keys: incident_query kwargs, form state, and a canonical query string (empty for the default list).

    Site / Customer are the asset extras (Site / DC / room, Customer / domain) of the incident's host.
    ``q`` is the free-text search (host, problem title, or incident number).
    """
    status_raw = (status or "").strip()
    status_key = status_raw.upper()
    open_raw = (open_filter or "").strip().lower()
    critical_only = (severity or "").strip().lower() in {"critical", "crit"}
    open_only = False
    unacked_only = False
    exact = ""
    status_group = "all"
    if status_raw.lower() == "active" or (not status_raw and open_raw in {"1", "true", "yes"}):
        open_only = True
        status_group = "active"
    elif status_raw.lower() == UNACKED_GROUP:
        unacked_only = True
        status_group = UNACKED_GROUP
    elif status_key in INCIDENT_STATUSES:
        exact = status_key
        status_group = status_key
    days_raw = (days or "").strip()
    days_n = clamp_days(days_raw) if days_raw else None
    window_key = (window or "").strip().lower()
    if window_key not in INCIDENT_WINDOWS and window_key != "custom":
        window_key = ""
    from_date = parse_filter_date(started_from)
    to_date = parse_filter_date(started_to)
    if from_date and to_date and from_date > to_date:
        from_date, to_date = to_date, from_date
    since: datetime | None = None
    until: datetime | None = None
    active_window = ""
    if window_key in INCIDENT_WINDOWS:
        since = utcnow() - INCIDENT_WINDOWS[window_key]
        active_window = window_key
        days_n = None
    elif window_key == "custom" or from_date or to_date:
        active_window = "custom"
        days_n = None
        if from_date:
            since = local_day_start(from_date)
        if to_date:
            until = local_day_start(to_date + timedelta(days=1))
    site_value = " ".join((site or "").split())
    customer_value = " ".join((customer or "").split())
    q_value = " ".join((q or "").split())
    keep: list[tuple[str, str]] = []
    if status_group != "all":
        keep.append(("status", status_group))
    if critical_only:
        keep.append(("severity", "critical"))
    if active_window in INCIDENT_WINDOWS:
        keep.append(("window", active_window))
    elif active_window == "custom":
        keep.append(("window", "custom"))
        if from_date:
            keep.append(("from", from_date.isoformat()))
        if to_date:
            keep.append(("to", to_date.isoformat()))
    elif days_n is not None:
        keep.append(("days", str(days_n)))
    if site_value:
        keep.append(("site", site_value))
    if customer_value:
        keep.append(("customer", customer_value))
    if q_value:
        keep.append(("q", q_value))
    return {
        "query": {
            "days": days_n,
            "since": since,
            "until": until,
            "status": exact,
            "open_only": open_only,
            "critical_only": critical_only,
            "unacked_only": unacked_only,
            "site": site_value,
            "customer": customer_value,
            "q": q_value,
        },
        "status_group": status_group,
        "severity_group": "critical" if critical_only else "",
        "days": "" if active_window else days_raw,
        "window": active_window,
        "date_from": from_date.isoformat() if active_window == "custom" and from_date else "",
        "date_to": to_date.isoformat() if active_window == "custom" and to_date else "",
        "site": site_value,
        "customer": customer_value,
        "q": q_value,
        "qs": urlencode(keep),
    }


def incident_neighbors(db: Session, incident: Incident, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Older / newer incident in the /incidents order (newest first, Incident.id desc) under the same filter."""
    filters = filters or incident_list_filters()
    query = incident_query(db, **filters["query"])
    older = query.filter(Incident.id < incident.id).order_by(Incident.id.desc()).with_entities(Incident.number).first()
    newer = query.filter(Incident.id > incident.id).order_by(Incident.id.asc()).with_entities(Incident.number).first()
    member = query.filter(Incident.id == incident.id).count() > 0
    position = query.filter(Incident.id > incident.id).count() + 1 if member else None
    return {
        "older": older[0] if older else "",
        "newer": newer[0] if newer else "",
        "position": position,
        "total": query.count(),
        "qs": filters["qs"],
    }


def dashboard_incident_tiles(db: Session) -> list[dict[str, Any]]:
    """Tile label, count, CSS tone, and the exact /incidents link whose list has that many rows."""
    specs = [
        ("Open", "crit", {"unacked_only": True}, f"/incidents?status={UNACKED_GROUP}"),
        ("Critical", "crit", {"open_only": True, "critical_only": True}, "/incidents?status=active&severity=critical"),
        ("Investigating", "warn", {"status": "INVESTIGATING"}, "/incidents?status=INVESTIGATING"),
        ("Escalated", "warn", {"status": "ESCALATED"}, "/incidents?status=ESCALATED"),
        ("Resolved", "ok", {"status": "RESOLVED"}, "/incidents?status=RESOLVED"),
    ]
    tiles = []
    for label, tone, filters, href in specs:
        tiles.append(
            {
                "label": label,
                "tone": tone,
                "href": href,
                "count": incident_query(db, **filters).count(),
                "key": label.lower(),
            }
        )
    return tiles


def incident_heat(tiles: list[dict[str, Any]]) -> str:
    """Dashboard Incidents tile pulse: crit = any active critical, warn = other active work, "" = calm (no pulse)."""
    counts = {tile.get("key"): int(tile.get("count") or 0) for tile in tiles}
    if counts.get("critical"):
        return "crit"
    if counts.get("open") or counts.get("investigating") or counts.get("escalated"):
        return "warn"
    return ""


def list_history(
    db: Session,
    *,
    days: int | None = DEFAULT_DAYS,
    status: str = "",
    asset: str = "",
    number: str = "",
    limit: int = LIST_LIMIT,
    offset: int = 0,
    open_only: bool = False,
    closed_only: bool = False,
    critical_only: bool = False,
    unacked_only: bool = False,
    page: Any | None = None,
    site: str = "",
    customer: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
    q: str = "",
) -> tuple[list[Incident], int]:
    limit = max(1, min(int(limit or LIST_LIMIT), 500))
    query = incident_query(
        db,
        days=days,
        since=since,
        until=until,
        status=status,
        open_only=open_only,
        closed_only=closed_only,
        critical_only=critical_only,
        unacked_only=unacked_only,
        site=site,
        customer=customer,
        q=q,
    )
    number = (number or "").strip()
    if number:
        query = query.filter(Incident.number.ilike(f"%{number}%"))
    asset = (asset or "").strip()
    if asset:
        needle = f"%{asset}%"
        query = query.filter(
            exists().where(
                Asset.id == Incident.asset_id,
                or_(
                    Asset.asset_id.ilike(needle),
                    Asset.hostname.ilike(needle),
                    Asset.ip.ilike(needle),
                ),
            )
        )
    total = query.count()
    if page is not None:
        _, _, offset = parse_page(page, total=total, size=limit)
    else:
        offset = max(0, int(offset or 0))
    rows = query.options(joinedload(Incident.asset)).order_by(Incident.id.desc()).offset(offset).limit(limit).all()
    return rows, total


def notifications_for(db: Session, incident: Incident) -> list[Notification]:
    return (
        db.query(Notification)
        .filter(Notification.incident_id == incident.id)
        .order_by(Notification.id.asc())
        .all()
    )


def reported_to_for(db: Session, incidents: list[Incident]) -> dict[int, str]:
    """Unique incident-report recipients, in send order."""
    ids = [row.id for row in incidents if getattr(row, "id", None)]
    if not ids:
        return {}
    rows = (
        db.query(Notification.incident_id, Notification.target)
        .filter(Notification.incident_id.in_(ids), Notification.step_key == "incident-report")
        .order_by(Notification.id.asc())
        .all()
    )
    grouped: dict[int, list[str]] = {}
    for incident_id, target in rows:
        text = str(target or "").strip()
        if not text:
            continue
        bucket = grouped.setdefault(int(incident_id), [])
        if text not in bucket:
            bucket.append(text)
    return {key: ", ".join(values) for key, values in grouped.items()}


def audit_for(db: Session, number: str) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.object_type == "incident", AuditLog.object_id == number)
        .order_by(AuditLog.id.asc())
        .all()
    )


def notes_for(db: Session, incident: Incident) -> list[IncidentNote]:
    return (
        db.query(IncidentNote)
        .filter(IncidentNote.incident_id == incident.id)
        .order_by(IncidentNote.id.asc())
        .all()
    )


def add_note(db: Session, incident: Incident, actor: str, body: str) -> IncidentNote:
    text = (body or "").strip()[:NOTE_MAX]
    if not text:
        raise ValueError("note is empty")
    row = IncidentNote(incident_id=incident.id, actor=actor, body=text)
    db.add(row)
    audit(
        db,
        "incident.note",
        actor=actor,
        object_type="incident",
        object_id=incident.number,
        data={"chars": len(text)},
    )
    db.commit()
    db.refresh(row)
    return row


def ack_circle(incident: Incident) -> dict[str, str]:
    """History Ack column: green acked, yellow in-progress without ack, red not acked."""
    if incident.ack_at or (incident.ack_by or "").strip():
        return {"css": "green", "label": "Acknowledged"}
    if (incident.status or "").upper() in {"INVESTIGATING", "ESCALATED"}:
        return {"css": "yellow", "label": "Not acknowledged"}
    return {"css": "red", "label": "Not acknowledged"}


def acknowledge(incident: Incident, actor: str) -> None:
    """Acknowledge button: record ack_at (stops the mail ladder). OPEN → INVESTIGATING; other statuses stay."""
    if (incident.status or "").upper() == "OPEN":
        incident.status = "INVESTIGATING"
    apply_status_fields(incident, "INVESTIGATING", actor)


def apply_status_fields(incident: Incident, status: str, actor: str) -> None:
    """Ack / resolve timestamps. Safe to call from UI and API."""
    now = utcnow()
    status = status.upper()
    if status == "INVESTIGATING" and not incident.ack_at:
        incident.ack_at = now
        incident.ack_by = actor
    if status in {"RESOLVED", "CLOSED"}:
        incident.ended_at = now
        incident.resolved_at = now
        incident.resolved_by = actor


def notification_as_dict(row: Notification) -> dict[str, Any]:
    return {
        "id": row.id,
        "target": row.target,
        "subject": row.subject,
        "body": row.body,
        "status": row.status,
        "step_key": row.step_key,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "channel": row.channel,
    }


def audit_as_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "at": row.at.isoformat() if row.at else None,
        "actor": row.actor,
        "action": row.action,
        "data": row.data or {},
    }


def note_as_dict(row: IncidentNote) -> dict[str, Any]:
    return {
        "id": row.id,
        "at": row.at.isoformat() if row.at else None,
        "actor": row.actor,
        "body": row.body,
    }
