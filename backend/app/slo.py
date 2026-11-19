"""Service level objectives from incident history.

Availability = share of the window without an open incident of the chosen severities on the
SLO's scope. Quiet incidents (maintenance, noise filters) do not count: they did not reach
people, and planned maintenance should not burn the budget. Error budget = (100 - target) %
of the window; burn rate = budget used in the last 24 h compared with an even spend.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.audit import audit
from app.models import Asset, Incident, Slo, utcnow

SCOPES = ("all", "asset", "type", "site")
QUIET = ("MAINT", "SUPPRESSED")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _in_scope(slo: Slo, asset: Asset | None) -> bool:
    from app.noise_filters import in_scope

    return in_scope(slo, asset)  # same rules as noise filters (all / asset / type / site)


def _merge(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _down_minutes(spans: list[tuple[datetime, datetime]], since: datetime, until: datetime) -> float:
    total = 0.0
    for start, end in spans:
        a, b = max(start, since), min(end, until)
        if b > a:
            total += (b - a).total_seconds() / 60
    return total


def evaluate(db: Session, slo: Slo, now: datetime | None = None) -> dict[str, Any]:
    """Availability, budget and burn for one SLO over its window, ending now."""
    now = _aware(now) or utcnow()
    since = now - timedelta(days=int(slo.window_days or 30))
    severities = [s.upper() for s in (slo.severities or ["CRITICAL"])]
    rows = (
        db.query(Incident)
        .filter(Incident.started_at < now, Incident.status.notin_(QUIET), Incident.severity.in_(severities))
        .filter((Incident.resolved_at.is_(None)) | (Incident.resolved_at >= since))
        .all()
    )
    spans = []
    worst: dict[str, float] = {}
    for inc in rows:
        if not _in_scope(slo, inc.asset):
            continue
        start, end = _aware(inc.started_at), _aware(inc.resolved_at) or now
        spans.append((start, end))
        name = inc.asset.hostname if inc.asset else "(no asset)"
        worst[name] = worst.get(name, 0) + _down_minutes([(start, end)], since, now)
    merged = _merge(spans)
    window_min = (now - since).total_seconds() / 60
    down = _down_minutes(merged, since, now)
    budget = window_min * (100 - float(slo.target)) / 100
    last_day = _down_minutes(merged, now - timedelta(days=1), now)
    even_per_day = budget / max(1, int(slo.window_days or 30))
    used_pct = 100.0 if budget <= 0 and down > 0 else (down / budget * 100 if budget > 0 else 0.0)
    status = "breached" if used_pct >= 100 else "at_risk" if used_pct >= 75 else "ok"
    return {
        "id": slo.id, "name": slo.name, "scope": slo.scope, "scope_value": slo.scope_value, "target": float(slo.target),
        "window_days": int(slo.window_days or 30), "severities": severities, "owner": slo.owner,
        "availability": round(100 * (1 - down / window_min), 4) if window_min else 100.0,
        "downtime_min": round(down, 1), "budget_min": round(budget, 1), "budget_left_min": round(budget - down, 1),
        "budget_used_pct": round(used_pct, 1), "burn_rate_24h": round(last_day / even_per_day, 2) if even_per_day > 0 else 0.0,
        "incidents": len(spans), "status": status,
        "worst": [{"asset": k, "downtime_min": round(v, 1)} for k, v in sorted(worst.items(), key=lambda kv: -kv[1])[:5]],
    }


def evaluate_all(db: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    return [evaluate(db, s, now) for s in db.query(Slo).order_by(Slo.name).all()]


def save(db: Session, data: dict[str, Any], actor: str, row: Slo | None = None) -> Slo:
    """Create or update an SLO. Raises ValueError on bad input or a duplicate name."""
    name = " ".join(str(data.get("name") or "").split())[:120]
    if not name:
        raise ValueError("name is required")
    clash = db.query(Slo).filter(Slo.name == name)
    if row is not None:
        clash = clash.filter(Slo.id != row.id)
    if clash.first():
        raise ValueError("an SLO with that name exists")
    scope = str(data.get("scope") or "all")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    value = str(data.get("scope_value") or "").strip()
    if scope != "all" and not value:
        raise ValueError("pick the asset, type or site for this scope")
    target = float(data.get("target") if data.get("target") is not None else 99.9)
    if not 50 <= target < 100:
        raise ValueError("target must be at least 50 and below 100 (%)")
    window = int(data["window_days"]) if data.get("window_days") is not None else 30
    if not 1 <= window <= 365:
        raise ValueError("window must be 1–365 days")
    sev = [s.upper() for s in (data.get("severities") or ["CRITICAL"]) if s.upper() in ("CRITICAL", "HIGH", "WARNING")]
    if not sev:
        raise ValueError("pick at least one severity")
    row = row or Slo(created_by=actor)
    row.name, row.scope, row.scope_value, row.target, row.window_days, row.severities = name, scope, "" if scope == "all" else value, target, window, sev
    row.owner = str(data.get("owner") or "")[:255]
    db.add(row)
    audit(db, "slo.save", actor=actor, object_type="slo", object_id=name, data={"target": target, "window": window, "scope": scope})
    db.commit()
    db.refresh(row)
    return row
