from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.audit import audit
from app.journal import report
from app.metrics import reset_demo_gauges, set_demo_cpu, set_demo_disk
from app.models import (
    Asset,
    Evidence,
    Incident,
    IncidentEvent,
    Investigation,
    Job,
    MaintenanceWindow,
    MailContact,
    Notification,
    Playbook,
    Playrule,
    ScheduledReport,
    User,
    utcnow,
)
from app.seed import (
    DEMO_ASSET,
    DEMO_SW_ASSET,
    DEMO_WIN_ASSET,
    ensure_demo_asset,
    ensure_demo_similar_history,
    ensure_demo_switch_asset,
    ensure_demo_windows_asset,
    is_demo_asset_id,
    seed,
)
from app.email_service import send_smtp, smtp_ssl_context
from app.incident_report_mail import build_incident_report, build_incident_report_html
from app.notifications import build_escalation_body, build_escalation_html
from app.settings import settings

log = logging.getLogger("forgesre")

DEMO_MAIL_MARK = "[DEMO]"
DEMO_BODY_LINE = "DEMO incident on forge-demo-01. Lab only — not a production fire."


def demo_body_line(incident: Incident | None = None) -> str:
    host = DEMO_ASSET
    if incident is not None:
        asset = getattr(incident, "asset", None)
        if asset is not None:
            host = str(getattr(asset, "hostname", "") or getattr(asset, "asset_id", "") or host)
        else:
            fingerprint = str(getattr(incident, "fingerprint", "") or "")
            if ":" in fingerprint and is_demo_asset_id(fingerprint.split(":", 1)[-1]):
                host = fingerprint.split(":", 1)[-1]
    return f"DEMO incident on {host}. Lab only — not a production fire."


def is_demo_incident(incident: Incident | None) -> bool:
    """True when the row belongs to a seeded forge-demo-* lab asset. No extra DB column."""
    if incident is None:
        return False
    asset = getattr(incident, "asset", None)
    if asset is not None and is_demo_asset_id(getattr(asset, "asset_id", "") or getattr(asset, "hostname", "")):
        return True
    payload = incident.alert_payload if isinstance(getattr(incident, "alert_payload", None), dict) else {}
    labels = payload.get("labels") if isinstance(payload, dict) else None
    if isinstance(labels, dict) and (
        is_demo_asset_id(str(labels.get("asset") or "")) or is_demo_asset_id(str(labels.get("instance") or ""))
    ):
        return True
    fingerprint = str(getattr(incident, "fingerprint", "") or "")
    if ":" in fingerprint:
        return is_demo_asset_id(fingerprint.split(":", 1)[-1])
    return is_demo_asset_id(fingerprint)


HOST_DOWN_ALERTNAMES = (
    "NodeExporterDown",
    "WindowsExporterDown",
    "SnmpDeviceUnreachable",
)

HOST_DOWN_STATUSES_HIDDEN = ("RESOLVED", "CLOSED")


def incident_alertname(incident: Incident | None) -> str:
    """Alertname from labels, else the fingerprint prefix (Alertname:asset)."""
    if incident is None:
        return ""
    payload = incident.alert_payload if isinstance(getattr(incident, "alert_payload", None), dict) else {}
    labels = payload.get("labels") if isinstance(payload, dict) else None
    if isinstance(labels, dict):
        name = str(labels.get("alertname") or "").strip()
        if name:
            return name
    fingerprint = str(getattr(incident, "fingerprint", "") or "")
    if ":" in fingerprint:
        return fingerprint.split(":", 1)[0]
    return fingerprint


def is_host_down_incident(incident: Incident | None) -> bool:
    """Linux node_exporter down, Windows exporter down, or SNMP device unreachable."""
    return incident_alertname(incident) in HOST_DOWN_ALERTNAMES


def list_host_down_incidents(db: Session, limit: int = 12) -> list[Incident]:
    """Open host/SNMP-down incidents for the dashboard banner (newest first)."""
    cap = max(1, min(int(limit or 12), 20))
    rows = (
        db.query(Incident)
        .filter(Incident.status.notin_(HOST_DOWN_STATUSES_HIDDEN))
        .order_by(Incident.id.desc())
        .limit(80)
        .all()
    )
    return [row for row in rows if is_host_down_incident(row)][:cap]


def host_down_public(incident: Incident) -> dict[str, Any]:
    asset = getattr(incident, "asset", None)
    hostname = ""
    if asset is not None:
        hostname = str(getattr(asset, "hostname", "") or getattr(asset, "asset_id", "") or "")
    return {
        "number": incident.number,
        "title": incident.title or "",
        "status": incident.status,
        "severity": incident.severity,
        "alertname": incident_alertname(incident),
        "hostname": hostname,
        "demo": is_demo_incident(incident),
    }


def is_demo_mail(note: Notification | None) -> bool:
    """Escalation/outbox rows: DEMO prefix on the subject, or the linked incident."""
    if note is None:
        return False
    subject = str(getattr(note, "subject", "") or "")
    if subject.upper().startswith(DEMO_MAIL_MARK):
        return True
    return is_demo_incident(getattr(note, "incident", None))


def is_demo_journal(row: Any) -> bool:
    """Console rows for demo module, DEMO-prefixed summaries, or forge-demo-* object ids."""
    if row is None:
        return False
    if str(getattr(row, "module", "") or "") == "demo":
        return True
    summary = str(getattr(row, "summary", "") or "")
    head = summary.lstrip().upper()
    if head.startswith("DEMO") or head.startswith(DEMO_MAIL_MARK):
        return True
    return is_demo_asset_id(getattr(row, "object_id", None))


def demo_mail_subject(incident: Incident | None, subject: str) -> str:
    text = (subject or "").strip()
    if is_demo_incident(incident) and not text.upper().startswith(DEMO_MAIL_MARK):
        return f"{DEMO_MAIL_MARK} {text}"
    return text


def incident_seq(number: str) -> int | None:
    """Running counter from INC-000012, dash-dated, or INC-0134_16.08.2026_09:13."""
    text = str(number or "")
    if not text.upper().startswith("INC-"):
        return None
    rest = text.split("-", 1)[-1]
    head = rest.split("_", 1)[0] if "_" in rest else rest.split("-", 1)[0]
    if not head.isdigit():
        return None
    return int(head)


def incident_short_label(number: str) -> str:
    """Visible list label: #42 from INC-0042_…. Full id stays in the URL."""
    seq = incident_seq(number)
    return f"#{seq}" if seq is not None else str(number or "")


def parse_incident_wall(number: str) -> tuple[str, str] | None:
    """(DD.MM.YYYY, HH:MM) baked into INC-NNNN_DD.MM.YYYY_HH:MM."""
    parts = str(number or "").split("_")
    if len(parts) < 3:
        return None
    date, clock = parts[1], parts[2]
    if len(date) >= 10 and date[2:3] == "." and len(clock) >= 5 and clock[2:3] == ":":
        return date[:10], clock[:5]
    return None


def _appliance_local(when: datetime | None = None) -> datetime:
    stamp = when or utcnow()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    try:
        return stamp.astimezone(ZoneInfo(settings.timezone))
    except Exception:
        return stamp.astimezone(timezone.utc)


def incident_when_label(number: str, now: datetime | None = None) -> str:
    """Same calendar day as now → HH:MM; older → DD.MM HH:MM. Empty if the id has no wall clock."""
    parsed = parse_incident_wall(number)
    if parsed is None:
        return ""
    date, clock = parsed
    today = _appliance_local(now).strftime("%d.%m.%Y")
    if date == today:
        return clock
    return f"{date[:5]} {clock}"


def format_started_at(value: Any) -> str:
    """Full started_at for tooltips, without Postgres microseconds."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return _appliance_local(value).strftime("%d.%m.%Y %H:%M:%S")
    text = str(value).strip().replace("T", " ")
    if not text:
        return ""
    if "." in text:
        head, tail = text.split(".", 1)
        tz = ""
        for idx, ch in enumerate(tail):
            if ch in "+-Z":
                tz = tail[idx:]
                break
        return f"{head}{tz}"
    return text


def short_when_label(value: Any, now: datetime | None = None) -> str:
    """Same calendar day → HH:MM; older → DD.MM HH:MM from an appliance-local datetime."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        local = _appliance_local(value)
        today = _appliance_local(now)
        if local.date() == today.date():
            return local.strftime("%H:%M")
        return local.strftime("%d.%m %H:%M")
    text = format_started_at(value)
    return text[:16] if text else ""


def severity_pill(severity: str) -> str:
    """List severity chip: critical stays red even when the row is resolved."""
    sev = str(severity or "").upper()
    if sev in {"CRITICAL", "CRIT", "FATAL", "EMERGENCY"}:
        return "crit"
    return "warn"


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def duration_label(seconds: float) -> str:
    """18m, 2h 14m, 4d 3h. Under a minute is <1m."""
    minutes = max(0, int(seconds)) // 60
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hrs = divmod(hours, 24)
    return f"{days}d {hrs}h" if hrs else f"{days}d"


def incident_is_live(status: str) -> bool:
    return str(status or "").upper() in ACTIVE_INCIDENT_STATUSES


def _last_timeline_at(incident: Incident) -> datetime | None:
    latest: datetime | None = None
    for item in getattr(incident, "timeline", None) or []:
        raw = item.get("at") if isinstance(item, dict) else None
        if not raw:
            continue
        try:
            stamp = _as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            continue
        if stamp is not None and (latest is None or stamp > latest):
            latest = stamp
    return latest


def incident_end(incident: Incident) -> datetime | None:
    """When a RESOLVED/CLOSED incident stopped: person resolve time, else alert end, else last timeline step."""
    return (
        _as_utc(getattr(incident, "resolved_at", None))
        or _as_utc(getattr(incident, "ended_at", None))
        or _last_timeline_at(incident)
    )


def incident_duration(incident: Incident, now: datetime | None = None) -> str:
    """Live: started_at → now. Done: started_at → resolved/ended. Empty when unknown."""
    start = _as_utc(getattr(incident, "started_at", None))
    if start is None:
        return ""
    if incident_is_live(incident.status):
        end = _as_utc(now) or utcnow()
    else:
        end = incident_end(incident)
        if end is None:
            return ""
    return duration_label((end - start).total_seconds())


def incident_wall(incident: Incident, now: datetime | None = None) -> str:
    """13:07 today, 09.09 13:07 otherwise — from started_at; the id clock only when started_at is missing."""
    return (
        short_when_label(incident.started_at, now)
        or incident_when_label(incident.number, now)
        or format_started_at(incident.started_at)
    )


def incident_when(incident: Incident, now: datetime | None = None) -> dict[str, Any]:
    """When column: duration first for every status (open for / lasted), start wall clock second."""
    live = incident_is_live(incident.status)
    wall = incident_wall(incident, now)
    full = format_started_at(incident.started_at)
    duration = incident_duration(incident, now)
    verb = "Open for" if live else "Lasted"
    end = None if live else incident_end(incident)
    parts = [f"{verb} {duration}"] if duration else []
    if full:
        parts.append(f"started {full}")
    if end is not None and duration:
        parts.append(f"ended {format_started_at(end)}")
    title = " · ".join(parts) or full
    return {
        "live": live,
        "primary": duration or wall,
        "secondary": wall if duration else "",
        "wall": wall,
        "duration": duration,
        "verb": verb,
        "ended": short_when_label(end, now) if end is not None else "",
        "ended_full": format_started_at(end) if end is not None else "",
        "title": title[:1].upper() + title[1:] if title else "",
    }


def incident_host(incident: Incident | None) -> str:
    """Matched asset hostname, else the alert's asset/instance label (port dropped)."""
    if incident is None:
        return ""
    asset = getattr(incident, "asset", None)
    if asset is not None:
        name = str(getattr(asset, "hostname", "") or getattr(asset, "asset_id", "") or "").strip()
        if name:
            return name
    payload = incident.alert_payload if isinstance(getattr(incident, "alert_payload", None), dict) else {}
    labels = payload.get("labels") if isinstance(payload, dict) else None
    name = ""
    if isinstance(labels, dict):
        name = str(labels.get("asset") or labels.get("instance") or "").strip()
    if name == UNLABELED_ASSET:
        return ""
    head, sep, port = name.rpartition(":")
    if sep and port.isdigit() and ":" not in head:
        name = head
    return name


def format_incident_number(seq: int, when: datetime | None = None) -> str:
    """INC-0134_16.08.2026_09:13 in the appliance timezone (wall clock)."""
    local = _appliance_local(when)
    return f"INC-{seq:04d}_{local:%d.%m.%Y}_{local:%H:%M}"


def next_incident_number(db: Session, when: datetime | None = None) -> str:
    highest = 0
    for (number,) in db.query(Incident.number).all():
        seq = incident_seq(str(number or ""))
        if seq is not None:
            highest = max(highest, seq)
    return format_incident_number(highest + 1, when)


def match_playrule(
    db: Session,
    alertname: str,
    labels: dict[str, Any] | None = None,
    asset: Asset | None = None,
) -> Playrule | None:
    """Alertname only. condition.metric/operator/value are operator notes and are never evaluated.

    An asset's Client playrules (assets.playrule_ids) are tried first, in the order saved; the
    first enabled one whose alertname matches wins. Otherwise the global first-by-id match.
    """
    del labels
    wanted = (alertname or "").strip().lower()
    if not wanted:
        return None
    rules = db.query(Playrule).filter_by(enabled=True).order_by(Playrule.id).all()

    def _hits(rule: Playrule) -> bool:
        expected = str((rule.condition or {}).get("alertname") or "").strip().lower()
        return bool(expected) and expected == wanted

    from app.asset_extras import asset_playrule_ids

    by_id = {rule.id: rule for rule in rules}
    for pk in asset_playrule_ids(asset):
        rule = by_id.get(pk)
        if rule is not None and _hits(rule):
            return rule
    for rule in rules:
        if _hits(rule):
            return rule
    return None


def playrule_from_asset(rule: Playrule | None, asset: Asset | None) -> bool:
    """True when the matched playrule is one the asset lists as a Client playrule."""
    from app.asset_extras import asset_playrule_ids

    return bool(rule is not None and rule.id in asset_playrule_ids(asset))


def append_timeline(incident: Incident, node_id: str, title: str, detail: str) -> None:
    timeline = list(incident.timeline or [])
    if any(item.get("id") == node_id for item in timeline):
        for item in timeline:
            if item.get("id") == node_id:
                item["detail"] = detail
                item["at"] = utcnow().isoformat()
        incident.timeline = timeline
        return
    timeline.append(
        {
            "id": node_id,
            "title": title,
            "detail": detail,
            "at": utcnow().isoformat(),
        }
    )
    incident.timeline = timeline


def refresh_asset_status(db: Session, asset: Asset | None) -> None:
    if asset is None:
        return
    open_incidents = (
        db.query(Incident)
        .filter(
            Incident.asset_id == asset.id,
            Incident.status.in_(["OPEN", "INVESTIGATING", "ESCALATED"]),
        )
        .all()
    )
    if any(item.severity.upper() in {"CRITICAL", "HIGH"} for item in open_incidents):
        asset.status = "critical"
    elif open_incidents:
        asset.status = "warning"
    else:
        asset.status = "healthy"


ACTIVE_INCIDENT_STATUSES = ("OPEN", "INVESTIGATING", "ESCALATED")
UNLABELED_ASSET = "unlabeled"
INCIDENT_SOURCES = {"prometheus": "Prometheus", "zabbix": "Zabbix"}
_RESOLVED_BY = {"prometheus": "Alertmanager", "zabbix": "Zabbix"}


def incident_source(incident: Incident | None) -> str:
    """prometheus | zabbix. Rows from before the column existed are Prometheus."""
    value = str(getattr(incident, "source", "") or "").strip().lower()
    return value if value in INCIDENT_SOURCES else "prometheus"


def incident_source_label(incident: Incident | None) -> str:
    return INCIDENT_SOURCES[incident_source(incident)]


def _loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip in {"::1", "0.0.0.0", "localhost"}


def match_alert_asset(
    db: Session,
    name: str = "",
    *,
    ip: str = "",
    zabbix_hostid: str = "",
) -> Asset | None:
    """Asset for an alert: Zabbix host id, then asset_id / hostname, then IP.

    Prometheus alerts only carry ``asset``/``instance`` so they keep the old
    asset_id-or-hostname match. IP matching skips lab rows and loopback.
    """
    from app.demo_ids import is_lab_inventory_row

    hostid = (zabbix_hostid or "").strip()
    if hostid:
        found = db.query(Asset).filter(Asset.zabbix_hostid == hostid).first()
        if found is not None:
            return found
    name = (name or "").strip()
    if name:
        found = db.query(Asset).filter((Asset.asset_id == name) | (Asset.hostname == name)).first()
        if found is not None:
            return found
        lowered = name.lower()
        found = (
            db.query(Asset)
            .filter((func.lower(Asset.hostname) == lowered) | (func.lower(Asset.asset_id) == lowered))
            .first()
        )
        if found is not None and (ip or hostid):
            return found
    ip = (ip or "").strip()
    if ip and not _loopback(ip):
        for row in db.query(Asset).filter(Asset.ip == ip).order_by(Asset.id).all():
            if not is_lab_inventory_row(row):
                return row
    return None


def alert_fingerprint(alert: dict[str, Any]) -> str:
    labels = alert.get("labels") or {}
    forge = alert.get("forge") if isinstance(alert.get("forge"), dict) else {}
    alertname = str(labels.get("alertname") or "Alert")
    asset_name = str(labels.get("asset") or labels.get("instance") or "").strip()
    return str(forge.get("fingerprint") or "").strip()[:255] or f"{alertname}:{asset_name or UNLABELED_ASSET}"


def ingest_alertmanager(db: Session, payload: dict[str, Any], *, source: str = "prometheus") -> list[Incident]:
    """Alertmanager (or Zabbix, normalized to the same shape) → incidents.

    One active incident per fingerprint: ``alertname:asset`` for Prometheus,
    ``zabbix:{triggerid}:{host}`` for Zabbix (``alert["forge"]["fingerprint"]``).
    A firing alert after that incident went RESOLVED opens a new INC (fresh
    escalation ladder) and links both timelines. CLOSED is final. A resolved
    alert marks the active incident RESOLVED; it never closes it.

    Prometheus: Alertmanager groups by [alertname, asset] and resends the whole
    group, so one fingerprint can carry several series (interfaces, mountpoints).
    The incident resolves only when every series of that fingerprint in the
    payload is resolved, whatever their order. Zabbix events stay one-by-one.
    """
    from app.jobs import enqueue

    source = source if source in INCIDENT_SOURCES else "prometheus"
    resolver = _RESOLVED_BY[source]
    created: list[Incident] = []
    group_status = (payload.get("status") or "firing").lower()
    alerts = list(payload.get("alerts") or [])
    grouped = source == "prometheus"
    still_firing: set[str] = set()
    if grouped:
        still_firing = {
            alert_fingerprint(alert)
            for alert in alerts
            if (alert.get("status") or group_status).lower() != "resolved"
        }
    resolved_here: set[str] = set()
    for alert in alerts:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        alert_status = (alert.get("status") or group_status).lower()
        alertname = str(labels.get("alertname") or "Alert")
        asset_name = str(labels.get("asset") or labels.get("instance") or "").strip()
        fingerprint = alert_fingerprint(alert)
        if grouped and alert_status == "resolved" and (fingerprint in still_firing or fingerprint in resolved_here):
            continue
        incident = (
            db.query(Incident)
            .filter(Incident.fingerprint == fingerprint, Incident.status.in_(ACTIVE_INCIDENT_STATUSES))
            .order_by(Incident.id.desc())
            .first()
        )
        asset = None
        if asset_name or labels.get("ip") or labels.get("zabbix_hostid"):
            asset = match_alert_asset(
                db,
                asset_name,
                ip=str(labels.get("ip") or ""),
                zabbix_hostid=str(labels.get("zabbix_hostid") or ""),
            )
        if alert_status == "resolved":
            resolved_here.add(fingerprint)
            if incident:
                incident.status = "RESOLVED"
                incident.ended_at = utcnow()
                append_timeline(incident, "alert", "ALERT", f"{alertname} resolved by {resolver}")
                db.add(IncidentEvent(incident_id=incident.id, kind="resolved", data=labels))
                refresh_asset_status(db, asset)
                if asset and asset.asset_id == DEMO_ASSET:
                    reset_demo_gauges()
            continue
        previous_resolved = None
        if incident is None:
            previous_resolved = (
                db.query(Incident)
                .filter(Incident.fingerprint == fingerprint, Incident.status == "RESOLVED")
                .order_by(Incident.id.desc())
                .first()
            )
        if incident is None:
            from app.asset_alarms import bundled_alert_skip_reason

            skip = bundled_alert_skip_reason(asset, alertname, alert)
            if skip:
                report(
                    db,
                    "incident",
                    "suppress",
                    "ok",
                    summary=f"Skipped {alertname} on {asset_name}",
                    detail=skip,
                    object_type="asset",
                    object_id=str(getattr(asset, "asset_id", "") or asset_name),
                )
                continue
            rule = match_playrule(db, alertname, labels, asset=asset)
            incident = Incident(
                number=next_incident_number(db),
                title=str(annotations.get("summary") or alertname),
                severity=str(labels.get("severity") or (rule.severity if rule else "warning")).upper(),
                status="OPEN",
                fingerprint=fingerprint,
                source=source,
                asset_id=asset.id if asset else None,
                playrule_id=rule.id if rule else None,
                playbook_id=rule.playbook_id if rule else None,
                summary=str(annotations.get("description") or ""),
                alert_payload=alert,
                timeline=[],
            )
            db.add(incident)
            db.flush()
            fired = f"{alertname} fired" if source == "prometheus" else f"{alertname} fired in Zabbix"
            append_timeline(incident, "alert", "ALERT", fired)
            append_timeline(incident, "incident", "INCIDENT", f"{incident.number} created")
            if previous_resolved is not None:
                append_timeline(
                    incident,
                    "refire",
                    "RE-FIRED",
                    f"Same alert fired again after {previous_resolved.number} was RESOLVED",
                )
                append_timeline(
                    previous_resolved,
                    "refire",
                    "RE-FIRED",
                    f"Fired again as {incident.number}",
                )
            if rule:
                via = " (asset client playrule)" if playrule_from_asset(rule, asset) else ""
                append_timeline(incident, "playrule", "PLAYRULE", f"{rule.name}{via}")
                if rule.playbook:
                    append_timeline(incident, "playbook", "PLAYBOOK", rule.playbook.name)
            db.add(IncidentEvent(incident_id=incident.id, kind="created", data=labels))
            audit(
                db,
                action="incident.create",
                object_type="incident",
                object_id=incident.number,
                data={
                    "alertname": alertname,
                    **({"source": source} if source != "prometheus" else {}),
                    **({"refire_of": previous_resolved.number} if previous_resolved is not None else {}),
                },
            )
            created.append(incident)
        if incident.id and not db.query(Evidence.id).filter_by(incident_id=incident.id).first():
            collect_evidence(db, incident, alert)
        refresh_asset_status(db, asset)
        ensure_notification(db, incident, step_key="immediate")
    db.commit()
    for incident in created:
        db.refresh(incident)
        mark = "DEMO " if is_demo_incident(incident) else ""
        report(
            db,
            "incident",
            "create",
            "ok",
            summary=f"{mark}{incident.number} {incident.title}",
            detail=(
                f"asset={incident.asset.hostname if incident.asset else 'unknown'} "
                f"fingerprint={incident.fingerprint} source={source}"
            ),
            object_type="incident",
            object_id=incident.number,
        )
        enqueue(db, "investigate", incident.number, payload={"actor": "system", "use_llm": False})
    return created


def collect_evidence(db: Session, incident: Incident, alert: dict[str, Any] | None = None) -> None:
    alert = alert or incident.alert_payload or {}
    labels = alert.get("labels") if isinstance(alert, dict) else {}
    if not labels and isinstance(alert, dict):
        labels = alert
    metrics = query_prometheus(incident.asset)
    from rca.collector import DEMO_LOGS_LIMITATION, HOST_LOGS_LIMITATION, loki_query_for

    asset_dict = {}
    if incident.asset:
        asset_dict = {
            "asset_id": incident.asset.asset_id,
            "type": incident.asset.type,
            "hostname": incident.asset.hostname,
        }
    loki_query = loki_query_for(asset_dict)
    if loki_query:
        logs = query_loki(query=loki_query)
        log_payload: dict[str, Any] = {"lines": logs, "query": loki_query}
        if str(asset_dict.get("asset_id") or "") == "forge-demo-01":
            log_payload["scope"] = "appliance-demo"
            log_payload["label"] = "DEMO"
            log_payload["note"] = DEMO_LOGS_LIMITATION
    else:
        logs = []
        log_payload = {"skipped": True, "reason": HOST_LOGS_LIMITATION, "lines": []}
    history_rows = (
        db.query(Incident)
        .filter(Incident.asset_id == incident.asset_id, Incident.id != incident.id)
        .order_by(Incident.id.desc())
        .limit(5)
        .all()
    )
    history = [{"number": item.number, "title": item.title, "status": item.status, "severity": item.severity} for item in history_rows]
    items = [
        ("alert", "Alert", alert),
        ("metrics", "Metrics", metrics),
        ("logs", "Logs", log_payload),
        ("history", "Previous incidents", {"incidents": history}),
    ]
    if incident.asset:
        items.insert(
            1,
            (
                "asset",
                "Asset",
                {
                    "asset_id": incident.asset.asset_id,
                    "hostname": incident.asset.hostname,
                    "ip": incident.asset.ip,
                    "status": incident.asset.status,
                },
            ),
        )
    for kind, title, payload in items:
        rollup_id = f"ROLLUP-{kind}"
        existing = (
            db.query(Evidence)
            .filter(Evidence.incident_id == incident.id, Evidence.evidence_id == rollup_id)
            .first()
        )
        if existing is None:
            existing = (
                db.query(Evidence)
                .filter(Evidence.incident_id == incident.id, Evidence.kind == kind, Evidence.hash == "")
                .first()
            )
        if existing:
            existing.payload = payload
            existing.captured_at = utcnow()
            existing.evidence_id = rollup_id
        else:
            db.add(Evidence(incident_id=incident.id, kind=kind, title=title, payload=payload, evidence_id=rollup_id))
    persist_rca_evidence(db, incident, alert, metrics, logs, history)
    append_timeline(incident, "evidence", "EVIDENCE", "Alert, metrics, logs, and history captured")
    db.commit()


def persist_rca_evidence(
    db: Session,
    incident: Incident,
    alert: dict[str, Any],
    metrics: dict[str, Any],
    logs: list[str],
    history: list[dict[str, Any]],
) -> None:
    from rca.collector import collect_evidence_set

    labels = (alert.get("labels") if isinstance(alert, dict) else None) or alert or {}
    playrules = []
    if incident.playrule:
        playrules.append(
            {
                "name": incident.playrule.name,
                "playbook": incident.playbook.name if incident.playbook else "",
                "condition": incident.playrule.condition,
            }
        )
    asset = {}
    if incident.asset:
        asset = {
            "asset_id": incident.asset.asset_id,
            "hostname": incident.asset.hostname,
            "ip": incident.asset.ip,
            "type": incident.asset.type,
            "monitoring_profile": incident.asset.monitoring_profile,
            "scrape_address": incident.asset.scrape_address,
            "status": incident.asset.status,
        }
    maintenance = overlapping_maintenance(db, asset.get("asset_id") or "", incident.started_at or utcnow())

    query_map = (metrics or {}).get("queries") or {}
    values_by_expr: dict[str, Any] = {}
    for key, expr in query_map.items():
        if key in {"queries", "error"}:
            continue
        if key in metrics:
            values_by_expr[str(expr)] = {"value": metrics[key], "query": expr}
    prom_error = metrics.get("error") if isinstance(metrics, dict) else None

    def metric_fetcher(expr: str) -> dict[str, Any]:
        if expr in values_by_expr:
            return values_by_expr[expr]
        if prom_error:
            return {"error": prom_error, "query": expr}
        return {"value": None, "query": expr}

    def log_fetcher(query: str, start, end) -> dict[str, Any]:
        del query, start, end
        return {"lines": list(logs or [])}

    bundle, _limitations = collect_evidence_set(
        incident={"number": incident.number, "title": incident.title, "severity": incident.severity, "asset": asset.get("hostname")},
        asset=asset,
        alert=labels if isinstance(labels, dict) else {},
        history=history,
        playrules=playrules,
        maintenance=maintenance,
        metric_fetcher=metric_fetcher,
        log_fetcher=log_fetcher if settings.loki_enabled else None,
        window_minutes=settings.rca_window_minutes,
        max_log_lines=settings.rca_max_log_lines,
    )
    for item in bundle:
        if item.hash and db.query(Evidence).filter_by(incident_id=incident.id, hash=item.hash).first():
            continue
        db.add(
            Evidence(
                incident_id=incident.id,
                kind=item.type,
                title=f"{item.type} {item.evidence_id}",
                payload=item.to_dict(),
                evidence_id=item.evidence_id,
                source=item.source,
                query=item.query,
                asset_ref=item.asset_id,
                hash=item.hash,
                confidence=item.confidence,
            )
        )


def overlapping_maintenance(db: Session, asset_ref: str, at: datetime) -> list[dict[str, Any]]:
    if not asset_ref:
        return []
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    rows = (
        db.query(MaintenanceWindow)
        .filter(MaintenanceWindow.asset_ref == asset_ref, MaintenanceWindow.starts_at <= at, MaintenanceWindow.ends_at >= at)
        .all()
    )
    return [
        {
            "asset_ref": row.asset_ref,
            "summary": row.summary,
            "starts_at": row.starts_at.isoformat() if row.starts_at else "",
            "ends_at": row.ends_at.isoformat() if row.ends_at else "",
        }
        for row in rows
    ]


def query_prometheus(asset: Asset | None = None) -> dict[str, Any]:
    from rca.collector import promql_queries_for

    asset_dict: dict[str, Any] = {}
    if asset:
        asset_dict = {
            "asset_id": asset.asset_id,
            "hostname": asset.hostname,
            "ip": asset.ip,
            "type": asset.type,
            "monitoring_profile": asset.monitoring_profile,
            "scrape_address": asset.scrape_address,
        }
    demo = bool(asset and asset.asset_id == DEMO_ASSET)
    packed = promql_queries_for(asset_dict)
    queries = {key: expr for key, (expr, _unit) in packed.items()}
    out: dict[str, Any] = {"queries": dict(queries)}
    try:
        for key, expr in queries.items():
            sample = query_prometheus_expr(expr)
            if sample.get("error"):
                out["error"] = sample["error"]
                break
            if "value" in sample and sample["value"] is not None:
                out[key] = sample["value"]
    except Exception as exc:
        out["error"] = str(exc)
    if demo:
        from app.metrics import demo_metric_values

        live = demo_metric_values()
        out["cpu_percent"] = live["forgesre_demo_cpu_percent"]
        out["disk_percent"] = live["forgesre_demo_disk_percent"]
        out.setdefault("queries", {})["cpu_percent"] = "forgesre_demo_cpu_percent"
        out.setdefault("queries", {})["disk_percent"] = "forgesre_demo_disk_percent"
    return out


def query_prometheus_expr(expr: str, timeout: float = 5.0) -> dict[str, Any]:
    from app.metrics import demo_metric_values

    live = demo_metric_values()
    if expr in live:
        return {"value": live[expr], "query": expr}
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{settings.prometheus_url}/api/v1/query", params={"query": expr})
            response.raise_for_status()
            data = response.json()
            result = (data.get("data") or {}).get("result") or []
            if result:
                return {"value": float(result[0]["value"][1]), "query": expr}
            return {"value": None, "query": expr}
    except Exception as exc:
        return {"error": str(exc), "query": expr}


def query_prometheus_range(expr: str, hours: float = 1.0, step: str = "5m", timeout: float = 2.0) -> dict[str, Any]:
    """Last-hour samples for a tiny SVG sparkline. Empty/error → no sparkline (never fake zeros)."""
    import time

    from app.metrics import demo_metric_values

    live = demo_metric_values()
    if expr in live:
        return {"values": [live[expr]], "query": expr}
    end = time.time()
    start = end - max(0.1, float(hours)) * 3600
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                f"{settings.prometheus_url}/api/v1/query_range",
                params={"query": expr, "start": start, "end": end, "step": step},
            )
            response.raise_for_status()
            data = response.json()
            result = (data.get("data") or {}).get("result") or []
            if not result:
                return {"values": [], "query": expr}
            points = []
            for pair in result[0].get("values") or []:
                try:
                    points.append(float(pair[1]))
                except (TypeError, ValueError, IndexError):
                    continue
            return {"values": points, "query": expr}
    except Exception as exc:
        return {"error": str(exc), "query": expr}


def query_loki(limit: int = 20, query: str = '{job="forgesre"}', start=None, end=None) -> list[str]:
    if not settings.loki_enabled:
        return []
    params: dict[str, Any] = {"query": query, "limit": str(limit)}
    if start is not None:
        params["start"] = start.isoformat()
    if end is not None:
        params["end"] = end.isoformat()
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{settings.loki_url}/loki/api/v1/query_range", params=params)
            response.raise_for_status()
            data = response.json()
            lines: list[str] = []
            for stream in (data.get("data") or {}).get("result") or []:
                for _ts, line in stream.get("values") or []:
                    lines.append(line)
            return lines[:limit]
    except Exception:
        if start is not None:
            return query_loki(limit=limit, query=query)
        return []


def investigation_context(db: Session, incident: Incident) -> dict[str, Any]:
    from rca.collector import DEMO_LOGS_LIMITATION, HOST_LOGS_LIMITATION

    metrics = {}
    logs: list[str] = []
    queries: dict[str, str] = {}
    skipped_logs = False
    demo_logs = False
    limitations: list[str] = []
    for item in incident.evidence:
        if item.kind == "metrics":
            metrics = dict(item.payload or {})
            queries.update((item.payload or {}).get("queries") or {})
        if item.kind == "logs":
            packed = dict(item.payload or {})
            logs = packed.get("lines") or []
            skipped_logs = bool(packed.get("skipped"))
            demo_logs = packed.get("label") == "DEMO" or packed.get("scope") == "appliance-demo"
            if skipped_logs:
                limitations.append(str(packed.get("reason") or HOST_LOGS_LIMITATION))
            elif demo_logs:
                limitations.append(DEMO_LOGS_LIMITATION)
        if item.query:
            queries.setdefault(item.kind, item.query)
    history = [ev.payload for ev in incident.evidence if ev.kind == "history"]
    playrules = []
    if incident.playrule:
        playrules.append(
            {
                "name": incident.playrule.name,
                "playbook": incident.playbook.name if incident.playbook else "",
                "condition": incident.playrule.condition,
            }
        )
    asset_id = incident.asset.asset_id if incident.asset else ""
    maintenance = overlapping_maintenance(db, asset_id, incident.started_at or utcnow())
    if metrics.get("error"):
        limitations.append("Metrics unavailable.")
    if not logs and not skipped_logs and not demo_logs and not settings.loki_enabled:
        limitations.append("Logs unavailable.")
    return {
        "incident": {
            "number": incident.number,
            "title": incident.title,
            "severity": incident.severity,
            "status": incident.status,
            "asset": incident.asset.hostname if incident.asset else None,
        },
        "asset": {
            "asset_id": incident.asset.asset_id if incident.asset else None,
            "hostname": incident.asset.hostname if incident.asset else None,
            "ip": incident.asset.ip if incident.asset else None,
            "type": incident.asset.type if incident.asset else None,
        },
        "alert": incident.alert_payload.get("labels") if incident.alert_payload else {},
        "metrics": metrics,
        "logs": logs,
        "history": history,
        "playrules": playrules,
        "maintenance": maintenance,
        "queries": queries,
        "limitations": limitations,
    }


def run_investigation(
    db: Session,
    incident: Incident,
    actor: str = "system",
    *,
    force: bool = False,
    use_llm: bool = True,
) -> Investigation:
    """Run ForgeRCA. Pass use_llm=False for an immediate builtin result in the UI."""
    latest = (
        db.query(Investigation)
        .filter_by(incident_id=incident.id)
        .order_by(Investigation.id.desc())
        .first()
    )
    if latest is not None and not force:
        return latest
    if force or not db.query(Evidence.id).filter_by(incident_id=incident.id).first():
        collect_evidence(db, incident)
    db.refresh(incident)
    from rca.engines import get_engine
    from rca.llm import make_provider
    from rca.types import EvidenceItem, RCAContext

    items: list[EvidenceItem] = []
    for row in incident.evidence:
        eid = row.evidence_id or ""
        if eid.startswith("ROLLUP-") or eid.startswith("EV-LEGACY"):
            continue
        payload = row.payload or {}
        item = EvidenceItem(
            evidence_id=eid or f"EV-DB-{row.id}",
            type=row.kind,
            source=row.source or "forgesre",
            timestamp=row.captured_at.isoformat() if row.captured_at else "",
            asset_id=row.asset_ref,
            content=payload.get("content", payload),
            query=row.query,
            metadata=payload.get("metadata") or {},
            confidence=row.confidence or 1.0,
            hash=row.hash,
        )
        items.append(item)
    ctx_dict = investigation_context(db, incident)
    if items:
        ctx = RCAContext.from_legacy(ctx_dict)
        ctx.evidence = items
    else:
        ctx = ctx_dict

    llm = make_provider(
        settings.llm_url if settings.ai_enabled and use_llm else None,
        settings.llm_model,
        timeout=settings.llm_timeout,
    )
    engine = get_engine(settings.rca_engine, llm=llm)
    result = engine.investigate(ctx)
    packed = result.get("result") or {}
    row = Investigation(
        incident_id=incident.id,
        summary=result.get("summary") or "",
        likely_cause=result.get("likely_cause") or "",
        confidence=float(result.get("confidence") or 0),
        evidence=result.get("evidence") or [],
        recommended_action=result.get("recommended_action") or "",
        provider=result.get("provider") or "builtin-analyst",
        disclaimer=result.get("disclaimer") or "AI has not modified the system.",
        result=packed,
        engine=packed.get("engine") or engine.get_name(),
        engine_version=packed.get("engine_version") or engine.get_version(),
        model=packed.get("model") or "",
        requested_by=actor,
    )
    db.add(row)
    incident.status = "INVESTIGATING" if incident.status == "OPEN" else incident.status
    append_timeline(incident, "ai", "AI ANALYSIS", row.summary)
    append_timeline(incident, "rca", "RCA", row.likely_cause)
    db.add(
        IncidentEvent(
            incident_id=incident.id,
            actor=actor,
            kind="ai_investigation",
            data={
                "provider": row.provider,
                "engine": row.engine,
                "engine_version": row.engine_version,
                "model": row.model,
                "confidence": row.confidence,
                "evidence_ids": packed.get("supporting_evidence") or [],
            },
        )
    )
    audit(
        db,
        action="ai.investigation",
        actor=actor,
        object_type="incident",
        object_id=incident.number,
        data={
            "provider": row.provider,
            "engine": row.engine,
            "engine_version": row.engine_version,
            "model": row.model,
            "confidence": row.confidence,
            "evidence_ids": packed.get("supporting_evidence") or [],
        },
    )
    db.commit()
    db.refresh(row)
    report(
        db,
        "rca",
        "investigate",
        "ok",
        summary=f"{incident.number} {row.provider} confidence={int(row.confidence or 0)}%",
        detail=(row.likely_cause or row.summary or "")[:400],
        object_type="incident",
        object_id=incident.number,
    )
    return row


def queue_llm_rewrite(db: Session, incident: Incident, actor: str = "system") -> None:
    """Optional second pass: local LLM rewrites text after builtin RCA is already on screen."""
    from app.jobs import enqueue

    if not settings.ai_enabled or not settings.llm_url:
        return
    latest = (
        db.query(Investigation)
        .filter_by(incident_id=incident.id)
        .order_by(Investigation.id.desc())
        .first()
    )
    if latest is not None and latest.provider == "forgerca-llm":
        return
    busy = (
        db.query(Job)
        .filter(
            Job.kind == "investigate",
            Job.object_id == incident.number,
            Job.status.in_(["pending", "running"]),
        )
        .first()
    )
    if busy is not None:
        return
    enqueue(
        db,
        "investigate",
        incident.number,
        payload={"actor": actor, "force": True, "use_llm": True},
    )


NO_RECIPIENT_STATUS = "no-recipient"
NO_RECIPIENT_ERROR = "No recipient: asset has no owner email and the escalation step names a role, not an address. Not sent."


def escalation_recipient(incident: Incident, step_target: str) -> str:
    """Explicit address on the step wins; else the asset owner email; else empty (do not invent role@…)."""
    explicit = _valid_email(step_target) if "@" in (step_target or "") else ""
    if explicit:
        return explicit
    asset = getattr(incident, "asset", None)
    return _valid_email((getattr(asset, "owner_email", "") or "") if asset is not None else "")


def ensure_notification(db: Session, incident: Incident, step_key: str, target: str | None = None) -> Notification:
    existing = (
        db.query(Notification)
        .filter(Notification.incident_id == incident.id, Notification.step_key == step_key)
        .first()
    )
    if existing:
        return existing
    mapped = {
        "immediate": "team",
        "15m": "team-lead",
        "30m": "engineer",
    }
    policy_role = (target or mapped.get(step_key, "team")).strip() or "team"
    if incident.asset is None and incident.asset_id:
        incident.asset = db.get(Asset, incident.asset_id)
    recipient = escalation_recipient(incident, policy_role)
    stored_target = recipient or policy_role
    subject = demo_mail_subject(incident, f"{incident.number} {incident.title}")
    body = build_escalation_body(incident, step_key, policy_role)
    html_body = build_escalation_html(incident, step_key, policy_role)
    row = Notification(
        incident_id=incident.id,
        channel="email",
        target=stored_target,
        subject=subject,
        body=body,
        status="generated",
        step_key=step_key,
    )
    if not recipient:
        row.status = NO_RECIPIENT_STATUS
        row.error = NO_RECIPIENT_ERROR
    elif settings.email_enabled and settings.smtp_host:
        try:
            _send_smtp(recipient, subject, body, html=html_body)
            row.status = "sent"
        except Exception as exc:
            row.status = "failed"
            row.error = str(exc)
    else:
        row.status = "generated"
        row.error = "SMTP disabled; notification generated but not sent"
    db.add(row)
    audit(
        db,
        action="notification.create",
        object_type="incident",
        object_id=incident.number,
        data={"target": stored_target, "policy_role": policy_role, "status": row.status},
    )
    if step_key != "immediate" and incident.status in {"OPEN", "INVESTIGATING"}:
        incident.status = "ESCALATED"
        detail = f"Escalated to {stored_target}" if recipient else f"Escalation step {step_key}: no recipient ({policy_role})"
        append_timeline(incident, "playbook", "PLAYBOOK", detail)
    db.commit()
    note_status = "error" if row.status in {"failed", NO_RECIPIENT_STATUS} else "ok"
    mark = "DEMO " if is_demo_incident(incident) else ""
    report(
        db,
        "notification",
        step_key or "notify",
        note_status,
        summary=f"{mark}{incident.number} → {stored_target} ({row.status})",
        detail=row.error or f"policy_role={policy_role}",
        object_type="incident",
        object_id=incident.number,
    )
    return row


def _send_smtp(target: str, subject: str, body: str, html: str | None = None) -> None:
    send_smtp(target, subject, body, html=html)


def _valid_email(value: str) -> str:
    text = (value or "").strip()
    local, _, domain = text.partition("@")
    if not local or "." not in domain:
        return ""
    return text


def remember_mail_contact(db: Session, email: str, name: str = "", actor: str = "") -> MailContact | None:
    address = _valid_email(email)
    if not address:
        return None
    row = db.query(MailContact).filter(func.lower(MailContact.email) == address.lower()).first()
    if row is None:
        row = MailContact(email=address, name=(name or "").strip(), created_by=actor)
        db.add(row)
        db.flush()
        return row
    if name and not (row.name or "").strip():
        row.name = name.strip()
    return row


def list_mail_addresses(db: Session) -> list[dict[str, str]]:
    """Saved book first, then asset owners, previous outbox, UI users."""
    seen: dict[str, dict[str, str]] = {}

    def add(email: str, label: str, source: str) -> None:
        address = _valid_email(email)
        if not address:
            return
        key = address.lower()
        if key in seen:
            if label and not seen[key].get("label"):
                seen[key]["label"] = label
            return
        seen[key] = {"email": address, "label": (label or "").strip(), "source": source}

    for row in db.query(MailContact).order_by(MailContact.email):
        add(row.email, row.name, "saved")
    for asset in db.query(Asset).order_by(Asset.hostname):
        add(asset.owner_email, asset.contact_name or asset.hostname, "asset")
    for (target,) in db.query(Notification.target).distinct():
        add(str(target or ""), "", "outbox")
    for user in db.query(User).order_by(User.email):
        add(user.email, user.name, "user")
    return sorted(seen.values(), key=lambda item: item["email"].lower())


def send_outbound_mail(
    db: Session,
    *,
    target: str,
    subject: str,
    body: str,
    actor: str = "system",
    step_key: str = "manual",
    incident: Incident | None = None,
    html: str | None = None,
) -> Notification:
    """Store an outbox row and send if SMTP is on. Does not change incident status.

    ``html`` is the optional text/html alternative (incident report). Freeform Ops
    compose leaves it unset so the message stays text/plain.
    """
    subject = demo_mail_subject(incident, subject.strip() or "(no subject)")
    body = body or ""
    if incident is not None and is_demo_incident(incident):
        line = demo_body_line(incident)
        if line not in body:
            body = f"{line}\n\n{body}" if body else f"{line}\n"
    row = Notification(
        incident_id=incident.id if incident else None,
        channel="email",
        target=target.strip(),
        subject=subject,
        body=body,
        status="generated",
        step_key=step_key,
    )
    if settings.email_enabled and settings.smtp_host:
        try:
            _send_smtp(row.target, row.subject, row.body, html=html)
            row.status = "sent"
        except Exception as exc:
            row.status = "failed"
            row.error = str(exc)
    else:
        row.status = "generated"
        row.error = "SMTP disabled; notification generated but not sent"
    remember_mail_contact(db, row.target, actor=actor)
    db.add(row)
    audit(
        db,
        action="notification.create",
        actor=actor,
        object_type="incident" if incident else "mail",
        object_id=incident.number if incident else row.target,
        data={"target": row.target, "step_key": step_key, "status": row.status},
    )
    db.commit()
    db.refresh(row)
    report(
        db,
        "notification",
        step_key or "mail",
        "error" if row.status == "failed" else "ok",
        summary=f"{row.subject} → {row.target} ({row.status})",
        detail=row.error[:400] if row.error else "",
        object_type="incident" if incident else "mail",
        object_id=incident.number if incident else row.target,
    )
    return row


def build_performance_report(db: Session, asset_ids: list[str]) -> str:
    wanted = [str(item).strip() for item in (asset_ids or []) if str(item).strip()]
    q = db.query(Asset)
    if wanted:
        q = q.filter(Asset.asset_id.in_(wanted))
    assets = q.order_by(Asset.hostname).all()
    lines = [
        "ForgeSRE performance report",
        f"Generated at {utcnow().isoformat()}",
        "Not an incident. Read-only snapshot from Prometheus / demo gauges.",
        "",
    ]
    if not assets:
        lines.append("No assets selected.")
        return "\n".join(lines) + "\n"
    for asset in assets:
        sample = query_prometheus(asset)
        lines.append(f"## {asset.hostname} ({asset.asset_id})")
        lines.append(f"type={asset.type or '—'} status={asset.status or '—'} ip={asset.ip or '—'}")
        if sample.get("error"):
            lines.append(f"metrics error: {sample['error']}")
        else:
            for key in ("cpu_percent", "disk_percent", "memory_percent", "up"):
                if key in sample and sample[key] is not None:
                    lines.append(f"{key}={sample[key]}")
        lines.append("")
    return "\n".join(lines) + "\n"


def send_performance_report(
    db: Session,
    *,
    asset_ids: list[str],
    to_email: str,
    actor: str = "system",
    name: str = "performance",
) -> Notification:
    """Build the scheduled-report snapshot and send (or store generated) now."""
    contact = remember_mail_contact(db, to_email, actor=actor)
    if contact is None:
        raise ValueError("Need a valid email address")
    body = build_performance_report(db, list(asset_ids or []))
    return send_outbound_mail(
        db,
        target=contact.email,
        subject=f"[ForgeSRE] {name}",
        body=body,
        actor=actor,
        step_key="report",
    )


def send_incident_report(db: Session, incident: Incident, target: str, actor: str = "system") -> Notification:
    contact = remember_mail_contact(db, target, actor=actor)
    if contact is None:
        raise ValueError("Need a valid email address")
    body = build_incident_report(db, incident)
    return send_outbound_mail(
        db,
        target=contact.email,
        subject=demo_mail_subject(incident, f"[ForgeSRE] {incident.number} {incident.title}"),
        body=body,
        actor=actor,
        step_key="incident-report",
        incident=incident,
        html=build_incident_report_html(db, incident),
    )


def run_scheduled_report(db: Session, row: ScheduledReport, actor: str = "system") -> Notification:
    mail = send_performance_report(
        db,
        asset_ids=list(row.asset_ids or []),
        to_email=row.to_email,
        actor=actor,
        name=row.name or "performance",
    )
    now = utcnow()
    hours = max(1, int(row.interval_hours or 6))
    row.last_run_at = now
    row.next_run_at = now + timedelta(hours=hours)
    db.add(row)
    db.commit()
    return mail


def process_scheduled_reports(db: Session) -> int:
    now = utcnow()
    due = (
        db.query(ScheduledReport)
        .filter(ScheduledReport.enabled.is_(True))
        .order_by(ScheduledReport.id)
        .all()
    )
    ran = 0
    for row in due:
        nxt = row.next_run_at
        if nxt is not None and nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        if nxt is not None and nxt > now:
            continue
        run_scheduled_report(db, row, actor="scheduler")
        ran += 1
    return ran


def process_escalations(db: Session) -> None:
    now = utcnow()
    open_rows = (
        db.query(Incident)
        .filter(Incident.status.in_(["OPEN", "INVESTIGATING", "ESCALATED"]), Incident.ack_at.is_(None))
        .all()
    )
    for incident in open_rows:
        started = incident.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (now - started).total_seconds() / 60
        for step in escalation_steps(incident):
            if elapsed >= float(step["after_minutes"]):
                ensure_notification(db, incident, step["step_key"], target=step["target"])


def parse_policy_steps(text: str) -> list[dict[str, Any]]:
    """Parse 'minutes role' lines into EscalationPolicy.steps."""
    skip = {"min", "mins", "minute", "minutes", "email", "→", "->"}
    rows: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.replace("→", " ").replace("->", " ").strip()
        if not line:
            continue
        tokens = [part for part in line.replace(",", " ").split() if part.lower() not in skip]
        if not tokens:
            continue
        first = tokens[0].rstrip("mM")
        try:
            after = int(first)
        except ValueError:
            continue
        target = tokens[1] if len(tokens) > 1 else "team"
        channel = "email"
        extra = tokens[2].lower() if len(tokens) > 2 else ""
        if extra in {"email", "webhook"}:
            channel = extra
        rows.append({"after_minutes": after, "target": target, "channel": channel})
    return rows or [
        {"after_minutes": 0, "target": "team", "channel": "email"},
        {"after_minutes": 15, "target": "team-lead", "channel": "email"},
        {"after_minutes": 30, "target": "engineer", "channel": "email"},
    ]


def escalation_steps(incident: Incident) -> list[dict[str, Any]]:
    policy = incident.playrule.escalation_policy if incident.playrule else None
    raw = list(policy.steps) if policy and policy.steps else [
        {"after_minutes": 0, "target": "team", "channel": "email"},
        {"after_minutes": 15, "target": "team-lead", "channel": "email"},
        {"after_minutes": 30, "target": "engineer", "channel": "email"},
    ]
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(raw):
        after = int(step.get("after_minutes") or 0)
        key = str(step.get("step_key") or ("immediate" if after == 0 and index == 0 else f"{after}m"))
        steps.append(
            {
                "after_minutes": after,
                "target": str(step.get("target") or "team"),
                "channel": str(step.get("channel") or "email"),
                "step_key": key,
            }
        )
    return steps


def close_open_incidents(db: Session, fingerprint: str, *, include_resolved: bool = False) -> None:
    blocked = {"CLOSED"} if include_resolved else {"CLOSED", "RESOLVED"}
    open_rows = (
        db.query(Incident)
        .filter(Incident.fingerprint == fingerprint, Incident.status.notin_(blocked))
        .all()
    )
    if not open_rows:
        return
    now = utcnow()
    for row in open_rows:
        row.status = "CLOSED"
        row.ended_at = now
        append_timeline(row, "closed", "CLOSED", "Closed so a new demo incident can open")
    db.commit()


def _prepare_demo_lab(db: Session) -> None:
    from app.inventory import seed_demo_candidate

    seed(db)
    linux = ensure_demo_asset(db)
    if linux is not None:
        ensure_demo_similar_history(db, linux)
    ensure_demo_windows_asset(db)
    ensure_demo_switch_asset(db)
    seed_demo_candidate(db)


def _run_lab_incident(
    db: Session,
    *,
    asset_id: str,
    alertname: str,
    summary: str,
    description: str,
    action: str,
    actor: str,
) -> Incident:
    close_open_incidents(db, f"{alertname}:{asset_id}", include_resolved=True)
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": alertname,
                    "severity": "warning",
                    "asset": asset_id,
                    "instance": asset_id,
                },
                "annotations": {
                    "summary": summary,
                    "description": description,
                },
                "fingerprint": f"demo-{action}-{asset_id}",
                "startsAt": utcnow().isoformat(),
            }
        ],
    }
    created = ingest_alertmanager(db, payload)
    fingerprint = f"{alertname}:{asset_id}"
    incident = created[0] if created else (
        db.query(Incident).filter(Incident.fingerprint == fingerprint).order_by(Incident.id.desc()).first()
    )
    if incident:
        run_investigation(db, incident, actor=actor, use_llm=False)
        queue_llm_rewrite(db, incident, actor=actor)
        ensure_notification(db, incident, "immediate")
        report(
            db,
            "demo",
            action,
            "ok",
            summary=f"DEMO {alertname} opened {incident.number} on {asset_id}",
            object_type="incident",
            object_id=incident.number,
        )
    else:
        report(db, "demo", action, "error", summary=f"DEMO {alertname} did not create an incident")
    return incident


def run_demo(db: Session) -> Incident:
    _prepare_demo_lab(db)
    set_demo_cpu(94)
    log.warning("DEMO: CPU on %s raised to 94%% for HighCPU alert (lab gauge)", DEMO_ASSET)
    return _run_lab_incident(
        db,
        asset_id=DEMO_ASSET,
        alertname="HighCPU",
        summary="High CPU",
        description="CPU usage reached 94% on forge-demo-01. Lab DEMO gauge — not a customer host.",
        action="highcpu",
        actor="demo",
    )


def run_demo_rca(db: Session) -> Incident:
    """Filesystem RCA acceptance path. Does not fill a real disk."""
    _prepare_demo_lab(db)
    set_demo_disk(94)
    log.warning("DEMO: filesystem on %s raised to 94%% for FilesystemUsageHigh (lab gauge)", DEMO_ASSET)
    log.error("DEMO: log growth suspected on %s (synthetic evidence)", DEMO_ASSET)
    return _run_lab_incident(
        db,
        asset_id=DEMO_ASSET,
        alertname="FilesystemUsageHigh",
        summary="Filesystem usage high",
        description="Filesystem usage reached 94% on forge-demo-01. Lab DEMO gauge — does not fill a real disk.",
        action="rca",
        actor="demo-rca",
    )


def run_demo_host(db: Session) -> Incident:
    """NodeExporterDown on forge-demo-01. Does not stop a real scrape."""
    _prepare_demo_lab(db)
    log.warning("DEMO: NodeExporterDown opened on %s (lab incident; scrape is unchanged)", DEMO_ASSET)
    return _run_lab_incident(
        db,
        asset_id=DEMO_ASSET,
        alertname="NodeExporterDown",
        summary="Host unreachable (demo)",
        description="node_exporter scrape treated as down on forge-demo-01. Lab only — the real scrape is unchanged.",
        action="host",
        actor="demo-host",
    )


def run_demo_windows(db: Session) -> Incident:
    """WindowsCPUHigh on forge-demo-win-01. Lab-only — that asset is not scraped."""
    _prepare_demo_lab(db)
    log.warning("DEMO: WindowsCPUHigh opened on %s (lab incident; demo host is not scraped)", DEMO_WIN_ASSET)
    return _run_lab_incident(
        db,
        asset_id=DEMO_WIN_ASSET,
        alertname="WindowsCPUHigh",
        summary="Windows CPU high (lab)",
        description=(
            "Lab scenario on forge-demo-win-01. DEMO tagged. "
            "This demo host is not scraped. Real Windows assets use windows_exporter :9182."
        ),
        action="windows",
        actor="demo-windows",
    )


def run_demo_network(db: Session) -> Incident:
    """SnmpDeviceUnreachable on forge-demo-sw-01. Does not walk a real device."""
    _prepare_demo_lab(db)
    log.warning("DEMO: SnmpDeviceUnreachable opened on %s (lab incident; not a live SNMP walk)", DEMO_SW_ASSET)
    return _run_lab_incident(
        db,
        asset_id=DEMO_SW_ASSET,
        alertname="SnmpDeviceUnreachable",
        summary="Network device unreachable (lab)",
        description=(
            "Lab scenario on forge-demo-sw-01. DEMO tagged. "
            "Not a live SNMP walk — snmp_exporter is not polling this seeded switch."
        ),
        action="network",
        actor="demo-network",
    )


def run_demo_nodecpu(db: Session) -> Incident:
    """NodeCPUHigh on forge-demo-01. Second Linux alertname, same demo host."""
    _prepare_demo_lab(db)
    log.warning("DEMO: NodeCPUHigh opened on %s (lab incident; node_exporter scrape unchanged)", DEMO_ASSET)
    return _run_lab_incident(
        db,
        asset_id=DEMO_ASSET,
        alertname="NodeCPUHigh",
        summary="Linux NodeCPUHigh (lab)",
        description="Lab scenario: NodeCPUHigh on forge-demo-01. DEMO tagged. Does not change a real host.",
        action="nodecpu",
        actor="demo-nodecpu",
    )
