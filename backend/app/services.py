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


def format_incident_number(seq: int, when: datetime | None = None) -> str:
    """INC-0134_16.08.2026_09:13 in the appliance timezone (wall clock)."""
    stamp = when or utcnow()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    try:
        local = stamp.astimezone(ZoneInfo(settings.timezone))
    except Exception:
        local = stamp.astimezone(timezone.utc)
    return f"INC-{seq:04d}_{local:%d.%m.%Y}_{local:%H:%M}"


def next_incident_number(db: Session, when: datetime | None = None) -> str:
    highest = 0
    for (number,) in db.query(Incident.number).all():
        seq = incident_seq(str(number or ""))
        if seq is not None:
            highest = max(highest, seq)
    return format_incident_number(highest + 1, when)


def match_playrule(db: Session, alertname: str, labels: dict[str, Any]) -> Playrule | None:
    rules = db.query(Playrule).filter_by(enabled=True).all()
    for rule in rules:
        condition = rule.condition or {}
        expected = str(condition.get("alertname") or "")
        if expected and expected.lower() == alertname.lower():
            return rule
        metric = str(condition.get("metric") or "")
        if metric and metric in {str(labels.get("metric") or ""), alertname.lower()}:
            return rule
    return None


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


def ingest_alertmanager(db: Session, payload: dict[str, Any]) -> list[Incident]:
    from app.jobs import enqueue

    created: list[Incident] = []
    group_status = (payload.get("status") or "firing").lower()
    for alert in payload.get("alerts") or []:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        alert_status = (alert.get("status") or group_status).lower()
        alertname = str(labels.get("alertname") or "Alert")
        asset_name = str(labels.get("asset") or labels.get("instance") or DEMO_ASSET)
        fingerprint = f"{alertname}:{asset_name}"
        incident = (
            db.query(Incident)
            .filter(Incident.fingerprint == fingerprint, Incident.status.notin_(["CLOSED"]))
            .order_by(Incident.id.desc())
            .first()
        )
        asset = (
            db.query(Asset).filter((Asset.asset_id == asset_name) | (Asset.hostname == asset_name)).first()
        )
        if alert_status == "resolved":
            if incident:
                incident.status = "RESOLVED"
                incident.ended_at = utcnow()
                append_timeline(incident, "alert", "ALERT", f"{alertname} resolved")
                db.add(IncidentEvent(incident_id=incident.id, kind="resolved", data=labels))
                refresh_asset_status(db, asset)
                if asset and asset.asset_id == DEMO_ASSET:
                    reset_demo_gauges()
            continue
        if incident is None:
            rule = match_playrule(db, alertname, labels)
            incident = Incident(
                number=next_incident_number(db),
                title=str(annotations.get("summary") or alertname),
                severity=str(labels.get("severity") or (rule.severity if rule else "warning")).upper(),
                status="OPEN",
                fingerprint=fingerprint,
                asset_id=asset.id if asset else None,
                playrule_id=rule.id if rule else None,
                playbook_id=rule.playbook_id if rule else None,
                summary=str(annotations.get("description") or ""),
                alert_payload=alert,
                timeline=[],
            )
            db.add(incident)
            db.flush()
            append_timeline(incident, "alert", "ALERT", f"{alertname} fired")
            append_timeline(incident, "incident", "INCIDENT", f"{incident.number} created")
            if rule:
                append_timeline(incident, "playrule", "PLAYRULE", rule.name)
                if rule.playbook:
                    append_timeline(incident, "playbook", "PLAYBOOK", rule.playbook.name)
            db.add(IncidentEvent(incident_id=incident.id, kind="created", data=labels))
            audit(
                db,
                action="incident.create",
                object_type="incident",
                object_id=incident.number,
                data={"alertname": alertname},
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
            detail=f"asset={incident.asset.hostname if incident.asset else 'unknown'} fingerprint={incident.fingerprint}",
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
    from rca.collector import loki_query_for

    asset_dict = {}
    if incident.asset:
        asset_dict = {"asset_id": incident.asset.asset_id, "type": incident.asset.type}
    logs = query_loki(query=loki_query_for(asset_dict))
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
        ("logs", "Logs", {"lines": logs}),
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
        if logs:
            return {"lines": logs}
        return {"error": "no log lines"}

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
            "type": asset.type,
            "monitoring_profile": asset.monitoring_profile,
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


def query_prometheus_expr(expr: str) -> dict[str, Any]:
    from app.metrics import demo_metric_values

    live = demo_metric_values()
    if expr in live:
        return {"value": live[expr], "query": expr}
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{settings.prometheus_url}/api/v1/query", params={"query": expr})
            response.raise_for_status()
            data = response.json()
            result = (data.get("data") or {}).get("result") or []
            if result:
                return {"value": float(result[0]["value"][1]), "query": expr}
            return {"value": None, "query": expr}
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
    metrics = {}
    logs: list[str] = []
    queries: dict[str, str] = {}
    for item in incident.evidence:
        if item.kind == "metrics":
            metrics = dict(item.payload or {})
            queries.update((item.payload or {}).get("queries") or {})
        if item.kind == "logs":
            logs = (item.payload or {}).get("lines") or []
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
    limitations = []
    if metrics.get("error"):
        limitations.append("Metrics unavailable.")
    if not logs and not settings.loki_enabled:
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
    policy_role = target or mapped.get(step_key, "team")
    if incident.asset is None and incident.asset_id:
        incident.asset = db.get(Asset, incident.asset_id)
    owner_email = ((incident.asset.owner_email if incident.asset else "") or "").strip()
    stored_target = owner_email or policy_role
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
    if settings.email_enabled and settings.smtp_host:
        try:
            _send_smtp(stored_target, subject, body, html=html_body)
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
        append_timeline(incident, "playbook", "PLAYBOOK", f"Escalated to {stored_target}")
    db.commit()
    note_status = "error" if row.status == "failed" else "ok"
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
    body = build_performance_report(db, list(row.asset_ids or []))
    mail = send_outbound_mail(
        db,
        target=row.to_email,
        subject=f"[ForgeSRE] {row.name}",
        body=body,
        actor=actor,
        step_key="report",
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
    """WindowsCPUHigh on forge-demo-win-01. Does not scrape windows_exporter."""
    _prepare_demo_lab(db)
    log.warning("DEMO: WindowsCPUHigh opened on %s (lab incident; no windows_exporter scrape)", DEMO_WIN_ASSET)
    return _run_lab_incident(
        db,
        asset_id=DEMO_WIN_ASSET,
        alertname="WindowsCPUHigh",
        summary="Windows CPU high (lab)",
        description=(
            "Lab scenario on forge-demo-win-01. DEMO tagged. "
            "ForgeSRE does not scrape windows_exporter; this is not a live Windows metric."
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
