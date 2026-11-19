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


def incidents_page(
    db: Session, tab: str = "active", severity: str = "", q: str = "", limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """One page of the incident list plus the count of every tab (with the same filters)."""
    tab = tab if tab in TABS else "active"
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    base = db.query(Incident).outerjoin(Asset, Incident.asset_id == Asset.id)
    if severity:
        base = base.filter(Incident.severity.ilike(severity))
    text = q.strip()
    if text:
        like = f"%{text}%"
        base = base.filter(
            or_(Incident.number.ilike(like), Incident.title.ilike(like), Asset.hostname.ilike(like), Asset.ip.ilike(like))
        )
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
        "notifications": [notification_as_dict(row) for row in notifications_for(db, incident)],
        "notes": [note_as_dict(row) for row in notes_for(db, incident)],
        "audit": [audit_as_dict(row) for row in audit_for(db, incident.number)],
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
