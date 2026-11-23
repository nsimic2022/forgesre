"""Alert quality: which alert rules page people for nothing.

Per alert name over the last N days: how many incidents, how many a person acknowledged, how
many cleared by themselves within minutes, how many flapped or were linked to a bigger incident,
and a plain suggestion (keep, tune the rule, add a noise filter). Database reads only.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.models import Incident, utcnow

SELF_CLEAR_MIN = 10  # resolved without anyone acknowledging within this many minutes = noise
MIN_FOR_ADVICE = 5


def _minutes(a, b) -> float | None:
    if a is None or b is None:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=b.tzinfo)
    if b.tzinfo is None:
        b = b.replace(tzinfo=a.tzinfo)
    return max(0.0, (b - a).total_seconds() / 60)


def advice(row: dict[str, Any]) -> str:
    """One plain suggestion for an alert, from its numbers."""
    if row["incidents"] < MIN_FOR_ADVICE:
        return "Too few to judge"
    if row["self_cleared_pct"] >= 70:
        return "Mostly clears by itself: add a noise filter or a longer 'for:' on the rule"
    if row["flapping"] >= 3:
        return "Flaps: raise the threshold or add hysteresis"
    if row["acked_pct"] < 30:
        return "Rarely acknowledged: check whether anyone needs this page"
    return "Keep: people act on it"


def report(db: Session, days: int = 30, limit: int = 200) -> dict[str, Any]:
    """Rows sorted noisiest first (most incidents nobody acknowledged)."""
    from app.alarm_noise import STORM_FINGERPRINT
    from app.dc_noc import incident_alertname

    days = max(1, min(365, int(days)))
    since = utcnow() - timedelta(days=days)
    rows = (db.query(Incident).filter(Incident.started_at >= since, Incident.fingerprint != STORM_FINGERPRINT)
            .order_by(Incident.started_at.desc()).limit(20000).all())
    by: dict[str, list[Incident]] = defaultdict(list)
    for inc in rows:
        by[incident_alertname(inc) or inc.title or "unknown"].append(inc)
    out: list[dict[str, Any]] = []
    for name, items in by.items():
        acked = sum(1 for i in items if i.ack_at)
        self_cleared = sum(1 for i in items if not i.ack_at and (m := _minutes(i.started_at, i.resolved_at)) is not None and m <= SELF_CLEAR_MIN)
        durations = [m for i in items if (m := _minutes(i.started_at, i.resolved_at)) is not None]
        row = {
            "alertname": name, "incidents": len(items),
            "acked_pct": round(100 * acked / len(items)), "self_cleared_pct": round(100 * self_cleared / len(items)),
            "flapping": sum(1 for i in items if (i.flap_count or 0) > 0),
            "linked": sum(1 for i in items if i.caused_by_id),
            "median_minutes": round(median(durations), 1) if durations else None,
            "assets": len({i.asset_id for i in items if i.asset_id}),
        }
        row["advice"] = advice(row)
        out.append(row)
    out.sort(key=lambda r: (r["incidents"] * (100 - r["acked_pct"]), r["incidents"]), reverse=True)
    total = len(rows)
    actionable = sum(1 for i in rows if i.ack_at)
    return {"days": days, "incidents": total, "actionable_pct": round(100 * actionable / total) if total else None,
            "rows": out[:limit]}
