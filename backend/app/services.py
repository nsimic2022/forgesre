from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audit import audit
from app.metrics import set_demo_cpu, set_demo_disk
from app.models import (
    Asset,
    Evidence,
    EscalationPolicy,
    Incident,
    IncidentEvent,
    Investigation,
    MaintenanceWindow,
    Notification,
    Playbook,
    Playrule,
)
from app.seed import DEMO_ASSET
from app.settings import settings

log = logging.getLogger("forgesre")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def next_incident_number(db: Session) -> str:
    current = db.query(func.count(Incident.id)).scalar() or 0
    return f"INC-{current + 1:06d}"


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
    created: list[Incident] = []
    status = (payload.get("status") or "firing").lower()
    for alert in payload.get("alerts") or []:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
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
        if status == "resolved" and incident:
            incident.status = "RESOLVED"
            incident.ended_at = utcnow()
            append_timeline(incident, "alert", "ALERT", f"{alertname} resolved")
            db.add(IncidentEvent(incident_id=incident.id, kind="resolved", data=labels))
            refresh_asset_status(db, asset)
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
        collect_evidence(db, incident, alert)
        refresh_asset_status(db, asset)
        ensure_notification(db, incident, step_key="immediate")
    db.commit()
    for incident in created:
        db.refresh(incident)
        try:
            run_investigation(db, incident)
        except Exception:
            log.exception("investigation failed for %s", incident.number)
    return created


def collect_evidence(db: Session, incident: Incident, alert: dict[str, Any] | None = None) -> None:
    alert = alert or incident.alert_payload or {}
    labels = alert.get("labels") if isinstance(alert, dict) else {}
    if not labels and isinstance(alert, dict):
        labels = alert
    metrics = query_prometheus()
    logs = query_loki()
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

    def metric_fetcher(expr: str) -> dict[str, Any]:
        sample = query_prometheus_expr(expr)
        if "error" in metrics and sample.get("error"):
            return {"error": metrics["error"]}
        return sample

    def log_fetcher(query: str, start, end) -> dict[str, Any]:
        lines = query_loki(limit=settings.rca_max_log_lines, query=query, start=start, end=end)
        if not lines and logs:
            return {"lines": logs}
        if not lines:
            return {"error": "no log lines"}
        return {"lines": lines}

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


def query_prometheus() -> dict[str, Any]:
    queries = {
        "cpu_percent": "forgesre_demo_cpu_percent",
        "disk_percent": "forgesre_demo_disk_percent",
        "disk_volume_percent": "forgesre_disk_used_percent",
        "up": "forgesre_up",
    }
    out: dict[str, Any] = {}
    try:
        for key, expr in queries.items():
            sample = query_prometheus_expr(expr)
            if sample.get("error"):
                out["error"] = sample["error"]
                break
            if "value" in sample and sample["value"] is not None:
                out[key] = sample["value"]
            out.setdefault("queries", {})[key] = expr
    except Exception as exc:
        out["error"] = str(exc)
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


def run_investigation(db: Session, incident: Incident, actor: str = "system") -> Investigation:
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
        settings.llm_url if settings.ai_enabled else None,
        settings.llm_model,
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
    return row


def ensure_notification(db: Session, incident: Incident, step_key: str, target: str | None = None) -> Notification:
    existing = (
        db.query(Notification)
        .filter(Notification.incident_id == incident.id, Notification.step_key == step_key)
        .first()
    )
    if existing:
        return existing
    policy = None
    if incident.playrule_id:
        rule = db.get(Playrule, incident.playrule_id)
        if rule and rule.escalation_policy_id:
            policy = db.get(EscalationPolicy, rule.escalation_policy_id)
    mapped = {
        "immediate": "team",
        "15m": "team-lead",
        "30m": "engineer",
    }
    target = target or mapped.get(step_key, "team")
    subject = f"{incident.number} {incident.title}"
    body = (
        f"Incident: {incident.number}\n"
        f"Title: {incident.title}\n"
        f"Severity: {incident.severity}\n"
        f"Status: {incident.status}\n"
        f"Asset: {incident.asset.hostname if incident.asset else 'unknown'}\n"
        f"Playbook: {incident.playbook.name if incident.playbook else 'n/a'}\n"
    )
    row = Notification(
        incident_id=incident.id,
        channel="email",
        target=target,
        subject=subject,
        body=body,
        status="generated",
        step_key=step_key,
    )
    if settings.email_enabled and settings.smtp_host:
        try:
            _send_smtp(target, subject, body)
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
        data={"target": target, "status": row.status},
    )
    if step_key != "immediate" and incident.status in {"OPEN", "INVESTIGATING"}:
        incident.status = "ESCALATED"
        append_timeline(incident, "playbook", "PLAYBOOK", f"Escalated to {target}")
    db.commit()
    return row


def _send_smtp(target: str, subject: str, body: str) -> None:
    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = f"{target}@forgesre.local"
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        if settings.smtp_tls:
            client.starttls()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


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
        ensure_notification(db, incident, "immediate")
        if elapsed >= 15:
            ensure_notification(db, incident, "15m")
        if elapsed >= 30:
            ensure_notification(db, incident, "30m")


def run_demo(db: Session) -> Incident:
    from app.inventory import seed_demo_candidate

    seed_demo_candidate(db)
    set_demo_cpu(94)
    log.warning("demo: CPU on %s raised to 94%% for HighCPU alert", DEMO_ASSET)
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighCPU",
                    "severity": "warning",
                    "asset": DEMO_ASSET,
                    "instance": DEMO_ASSET,
                },
                "annotations": {
                    "summary": "High CPU",
                    "description": "CPU usage reached 94% on forge-demo-01.",
                },
                "fingerprint": f"demo-highcpu-{DEMO_ASSET}",
                "startsAt": utcnow().isoformat(),
            }
        ],
    }
    created = ingest_alertmanager(db, payload)
    incident = created[0] if created else (
        db.query(Incident).filter(Incident.fingerprint == f"demo-highcpu-{DEMO_ASSET}").order_by(Incident.id.desc()).first()
    )
    if incident and not incident.investigations:
        run_investigation(db, incident)
    if incident:
        ensure_notification(db, incident, "immediate")
    return incident


def run_demo_rca(db: Session) -> Incident:
    """Filesystem RCA acceptance path. Does not fill a real disk."""
    from app.inventory import seed_demo_candidate

    seed_demo_candidate(db)
    set_demo_disk(94)
    log.warning("demo-rca: filesystem on %s raised to 94%% for FilesystemUsageHigh", DEMO_ASSET)
    log.error("demo-rca: log growth suspected on %s (synthetic evidence)", DEMO_ASSET)
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "FilesystemUsageHigh",
                    "severity": "warning",
                    "asset": DEMO_ASSET,
                    "instance": DEMO_ASSET,
                },
                "annotations": {
                    "summary": "Filesystem usage high",
                    "description": "Filesystem usage reached 94% on forge-demo-01.",
                },
                "fingerprint": f"demo-disk-{DEMO_ASSET}",
                "startsAt": utcnow().isoformat(),
            }
        ],
    }
    created = ingest_alertmanager(db, payload)
    fingerprint = f"FilesystemUsageHigh:{DEMO_ASSET}"
    incident = created[0] if created else (
        db.query(Incident).filter(Incident.fingerprint == fingerprint).order_by(Incident.id.desc()).first()
    )
    if incident:
        run_investigation(db, incident, actor="demo-rca")
        ensure_notification(db, incident, "immediate")
    return incident
