"""Incidents pages of the new UI: the paged list and the detail view.

The detail view gathers everything the page draws in one call: ForgeRCA facts and
hypotheses, the ForgeAI opinion, playbook checklist, notifications, notes, timeline and
the asset card. Writes (acknowledge, resolve, notes) keep using the existing endpoints.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.audit import audit
from app.history import audit_as_dict, audit_for, note_as_dict, notes_for, notification_as_dict, notifications_for
from app.ladder_cubes import incident_ladder_cubes
from app.models import Asset, Incident, User
from app.noc import ACTIVE, RESOLVED, _level, _llm_jobs, _row, _sent_counts, forgeai_mode
from app.security import can

TABS = ("active", "acknowledged", "resolved", "all")


def _tab_filter(query, tab: str):
    if tab == "active":
        return query.filter(Incident.status.in_(ACTIVE), Incident.ack_at.is_(None))
    if tab == "acknowledged":
        return query.filter(Incident.status.in_(ACTIVE), Incident.ack_at.is_not(None))
    if tab == "resolved":
        return query.filter(Incident.status.in_(RESOLVED))
    return query


def _local_day(day: str, *, end: bool = False):
    """Start of ``day`` (YYYY-MM-DD) in the appliance time zone, as UTC; ``end`` = start of the next day."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from app.settings import settings

    try:
        zone = ZoneInfo(settings.timezone or "UTC")
    except Exception:
        zone = ZoneInfo("UTC")
    start = datetime.strptime(day.strip(), "%Y-%m-%d").replace(tzinfo=zone)
    return (start + timedelta(days=1) if end else start).astimezone(timezone.utc)


def filtered_incidents(db: Session, severity: str = "", q: str = "", date_from: str = "", date_to: str = ""):
    """Incident query with the list filters: severity, text (number, title, host, IP) and a day range."""
    base = db.query(Incident).outerjoin(Asset, Incident.asset_id == Asset.id)
    if date_from:
        base = base.filter(Incident.started_at >= _local_day(date_from))
    if date_to:
        base = base.filter(Incident.started_at < _local_day(date_to, end=True))
    if severity:
        base = base.filter(Incident.severity.ilike(severity))
    text = q.strip()
    if text:
        like = f"%{text}%"
        base = base.filter(
            or_(Incident.number.ilike(like), Incident.title.ilike(like), Asset.hostname.ilike(like), Asset.ip.ilike(like))
        )
    return base


def incidents_page(
    db: Session, tab: str = "active", severity: str = "", q: str = "", limit: int = 50, offset: int = 0,
    date_from: str = "", date_to: str = "",
) -> dict[str, Any]:
    """One page of the incident list plus the count of every tab (with the same filters)."""
    tab = tab if tab in TABS else "active"
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    base = filtered_incidents(db, severity, q, date_from, date_to)
    counts = {name: _tab_filter(base, name).count() for name in TABS}
    rows = (
        _tab_filter(base, tab)
        .options(joinedload(Incident.asset), selectinload(Incident.investigations))
        .order_by(Incident.started_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    sent = _sent_counts(db, [r.id for r in rows])
    jobs = _llm_jobs(db)
    mode = forgeai_mode()
    return {
        "tab": tab,
        "counts": counts,
        "total": counts[tab],
        "offset": offset,
        "limit": limit,
        "rows": [_row(r, sent, jobs, mode, db) for r in rows],
    }


def incidents_csv(db: Session, tab: str = "all", **filters: str) -> str:
    """The filtered list as CSV with a UTF-8 BOM (Excel opens it with the right letters), newest first, max 10 000."""
    import csv
    import io

    rows = _tab_filter(filtered_incidents(db, **filters), tab if tab in TABS else "all").options(joinedload(Incident.asset)).order_by(Incident.started_at.desc()).limit(10000).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["number", "severity", "status", "title", "asset", "ip", "source", "started_at", "ack_at", "ack_by", "resolved_at", "resolved_by", "root_cause"])
    for i in rows:
        writer.writerow([
            i.number, (i.severity or "").upper(), i.status, i.title, i.asset.hostname if i.asset else "", i.asset.ip if i.asset else "",
            i.source or "", i.started_at.isoformat() if i.started_at else "", i.ack_at.isoformat() if i.ack_at else "", i.ack_by or "",
            i.resolved_at.isoformat() if i.resolved_at else "", i.resolved_by or "", i.root_cause or "",
        ])
    return "\ufeff" + out.getvalue()


def _split_investigations(incident: Incident) -> tuple[Any, Any]:
    """Latest ForgeRCA (rules) run and latest ForgeAI (LLM) run, either may be None."""
    rca = ai = None
    for inv in incident.investigations or []:
        if (inv.provider or "").endswith("-llm"):
            ai = inv
        else:
            rca = inv
    return rca, ai


def _playbook_steps(incident: Incident) -> list[str]:
    steps = []
    for step in (incident.playbook.steps if incident.playbook else None) or []:
        title = str(step.get("title") if isinstance(step, dict) else step).strip()
        if title:
            steps.append(title)
    return steps


def incident_view(db: Session, incident: Incident, user: User) -> dict[str, Any]:
    from app.changes import changes_before

    """Everything the incident detail page shows. Evidence queries only for read_evidence."""
    from app.api import _asset

    rca, ai = _split_investigations(incident)
    result = dict(rca.result or {}) if rca else {}
    see_evidence = can(user, "read_evidence")
    cubes = incident_ladder_cubes(incident, db)
    mode = forgeai_mode()
    steps = _playbook_steps(incident)
    done = sorted({int(i) for i in (incident.checklist or []) if isinstance(i, int) and 0 <= i < len(steps)})
    notifications = [notification_as_dict(row) for row in notifications_for(db, incident)]
    notes = [note_as_dict(row) for row in notes_for(db, incident)]
    audits = [audit_as_dict(row) for row in audit_for(db, incident.number)]
    return {
        "number": incident.number,
        "title": incident.title,
        "severity": (incident.severity or "WARNING").upper(),
        "status": incident.status,
        "source": incident.source or "",
        "summary": incident.summary or "",
        "started_at": incident.started_at.isoformat() if incident.started_at else None,
        "ack_at": incident.ack_at.isoformat() if incident.ack_at else None,
        "ack_by": incident.ack_by or "",
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        "resolved_by": incident.resolved_by or "",
        "level": _level(cubes),
        "levels": cubes,
        "asset": _asset(incident.asset) if incident.asset else None,
        "playrule": incident.playrule.name if incident.playrule else None,
        "playbook": {"name": incident.playbook.name, "steps": steps, "done": done} if incident.playbook else None,
        "forgerca": {
            "cause": rca.likely_cause if rca else "",
            "summary": rca.summary if rca else "",
            "confidence": rca.confidence if rca else 0,
            "facts": result.get("facts") or [],
            "hypotheses": result.get("hypotheses") or [],
            "actions": [a.get("text") for a in result.get("recommended_actions") or [] if isinstance(a, dict)],
            "limitations": result.get("limitations") or [],
            "at": rca.created_at.isoformat() if rca and rca.created_at else None,
        }
        if rca
        else None,
        "forgeai": {
            "mode": mode,
            "text": ai.summary if ai else "",
            "cause": ai.likely_cause if ai else "",
            "action": ai.recommended_action if ai else "",
            "model": ai.model if ai else "",
            "at": ai.created_at.isoformat() if ai and ai.created_at else None,
        },
        "evidence": [
            {"id": ev.evidence_id, "source": ev.source or ev.kind, "title": ev.title, "query": ev.query}
            for ev in incident.evidence or []
        ]
        if see_evidence
        else [],
        "timeline": incident.timeline or [],
        "changes": changes_before(db, incident),
        "notifications": notifications,
        "notes": notes,
        "audit": audits,
        "events": events(incident, notifications, notes, audits),
        "who_to_call": who_to_call(db, incident),
        "similar": similar_before(db, incident),
        "review": {
            "root_cause": incident.root_cause or "",
            "rca_rating": incident.rca_rating or "",
            "ai_rating": incident.ai_rating or "",
            "reviewed_by": incident.reviewed_by or "",
            "reviewed_at": incident.reviewed_at.isoformat() if incident.reviewed_at else None,
        },
        "can": {
            "work": can(user, "write_incidents"),
            "investigate": can(user, "investigate"),
            "cloud": can(user, "ai_cloud"),
        },
    }


def who_to_call(db: Session, incident: Incident) -> list[dict[str, str]]:
    """People to ring for this incident, in order: on call now, the asset contact, its backup, support."""
    from app.asset_extras import normalize_extras, support_status
    from app.dc_noc import current_oncall

    out: list[dict[str, str]] = []
    person = current_oncall(db) or {}
    if person.get("name") or person.get("email"):
        out.append({"role": "On call now", "name": str(person.get("name") or ""), "phone": str(person.get("phone") or ""), "email": str(person.get("email") or "")})
    asset = incident.asset
    if asset is not None:
        if asset.contact_name or asset.owner_phone or asset.owner_email:
            out.append({"role": f"Asset contact{f' · {asset.owner}' if asset.owner else ''}", "name": asset.contact_name or "", "phone": asset.owner_phone or "", "email": asset.owner_email or ""})
        extras = normalize_extras(asset.extras)
        if extras.get("backup_name") or extras.get("backup_phone"):
            out.append({"role": "Backup contact", "name": extras.get("backup_name", ""), "phone": extras.get("backup_phone", ""), "email": extras.get("backup_email", "")})
        support = support_status(asset)
        if support.get("state") != "unknown":
            out.append({"role": "Vendor support", "name": str(support.get("label") or ""), "phone": "", "email": "", "note": str(support.get("call_note") or "")})
    return out


def similar_before(db: Session, incident: Incident, limit: int = 5) -> list[dict[str, Any]]:
    """Earlier incidents with the same alert on the same asset, newest first, with what fixed them."""
    if incident.asset_id is None:
        return []
    rows = (
        db.query(Incident)
        .filter(Incident.asset_id == incident.asset_id, Incident.id != incident.id, Incident.title == incident.title)
        .order_by(Incident.started_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for row in rows:
        end = row.resolved_at or row.ended_at
        minutes = int((end - row.started_at).total_seconds() // 60) if end and row.started_at else None
        out.append({
            "number": row.number, "status": row.status, "started_at": row.started_at.isoformat() if row.started_at else None,
            "minutes": minutes, "root_cause": row.root_cause or "", "resolved_by": row.resolved_by or "",
        })
    return out


_AUDIT_TEXT = {
    "incident.checklist": lambda d: f"{'ticked' if d.get('done') else 'unticked'} playbook step: {d.get('title', '')}",
    "incident.review": lambda d: "saved the review" + (f": {d.get('root_cause')}" if d.get("root_cause") else ""),
}


def events(incident: Incident, notifications: list[dict], notes: list[dict], audits: list[dict]) -> list[dict[str, str]]:
    """One time-ordered story of the incident: alarm, mails and chat, ack, escalation, notes, playbook, resolve."""
    rows: list[dict[str, str]] = []
    if incident.started_at:
        rows.append({"at": incident.started_at.isoformat(), "kind": "alarm", "who": incident.source or "alarm", "text": f"Alarm fired: {incident.title}"})
    for item in incident.timeline or []:
        title = str(item.get("title") or "")
        if title and item.get("at"):
            rows.append({"at": str(item["at"]), "kind": "system", "who": "ForgeSRE", "text": f"{title}{': ' + str(item.get('detail')) if item.get('detail') else ''}"[:300]})
    for n in notifications:
        status = n.get("status") or ""
        rows.append({"at": n.get("created_at") or "", "kind": "notify", "who": n.get("channel") or "email",
                     "text": f"{n.get('channel') or 'email'} to {n.get('target')} · {status}{' · ' + n['error'] if n.get('error') else ''}"})
    for note in notes:
        rows.append({"at": note.get("at") or "", "kind": "note", "who": note.get("actor") or "", "text": note.get("body") or ""})
    for a in audits:
        render = _AUDIT_TEXT.get(a.get("action") or "")
        if render is not None:
            rows.append({"at": a.get("at") or "", "kind": "work", "who": a.get("actor") or "", "text": render(a.get("data") or {})})
    if incident.ack_at:
        rows.append({"at": incident.ack_at.isoformat(), "kind": "ack", "who": incident.ack_by or "", "text": "Acknowledged"})
    if incident.resolved_at:
        rows.append({"at": incident.resolved_at.isoformat(), "kind": "resolve", "who": incident.resolved_by or "system", "text": "Resolved"})
    return sorted((r for r in rows if r["at"]), key=lambda r: r["at"])


def set_checklist_step(db: Session, incident: Incident, step: int, done: bool, actor: str) -> list[int]:
    """Tick or untick one playbook step for this incident; audited. Returns the done list."""
    steps = _playbook_steps(incident)
    if not 0 <= step < len(steps):
        raise ValueError("no such step")
    current = {int(i) for i in (incident.checklist or []) if isinstance(i, int)}
    current.add(step) if done else current.discard(step)
    incident.checklist = sorted(current)
    audit(
        db,
        "incident.checklist",
        actor=actor,
        object_type="incident",
        object_id=incident.number,
        data={"step": step, "title": steps[step], "done": done},
        commit=True,
    )
    return incident.checklist
