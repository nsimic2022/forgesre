"""Create, update and remove playrules and playbooks for the new UI (JSON API).

Same rules as the classic pages in app/web.py: rule names are unique, a rule without an
escalation policy gets ``default-warning``, removing a rule or playbook clears the link on
existing incidents instead of deleting them. Every write is audited; callers check
``write_play`` first.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.audit import audit
from app.models import EscalationPolicy, Incident, Playbook, Playrule

SEVERITIES = ("critical", "high", "warning")
OPERATORS = (">", ">=", "<", "==")


def _policy(db: Session, policy_id: int | None) -> EscalationPolicy | None:
    policy = db.get(EscalationPolicy, policy_id) if policy_id else None
    return policy or db.query(EscalationPolicy).filter_by(slug="default-warning").first()


def _condition(data: dict[str, Any], name: str) -> dict[str, Any]:
    from app.web import playrule_condition

    operator = str(data.get("operator") or ">").strip()
    if operator not in OPERATORS:
        raise ValueError(f"operator must be one of {', '.join(OPERATORS)}")
    value = data.get("value")
    return playrule_condition(
        str(data.get("alertname") or ""), name, str(data.get("metric") or ""), operator, "" if value is None else str(value)
    )


def save_playrule(db: Session, data: dict[str, Any], actor: str, rule: Playrule | None = None) -> Playrule:
    """Create a playrule, or update ``rule``. Raises ValueError on a bad or duplicate name."""
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    clash = db.query(Playrule).filter(Playrule.name == name)
    if rule is not None:
        clash = clash.filter(Playrule.id != rule.id)
    if clash.first() is not None:
        raise ValueError("a playrule with that name already exists")
    severity = str(data.get("severity") or "warning").strip().lower()
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    playbook_id = data.get("playbook_id") or None
    if playbook_id and db.get(Playbook, playbook_id) is None:
        raise ValueError("playbook not found")
    policy = _policy(db, data.get("escalation_policy_id") or None)
    row = rule or Playrule(name=name)
    row.name = name
    row.description = str(data.get("description") or "").strip()
    row.enabled = bool(data.get("enabled", True))
    row.severity = severity
    row.condition = _condition(data, name)
    row.playbook_id = playbook_id
    row.escalation_policy_id = policy.id if policy else None
    if rule is None:
        db.add(row)
    audit(db, "playrule.update" if rule else "playrule.create", actor=actor, object_type="playrule", object_id=name)
    db.commit()
    db.refresh(row)
    return row


def delete_playrule(db: Session, rule: Playrule, actor: str) -> None:
    """Remove a playrule. Incidents it opened keep their playbook; only the rule link is cleared."""
    name = rule.name
    db.query(Incident).filter(Incident.playrule_id == rule.id).update({Incident.playrule_id: None})
    db.delete(rule)
    audit(db, "playrule.remove", actor=actor, object_type="playrule", object_id=name)
    db.commit()


def save_playbook(db: Session, data: dict[str, Any], actor: str, book: Playbook | None = None) -> Playbook:
    """Create a playbook (slug made unique from the name) or update ``book``. Steps are one title each."""
    from app.web import _unique_playbook_slug

    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    steps = [{"title": str(s).strip()} for s in data.get("steps") or [] if str(s).strip()]
    if not steps:
        raise ValueError("a playbook needs at least one step")
    row = book or Playbook(slug=_unique_playbook_slug(db, str(data.get("slug") or name)))
    row.name = name
    row.description = str(data.get("description") or "").strip()
    row.steps = steps
    if book is None:
        db.add(row)
    audit(db, "playbook.update" if book else "playbook.create", actor=actor, object_type="playbook", object_id=row.slug)
    db.commit()
    db.refresh(row)
    return row


def delete_playbook(db: Session, book: Playbook, actor: str) -> None:
    """Remove a playbook. Rules and incidents that used it keep working without a checklist."""
    slug = book.slug
    db.query(Playrule).filter(Playrule.playbook_id == book.id).update({Playrule.playbook_id: None})
    db.query(Incident).filter(Incident.playbook_id == book.id).update({Incident.playbook_id: None})
    db.delete(book)
    audit(db, "playbook.remove", actor=actor, object_type="playbook", object_id=slug)
    db.commit()
