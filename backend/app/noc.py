"""Data for the NOC overview page of the new UI: one compact JSON per poll.

Built for a screen that refreshes every few seconds: three small queries for incidents,
one for notifications, one for jobs, one for assets. No evidence, no timelines.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
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


SIMILAR_MINUTES = 15


def forgeai_leader(db: Session, incident: Incident, _depth: int = 0) -> Incident | None:
    """The incident whose ForgeAI analysis covers this one, or None when it gets its own.

    A child of a correlated parent (caused_by) uses the parent's; an incident with the same title
    (alert) that started up to SIMILAR_MINUTES after an earlier open one uses the earlier one, so a
    burst of the same alarm is analysed once instead of filling the model's queue.
    """
    if incident.caused_by_id and _depth == 0:
        parent = db.get(Incident, incident.caused_by_id)
        if parent is not None:
            return parent
    if not incident.started_at or not incident.title:
        return None
    start = _aware(incident.started_at)
    earlier = (db.query(Incident)
               .filter(Incident.title == incident.title, Incident.id != incident.id, Incident.caused_by_id.is_(None),
                       Incident.status.in_(ACTIVE), Incident.started_at <= incident.started_at,
                       Incident.started_at >= start - timedelta(minutes=SIMILAR_MINUTES))
               .order_by(Incident.started_at, Incident.id).first())
    if earlier is not None and (_aware(earlier.started_at), earlier.id) < (start, incident.id):
        # a long burst chains: follow to the first one, which holds the analysis
        root = forgeai_leader(db, earlier, _depth + 1) if _depth < 5 else None
        return root or earlier
    return None


def _llm_failed(db: Session) -> set[str]:
    """Incidents whose newest ForgeAI pass (investigate with use_llm) ended in an error, last 2 days."""
    since = utcnow() - timedelta(days=2)
    newest: dict[str, Job] = {}
    for row in db.query(Job).filter(Job.kind == "investigate", Job.created_at >= since).order_by(Job.id).all():
        if (row.payload or {}).get("use_llm") is True:
            newest[row.object_id] = row
    return {n for n, row in newest.items() if row.status == "error"}


def _llm_queue(db: Session) -> dict[str, int]:
    """Incident number -> place in the ForgeAI queue (1 = next), in the order the lane takes them."""
    from app.jobs import llm_order

    rows = db.query(Job).filter(Job.kind == "investigate", Job.status == "pending").order_by(Job.id).limit(200).all()
    rows = llm_order([r for r in rows if (r.payload or {}).get("use_llm") is True])
    return {r.object_id: i + 1 for i, r in enumerate(rows)}


def _forgeai_state(incident: Incident, llm_jobs: dict[str, str], mode: str, failed: set[str] | None = None, grouped: bool = False) -> str:
    """What the ForgeAI column shows: off, queued, running, ready-local, ready-external, failed, grouped or none."""
    if mode == "off":
        return "off"
    job = llm_jobs.get(incident.number)
    if job in ("pending", "running"):
        return "queued" if job == "pending" else "running"
    if failed and incident.number in failed:
        return "failed"
    for inv in reversed(incident.investigations or []):
        if (inv.provider or "").endswith("-llm"):
            return f"ready-{mode}"
    return "grouped" if grouped else "none"


def _level(cubes: list[dict[str, Any]]) -> str:
    """Highest escalation level this incident already notified (red cube), else L1."""
    fired = [c["level"] for c in cubes if c.get("tone") == "red"]
    return f"L{max(fired)}" if fired else "L1"


def _row(incident: Incident, sent: dict[int, int], llm_jobs: dict[str, str], mode: str, db: Session,
         failed: set[str] | None = None, queue: dict[str, int] | None = None) -> dict[str, Any]:
    asset = incident.asset
    leader = forgeai_leader(db, incident) if mode != "off" else None
    # the ForgeRCA column shows the rules engine, never a ForgeAI (LLM) answer
    latest = next((i for i in reversed(incident.investigations or []) if not (i.provider or "").endswith("-llm")), None)
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
        "forgeai": _forgeai_state(incident, llm_jobs, mode, failed, grouped=leader is not None),
        "forgeai_ref": leader.number.split("_")[0] if leader is not None else "",
        "forgeai_ref_number": leader.number if leader is not None else "",
        "forgeai_queue": (queue or {}).get(incident.number, 0),
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
    failed, queue = _llm_failed(db), _llm_queue(db)
    rows_active = [_row(i, sent, llm_jobs, mode, db, failed, queue) for i in active]
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


DAY_LOOKBACK = 31


def incident_day(db: Session, day: date, tz: str | None = None) -> dict[str, Any]:
    """Incidents opened per local hour on one past day, split into AM (00-11) and PM (12-23).

    Backs the day clock on the wall screen (click a day in the week strip). Same rules as
    incident_clock: quiet incidents are left out, severities are critical / high / warning.
    """
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz or settings.timezone or "UTC")
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = start + timedelta(days=1)
    buckets = [{"hour": h, "critical": 0, "high": 0, "warning": 0} for h in range(24)]
    rows = (
        db.query(Incident.started_at, Incident.severity)
        .filter(Incident.started_at >= start.astimezone(timezone.utc) - timedelta(hours=1),
                Incident.started_at < end.astimezone(timezone.utc) + timedelta(hours=1),
                Incident.status.notin_(QUIET_STATUSES))
        .all()
    )
    for started_at, severity in rows:
        local = _aware(started_at).astimezone(zone)
        if local.date() != day:
            continue
        key = {"CRITICAL": "critical", "HIGH": "high"}.get(str(severity or "").upper(), "warning")
        buckets[local.hour][key] += 1
    totals = {k: sum(b[k] for b in buckets) for k in ("critical", "high", "warning")}
    return {
        "date": day.isoformat(),
        "label": start.strftime("%a %d %b"),
        "timezone": str(zone),
        "am": buckets[:12],
        "pm": buckets[12:],
        "total": sum(totals.values()),
        **totals,
    }
