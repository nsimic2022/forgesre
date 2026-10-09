"""2×2 Ladder squares for the Assets list and the Incidents list.

Cubes are the same size as the ICMP / port / SNMP squares (reach-sq).
Labels are L1 L2 on the first row and L3 L4 on the second.

Asset list — is the level on this host, and has an active incident climbed it:
- Green: the level is added (enabled, at least one email).
- Red: an active incident (OPEN / INVESTIGATING / ESCALATED) already has a
  notification for that level's mail step, or the incident status is ESCALATED
  and enough minutes have passed for that rung. Red wins over green.
- Gray: the level is not on the asset. An empty Ladder (Default ladder) is
  gray here; the climb is on the incident row.

Incident list — how far this incident climbed:
- Red: this incident already has a notification for that level
  (the step_key process_escalations / notify_first_step stored).
- Green: the level is configured and this incident has not mailed it yet.
- Gray: no such level.

When the asset Ladder is empty, escalation steps (the matched rule's ladder,
otherwise Default warning) map in order onto L1, L2, L3, and L4. Default
warning is three steps (0 team, 15 team-lead, 30 engineer), so L1–L3 follow
those steps and L4 stays gray. A policy with more than four steps folds the
fourth and later steps into L4.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session, object_session

from app.asset_ladder import LADDER_LEVELS, filled_ladder_levels
from app.models import Asset, Incident, Notification, utcnow

_ACTIVE = ("OPEN", "INVESTIGATING", "ESCALATED")


def _elapsed_minutes(incident: Incident) -> float:
    started = incident.started_at
    if started is None:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - started).total_seconds() / 60.0


def _cube(level: int, tone: str, title: str) -> dict[str, Any]:
    return {"level": level, "tone": tone, "title": title}


def _fired_keys(db: Session, incident_ids: list[int]) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {pk: set() for pk in incident_ids}
    if not incident_ids:
        return out
    rows = (
        db.query(Notification.incident_id, Notification.step_key)
        .filter(Notification.incident_id.in_(incident_ids))
        .all()
    )
    for incident_id, step_key in rows:
        if step_key:
            out.setdefault(int(incident_id), set()).add(str(step_key))
    return out


def level_step_keys(levels: list[dict[str, Any]]) -> dict[int, set[str]]:
    """step_key values process_escalations stores for each filled Ladder level.

    Same order as active_mail_steps, then the same keys as escalation_steps
    (immediate, 15m, 15m-2, …).
    """
    from app.services import _keyed_steps

    raw: list[dict[str, Any]] = []
    owners: list[int] = []
    for level in levels:
        minutes = int(level["after_minutes"])
        for email in level["emails"]:
            raw.append({"after_minutes": minutes, "target": email, "channel": "email"})
            owners.append(int(level["level"]))
    keyed = _keyed_steps(raw)
    out: dict[int, set[str]] = {number: set() for number, _label in LADDER_LEVELS}
    for owner, step in zip(owners, keyed):
        out.setdefault(owner, set()).add(str(step["step_key"]))
    return out


def _asset_session(asset: Asset | None, db: Session | None) -> Session | None:
    if db is not None:
        return db
    if asset is None:
        return None
    return object_session(asset)


def asset_ladder_cubes(asset: Asset | None, db: Session | None = None) -> list[dict[str, Any]]:
    """Four cubes for one asset row. See the module note for red vs green."""
    session = _asset_session(asset, db)
    levels = filled_ladder_levels(session, asset) if session is not None else []
    by_level = {int(row["level"]): row for row in levels}
    keys = level_step_keys(levels) if levels else {}
    active: list[Incident] = []
    fired: dict[int, set[str]] = {}
    if session is not None and asset is not None and getattr(asset, "id", None) and levels:
        active = (
            session.query(Incident)
            .filter(Incident.asset_id == asset.id, Incident.status.in_(_ACTIVE))
            .all()
        )
        fired = _fired_keys(session, [int(inc.id) for inc in active if inc.id])
    cubes: list[dict[str, Any]] = []
    for level, label in LADDER_LEVELS:
        row = by_level.get(level)
        if row is None:
            cubes.append(_cube(level, "grey", f"{label} · not configured"))
            continue
        minutes = int(row["after_minutes"])
        red = False
        for incident in active:
            mailed = bool(keys.get(level, set()) & fired.get(int(incident.id), set()))
            climbed = (incident.status or "").upper() == "ESCALATED" and _elapsed_minutes(incident) >= minutes
            if mailed or climbed:
                red = True
                break
        title = f"{label} · {minutes} min"
        if red:
            title += " · escalated"
        cubes.append(_cube(level, "red" if red else "green", title))
    return cubes


def _policy_cubes(steps: list[dict[str, Any]], fired: set[str]) -> list[dict[str, Any]]:
    """Map ladder steps in order onto L1–L4. Extra steps past L4 fold into L4."""
    cubes: list[dict[str, Any]] = []
    for index, (level, _label) in enumerate(LADDER_LEVELS):
        if level < 4:
            if index >= len(steps):
                cubes.append(_cube(level, "grey", f"L{level} · no such level"))
                continue
            step = steps[index]
            red = str(step.get("step_key") or "") in fired
            title = f"L{level} · {int(step.get('after_minutes') or 0)} min · {step.get('target') or 'team'}"
            if red:
                title += " · escalated"
            cubes.append(_cube(level, "red" if red else "green", title))
            continue
        tail = steps[3:]
        if not tail:
            cubes.append(_cube(4, "grey", "L4 · no such level"))
            continue
        red = any(str(step.get("step_key") or "") in fired for step in tail)
        first = tail[0]
        title = f"L4 · {int(first.get('after_minutes') or 0)} min · {first.get('target') or 'team'}"
        if len(tail) > 1:
            title += f" · +{len(tail) - 1} later"
        if red:
            title += " · escalated"
        cubes.append(_cube(4, "red" if red else "green", title))
    return cubes


def incident_ladder_cubes(incident: Incident | None, db: Session | None = None) -> list[dict[str, Any]]:
    """Four cubes for one incident row. See the module note for the Default ladder map."""
    if incident is None:
        return [_cube(level, "grey", f"L{level} · no such level") for level, _label in LADDER_LEVELS]
    session = db or object_session(incident)
    if session is None:
        return [_cube(level, "grey", f"L{level} · no such level") for level, _label in LADDER_LEVELS]
    asset = incident.asset
    if asset is None and incident.asset_id:
        asset = session.get(Asset, incident.asset_id)
    levels = filled_ladder_levels(session, asset)
    fired = _fired_keys(session, [int(incident.id)]).get(int(incident.id), set()) if incident.id else set()
    if levels:
        by_level = {int(row["level"]): row for row in levels}
        keys = level_step_keys(levels)
        cubes: list[dict[str, Any]] = []
        for level, label in LADDER_LEVELS:
            row = by_level.get(level)
            if row is None:
                cubes.append(_cube(level, "grey", f"{label} · not configured"))
                continue
            red = bool(keys.get(level, set()) & fired)
            title = f"{label} · {int(row['after_minutes'])} min"
            if red:
                title += " · escalated"
            cubes.append(_cube(level, "red" if red else "green", title))
        return cubes
    from app.services import escalation_steps

    return _policy_cubes(escalation_steps(incident, session), fired)
