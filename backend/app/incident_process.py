"""How people run incidents: major incidents, SLA timers, shift handover, follow-up actions.

- Major incident: a commander, a bridge call link and status updates on a timer; each update goes
  to the incident's notes and to the chat channels, and the stakeholder message is ready to copy.
- SLA: response (acknowledge) and resolve targets per severity from ``sla.*``; shown as due,
  met or breached. Display and reports only: SLA does not change escalation.
- Handover: the outgoing shift writes notes, ForgeSRE snapshots the open incidents, the next
  person presses "I take over" (it cannot be the same person).
- Follow-up actions: what must change after an incident, with an owner and a due date.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import FollowUpAction, Incident, ShiftHandover, utcnow

SEVERITIES = ("critical", "high", "warning")
SLA_DEFAULTS = {"response": {"critical": 15, "high": 30, "warning": 120}, "resolve": {"critical": 240, "high": 480, "warning": 1440}}
TEXT_MAX = 2000


def _aware(at: datetime | None) -> datetime | None:
    if at is None:
        return None
    return at if at.tzinfo else at.replace(tzinfo=utcnow().tzinfo)


def _short(number: str) -> str:
    return (number or "").split("_")[0]


# ---- Major incident -------------------------------------------------------------------------

def next_update_due(incident: Incident) -> datetime | None:
    if not incident.major:
        return None
    base = _aware(incident.last_update_at) or _aware(incident.started_at) or utcnow()
    return base + timedelta(minutes=max(5, int(incident.update_minutes or 30)))


def stakeholder_text(incident: Incident) -> str:
    """The message a commander sends to management and other teams, filled in from the incident."""
    host = incident.asset.hostname if incident.asset is not None else "several systems"
    started = _aware(incident.started_at)
    mins = int((utcnow() - started).total_seconds() // 60) if started else 0
    due = next_update_due(incident)
    lines = [
        f"[MAJOR INCIDENT] {_short(incident.number)}: {incident.title}",
        f"Impact: {(incident.severity or '').title()} on {host}.",
        f"Started: {started.strftime('%H:%M UTC') if started else '?'} ({mins} min ago). Status: {incident.status}.",
        f"Incident commander: {incident.commander or 'not named yet'}.",
    ]
    if incident.bridge_url:
        lines.append(f"Bridge call: {incident.bridge_url}")
    if due:
        lines.append(f"Next update by {due.strftime('%H:%M UTC')}.")
    return "\n".join(lines)


def major_view(incident: Incident) -> dict[str, Any]:
    due = next_update_due(incident)
    return {"on": bool(incident.major), "commander": incident.commander or "", "bridge_url": incident.bridge_url or "",
            "update_minutes": int(incident.update_minutes or 30),
            "last_update_at": _aware(incident.last_update_at).isoformat() if incident.last_update_at else None,
            "next_due": due.isoformat() if due else None, "overdue": bool(due and due < utcnow()),
            "message": stakeholder_text(incident) if incident.major else ""}


def set_major(db: Session, incident: Incident, actor: str, *, on: bool, commander: str = "", bridge_url: str = "",
              update_minutes: int = 30) -> None:
    """Declare (or stand down) a major incident; audited and on the timeline."""
    from app.audit import audit
    from app.services import append_timeline

    bridge = bridge_url.strip()[:500]
    if bridge and not bridge.startswith(("http://", "https://", "tel:")):
        raise ValueError("the bridge link must start with https://, http:// or tel:")
    incident.major = on
    if on:
        incident.commander = commander.strip()[:255] or actor
        incident.bridge_url = bridge
        incident.update_minutes = max(5, min(240, int(update_minutes or 30)))
        append_timeline(incident, "major", "MAJOR", f"Major incident; commander {incident.commander}")
    else:
        append_timeline(incident, "major", "MAJOR OFF", f"Stood down by {actor}")
    audit(db, "incident.major", actor=actor, object_type="incident", object_id=incident.number,
          data={"on": on, "commander": incident.commander, "bridge": bool(bridge)})
    db.commit()


def post_update(db: Session, incident: Incident, actor: str, text: str) -> dict[str, Any]:
    """A status update: a note on the incident, the timer restarts, and the chat channels get it."""
    from app.chat import _queue
    from app.history import add_note, note_as_dict
    from app.services import append_timeline

    body = " ".join((text or "").split())[:TEXT_MAX]
    if not body:
        raise ValueError("write the update first")
    note = add_note(db, incident, actor=actor, body=f"Status update: {body}")
    incident.last_update_at = utcnow()
    append_timeline(incident, "status-update", "UPDATE", body[:200])
    db.commit()
    try:
        _queue(db, incident, "update", f"update-{note.id}", body[:300])
    except Exception:  # noqa: BLE001 — chat must not lose the update that is already saved
        db.rollback()
    return note_as_dict(note)


def active_majors(db: Session) -> list[dict[str, Any]]:
    from app.services import tracked_incident_statuses

    rows = db.query(Incident).filter(Incident.major.is_(True), Incident.status.in_(tracked_incident_statuses())).all()
    return [{"number": r.number, "title": r.title, "commander": r.commander or "", "bridge_url": r.bridge_url or "",
             **{k: major_view(r)[k] for k in ("next_due", "overdue")}} for r in rows]


# ---- SLA ------------------------------------------------------------------------------------

def sla_config() -> dict[str, Any]:
    from app.settings import settings

    raw = settings.yaml.get("sla") if isinstance(settings.yaml, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {"enabled": bool(raw.get("enabled", True))}
    for kind, defaults in SLA_DEFAULTS.items():
        given = raw.get(kind) if isinstance(raw.get(kind), dict) else {}
        out[kind] = {}
        for sev, minutes in defaults.items():
            try:
                out[kind][sev] = max(1, min(43200, int(given.get(sev, minutes))))
            except (TypeError, ValueError):
                out[kind][sev] = minutes
    return out


def _clock(start: datetime | None, target: int, done_at: datetime | None) -> dict[str, Any]:
    if start is None:
        return {"state": "none"}
    due = start + timedelta(minutes=target)
    if done_at is not None:
        return {"state": "met" if done_at <= due else "breached", "due": due.isoformat(), "target_min": target}
    left = int((due - utcnow()).total_seconds() // 60)
    return {"state": "breached" if left < 0 else "running", "due": due.isoformat(), "target_min": target, "minutes_left": left}


def sla_state(incident: Incident) -> dict[str, Any] | None:
    """Response (acknowledged in time) and resolve (resolved in time) for this incident's severity."""
    cfg = sla_config()
    sev = (incident.severity or "").lower()
    if not cfg["enabled"] or sev not in SEVERITIES or incident.source == "forgesre":
        return None
    start = _aware(incident.started_at)
    return {"response": _clock(start, cfg["response"][sev], _aware(incident.ack_at)),
            "resolve": _clock(start, cfg["resolve"][sev], _aware(incident.resolved_at))}


def sla_report(db: Session, days: int = 30) -> dict[str, Any]:
    """Met / breached per severity over the last ``days`` (incidents that finished their clock)."""
    since = utcnow() - timedelta(days=max(1, min(365, days)))
    rows = db.query(Incident).filter(Incident.started_at >= since).all()
    out = {sev: {"response_met": 0, "response_breached": 0, "resolve_met": 0, "resolve_breached": 0} for sev in SEVERITIES}
    for inc in rows:
        st = sla_state(inc)
        if st is None:
            continue
        sev = (inc.severity or "").lower()
        for kind in ("response", "resolve"):
            if st[kind]["state"] in {"met", "breached"}:
                out[sev][f"{kind}_{st[kind]['state']}"] += 1
    return {"days": days, "enabled": sla_config()["enabled"], "by_severity": out}


# ---- Shift handover -------------------------------------------------------------------------

def handover_as_dict(row: ShiftHandover) -> dict[str, Any]:
    return {"id": row.id, "created_by": row.created_by, "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
            "notes": row.notes, "open_incidents": list(row.open_incidents or []), "taken_by": row.taken_by,
            "taken_at": _aware(row.taken_at).isoformat() if row.taken_at else None}


def hand_over(db: Session, actor: str, notes: str) -> ShiftHandover:
    """Write a handover: notes plus a snapshot of the incidents still open."""
    from app.audit import audit
    from app.services import tracked_incident_statuses

    text = (notes or "").strip()[:TEXT_MAX * 2]
    open_rows = (db.query(Incident).filter(Incident.status.in_(tracked_incident_statuses()))
                 .order_by(Incident.started_at.desc()).limit(50).all())
    row = ShiftHandover(created_by=actor, notes=text,
                        open_incidents=[{"number": _short(i.number), "title": i.title, "severity": i.severity,
                                         "acked": bool(i.ack_at)} for i in open_rows])
    db.add(row)
    audit(db, "shift.handover", actor=actor, object_type="handover", object_id="new", data={"open": len(open_rows)})
    db.commit()
    return row


def take_over(db: Session, row: ShiftHandover, actor: str) -> ShiftHandover:
    """The next shift confirms it has read the handover. The writer cannot confirm their own."""
    from app.audit import audit

    if row.taken_by:
        raise ValueError(f"already taken over by {row.taken_by}")
    if row.created_by == actor:
        raise ValueError("the next shift confirms the handover, not the person who wrote it")
    row.taken_by, row.taken_at = actor, utcnow()
    audit(db, "shift.takeover", actor=actor, object_type="handover", object_id=str(row.id))
    db.commit()
    return row


def latest_handovers(db: Session, limit: int = 10) -> list[dict[str, Any]]:
    rows = db.query(ShiftHandover).order_by(ShiftHandover.id.desc()).limit(limit).all()
    return [handover_as_dict(r) for r in rows]


# ---- Follow-up actions ----------------------------------------------------------------------

def action_as_dict(row: FollowUpAction, incident: Incident | None = None) -> dict[str, Any]:
    overdue = bool(row.due and not row.done and row.due < date.today())
    data = {"id": row.id, "text": row.text, "owner": row.owner, "due": row.due.isoformat() if row.due else None,
            "done": bool(row.done), "done_at": _aware(row.done_at).isoformat() if row.done_at else None,
            "created_by": row.created_by, "overdue": overdue}
    if incident is not None:
        data["incident"] = incident.number
        data["incident_title"] = incident.title
    return data


def _parse_due(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError("due must be a date like 2026-12-01") from exc


def add_action(db: Session, incident: Incident, actor: str, text: str, owner: str = "", due: str | None = None) -> FollowUpAction:
    from app.audit import audit

    body = " ".join((text or "").split())[:TEXT_MAX]
    if not body:
        raise ValueError("describe the action")
    row = FollowUpAction(incident_id=incident.id, text=body, owner=owner.strip()[:255], due=_parse_due(due), created_by=actor)
    db.add(row)
    audit(db, "incident.action", actor=actor, object_type="incident", object_id=incident.number, data={"owner": row.owner})
    db.commit()
    return row


def update_action(db: Session, row: FollowUpAction, actor: str, changes: dict[str, Any]) -> FollowUpAction:
    from app.audit import audit

    if "text" in changes and changes["text"] is not None:
        body = " ".join(str(changes["text"]).split())[:TEXT_MAX]
        if not body:
            raise ValueError("describe the action")
        row.text = body
    if "owner" in changes and changes["owner"] is not None:
        row.owner = str(changes["owner"]).strip()[:255]
    if "due" in changes:
        row.due = _parse_due(changes["due"])
    if "done" in changes and changes["done"] is not None:
        row.done = bool(changes["done"])
        row.done_at = utcnow() if row.done else None
    audit(db, "incident.action.update", actor=actor, object_type="action", object_id=str(row.id), data={"done": row.done})
    db.commit()
    return row


def open_actions(db: Session, limit: int = 200) -> list[dict[str, Any]]:
    """Every open action, overdue and soonest due first."""
    rows = (db.query(FollowUpAction, Incident).join(Incident, Incident.id == FollowUpAction.incident_id)
            .filter(FollowUpAction.done.is_(False)).all())
    rows.sort(key=lambda r: (r[0].due is None, r[0].due or date.max, r[0].id))
    return [action_as_dict(a, i) for a, i in rows[:limit]]
