"""Automation page of the new UI: what happens between an alarm and a person.

Shows today's pipeline counts, the routing rules (playrules), playbooks, escalation
policies and maintenance windows, and a "try an alarm" simulator that runs the same
matching code as real alert intake, without creating anything.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Asset, EscalationPolicy, Incident, MaintenanceWindow, Notification, Playbook, Playrule, utcnow


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _steps(policy: EscalationPolicy | None) -> list[dict[str, Any]]:
    out = []
    for step in (policy.steps if policy else None) or []:
        if isinstance(step, dict):
            out.append(
                {
                    "after_minutes": int(step.get("after_minutes") or 0),
                    "target": str(step.get("target") or ""),
                    "channel": str(step.get("channel") or "email"),
                    "level": step.get("level"),
                }
            )
    return out


def automation_overview(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Pipeline counts for today and every rule, playbook, policy and maintenance window."""
    now = now or utcnow()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = now - timedelta(days=30)
    today = db.query(Incident).filter(Incident.started_at >= day).all()
    muted = sum(1 for i in today if (i.status or "").upper() in ("MAINT", "SUPPRESSED"))
    routed = sum(1 for i in today if i.playrule_id)
    notified = db.query(Notification).filter(Notification.created_at >= day).count()
    per_rule = Counter(
        rid for (rid,) in db.query(Incident.playrule_id).filter(Incident.started_at >= month, Incident.playrule_id.is_not(None))
    )
    rules = db.query(Playrule).order_by(Playrule.id).all()
    used_books = Counter(r.playbook_id for r in rules if r.playbook_id)
    used_policies = Counter(r.escalation_policy_id for r in rules if r.escalation_policy_id)
    hostnames = {a.asset_id: a.hostname for a in db.query(Asset.asset_id, Asset.hostname)}
    windows = (
        db.query(MaintenanceWindow)
        .filter(MaintenanceWindow.ends_at >= now)
        .order_by(MaintenanceWindow.starts_at)
        .limit(100)
        .all()
    )
    return {
        "pipeline": {
            "incidents_today": len(today),
            "muted_today": muted,
            "routed_today": routed,
            "notifications_today": notified,
        },
        "rules": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "enabled": r.enabled,
                "severity": r.severity,
                "condition": r.condition or {},
                "playbook": r.playbook.name if r.playbook else None,
                "policy": r.escalation_policy.name if r.escalation_policy else None,
                "hits_30d": per_rule.get(r.id, 0),
            }
            for r in rules
        ],
        "playbooks": [
            {
                "id": b.id,
                "name": b.name,
                "description": b.description,
                "steps": [str(s.get("title") if isinstance(s, dict) else s) for s in b.steps or []],
                "rules": used_books.get(b.id, 0),
            }
            for b in db.query(Playbook).order_by(Playbook.name).all()
        ],
        "policies": [
            {"id": p.id, "name": p.name, "steps": _steps(p), "rules": used_policies.get(p.id, 0)}
            for p in db.query(EscalationPolicy).order_by(EscalationPolicy.name).all()
        ],
        "maintenance": [
            {
                "asset": hostnames.get(w.asset_ref, w.asset_ref),
                "summary": w.summary,
                "starts_at": _iso(w.starts_at),
                "ends_at": _iso(w.ends_at),
                "active": bool(w.starts_at) and _aware(w.starts_at) <= _aware(now),
            }
            for w in windows
        ],
    }


def simulate_alarm(db: Session, alertname: str, asset_ref: str = "", value: str = "", at: datetime | None = None) -> dict[str, Any]:
    """What ForgeSRE would do with this alarm, using the real matching code. Writes nothing.

    Steps: find the asset (hostname, asset id or IP), check maintenance windows at ``at``,
    then match_playrule with the sample value, then read the rule's playbook and policy.
    """
    from app.services import match_playrule, overlapping_maintenance

    at = at or utcnow()
    asset = None
    ref = asset_ref.strip()
    if ref:
        asset = (
            db.query(Asset)
            .filter(or_(Asset.hostname == ref, Asset.asset_id == ref, Asset.ip == ref))
            .first()
        )
    trace: list[str] = []
    if ref and asset is None:
        trace.append(f"No asset matches “{ref}”: the incident would open without an asset.")
    elif asset is not None:
        trace.append(f"Asset {asset.hostname} ({asset.type}).")
    windows = overlapping_maintenance(db, asset.asset_id, at) if asset is not None else []
    if windows:
        trace.append(f"In maintenance: {windows[0].get('summary') or 'window'} until {windows[0].get('ends_at')}.")
        return {"outcome": "muted", "trace": trace, "rule": None, "playbook": None, "policy": None, "steps": []}
    labels = {"alertname": alertname}
    annotations = {"value": value} if str(value).strip() else {}
    rule = match_playrule(db, alertname, labels=labels, asset=asset, annotations=annotations)
    if rule is None:
        trace.append("No rule matches: the incident opens with no playbook and the Default escalation.")
        default = db.query(EscalationPolicy).filter_by(slug="default-warning").first()
        return {"outcome": "incident", "trace": trace, "rule": None, "playbook": None, "policy": default.name if default else None, "steps": _steps(default)}
    trace.append(f"Rule “{rule.name}” matches ({rule.severity}).")
    if rule.playbook:
        trace.append(f"Playbook {rule.playbook.name} is attached.")
    if rule.escalation_policy:
        trace.append(f"Escalation “{rule.escalation_policy.name}” starts.")
    return {
        "outcome": "incident",
        "trace": trace,
        "rule": rule.name,
        "severity": rule.severity,
        "playbook": rule.playbook.name if rule.playbook else None,
        "policy": rule.escalation_policy.name if rule.escalation_policy else None,
        "steps": _steps(rule.escalation_policy),
    }
