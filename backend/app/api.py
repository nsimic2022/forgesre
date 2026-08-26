from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audit import audit
from app.journal import MODULES, entry_as_dict, list_entries, module_counts, report
from app.db import get_db
from app.metrics import reset_demo_gauges, set_demo_cpu, set_demo_disk
from app.models import Asset, DiscoveryCandidate, EscalationPolicy, Evidence, Incident, Investigation, Playbook, Playrule, User
from app.history import (
    add_note,
    apply_status_fields,
    audit_as_dict,
    audit_for,
    clamp_days,
    list_history,
    note_as_dict,
    notes_for,
    notification_as_dict,
    notifications_for,
)
from app.security import can, user_from_session, verify_password
from app.inventory import (
    approve_candidate,
    clone_prefill,
    create_manual_asset,
    delete_asset,
    ignore_candidate,
    is_snmp_asset,
    run_scan,
    sd_targets,
    sd_snmp_targets,
    seed_demo_candidate,
    similar_incident_groups,
    sync_netbox,
    update_asset,
)
from app.asset_probe import apply_probe_to_asset, refresh_reachability, reachability_snapshot
from app.asset_verify import select_assets as verify_select_assets
from app.asset_verify import urllib_am_health, urllib_prom_targets, verify_target
from app.exporter_detect import detect_exporter, is_auto_asset_type
from app.demo_ids import is_lab_inventory_row
from app.seed import seed
from app.services import (
    host_down_public,
    ingest_alertmanager,
    is_demo_incident,
    list_host_down_incidents,
    run_demo,
    run_demo_host,
    run_demo_network,
    run_demo_nodecpu,
    run_demo_rca,
    run_demo_windows,
)
from app.settings import settings
from app.stack import component_label, doctor_soft_status, ensure_snmp_exporter, snmp_target_count

log = logging.getLogger("forgesre")
router = APIRouter(prefix="/api/v1")


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return user_from_session(db, request.cookies.get("forgesre_session"))


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require(permission: str):
    def _inner(user: User = Depends(require_user)) -> User:
        if not can(user, permission):
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return _inner


class LoginBody(BaseModel):
    email: str
    password: str


class UserBody(BaseModel):
    email: str
    name: str
    password: str
    role: str = "analyst"


class UserUpdateBody(BaseModel):
    email: str | None = None
    name: str | None = None
    password: str | None = None
    role: str | None = None


class PlayruleBody(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    severity: str = "warning"
    condition: dict[str, Any] = Field(default_factory=dict)
    playbook_id: int | None = None


class PlaybookBody(BaseModel):
    name: str
    slug: str
    description: str = ""
    steps: list[Any] = Field(default_factory=list)


class NoteBody(BaseModel):
    body: str = ""


class StatusBody(BaseModel):
    status: str
    note: str = ""


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    db.query(Asset).first()
    return {"status": "ready"}


class JournalBody(BaseModel):
    module: str
    action: str = ""
    status: str = "ok"
    summary: str = ""
    detail: str = ""
    object_type: str = ""
    object_id: str = ""


@router.get("/journal")
def list_journal(
    module: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(require("read_play")),
) -> dict:
    rows = list_entries(db, module=module, status=status, q=q, limit=limit)
    return {
        "modules": module_counts(db),
        "entries": [entry_as_dict(row) for row in rows],
    }


@router.post("/journal")
def create_journal(
    body: JournalBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("admin")),
) -> dict:
    row = report(
        db,
        body.module,
        body.action,
        body.status,
        summary=body.summary,
        detail=body.detail,
        object_type=body.object_type,
        object_id=body.object_id,
    )
    if row is None:
        raise HTTPException(status_code=500, detail="journal write failed")
    return entry_as_dict(row)


@router.post("/auth/login")
def api_login(body: LoginBody, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.query(User).filter_by(email=body.email).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    audit(db, "login", actor=user.email, ip=request.client.host if request.client else "", commit=True)
    return {"email": user.email, "role": user.role, "name": user.name}


@router.get("/me")
def me(user: User = Depends(require_user)) -> dict[str, Any]:
    return {"email": user.email, "role": user.role, "name": user.name}


@router.get("/assets")
def list_assets(db: Session = Depends(get_db), user: User = Depends(require("read_assets"))) -> list[dict]:
    return [_asset(item) for item in db.query(Asset).order_by(Asset.number, Asset.hostname).all()]


@router.get("/assets/reachability")
def assets_reachability(
    db: Session = Depends(get_db),
    user: User = Depends(require("read_assets")),
    refresh: bool = True,
) -> list[dict[str, Any]]:
    """Last-known ping/exporter colors. Optional live probe (does not block the HTML list)."""
    del user
    rows = db.query(Asset).order_by(Asset.hostname).all()
    if not refresh:
        return [reachability_snapshot(item) for item in rows]
    payload = refresh_reachability(rows)
    db.commit()
    return payload



def _latest_incident(db: Session, asset: Asset) -> dict[str, Any] | None:
    incident = (
        db.query(Incident)
        .filter(Incident.asset_id == asset.id)
        .order_by(Incident.id.desc())
        .first()
    )
    if incident is None:
        return None
    return {
        "number": incident.number,
        "title": incident.title or "",
        "status": incident.status or "",
        "started_at": incident.started_at.isoformat() if incident.started_at else "",
    }


def _latest_rca(db: Session, asset: Asset) -> dict[str, Any] | None:
    incident = (
        db.query(Incident)
        .filter(Incident.asset_id == asset.id)
        .order_by(Incident.id.desc())
        .first()
    )
    if incident is None:
        return None
    latest = incident.investigations[-1] if incident.investigations else None
    if latest is None:
        return None
    result = latest.result if isinstance(latest.result, dict) else {}
    facts = result.get("facts") if isinstance(result, dict) else None
    if not facts:
        facts = [{"text": line} for line in (latest.evidence or []) if line]
    return {
        "incident": incident.number,
        "summary": latest.summary or "",
        "likely_cause": latest.likely_cause or "",
        "provider": latest.provider or "",
        "facts": facts or [],
    }


def _sd_membership(db: Session) -> tuple[set[str], set[str]]:
    http_ids = {(row.get("labels") or {}).get("asset") for row in sd_targets(db)}
    snmp_ids = {(row.get("labels") or {}).get("asset") for row in sd_snmp_targets(db)}
    http_ids.discard(None)
    snmp_ids.discard(None)
    return {str(item) for item in http_ids}, {str(item) for item in snmp_ids}


def _live_metric_values(asset: Asset) -> dict[str, Any]:
    from app.services import query_prometheus

    live = query_prometheus(asset)
    return {
        key: value
        for key, value in live.items()
        if key not in {"queries", "error"} and not isinstance(value, (dict, list))
    }


def run_asset_verify(db: Session, asset: Asset, *, timeout: float = 2.0) -> dict[str, Any]:
    from app.services import query_prometheus_expr

    item = _asset(asset)
    http_ids, snmp_ids = _sd_membership(db)
    prom_url = settings.prometheus_url or "http://127.0.0.1:9090"
    am_url = settings.alertmanager_url or "http://127.0.0.1:9093"
    report = verify_target(
        item,
        timeout=timeout,
        in_http_sd=asset.asset_id in http_ids,
        in_snmp_sd=asset.asset_id in snmp_ids,
        query_fn=query_prometheus_expr,
        rca=_latest_rca(db, asset),
        live_metrics=_live_metric_values(asset),
        ai_enabled=bool(settings.ai_enabled and settings.llm_url),
        targets_fn=lambda: urllib_prom_targets(prom_url),
        am_health=urllib_am_health(am_url),
        incident=_latest_incident(db, asset),
    )
    if report.probe is not None:
        apply_probe_to_asset(asset, report.probe)
        db.commit()
    return report.as_dict()


@router.get("/verify")
def verify_assets_api(
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
    selector: str = "",
    include_demo: bool = False,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Live communication verify (not appliance ./forgesre test). write_assets same as Add/Edit."""
    del user
    timeout = min(8.0, max(0.4, float(timeout or 2.0)))
    rows = [_asset(item) for item in db.query(Asset).order_by(Asset.hostname).all()]
    chosen, skipped_demo = verify_select_assets(
        rows,
        selector,
        include_demo=include_demo,
        is_demo=lambda row: is_lab_inventory_row(row),
    )
    results: list[dict[str, Any]] = []
    models = {item.asset_id: item for item in db.query(Asset).all()}
    for row in chosen:
        asset = models.get(str(row.get("asset_id") or ""))
        if asset is None:
            continue
        results.append(run_asset_verify(db, asset, timeout=timeout))
    return {"results": results, "skipped_demo": skipped_demo, "selector": selector}


@router.get("/verify-support")
def verify_support_api(
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
) -> dict[str, Any]:
    """Inventory + SD + last RCA + live PromQL values. CLI overlays host ICMP/exporter probes."""
    del user
    http_ids, snmp_ids = _sd_membership(db)
    payload: dict[str, Any] = {}
    for asset in db.query(Asset).order_by(Asset.hostname).all():
        payload[asset.asset_id] = {
            "asset": _asset(asset),
            "in_http_sd": asset.asset_id in http_ids,
            "in_snmp_sd": asset.asset_id in snmp_ids,
            "rca": _latest_rca(db, asset),
            "incident": _latest_incident(db, asset),
            "live_metrics": _live_metric_values(asset),
        }
    am_url = settings.alertmanager_url or "http://127.0.0.1:9093"
    return {
        "assets": payload,
        "ai_enabled": bool(settings.ai_enabled and settings.llm_url),
        "prometheus_url": settings.prometheus_url,
        "alertmanager_url": am_url,
        "alertmanager": urllib_am_health(am_url),
    }


@router.get("/assets/{asset_id}/metrics")
def asset_metrics_api(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require("read_assets")),
) -> dict[str, Any]:
    """Class-based glance tiles (CPU/mem/disk/up) from Prometheus. Viewers can read."""
    del user
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="asset not found")
    from app.asset_metrics import safe_asset_metric_panel

    return safe_asset_metric_panel(item)


@router.get("/assets/{asset_id}/verify")
def verify_one_asset_api(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
    timeout: float = 2.0,
) -> dict[str, Any]:
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="asset not found")
    timeout = min(8.0, max(0.4, float(timeout or 2.0)))
    return run_asset_verify(db, item, timeout=timeout)


@router.post("/assets/{asset_id}/verify")
def verify_one_asset_post(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
    timeout: float = 2.0,
) -> dict[str, Any]:
    return verify_one_asset_api(asset_id, db, user, timeout)


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, db: Session = Depends(get_db), user: User = Depends(require("read_assets"))) -> dict:
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="asset not found")
    data = _asset(item)
    data["similar_incidents"] = similar_incident_groups(db, item)
    return data


@router.get("/incidents")
def list_incidents(
    db: Session = Depends(get_db),
    user: User = Depends(require("read_incidents")),
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows = db.query(Incident).order_by(Incident.id.desc()).offset(offset).limit(limit).all()
    return [_incident(item, include_evidence=can(user, "read_evidence")) for item in rows]


@router.get("/incidents/down")
def list_down_incidents(
    db: Session = Depends(get_db),
    user: User = Depends(require("read_incidents")),
) -> list[dict[str, Any]]:
    """Open NodeExporterDown / WindowsExporterDown / SnmpDeviceUnreachable rows for the dashboard banner."""
    del user
    return [host_down_public(row) for row in list_host_down_incidents(db)]


@router.get("/history")
def history_api(
    db: Session = Depends(get_db),
    user: User = Depends(require("read_incidents")),
    days: int = 90,
    status: str = "",
    asset: str = "",
    number: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict:
    rows, total = list_history(
        db,
        days=clamp_days(days),
        status=status,
        asset=asset,
        number=number,
        limit=limit,
        offset=offset,
    )
    include = can(user, "read_evidence")
    return {
        "days": clamp_days(days),
        "total": total,
        "incidents": [_incident(item, include_evidence=include) for item in rows],
    }


@router.get("/incidents/{number}")
def get_incident(number: str, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    data = _incident(item, include_evidence=can(user, "read_evidence"))
    data["notifications"] = [notification_as_dict(row) for row in notifications_for(db, item)]
    data["audit"] = [audit_as_dict(row) for row in audit_for(db, item.number)]
    data["notes"] = [note_as_dict(row) for row in notes_for(db, item)]
    return data


@router.post("/incidents/{number}/notes")
def add_incident_note(
    number: str,
    body: NoteBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_incidents")),
) -> dict:
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    try:
        row = add_note(db, item, actor=user.email, body=body.body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return note_as_dict(row)


@router.post("/incidents/{number}/status")
def set_status(
    number: str,
    body: StatusBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_incidents")),
) -> dict:
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    allowed = {"OPEN", "INVESTIGATING", "ESCALATED", "RESOLVED", "CLOSED"}
    status = body.status.upper()
    if status not in allowed:
        raise HTTPException(status_code=400, detail="invalid status")
    item.status = status
    apply_status_fields(item, status, user.email)
    audit(db, "incident.status", actor=user.email, object_type="incident", object_id=number, data={"status": status})
    db.commit()
    return _incident(item, include_evidence=True)


@router.post("/incidents/{number}/investigate")
def investigate_incident(
    number: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> dict:
    if not (can(user, "investigate") or can(user, "read_ai")):
        raise HTTPException(status_code=403, detail="forbidden")
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    from app.services import queue_llm_rewrite, run_investigation

    run_investigation(db, item, actor=user.email, use_llm=False)
    queue_llm_rewrite(db, item, actor=user.email)
    db.refresh(item)
    data = _incident(item, include_evidence=can(user, "read_evidence"))
    data["queued"] = bool(settings.ai_enabled and settings.llm_url)
    return data


@router.get("/incidents/{number}/investigation")
def get_incident_investigation(
    number: str,
    db: Session = Depends(get_db),
    user: User = Depends(require("read_incidents")),
) -> dict:
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    latest = item.investigations[-1] if item.investigations else None
    if latest is None:
        raise HTTPException(status_code=404, detail="investigation not found")
    return _investigation(latest, include_queries=can(user, "read_evidence"))


@router.get("/investigations/{investigation_id}")
def get_investigation(
    investigation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("read_incidents")),
) -> dict:
    row = db.get(Investigation, investigation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="investigation not found")
    return _investigation(row, include_queries=can(user, "read_evidence"))


@router.get("/investigations/{investigation_id}/evidence")
def get_investigation_evidence(
    investigation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("read_incidents")),
) -> list[dict]:
    row = db.get(Investigation, investigation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="investigation not found")
    include_queries = can(user, "read_evidence")
    ids = set((row.result or {}).get("supporting_evidence") or [])
    ids.update((row.result or {}).get("contradicting_evidence") or [])
    rows = db.query(Evidence).filter(Evidence.incident_id == row.incident_id).all()
    out = []
    for item in rows:
        if item.evidence_id.startswith("ROLLUP-"):
            continue
        if ids and item.evidence_id not in ids and item.kind in {"METRIC", "LOG", "ALERT"}:
            # still return all immutable RCA items; IDs filter is a hint not a hide
            pass
        out.append(_evidence_item(item, include_queries=include_queries))
    return out


@router.get("/playrules")
def list_playrules(db: Session = Depends(get_db), user: User = Depends(require("read_play"))) -> list[dict]:
    return [_playrule(item) for item in db.query(Playrule).order_by(Playrule.name).all()]


@router.post("/playrules")
def create_playrule(
    body: PlayruleBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_play")),
) -> dict:
    row = Playrule(**body.model_dump())
    if not row.escalation_policy_id:
        policy = db.query(EscalationPolicy).filter_by(slug="default-warning").first()
        if policy:
            row.escalation_policy_id = policy.id
    db.add(row)
    audit(db, "playrule.create", actor=user.email, object_type="playrule", object_id=body.name)
    db.commit()
    db.refresh(row)
    return _playrule(row)


@router.get("/playbooks")
def list_playbooks(db: Session = Depends(get_db), user: User = Depends(require("read_play"))) -> list[dict]:
    return [_playbook(item) for item in db.query(Playbook).order_by(Playbook.name).all()]


@router.post("/playbooks")
def create_playbook(
    body: PlaybookBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_play")),
) -> dict:
    row = Playbook(**body.model_dump())
    db.add(row)
    audit(db, "playbook.create", actor=user.email, object_type="playbook", object_id=body.slug)
    db.commit()
    db.refresh(row)
    return _playbook(row)


@router.get("/sd/prometheus")
def prometheus_sd(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    auth = request.headers.get("authorization") or ""
    token = auth.replace("Bearer ", "").strip()
    if token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="invalid sd token")
    return sd_targets(db)


@router.get("/sd/snmp")
def snmp_sd(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    auth = request.headers.get("authorization") or ""
    token = auth.replace("Bearer ", "").strip()
    if token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="invalid sd token")
    return sd_snmp_targets(db)


@router.get("/discovery/candidates")
def list_candidates(db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> list[dict]:
    rows = db.query(DiscoveryCandidate).order_by(DiscoveryCandidate.id.desc()).all()
    return [_candidate(item) for item in rows]


@router.post("/discovery/scan")
def discovery_scan(db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    result = run_scan(db)
    audit(db, "discovery.scan", actor=user.email, commit=True)
    return result


@router.post("/discovery/candidates/{candidate_id}/approve")
def discovery_approve(
    candidate_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
) -> dict:
    row = db.get(DiscoveryCandidate, candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    asset = approve_candidate(db, row, actor=user.email)
    return {"ok": True, "asset_id": asset.asset_id}


@router.post("/discovery/candidates/{candidate_id}/ignore")
def discovery_ignore(
    candidate_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
) -> dict:
    row = db.get(DiscoveryCandidate, candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    ignore_candidate(db, row, actor=user.email)
    return {"ok": True}


@router.post("/discovery/netbox-sync")
def discovery_netbox_sync(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    return sync_netbox(db)


class AssetBody(BaseModel):
    hostname: str
    ip: str = ""
    type: str = "Linux Server"
    environment: str = "Production"
    owner: str = "platform"
    contact_name: str = ""
    owner_email: str = ""
    owner_phone: str = ""
    notes: str = ""
    monitoring_profile: str = ""
    scrape_address: str = ""
    alarms: dict | None = None


class AssetUpdateBody(BaseModel):
    hostname: str | None = None
    ip: str | None = None
    type: str | None = None
    environment: str | None = None
    owner: str | None = None
    contact_name: str | None = None
    owner_email: str | None = None
    owner_phone: str | None = None
    notes: str | None = None
    scrape_address: str | None = None
    alarms: dict | None = None


class AssetCloneBody(BaseModel):
    hostname: str | None = None
    ip: str | None = None
    type: str | None = None
    environment: str | None = None
    owner: str | None = None
    contact_name: str | None = None
    owner_email: str | None = None
    owner_phone: str | None = None
    notes: str | None = None
    scrape_address: str | None = None
    alarms: dict | None = None


@router.post("/assets")
def create_asset_api(
    body: AssetBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
) -> dict:
    from app.inventory import create_manual_asset

    try:
        asset = create_manual_asset(
            db,
            hostname=body.hostname,
            ip=body.ip,
            type=body.type,
            environment=body.environment,
            owner=body.owner,
            contact_name=body.contact_name,
            owner_email=body.owner_email,
            owner_phone=body.owner_phone,
            notes=body.notes,
            monitoring_profile=body.monitoring_profile,
            scrape_address=body.scrape_address,
            actor=user.email,
            snmp_prober=_snmp_answer if is_auto_asset_type(body.type) else None,
            alarms=body.alarms,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _asset(asset)


@router.post("/assets/{asset_id}")
def update_asset_api(
    asset_id: str,
    body: AssetUpdateBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
) -> dict:
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="asset not found")
    asset = update_asset(
        db,
        item,
        hostname=body.hostname,
        ip=body.ip,
        type=body.type,
        environment=body.environment,
        owner=body.owner,
        contact_name=body.contact_name,
        owner_email=body.owner_email,
        owner_phone=body.owner_phone,
        notes=body.notes,
        scrape_address=body.scrape_address,
        actor=user.email,
        snmp_prober=_snmp_answer,
        alarms=body.alarms,
    )
    return _asset(asset)


@router.post("/assets/{asset_id}/clone")
def clone_asset_api(
    asset_id: str,
    body: AssetCloneBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
) -> dict:
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="asset not found")
    defaults = clone_prefill(db, item)
    try:
        asset = create_manual_asset(
            db,
            hostname=(body.hostname or "").strip() or defaults["hostname"],
            ip=body.ip if body.ip is not None else "",
            type=(body.type or "").strip() or defaults["type"],
            environment=(body.environment or "").strip() or defaults["environment"],
            owner=body.owner if body.owner is not None else defaults["owner"],
            contact_name=body.contact_name if body.contact_name is not None else defaults["contact_name"],
            owner_email=body.owner_email if body.owner_email is not None else defaults["owner_email"],
            owner_phone=body.owner_phone if body.owner_phone is not None else defaults["owner_phone"],
            notes=body.notes if body.notes is not None else defaults["notes"],
            scrape_address=body.scrape_address if body.scrape_address is not None else defaults["scrape_address"],
            actor=user.email,
            require_new=True,
            cloned_from=item.asset_id,
            alarms=body.alarms if body.alarms is not None else getattr(item, "alarms", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _asset(asset)


@router.post("/assets/{asset_id}/delete")
def delete_asset_api(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
) -> dict:
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="asset not found")
    try:
        return delete_asset(db, item, actor=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/detect-exporter")
def detect_exporter_api(
    ip: str = "",
    hint_type: str = "",
    user: User = Depends(require("write_assets")),
) -> dict[str, Any]:
    del user
    return detect_exporter(ip, hint_type=hint_type, snmp_prober=_snmp_answer).as_dict()


@router.post("/webhooks/alertmanager")
async def alertmanager_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    auth = request.headers.get("authorization") or ""
    token = auth.replace("Bearer ", "").strip()
    if token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="invalid webhook token")
    payload = await request.json()
    created = ingest_alertmanager(db, payload)
    return {"accepted": True, "incidents": [item.number for item in created]}


@router.post("/demo")
def demo(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    seed(db)
    set_demo_cpu(94)
    seed_demo_candidate(db)
    incident = run_demo(db)
    return {"ok": True, "incident": incident.number if incident else None}


@router.post("/demo-rca")
def demo_rca(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    seed(db)
    set_demo_disk(94)
    seed_demo_candidate(db)
    incident = run_demo_rca(db)
    return {"ok": True, "incident": incident.number if incident else None}


@router.post("/demo-host")
def demo_host(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    seed(db)
    seed_demo_candidate(db)
    incident = run_demo_host(db)
    return {"ok": True, "incident": incident.number if incident else None}


@router.post("/demo-windows")
def demo_windows(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    seed(db)
    seed_demo_candidate(db)
    incident = run_demo_windows(db)
    return {"ok": True, "incident": incident.number if incident else None}


@router.post("/demo-network")
def demo_network(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    seed(db)
    seed_demo_candidate(db)
    incident = run_demo_network(db)
    return {"ok": True, "incident": incident.number if incident else None}


@router.post("/demo-nodecpu")
def demo_nodecpu(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    seed(db)
    seed_demo_candidate(db)
    incident = run_demo_nodecpu(db)
    return {"ok": True, "incident": incident.number if incident else None}


@router.get("/system/status")
def system_status(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict:
    asset_counts = dict(db.query(Asset.status, func.count(Asset.id)).group_by(Asset.status).all())
    incident_counts = dict(db.query(Incident.status, func.count(Incident.id)).group_by(Incident.status).all())
    critical_open = (
        db.query(func.count(Incident.id))
        .filter(func.upper(Incident.severity) == "CRITICAL", Incident.status != "CLOSED")
        .scalar()
        or 0
    )
    return {
        "assets": {
            "total": int(sum(asset_counts.values())),
            "healthy": int(asset_counts.get("healthy") or 0),
            "warning": int(asset_counts.get("warning") or 0),
            "critical": int(asset_counts.get("critical") or 0),
            "offline": int(asset_counts.get("offline") or 0),
        },
        "incidents": {
            "open": int(incident_counts.get("OPEN") or 0),
            "critical": int(critical_open),
            "investigating": int(incident_counts.get("INVESTIGATING") or 0),
            "resolved": int(incident_counts.get("RESOLVED") or 0),
        },
        "monitoring": doctor_payload()["components"],
    }


@router.get("/system/doctor")
def doctor(request: Request, user: User | None = Depends(current_user)) -> dict:
    auth = request.headers.get("authorization") or ""
    token = auth.replace("Bearer ", "").strip()
    if user is None and token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="authentication required")
    return doctor_payload()


@router.get("/jobs")
def list_jobs_api(db: Session = Depends(get_db), user: User = Depends(require("read_play"))) -> list[dict]:
    from app.jobs import list_jobs

    return [
        {
            "id": row.id,
            "kind": row.kind,
            "status": row.status,
            "object_id": row.object_id,
            "error": row.error,
            "attempts": row.attempts,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }
        for row in list_jobs(db)
    ]


@router.post("/demo-reset")
def demo_reset(user: User = Depends(require("admin"))) -> dict:
    del user
    reset_demo_gauges()
    return {"ok": True, "cpu": 12, "disk": 35}


@router.post("/users")
def create_user(body: UserBody, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    from app.users import create_user as add_user

    try:
        row = add_user(db, user, email=body.email, name=body.name, password=body.password, role=body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"id": row.id, "email": row.email, "role": row.role}


@router.post("/users/{user_id}")
def update_user_api(
    user_id: int,
    body: UserUpdateBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("admin")),
) -> dict:
    from app.users import update_user as save_user

    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")
    try:
        save_user(
            db,
            user,
            row,
            email=body.email,
            name=body.name,
            password=body.password or "",
            role=body.role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"id": row.id, "email": row.email, "role": row.role}


@router.post("/users/{user_id}/delete")
def delete_user_api(
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("admin")),
) -> dict:
    from app.users import delete_user as remove_user

    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")
    try:
        email = remove_user(db, user, row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"ok": True, "email": email}


def _ok(status: str) -> dict[str, str]:
    return {"status": status}


_DOCTOR_TTL = 8.0
_doctor_cache: dict[str, Any] = {"at": 0.0, "payload": None}


def doctor_payload(*, force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _doctor_cache.get("payload")
    if (
        not force
        and cached is not None
        and now - float(_doctor_cache.get("at") or 0) < _DOCTOR_TTL
    ):
        return cached
    payload = _doctor_payload_fresh()
    _doctor_cache["at"] = now
    _doctor_cache["payload"] = payload
    return payload


def _doctor_payload_fresh() -> dict[str, Any]:
    components = {
        # Machine key "core" = compose service. Display: Core (container).
        # Always-ok stack row — not the curl of /api/v1/health (that is Core API).
        "core": _ok("ok"),
        "postgres": _probe_sql(),
        "prometheus": _http(f"{settings.prometheus_url}/-/ready", "GET"),
        "alertmanager": _http(f"{settings.alertmanager_url}/-/ready", "GET"),
        "loki": _http(f"{settings.loki_url}/loki/api/v1/status/buildinfo", "GET") if settings.loki_enabled else _ok("disabled"),
        "alloy": _http("http://127.0.0.1:12345/metrics", "GET") if settings.loki_enabled else _ok("disabled"),
        "grafana": _http("http://127.0.0.1:3000/api/health", "GET") if settings.grafana_enabled else _ok("disabled"),
        "snmp": _snmp_check(),
        "llm": _http((settings.llm_url or "").rstrip("/") + "/models", "GET") if settings.llm_url else _ok("disabled"),
        "netbox": _netbox_check(),
        "discovery": _ok("ok") if settings.discovery_enabled else _ok("disabled"),
    }
    for name, item in components.items():
        item["label"] = component_label(name)
    failed = [name for name, item in components.items() if not doctor_soft_status(item["status"])]
    return {
        "overall": "HEALTHY" if not failed else "DEGRADED",
        "components": components,
        "failed": failed,
    }


def _snmp_check() -> dict[str, str]:
    """snmp-exporter: running when :9116 answers; paused (not DOWN) with no targets."""
    if not settings.snmp_enabled:
        return _ok("disabled")
    url = f"{settings.snmp_exporter_url}/metrics"
    result = _http(url, "GET")
    if result.get("status") == "ok":
        return result
    targets = snmp_target_count()
    if targets <= 0:
        return {
            "status": "paused",
            "why": "No SNMP/network targets; snmp-exporter is paused (not down).",
            "test": f"curl -fsS {url}",
            "fix": "Add a Network device with an IP, then docker compose up -d snmp-exporter",
        }
    if ensure_snmp_exporter():
        time.sleep(1.5)
        result = _http(url, "GET")
        if result.get("status") == "ok":
            return result
    result["fix"] = "docker compose up -d snmp-exporter"
    result["test"] = f"curl -fsS {url}"
    return result


def _netbox_check() -> dict[str, str]:
    if not settings.netbox_enabled:
        return _ok("disabled")
    from app.netbox import netbox_status

    result = netbox_status(settings.netbox_url, settings.netbox_token)
    if result.get("ok"):
        return _ok("ok")
    return {
        "status": "error",
        "why": str(result.get("why") or "NetBox unreachable"),
        "test": f"curl -H 'Authorization: Token …' {settings.netbox_url}/api/status/",
        "fix": "Set inventory.netbox.url and NETBOX_API_TOKEN, or disable NetBox.",
    }


def _candidate(item: DiscoveryCandidate) -> dict[str, Any]:
    return {
        "id": item.id,
        "ip": item.ip,
        "proposed_role": item.proposed_role,
        "open_ports": item.open_ports,
        "status": item.status,
        "source": item.source,
        "asset_id": item.asset_id,
    }


def _probe_sql() -> dict[str, str]:
    from app.db import SessionLocal

    try:
        db = SessionLocal()
        db.execute(Asset.__table__.select().limit(1))
        db.close()
        return _ok("ok")
    except Exception as exc:
        return {"status": "error", "why": str(exc), "fix": "Check PostgreSQL container and DATABASE_URL"}


def _http(url: str, method: str) -> dict[str, str]:
    try:
        with httpx.Client(timeout=4.0) as client:
            response = client.request(method, url)
        if response.status_code < 400:
            return _ok("ok")
        return {
            "status": "error",
            "why": f"{url} returned {response.status_code}",
            "test": f"curl -fsS {url}",
            "fix": "Check the container logs and config/forgesre.yml",
        }
    except Exception as exc:
        return {
            "status": "error",
            "why": str(exc),
            "test": f"curl -fsS {url}",
            "fix": "Confirm the service is running: ./doctor.sh",
        }


def _snmp_answer(ip: str) -> bool:
    from discovery import probe_snmp_udp

    return bool(probe_snmp_udp(ip))


def _asset(item: Asset) -> dict[str, Any]:
    reach = reachability_snapshot(item)
    return {
        "number": item.number,
        "asset_id": item.asset_id,
        "hostname": item.hostname,
        "ip": item.ip,
        "type": item.type,
        "environment": item.environment,
        "status": item.status,
        "monitoring_profile": item.monitoring_profile,
        "owner": item.owner,
        "contact_name": item.contact_name,
        "owner_email": item.owner_email,
        "owner_phone": item.owner_phone,
        "notes": item.notes,
        "source": item.source,
        "scrape_address": item.scrape_address,
        "alarms": getattr(item, "alarms", None) or {},
        "snmp": is_snmp_asset(item),
        "ping": reach["ping"],
        "ping_detail": reach["ping_detail"],
        "exporter": reach["exporter"],
        "exporter_detail": reach["exporter_detail"],
        "exporter_label": reach["exporter_label"],
        "probe_checked_at": reach["checked_at"],
    }


def _incident(item: Incident, include_evidence: bool) -> dict[str, Any]:
    latest = item.investigations[-1] if item.investigations else None
    data: dict[str, Any] = {
        "number": item.number,
        "title": item.title,
        "severity": item.severity,
        "status": item.status,
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "ended_at": item.ended_at.isoformat() if item.ended_at else None,
        "ack_at": item.ack_at.isoformat() if item.ack_at else None,
        "ack_by": item.ack_by or "",
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
        "resolved_by": item.resolved_by or "",
        "asset": _asset(item.asset) if item.asset else None,
        "playrule": item.playrule.name if item.playrule else None,
        "playbook": item.playbook.name if item.playbook else None,
        "timeline": item.timeline or [],
        "summary": item.summary,
        "demo": is_demo_incident(item),
        "investigation": None,
    }
    if latest:
        data["investigation"] = _investigation(latest, include_queries=include_evidence)
    if include_evidence:
        data["evidence"] = [_evidence_item(ev, include_queries=True) for ev in item.evidence]
        data["alert"] = item.alert_payload
    return data


def _investigation(item: Investigation, include_queries: bool) -> dict[str, Any]:
    result = dict(item.result or {})
    if not include_queries:
        result.pop("visual", None)
    return {
        "id": item.id,
        "summary": item.summary,
        "likely_cause": item.likely_cause,
        "confidence": item.confidence,
        "evidence": item.evidence,
        "recommended_action": item.recommended_action,
        "provider": item.provider,
        "disclaimer": item.disclaimer,
        "engine": item.engine,
        "engine_version": item.engine_version,
        "model": item.model,
        "requested_by": item.requested_by,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "result": result,
    }


def _evidence_item(item: Evidence, include_queries: bool) -> dict[str, Any]:
    data = {
        "id": item.evidence_id or f"EV-LEGACY-{item.id}",
        "db_id": item.id,
        "type": item.kind,
        "source": item.source,
        "title": item.title,
        "timestamp": item.captured_at.isoformat() if item.captured_at else None,
        "asset_id": item.asset_ref,
        "content": (item.payload or {}).get("content", item.payload),
        "confidence": item.confidence,
        "hash": item.hash,
    }
    if include_queries:
        data["query"] = item.query
        data["payload"] = item.payload
    return data


def _playrule(item: Playrule) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "enabled": item.enabled,
        "severity": item.severity,
        "condition": item.condition,
        "playbook": item.playbook.name if item.playbook else None,
    }


def _playbook(item: Playbook) -> dict[str, Any]:
    return {
        "id": item.id,
        "slug": item.slug,
        "name": item.name,
        "description": item.description,
        "steps": item.steps,
    }
