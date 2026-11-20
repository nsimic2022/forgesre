"""Wall screen (TV mode) data in the style of a weather report: days, hours, and small tiles.

- ``days``: the last 7 days like forecast cards, each with incidents, criticals and the worst level.
- ``hours``: the last 24 hours, one point per hour: incidents open at that hour, new ones by
  severity and the share acknowledged within 15 minutes; maintenance windows as shaded bands.
- ``tiles``: small cards (value, a status word with a trend, one sentence), each with a
  ``detail`` list for the click-through on a monitor.

Everything is read from the database here; nothing calls another system, so the request stays
fast. Times are in the appliance timezone.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models import Asset, DiscoveryCandidate, ForgeAiCall, Incident, Job, MaintenanceWindow, utcnow
from app.settings import settings

SEV_RANK = {"CRITICAL": 3, "HIGH": 2, "WARNING": 1}
QUIET = ("MAINT", "SUPPRESSED")
ACK_TARGET_MIN = 15


def _zone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _minutes(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _trend(now: float | None, before: float | None, *, lower_is_better: bool = True) -> str:
    """up / down / steady; ``better`` / ``worse`` is decided by the caller from lower_is_better."""
    if now is None or before is None:
        return "steady"
    if abs(now - before) <= max(1.0, 0.1 * before):
        return "steady"
    return "down" if now < before else "up"


def _sentence_for(trend: str, what: str, lower_is_better: bool = True) -> str:
    if trend == "steady":
        return f"Steady: {what} is about the same as before."
    better = (trend == "down") == lower_is_better
    return f"{'Better' if better else 'Worse'}: {what} {'fell' if trend == 'down' else 'rose'} compared with before."


def _days(incidents: list[Incident], today: datetime, zone: ZoneInfo) -> list[dict[str, Any]]:
    out = []
    for back in range(6, -1, -1):
        day = (today - timedelta(days=back)).date()
        rows = [i for i in incidents if i.started_at and _aware(i.started_at).astimezone(zone).date() == day]
        sev = [SEV_RANK.get((i.severity or "").upper(), 0) for i in rows]
        worst = max(sev) if sev else 0
        repair = [(_aware(i.resolved_at) - _aware(i.started_at)).total_seconds() / 60 for i in rows if i.resolved_at]
        out.append({
            "date": day.isoformat(), "label": "Today" if back == 0 else "Yesterday" if back == 1 else day.strftime("%a"),
            "day": day.day, "incidents": len(rows), "critical": sev.count(3), "high": sev.count(2),
            "worst": {3: "CRITICAL", 2: "HIGH", 1: "WARNING"}.get(worst, "QUIET"), "mttr_min": _minutes(repair),
        })
    return out


def _hours(db: Session, incidents: list[Incident], now: datetime, zone: ZoneInfo) -> dict[str, Any]:
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    points = []
    for h in range(24):
        a, b = start + timedelta(hours=h), start + timedelta(hours=h + 1)
        new = [i for i in incidents if i.started_at and a <= _aware(i.started_at) < b]
        open_ = [i for i in incidents if i.started_at and _aware(i.started_at) < b and (not i.resolved_at or _aware(i.resolved_at) >= b)]
        fast = [i for i in new if i.ack_at and (_aware(i.ack_at) - _aware(i.started_at)) <= timedelta(minutes=ACK_TARGET_MIN)]
        points.append({
            "at": a.isoformat(), "hour": a.astimezone(zone).hour, "open": len(open_), "new": len(new),
            "critical": sum(1 for i in new if (i.severity or "").upper() == "CRITICAL"),
            "high": sum(1 for i in new if (i.severity or "").upper() == "HIGH"),
            "acked_fast_pct": round(100 * len(fast) / len(new)) if new else None,
        })
    bands = [{"from": max(_aware(w.starts_at), start).isoformat(), "to": min(_aware(w.ends_at), now).isoformat(), "asset": w.asset_ref, "summary": w.summary}
             for w in db.query(MaintenanceWindow).filter(MaintenanceWindow.ends_at >= start, MaintenanceWindow.starts_at <= now).all()]
    return {"from": start.isoformat(), "to": now.isoformat(), "points": points, "maintenance": bands}


def _tile(key: str, title: str, value: Any, unit: str, word: str, trend: str, sentence: str, *,
          tone: str = "info", gauge: float | None = None, detail: list[str] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"key": key, "title": title, "value": value, "unit": unit, "word": word, "trend": trend, "sentence": sentence,
            "tone": tone, "gauge": None if gauge is None else max(0.0, min(1.0, gauge)), "detail": detail or [], **(extra or {})}


def _tiles(db: Session, incidents: list[Incident], now: datetime, zone: ZoneInfo) -> list[dict[str, Any]]:
    from app.dc_noc import current_oncall

    tiles: list[dict[str, Any]] = []
    day = timedelta(days=1)

    def mtta(since: datetime, until: datetime) -> float | None:
        return _minutes([(_aware(i.ack_at) - _aware(i.started_at)).total_seconds() / 60 for i in incidents
                         if i.ack_at and i.started_at and since <= _aware(i.started_at) < until])

    def mttr(since: datetime, until: datetime) -> float | None:
        return _minutes([(_aware(i.resolved_at) - _aware(i.started_at)).total_seconds() / 60 for i in incidents
                         if i.resolved_at and i.started_at and since <= _aware(i.started_at) < until])

    a_now, a_before = mtta(now - day, now), mtta(now - 2 * day, now - day)
    trend = _trend(a_now, a_before)
    tiles.append(_tile("mtta", "Time to acknowledge", a_now if a_now is not None else "–", "min",
                       "On target" if a_now is not None and a_now <= ACK_TARGET_MIN else "Slow" if a_now is not None else "No data",
                       trend, _sentence_for(trend, "the average time to acknowledge (24 h)") if a_now is not None else "Nothing was acknowledged in the last 24 hours.",
                       tone="ok" if a_now is not None and a_now <= ACK_TARGET_MIN else "warn", gauge=None if a_now is None else a_now / (2 * ACK_TARGET_MIN),
                       detail=[f"Target: {ACK_TARGET_MIN} min", f"Day before: {a_before if a_before is not None else '–'} min"]))

    r_now, r_before = mttr(now - 7 * day, now), mttr(now - 14 * day, now - 7 * day)
    trend = _trend(r_now, r_before)
    tiles.append(_tile("mttr", "Time to resolve · 7 days", r_now if r_now is not None else "–", "min",
                       "Fast" if r_now is not None and r_now <= 60 else "Long" if r_now is not None else "No data", trend,
                       _sentence_for(trend, "the average time to resolve") if r_now is not None else "Nothing was resolved in the last 7 days.",
                       tone="ok" if r_now is not None and r_now <= 60 else "warn", gauge=None if r_now is None else r_now / 240,
                       detail=[f"Week before: {r_before if r_before is not None else '–'} min"]))

    try:
        from app.slo import evaluate_all

        slos = evaluate_all(db, now)
    except Exception:  # noqa: BLE001 — an SLO problem must not blank the wall screen
        slos = []
    if slos:
        worst = min(slos, key=lambda s: 100 - s["budget_used_pct"])
        left = round(100 - worst["budget_used_pct"], 1)
        tiles.append(_tile("slo", "Error budget", max(left, 0), "% left",
                           {"ok": "Healthy", "at_risk": "At risk", "breached": "Breached"}.get(worst["status"], worst["status"]),
                           "up" if worst["burn_rate_24h"] > 1 else "steady",
                           f"{worst['name']}: {worst['availability']}% over {worst['window_days']} days, burning {worst['burn_rate_24h']}x the even rate today.",
                           tone={"ok": "ok", "at_risk": "warn", "breached": "crit"}.get(worst["status"], "info"), gauge=left / 100,
                           detail=[f"{s['name']}: {round(100 - s['budget_used_pct'], 1)}% left" for s in slos[:6]]))

    duty = current_oncall(db, now)
    local = now.astimezone(zone)
    if duty:
        sh, sm = (int(x) for x in duty["start"].split(":"))
        eh, em = (int(x) for x in duty["end"].split(":"))
        span = max(1, (eh * 60 + em) - (sh * 60 + sm))
        done = (local.hour * 60 + local.minute) - (sh * 60 + sm)
        left = max(0, span - done)
        tiles.append(_tile("shift", "Shift", duty.get("person") or duty.get("name") or "On call", "",
                           f"{left // 60} h {left % 60:02d} min left", "steady", f"On call {duty['start']}–{duty['end']}" + (f" · {duty['phone']}" if duty.get("phone") else ""),
                           tone="info", gauge=done / span, extra={"start": duty["start"], "end": duty["end"]},
                           detail=[duty.get("email") or "", duty.get("phone2") or ""]))
    else:
        tiles.append(_tile("shift", "Shift", "Nobody", "", "No roster", "steady", "No on-call slot covers this hour.", tone="warn"))

    try:
        from app.backup import _row_when, list_archives

        archives = list_archives()  # a directory listing on this server; no other system is called
        newest = max((_aware(_row_when(r)) for r in archives), default=None)
        newest_label = archives[0].get("label", "") if archives else ""
    except Exception:  # noqa: BLE001 — an unreadable backup folder shows as "missing"
        newest, newest_label = None, ""
    if newest is not None:
        hours = round((now - newest).total_seconds() / 3600, 1)
        tiles.append(_tile("backup", "Last backup", hours, "h ago", "Fresh" if hours <= 26 else "Old", "steady",
                           "Backups run every night; older than a day needs a look." if hours > 26 else "The last backup is from the last day.",
                           tone="ok" if hours <= 26 else "crit", gauge=min(hours / 48, 1.0), detail=[newest_label]))
    else:
        tiles.append(_tile("backup", "Last backup", "never", "", "Missing", "steady", "No backup on this server. Run ./forgesre backup.", tone="crit"))

    since = now - day
    noisy = Counter(i.asset.hostname for i in incidents if i.asset is not None and i.started_at and _aware(i.started_at) >= since)
    top = noisy.most_common(5)
    tiles.append(_tile("noisy", "Noisiest assets · 24 h", top[0][1] if top else 0, "incidents", top[0][0] if top else "Quiet",
                       "steady", "The asset with the most new incidents in the last 24 hours." if top else "No asset had an incident in the last 24 hours.",
                       tone="warn" if top and top[0][1] >= 3 else "info", detail=[f"{h}: {n}" for h, n in top],
                       extra={"bars": [{"label": h, "value": n} for h, n in top]}))

    pending = db.query(Job).filter(Job.kind.in_(["forgeai", "investigate"]), Job.status.in_(["pending", "running"])).count()
    calls = db.query(ForgeAiCall).filter(ForgeAiCall.created_at >= since).all()
    used = sum(1 for c in calls if c.status in {"used", "trial"})
    rejected = sum(1 for c in calls if c.status == "rejected")
    tiles.append(_tile("forgeai", "ForgeAI", pending, "waiting", "Off" if not settings.ai_enabled else "Busy" if pending else "Idle", "steady",
                       f"{used} answers and {rejected} rejected by the checks in 24 hours." if settings.ai_enabled else "ForgeAI is off; ForgeRCA still analyses every incident.",
                       tone="off" if not settings.ai_enabled else "info", detail=[f"{len(calls)} questions in 24 h"]))

    hosts = db.query(Asset).all()
    down = [a.hostname for a in hosts if a.ping_status == "red" or a.exporter_status == "red"]
    tiles.append(_tile("hosts", "Hosts answering", len(hosts) - len(down), f"of {len(hosts)}", "All up" if not down else f"{len(down)} down", "steady",
                       "Every monitored host answers." if not down else "Down: " + ", ".join(down[:4]) + ("…" if len(down) > 4 else ""),
                       tone="ok" if not down else "crit", gauge=(len(hosts) - len(down)) / len(hosts) if hosts else None, detail=down[:12]))

    found = db.query(DiscoveryCandidate).filter(DiscoveryCandidate.status == "new").count()
    last = db.query(Job).filter(Job.kind == "discovery_scan", Job.status.in_(["done", "error"])).order_by(Job.id.desc()).first()
    when = "never" if last is None or not last.finished_at else f"{round((now - _aware(last.finished_at)).total_seconds() / 3600, 1)} h ago"
    tiles.append(_tile("discovery", "New devices", found, "waiting", "To review" if found else "Nothing new", "steady",
                       f"From LibreNMS; last read {when}." + (" The last read failed." if last is not None and last.status == "error" else ""),
                       tone="warn" if found else "info", detail=[f"Last read: {when}"]))
    return tiles


def tv_overview(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Days, hours and tiles for the wall screen (added to the NOC overview by GET /api/v1/tv)."""
    now = _aware(now) or utcnow()
    zone = _zone()
    since = now - timedelta(days=14)
    from sqlalchemy.orm import joinedload

    rows = db.query(Incident).options(joinedload(Incident.asset)).filter(Incident.started_at >= since).all()
    incidents = [i for i in rows if (i.status or "").upper() not in QUIET]
    today = now.astimezone(zone)
    return {"days": _days(incidents, today, zone), "hours": _hours(db, incidents, now, zone), "tiles": _tiles(db, incidents, now, zone)}
