"""Reports page of the new UI: period analytics, scheduled reports and CSV export.

Numbers come only from incidents and notifications already stored; nothing is estimated.
"Host-down time" is the summed duration of host-down incidents (NodeExporterDown,
WindowsExporterDown, SnmpDeviceUnreachable), not a measured SLA.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import Incident, ScheduledReport, utcnow

HOST_DOWN = ("NodeExporterDown", "WindowsExporterDown", "SnmpDeviceUnreachable")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _avg_minutes(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _stats(rows: list[Incident]) -> dict[str, Any]:
    mtta = [(_aware(i.ack_at) - _aware(i.started_at)).total_seconds() / 60 for i in rows if i.ack_at and i.started_at]
    end = lambda i: i.resolved_at or i.ended_at  # noqa: E731
    mttr = [(_aware(end(i)) - _aware(i.started_at)).total_seconds() / 60 for i in rows if end(i) and i.started_at]
    sev = Counter((i.severity or "WARNING").upper() for i in rows)
    return {
        "incidents": len(rows),
        "critical": sev.get("CRITICAL", 0),
        "high": sev.get("HIGH", 0),
        "warning": sev.get("WARNING", 0),
        "mtta_min": _avg_minutes([m for m in mtta if m >= 0]),
        "mttr_min": _avg_minutes([m for m in mttr if m >= 0]),
        "reviewed": sum(1 for i in rows if getattr(i, "root_cause", "")),
    }


def _period(db: Session, start: datetime, end: datetime) -> list[Incident]:
    return (
        db.query(Incident)
        .options(joinedload(Incident.asset))
        .filter(Incident.started_at >= start, Incident.started_at < end)
        .order_by(Incident.started_at)
        .all()
    )


def reports_overview(db: Session, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    """KPIs for the last ``days`` against the ``days`` before, incidents per day by severity,
    by asset type, noisiest assets, most frequent alerts, host-down time, scheduled reports."""
    days = max(1, min(int(days), 365))
    now = _aware(now) or utcnow()
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = _period(db, start, now)
    previous = _period(db, start - timedelta(days=days), start)

    per_day: dict[str, Counter] = defaultdict(Counter)
    for i in rows:
        per_day[_aware(i.started_at).date().isoformat()][(i.severity or "WARNING").upper()] += 1
    daily = []
    for n in range((now.date() - start.date()).days + 1):
        day = (start + timedelta(days=n)).date().isoformat()
        c = per_day.get(day, Counter())
        daily.append({"day": day, "critical": c.get("CRITICAL", 0), "high": c.get("HIGH", 0), "other": sum(c.values()) - c.get("CRITICAL", 0) - c.get("HIGH", 0)})

    by_type: dict[str, list[Incident]] = defaultdict(list)
    for i in rows:
        by_type[i.asset.type if i.asset else "No asset"].append(i)

    assets = Counter(i.asset.hostname for i in rows if i.asset)
    alerts = Counter(i.title for i in rows)
    down: Counter = Counter()
    for i in rows:
        if i.title in HOST_DOWN and i.asset:
            stop = _aware(i.resolved_at or i.ended_at) or now
            down[i.asset.hostname] += max(0.0, (stop - _aware(i.started_at)).total_seconds() / 60)

    from app.services import report_kind, report_recipients, schedule_label

    scheduled = [
        {
            "id": r.id,
            "name": r.name,
            "kind": report_kind(r),
            "recipients": report_recipients(r),
            "schedule": schedule_label(r),
            "enabled": bool(r.enabled),
            "last_run_at": _aware(r.last_run_at).isoformat() if r.last_run_at else None,
            "next_run_at": _aware(r.next_run_at).isoformat() if r.next_run_at else None,
        }
        for r in db.query(ScheduledReport).order_by(ScheduledReport.name).all()
    ]
    return {
        "days": days,
        "from": start.isoformat(),
        "to": now.isoformat(),
        "current": _stats(rows),
        "previous": _stats(previous),
        "daily": daily,
        "by_type": sorted(({"group": g, **_stats(items)} for g, items in by_type.items()), key=lambda r: -r["incidents"]),
        "noisy_assets": [{"asset": a, "incidents": c} for a, c in assets.most_common(10)],
        "top_alerts": [{"alert": a, "incidents": c} for a, c in alerts.most_common(10)],
        "host_down": [{"asset": a, "minutes": round(m)} for a, m in down.most_common(10)],
        "scheduled": scheduled,
    }


def incidents_csv(db: Session, days: int = 30, now: datetime | None = None) -> str:
    """Every incident of the period as CSV, for spreadsheets and audits."""
    days = max(1, min(int(days), 365))
    now = _aware(now) or utcnow()
    rows = _period(db, (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0), now)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["number", "severity", "status", "title", "asset", "asset_type", "source", "started_at", "ack_at", "ack_by", "resolved_at", "resolved_by", "root_cause"])
    for i in rows:
        writer.writerow(
            [
                i.number, (i.severity or "").upper(), i.status, i.title,
                i.asset.hostname if i.asset else "", i.asset.type if i.asset else "", i.source or "",
                _aware(i.started_at).isoformat() if i.started_at else "",
                _aware(i.ack_at).isoformat() if i.ack_at else "", i.ack_by or "",
                _aware(i.resolved_at).isoformat() if i.resolved_at else "", i.resolved_by or "",
                getattr(i, "root_cause", "") or "",
            ]
        )
    return out.getvalue()
