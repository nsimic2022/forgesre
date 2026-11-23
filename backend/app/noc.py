"""Data for the NOC overview page of the new UI: one compact JSON per poll.

Built for a screen that refreshes every few seconds: three small queries for incidents,
one for notifications, one for jobs, one for assets. No evidence, no timelines.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.dc_noc import current_oncall
from app.ladder_cubes import incident_ladder_cubes
from app.models import Asset, Incident, Job, Notification, utcnow
from app.settings import settings

ACTIVE = ("OPEN", "INVESTIGATING", "ESCALATED", "ACK", "ACKNOWLEDGED")
RESOLVED = ("RESOLVED", "CLOSED")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def forgeai_mode() -> str:
    """off | local | external, from ai.enabled and ai.llm.mode."""
    if not settings.ai_enabled:
        return "off"
    mode = str(((settings.yaml.get("ai") or {}).get("llm") or {}).get("mode") or "").lower()
    return "external" if mode == "external" else "local"


def _forgeai_state(incident: Incident, llm_jobs: dict[str, str], mode: str) -> str:
    """What the ForgeAI column shows: off, queued, running, ready-local, ready-external or none."""
    if mode == "off":
        return "off"
    job = llm_jobs.get(incident.number)
    if job in ("pending", "running"):
        return "queued" if job == "pending" else "running"
    for inv in reversed(incident.investigations or []):
        if (inv.provider or "").endswith("-llm"):
            return f"ready-{mode}"
    return "none"


def _level(cubes: list[dict[str, Any]]) -> str:
    """Highest escalation level this incident already notified (red cube), else L1."""
    fired = [c["level"] for c in cubes if c.get("tone") == "red"]
    return f"L{max(fired)}" if fired else "L1"


def _row(incident: Incident, sent: dict[int, int], llm_jobs: dict[str, str], mode: str, db: Session) -> dict[str, Any]:
    asset = incident.asset
    latest = incident.investigations[-1] if incident.investigations else None
    alert = incident.alert_payload or {}
    annotations = alert.get("annotations") if isinstance(alert, dict) else {}
    return {
        "number": incident.number,
        "severity": (incident.severity or "WARNING").upper(),
        "status": incident.status,
        "title": incident.title,
        "detail": (annotations or {}).get("description") or (annotations or {}).get("summary") or "",
        "source": incident.source or "",
        "started_at": _iso(incident.started_at),
        "ack_at": _iso(incident.ack_at),
        "ack_by": incident.ack_by or "",
        "resolved_at": _iso(incident.resolved_at),
        "asset": {"hostname": asset.hostname, "type": asset.type, "ip": asset.ip} if asset else None,
        "level": _level(incident_ladder_cubes(incident, db)),
        "messages": sent.get(incident.id, 0),
        "forgerca": (latest.likely_cause or latest.summary or "") if latest else "",
        "forgeai": _forgeai_state(incident, llm_jobs, mode),
        "flap_count": int(incident.flap_count or 0),
        "sla": _sla_short(incident),
        "major": bool(incident.major),
        "caused_by": _caused_by(db, incident),
    }


def _sla_short(incident: Incident) -> dict[str, Any] | None:
    """The clock that matters now: response until acknowledged, then resolve."""
    from app.incident_process import sla_state

    st = sla_state(incident)
    if st is None:
        return None
    kind = "response" if not incident.ack_at else "resolve"
    return {"kind": kind, **st[kind]}


def _caused_by(db: Session, incident: Incident) -> str:
    """Short number of the incident this one is linked to (parent down, alarm storm, by hand)."""
    if not incident.caused_by_id:
        return ""
    cause = db.get(Incident, incident.caused_by_id)
    return cause.number.split("_")[0] if cause is not None else ""


def _sent_counts(db: Session, ids: list[int]) -> dict[int, int]:
    """Notifications sent per incident id."""
    sent: dict[int, int] = defaultdict(int)
    if ids:
        for incident_id, count in (
            db.query(Notification.incident_id, func.count(Notification.id))
            .filter(Notification.incident_id.in_(ids))
            .group_by(Notification.incident_id)
            .all()
        ):
            sent[int(incident_id)] = int(count)
    return sent


def _llm_jobs(db: Session) -> dict[str, str]:
    """Incident number -> pending/running ForgeAI job status."""
    return {
        row.object_id: row.status
        for row in db.query(Job).filter(Job.kind == "investigate", Job.status.in_(("pending", "running"))).all()
        if (row.payload or {}).get("use_llm", True) is not False
    }


def _minutes(deltas: list[timedelta]) -> float | None:
    if not deltas:
        return None
    return round(sum(d.total_seconds() for d in deltas) / len(deltas) / 60, 1)


def noc_overview(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Everything the NOC overview page draws, as plain JSON."""
    now = _aware(now) or utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    load = (joinedload(Incident.asset), selectinload(Incident.investigations))
    active = (
        db.query(Incident).options(*load).filter(Incident.status.in_(ACTIVE)).order_by(Incident.started_at.desc()).limit(200).all()
    )
    resolved_today = (
        db.query(Incident)
        .options(*load)
        .filter(Incident.status.in_(RESOLVED), Incident.resolved_at >= day_start)
        .order_by(Incident.resolved_at.desc())
        .limit(100)
        .all()
    )
    sent = _sent_counts(db, [i.id for i in active + resolved_today])
    llm_jobs = _llm_jobs(db)
    mode = forgeai_mode()
    rows_active = [_row(i, sent, llm_jobs, mode, db) for i in active]
    not_acked = [r for r in rows_active if not r["ack_at"]]
    acked = [r for r in rows_active if r["ack_at"]]

    severities = defaultdict(int)
    for row in rows_active:
        severities[row["severity"]] += 1

    week = db.query(Incident).filter(Incident.started_at >= now - timedelta(days=7)).all()
    mtta = [(_aware(i.ack_at) - _aware(i.started_at)) for i in week if i.ack_at and i.started_at and _aware(i.ack_at) >= day_start]
    mttr = [(_aware(i.resolved_at) - _aware(i.started_at)) for i in week if i.resolved_at and i.started_at]

    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"up": 0, "total": 0})
    down: list[str] = []
    for asset in db.query(Asset).all():
        group = by_type[asset.type or "Other"]
        group["total"] += 1
        if asset.ping_status == "red" or asset.exporter_status == "red":
            down.append(asset.hostname)
        else:
            group["up"] += 1

    oncall = current_oncall(db, now)
    from app.incident_process import active_majors, latest_handovers

    handover = latest_handovers(db, 1)
    return {
        "generated_at": now.isoformat(),
        "major": active_majors(db),
        "handover": handover[0] if handover else None,
        "counts": {
            "open": len(rows_active),
            "critical": severities.get("CRITICAL", 0),
            "high": severities.get("HIGH", 0),
            "warning": severities.get("WARNING", 0),
            "not_acked": len(not_acked),
            "hosts_total": sum(g["total"] for g in by_type.values()),
            "hosts_down": len(down),
            "hosts_down_names": down[:5],
            "mtta_today_min": _minutes(mtta),
            "mttr_7d_min": _minutes(mttr),
        },
        "incidents": {
            "active": not_acked,
            "acknowledged": acked,
            "resolved_today": [_row(i, sent, llm_jobs, mode, db) for i in resolved_today],
        },
        "health": [{"group": name, **counts} for name, counts in sorted(by_type.items())],
        "oncall": {k: oncall.get(k) for k in ("name", "email", "phone")} if oncall else None,
        "ai": {"forgerca": True, "forgeai": mode},
    }


QUIET_STATUSES = ("MAINT", "SUPPRESSED")


def incident_clock(db: Session, now: datetime | None = None, hours: int = 12, tz: str | None = None) -> dict[str, Any]:
    """Incidents opened per local hour for the last ``hours`` hours (oldest first), by severity.

    The newest bucket is the current, partial hour. Quiet incidents (maintenance, noise
    filters) are left out: the clock shows what reached people. Times use the appliance zone.
    """
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz or settings.timezone or "UTC")
    now = _aware(now or utcnow())
    local_now = now.astimezone(zone)
    top = local_now.replace(minute=0, second=0, microsecond=0)
    starts = [top - timedelta(hours=hours - 1 - i) for i in range(hours)]
    buckets = [{"start": s.isoformat(), "hour": s.hour, "critical": 0, "high": 0, "warning": 0} for s in starts]
    since = starts[0].astimezone(timezone.utc)
    rows = (
        db.query(Incident.started_at, Incident.severity)
        .filter(Incident.started_at >= since, Incident.status.notin_(QUIET_STATUSES))
        .all()
    )
    for started_at, severity in rows:
        local = _aware(started_at).astimezone(zone)
        index = int((local - starts[0]).total_seconds() // 3600)
        if 0 <= index < hours:
            key = {"CRITICAL": "critical", "HIGH": "high"}.get(str(severity or "").upper(), "warning")
            buckets[index][key] += 1
    return {
        "now": local_now.isoformat(),
        "hour": local_now.hour,
        "minute": local_now.minute,
        "timezone": str(zone),
        "hours": buckets,
        "total": sum(b["critical"] + b["high"] + b["warning"] for b in buckets),
    }
