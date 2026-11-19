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


DEFAULT_POLICY_SLUG = "default-warning"


def policy_steps(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """L1–L4 rows ``{level, on, minutes, target}`` to stored steps. Raises ValueError with every problem."""
    from app.asset_ladder import LADDER_LEVELS, policy_steps_from_levels
    from app.services import policy_steps_problems

    by_level = {int(row.get("level") or 0): row for row in levels or []}
    rows = [by_level.get(level, {}) for level, _label in LADDER_LEVELS]
    ons = ["1" if row.get("on") else "" for row in rows]
    steps = policy_steps_from_levels(ons, [row.get("minutes") for row in rows], [row.get("target") for row in rows])
    problems = policy_steps_problems(steps)
    problems += [f"{label} is on but has no recipient." for (level, label), row in zip(LADDER_LEVELS, rows)
                 if row.get("on") and not str(row.get("target") or "").strip()]
    if not steps and not problems:
        problems.append("Turn on at least one level (L1–L4).")
    if problems:
        raise ValueError(" ".join(problems))
    return steps


def save_policy(db: Session, data: dict[str, Any], actor: str, policy: EscalationPolicy | None = None) -> EscalationPolicy:
    """Create an escalation policy (slug from the name when empty) or update ``policy``'s name and steps."""
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    steps = policy_steps(data.get("levels") or [])
    row = policy
    if row is None:
        base = str(data.get("slug") or name).strip().lower()
        slug = "-".join("".join(ch if ch.isalnum() else " " for ch in base).split())
        if not slug:
            raise ValueError("slug is required")
        if db.query(EscalationPolicy).filter_by(slug=slug).first() is not None:
            raise ValueError(f"slug {slug} already exists; edit that policy or pick another name")
        row = EscalationPolicy(slug=slug)
        db.add(row)
    row.name = name
    row.steps = steps
    audit(db, "escalation.update" if policy else "escalation.create", actor=actor, object_type="escalation_policy", object_id=row.slug)
    db.commit()
    db.refresh(row)
    return row


def delete_policy(db: Session, policy: EscalationPolicy, actor: str) -> int:
    """Remove a policy; its rules move to default-warning. The default policy itself cannot be removed."""
    if policy.slug == DEFAULT_POLICY_SLUG:
        raise ValueError("default-warning is used by new rules and cannot be removed; edit its steps instead")
    fallback = db.query(EscalationPolicy).filter_by(slug=DEFAULT_POLICY_SLUG).first()
    moved = (
        db.query(Playrule)
        .filter(Playrule.escalation_policy_id == policy.id)
        .update({Playrule.escalation_policy_id: fallback.id if fallback else None}, synchronize_session=False)
    )
    slug = policy.slug
    db.delete(policy)
    audit(db, "escalation.remove", actor=actor, object_type="escalation_policy", object_id=slug,
          data={"playrules_moved": int(moved or 0), "to": fallback.slug if fallback else ""})
    db.commit()
    return int(moved or 0)
