from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.audit import audit
from app.journal import MODULES, entry_as_dict, list_entries, module_counts, report
from app.db import get_db, get_db_session
from app.metrics import reset_demo_gauges, set_demo_cpu, set_demo_disk
from app.models import Asset, DiscoveryCandidate, EscalationPolicy, Evidence, Incident, Investigation, Playbook, Playrule, User, utcnow
from app.history import (
    acknowledge,
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
from app.security import authenticate, can, user_from_session
from app.inventory import (
    approve_candidate,
    clone_prefill,
    add_asset,
    delete_asset,
    ignore_candidate,
    is_snmp_asset,
    persist_live_classification,
    sd_targets,
    sd_snmp_host_targets,
    sd_snmp_power_targets,
    sd_snmp_routing_targets,
    sd_snmp_onboard_targets,
    sd_snmp_targets,
    seed_demo_candidate,
    snmp_auths,
    similar_incident_groups,
    sync_netbox,
    update_asset,
)
from app.asset_probe import apply_probe_to_asset, probe_target, refresh_reachability, reachability_snapshot
from app.asset_verify import (
    am_health as urllib_am_health,
    compose_verify,
    prom_targets as urllib_prom_targets,
    select_assets as verify_select_assets,
)
from app.exporter_detect import is_auto_asset_type
from app.demo_ids import is_lab_inventory_row
from app.seed import seed
from app.services import (
    alarm_ticker,
    host_down_public,
    incident_source,
    incident_source_label,
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
from app.asset_extras import asset_playrule_ids, public_extras, support_status
from app.playrule_mute import muted_playrule_ids
from app.asset_types import snmp_port_for
from app.host_resources import appliance_resources
from app.stack import (
    component_label,
    doctor_soft_status,
    journal_doctor_alarm_path,
    snmp_target_count,
)

log = logging.getLogger("forgesre")
router = APIRouter(prefix="/api/v1")


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return user_from_session(db, request.cookies.get("forgesre_session"))


def _webhook_token_ok(request: Request) -> bool:
    """Bearer == ALERTMANAGER_WEBHOOK_TOKEN (webhook, SD, doctor). Constant-time compare."""
    auth = request.headers.get("authorization") or ""
    token = auth.replace("Bearer ", "").strip()
    return hmac.compare_digest(token.encode(), (settings.webhook_token or "").encode())


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
    email2: str = ""
    name: str
    surname: str = ""
    password: str
    role: str = "analyst"
    phone: str = ""
    phone2: str = ""


class UserUpdateBody(BaseModel):
    email: str | None = None
    email2: str | None = None
    name: str | None = None
    surname: str | None = None
    password: str | None = None
    role: str | None = None
    phone: str | None = None
    phone2: str | None = None


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
    ip = request.client.host if request.client else ""
    user, wait = authenticate(db, body.email, body.password, ip)
    if wait:
        raise HTTPException(
            status_code=429,
            detail="too many failed sign-in attempts",
            headers={"Retry-After": str(wait)},
        )
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    audit(db, "login", actor=user.email, ip=ip, commit=True)
    return {"email": user.email, "role": user.role, "name": user.name}


@router.get("/me")
def me(user: User = Depends(require_user)) -> dict[str, Any]:
    """The signed-in user, plus the appliance version from .env (FORGESRE_VERSION) for the UI."""
    return {
        "email": user.email,
        "role": user.role,
        "name": user.name,
        "version": os.environ.get("FORGESRE_VERSION", "").strip().strip('"'),
    }


@router.get("/assets")
def list_assets(
    db: Session = Depends(get_db),
    user: User = Depends(require("read_assets")),
    source: str = "",
    agent: str = "",
) -> list[dict]:
    from app.inventory import assets_matching

    rows = db.query(Asset).order_by(Asset.number, Asset.hostname).all()
    if source or agent:
        rows = assets_matching(rows, source=source, agent=agent)
    return [_asset(item) for item in rows]


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
        "source": incident_source(incident),
        "source_label": incident_source_label(incident),
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


VERIFY_PROBE_WORKERS = 8


class VerifyContext:
    """SD membership, Prometheus /targets and Alertmanager health: fetched once per verify request."""

    def __init__(self, db: Session) -> None:
        self.http_ids, self.snmp_ids = _sd_membership(db)
        self.prom_url = settings.prometheus_url or "http://127.0.0.1:9090"
        self.am_health = urllib_am_health(settings.alertmanager_url or "http://127.0.0.1:9093")
        self._targets: dict[str, Any] | None = None

    def targets(self) -> dict[str, Any]:
        if self._targets is None:
            self._targets = urllib_prom_targets(self.prom_url)
        return self._targets


def _probe_assets(assets: list[Asset], *, timeout: float) -> list[Any]:
    items = [_asset(asset) for asset in assets]
    workers = max(1, min(VERIFY_PROBE_WORKERS, len(items)))
    if workers == 1:
        return [probe_target(item, timeout=timeout) for item in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda item: probe_target(item, timeout=timeout), items))


def verify_assets(db: Session, assets: list[Asset], *, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Probe in a bounded pool; classify, compose and save on this thread (the session is not thread-safe)."""
    from app.services import query_prometheus_expr

    if not assets:
        return []
    probes = _probe_assets(assets, timeout=timeout)
    for asset, probe in zip(assets, probes):
        if persist_live_classification(db, asset, probe):
            db.commit()
            db.refresh(asset)
    ctx = VerifyContext(db)
    ai_enabled = bool(settings.ai_enabled and settings.llm_url)
    reports: list[dict[str, Any]] = []
    for asset, probe in zip(assets, probes):
        report = compose_verify(
            _asset(asset),
            probe,
            in_http_sd=asset.asset_id in ctx.http_ids,
            in_snmp_sd=asset.asset_id in ctx.snmp_ids,
            query_fn=query_prometheus_expr,
            rca=_latest_rca(db, asset),
            live_metrics=_live_metric_values(asset),
            ai_enabled=ai_enabled,
            targets_fn=ctx.targets,
            am_health=ctx.am_health,
            incident=_latest_incident(db, asset),
        )
        apply_probe_to_asset(asset, probe)
        reports.append(report.as_dict())
    db.commit()
    return reports


def run_asset_verify(db: Session, asset: Asset, *, timeout: float = 2.0) -> dict[str, Any]:
    return verify_assets(db, [asset], timeout=timeout)[0]


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
    models = {item.asset_id: item for item in db.query(Asset).all()}
    assets = [models[key] for key in (str(row.get("asset_id") or "") for row in chosen) if key in models]
    results = verify_assets(db, assets, timeout=timeout)
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
    incident: str = "",
) -> dict[str, Any]:
    """Class-based glance tiles (CPU/mem/disk/up). Viewers can read.

    No ``incident`` → last hour (asset page). With the incident number for this
    asset, the range starts one hour before ``started_at`` and runs through now,
    or through the resolve time.
    """
    del user
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="asset not found")
    from app.asset_metrics import incident_graph_bounds, metric_panel_with_zabbix, safe_asset_metric_panel

    bounds: dict[str, Any] = {}
    number = (incident or "").strip()
    if number:
        row = db.query(Incident).filter_by(number=number).first()
        if row is not None and row.asset is not None and row.asset.asset_id == item.asset_id:
            window = incident_graph_bounds(row)
            bounds = {
                "range_start": window["start"],
                "range_end": window["end"],
                "marker": window["marker"],
            }
    return metric_panel_with_zabbix(item, safe_asset_metric_panel(item, **bounds))


@router.get("/assets/{asset_id}/capacity")
def asset_capacity_api(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require("read_assets")),
    refresh: bool = False,
) -> dict[str, Any]:
    """30-day disk and interface history with days-to-full. Read in a background job; poll while pending."""
    del user
    from app.asset_capacity import capacity_view

    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return capacity_view(db, item, refresh=refresh)


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


class MajorBody(BaseModel):
    on: bool = True
    commander: str = Field(default="", max_length=255)
    bridge_url: str = Field(default="", max_length=500)
    update_minutes: int = Field(default=30, ge=5, le=240)


class UpdateBody(BaseModel):
    text: str = Field(default="", max_length=2000)


class ActionBody(BaseModel):
    text: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=255)
    due: str | None = None
    done: bool | None = None


class HandoverBody(BaseModel):
    notes: str = Field(default="", max_length=4000)


def _incident_or_404(db: Session, number: str) -> Incident:
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return item


@router.post("/incidents/{number}/major")
def incident_major(number: str, body: MajorBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Declare or stand down a major incident (commander, bridge link, update timer). Analyst and up."""
    from app.incident_process import major_view, set_major

    item = _incident_or_404(db, number)
    try:
        set_major(db, item, user.email, on=body.on, commander=body.commander, bridge_url=body.bridge_url, update_minutes=body.update_minutes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return major_view(item)


@router.post("/incidents/{number}/ticket")
def incident_ticket(number: str, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Open a GLPI ticket for this incident in the background (tickets.glpi.enabled)."""
    from app.jobs import enqueue
    from app.tickets import CREATE_KIND, glpi_ready

    item = _incident_or_404(db, number)
    if item.ticket_ref:
        raise HTTPException(status_code=409, detail=f"this incident already has ticket {item.ticket_ref}")
    why = glpi_ready()
    if why:
        raise HTTPException(status_code=409, detail=why)
    job = enqueue(db, CREATE_KIND, item.number, object_type="incident", payload={"incident": item.number, "actor": user.email})
    db.commit()
    return {"job": getattr(job, "id", None)}


@router.post("/incidents/{number}/updates")
def incident_status_update(number: str, body: UpdateBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Post a status update: saved as a note, restarts the update timer, goes to the chat channels."""
    from app.incident_process import major_view, post_update

    item = _incident_or_404(db, number)
    try:
        note = post_update(db, item, user.email, body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"note": note, "major": major_view(item)}


@router.post("/incidents/{number}/actions")
def incident_action_add(number: str, body: ActionBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Add a follow-up action (what, who, by when) to an incident."""
    from app.incident_process import action_as_dict, add_action

    item = _incident_or_404(db, number)
    try:
        return action_as_dict(add_action(db, item, user.email, body.text or "", body.owner or "", body.due))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/actions/{action_id}")
def incident_action_update(action_id: int, body: ActionBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Change or tick off a follow-up action."""
    from app.incident_process import action_as_dict, update_action
    from app.models import FollowUpAction

    row = db.get(FollowUpAction, action_id)
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")
    try:
        return action_as_dict(update_action(db, row, user.email, body.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/actions")
def incident_actions_open(db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Every open follow-up action, overdue and soonest due first."""
    from app.incident_process import open_actions

    return {"actions": open_actions(db)}


@router.get("/sla")
def sla_summary(days: int = 30, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """SLA met / breached per severity (response = acknowledged in time, resolve = resolved in time)."""
    from app.incident_process import sla_config, sla_report

    return {**sla_report(db, days), "targets": sla_config()}


@router.get("/power")
def power_api(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict:
    """Power and environment panel: UPS rows, sensor readings, power assets that answer nothing."""
    from app.power import overview

    return overview(db)


@router.get("/racks")
def racks_api(site: str = "", db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict:
    """Rack elevation: devices by site / row / rack and U position, with open incidents."""
    from app.racks import racks

    return racks(db, site)


@router.get("/lifecycle")
def lifecycle_api(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict:
    """Support / warranty, end of life and licence dates that are expiring or expired, soonest first."""
    from app.lifecycle import overview

    return overview(db)


class SyntheticBody(BaseModel):
    name: str
    kind: str = "http"
    target: str
    asset: str = ""
    severity: str = "critical"
    enabled: bool = True


@router.get("/synthetic")
def synthetic_list(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict:
    """Synthetic checks with their last probe result from Prometheus."""
    from app.synthetic import overview

    return {**overview(db), "can_edit": can(user, "write_play")}


@router.post("/synthetic")
def synthetic_create(body: SyntheticBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Add a check (Engineer+); Prometheus picks it up through HTTP SD within 30 s."""
    from app.synthetic import as_dict, save

    try:
        row = save(db, body.model_dump(), user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "synthetic.create", actor=user.email, object_type="synthetic", object_id=row.name, data={"kind": row.kind, "target": row.target})
    db.commit()
    return as_dict(row)


@router.put("/synthetic/{check_id}")
def synthetic_update(check_id: int, body: SyntheticBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    from app.synthetic import as_dict, save

    try:
        row = save(db, body.model_dump(), user.email, check_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "synthetic.update", actor=user.email, object_type="synthetic", object_id=row.name, data={"kind": row.kind, "target": row.target})
    db.commit()
    return as_dict(row)


@router.delete("/synthetic/{check_id}")
def synthetic_delete(check_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    from app.models import SyntheticCheck

    row = db.get(SyntheticCheck, check_id)
    if row is None:
        raise HTTPException(status_code=404, detail="check not found")
    audit(db, "synthetic.delete", actor=user.email, object_type="synthetic", object_id=row.name, data={"target": row.target})
    db.delete(row)
    db.commit()
    return {"deleted": check_id}


@router.get("/network")
def network_api(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict:
    """Busiest interfaces, BGP peers and OSPF neighbours (SNMP via Prometheus)."""
    from app.network_view import overview

    return overview(db)


@router.get("/search")
def quick_search(q: str = "", db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Command palette (Ctrl+K): up to 8 incidents and 8 assets whose number, title, host or IP match."""
    text = q.strip()[:100]
    if len(text) < 2:
        return {"incidents": [], "assets": []}
    like = f"%{text}%"
    incidents = (db.query(Incident).filter(or_(Incident.number.ilike(like), Incident.title.ilike(like)))
                 .order_by(Incident.id.desc()).limit(8).all())
    assets = (db.query(Asset).filter(or_(Asset.asset_id.ilike(like), Asset.hostname.ilike(like), Asset.ip.ilike(like)))
              .order_by(Asset.hostname).limit(8).all()) if can(user, "read_assets") else []
    return {
        "incidents": [{"number": i.number, "title": i.title, "severity": i.severity, "status": i.status} for i in incidents],
        "assets": [{"asset_id": a.asset_id, "hostname": a.hostname, "ip": a.ip, "type": a.type} for a in assets],
    }


@router.get("/notify/critical")
def new_critical(after: int = -1, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Browser sound / desktop notification: open CRITICAL incidents newer than ``after`` (incident id).

    ``latest`` is the newest incident id (0 when there is none); the first call (no ``after``)
    only returns it, so a browser that just opened does not ring for everything already burning.
    """
    latest = db.query(func.max(Incident.id)).scalar() or 0
    if after < 0:
        return {"latest": latest, "items": []}
    rows = (db.query(Incident).filter(Incident.id > after, Incident.severity == "CRITICAL", Incident.status.in_(("OPEN", "INVESTIGATING", "ESCALATED")),
                                      Incident.caused_by_id.is_(None))
            .order_by(Incident.id).limit(5).all())
    return {"latest": latest, "items": [{"number": r.number, "title": r.title, "host": r.asset.hostname if r.asset else ""} for r in rows]}


class ViewBody(BaseModel):
    page: str = "incidents"
    name: str
    filters: dict[str, Any] = {}
    team: str = ""


@router.get("/views")
def views_list(page: str = "incidents", db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Saved views: yours and the ones shared with a team."""
    from app.known_errors import list_views

    return {"views": list_views(db, user, page), "can_share": can(user, "write_play"), "can_save": can(user, "write_incidents")}


@router.post("/views")
def views_create(body: ViewBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Save the current filters (Analyst+); sharing with a team needs Engineer."""
    from app.known_errors import save_view, view_dict

    try:
        row = save_view(db, user, body.page, body.name, body.filters, body.team)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return view_dict(row, user)


@router.delete("/views/{view_id}")
def views_delete(view_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    from app.known_errors import delete_view

    try:
        delete_view(db, user, view_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"deleted": view_id}


class KnownErrorBody(BaseModel):
    title: str
    alertname: str = ""
    pattern: str = ""
    symptoms: str = ""
    workaround: str = ""
    fix: str = ""
    link: str = ""
    status: str = "open"


@router.get("/known-errors")
def known_errors_list(q: str = "", db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Known-errors knowledge base, optionally filtered by text."""
    from app.known_errors import search_known_errors

    return {"items": search_known_errors(db, q), "can_edit": can(user, "write_play")}


@router.post("/known-errors")
def known_errors_create(body: KnownErrorBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Add a known error (Engineer+)."""
    from app.known_errors import ke_dict, save_known_error

    try:
        row = save_known_error(db, body.model_dump(), user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "known_error.create", actor=user.email, object_type="known_error", object_id=str(row.id), data={"title": row.title})
    db.commit()
    return ke_dict(row)


@router.put("/known-errors/{ke_id}")
def known_errors_update(ke_id: int, body: KnownErrorBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    from app.known_errors import ke_dict, save_known_error

    try:
        row = save_known_error(db, body.model_dump(), user.email, ke_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "known_error.update", actor=user.email, object_type="known_error", object_id=str(row.id), data={"title": row.title})
    db.commit()
    return ke_dict(row)


@router.delete("/known-errors/{ke_id}")
def known_errors_delete(ke_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    from app.models import KnownError

    row = db.get(KnownError, ke_id)
    if row is None:
        raise HTTPException(status_code=404, detail="known error not found")
    audit(db, "known_error.delete", actor=user.email, object_type="known_error", object_id=str(ke_id), data={"title": row.title})
    db.delete(row)
    db.commit()
    return {"deleted": ke_id}


class GoldenBody(BaseModel):
    number: str
    expect: str = ""


@router.get("/forgeai/golden")
def golden_list(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict:
    """ForgeAI golden test set: the incidents and the last runs with their scores."""
    from app.forgeai_golden import JOB_KIND, overview
    from app.models import Job

    pending = db.query(Job).filter(Job.kind == JOB_KIND, Job.status.in_(["pending", "running"])).count()
    return {**overview(db), "running": bool(pending), "can_edit": can(user, "write_play")}


@router.post("/forgeai/golden")
def golden_add(body: GoldenBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Add a resolved incident with a confirmed cause to the golden set (Engineer+)."""
    from app.forgeai_golden import add_case

    try:
        row = add_case(db, body.number, body.expect, user.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": row.id}


@router.delete("/forgeai/golden/{case_id}")
def golden_delete(case_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    from app.models import GoldenCase

    row = db.get(GoldenCase, case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="case not found")
    db.delete(row)
    db.commit()
    return {"deleted": case_id}


class GoldenRunBody(BaseModel):
    skill: str = ""


@router.post("/forgeai/golden/run")
def golden_run(body: GoldenRunBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Queue a run of the golden set on the local model (LLM lane, background)."""
    from app.forgeai_golden import JOB_KIND
    from app.jobs import enqueue
    from app.models import GoldenCase

    if not db.query(GoldenCase).count():
        raise HTTPException(status_code=400, detail="the golden set is empty")
    job = enqueue(db, JOB_KIND, "golden", object_type="forgeai", payload={"skill": body.skill.strip(), "actor": user.email})
    audit(db, "forgeai.golden.run", actor=user.email, object_type="forgeai", object_id="golden", data={"skill": body.skill})
    db.commit()
    return {"job": job.id if job else None}


class PushSubscribeBody(BaseModel):
    endpoint: str
    keys: dict[str, str]


@router.get("/push")
def push_info(db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Web push: on or off, the VAPID public key and how many devices this user subscribed."""
    from app.models import PushSubscription
    from app.web_push import enabled, public_key

    if not enabled():
        return {"enabled": False, "public_key": "", "devices": 0}
    devices = db.query(PushSubscription).filter_by(email=user.email).count()
    return {"enabled": True, "public_key": public_key(db), "devices": devices}


@router.post("/push/subscribe")
def push_subscribe(body: PushSubscribeBody, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Store this browser's push subscription for the signed-in user."""
    from app.web_push import enabled, save_subscription

    if not enabled():
        raise HTTPException(status_code=400, detail="web push is off (notifications.push.enabled)")
    try:
        row = save_subscription(db, user.email, body.endpoint, body.keys.get("p256dh", ""), body.keys.get("auth", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": row.id}


@router.post("/push/unsubscribe")
def push_unsubscribe(body: PushSubscribeBody, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    from app.models import PushSubscription

    deleted = db.query(PushSubscription).filter_by(endpoint=body.endpoint, email=user.email).delete()
    db.commit()
    return {"deleted": deleted}


@router.post("/push/test")
def push_test(db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Queue a test push to every device of the signed-in user."""
    from app.jobs import enqueue
    from app.web_push import JOB_KIND, enabled

    if not enabled():
        raise HTTPException(status_code=400, detail="web push is off (notifications.push.enabled)")
    job = enqueue(db, JOB_KIND, f"test:{user.email}", object_type="user", payload={"test_email": user.email})
    return {"job": job.id if job else None}


@router.get("/handover")
def handover_list(db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """The latest shift handovers, newest first."""
    from app.incident_process import latest_handovers

    return {"handovers": latest_handovers(db)}


@router.post("/handover")
def handover_create(body: HandoverBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Hand the shift over: notes plus a snapshot of the incidents still open."""
    from app.incident_process import hand_over, handover_as_dict

    return handover_as_dict(hand_over(db, user.email, body.notes))


@router.post("/handover/{handover_id}/take")
def handover_take(handover_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """The next shift confirms it read the handover ("I take over")."""
    from app.incident_process import handover_as_dict, take_over
    from app.models import ShiftHandover

    row = db.get(ShiftHandover, handover_id)
    if row is None:
        raise HTTPException(status_code=404, detail="handover not found")
    try:
        return handover_as_dict(take_over(db, row, user.email))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    from app.services import refresh_asset_status

    # Same as the incident page: INVESTIGATING is Acknowledge (OPEN → INVESTIGATING,
    # anything else stays, including ESCALATED). Never demote ESCALATED to OPEN.
    current = (item.status or "").upper()
    if status == "INVESTIGATING":
        acknowledge(item, user.email)
    elif status == "OPEN" and current == "ESCALATED":
        pass
    else:
        item.status = status
        apply_status_fields(item, status, user.email)
    db.flush()
    refresh_asset_status(db, item.asset)
    data = {"status": (item.status or "").upper()}
    if status == "INVESTIGATING":
        data["ack"] = True
    audit(db, "incident.status", actor=user.email, object_type="incident", object_id=number, data=data)
    db.commit()
    return _incident(item, include_evidence=True)


class BulkStatusBody(BaseModel):
    numbers: list[str] = Field(default_factory=list, max_length=200)
    action: str = "ack"


@router.post("/incidents/bulk")
def incidents_bulk(body: BulkStatusBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Acknowledge or resolve many incidents at once (one audit row each), e.g. 20 alarms from one cause.

    Unknown numbers are skipped and listed. The permission is the same as for one status change.
    """
    from app.services import refresh_asset_status

    action = body.action.lower()
    if action not in {"ack", "resolve"}:
        raise HTTPException(status_code=400, detail="action is ack or resolve")
    done, missing = [], []
    for number in dict.fromkeys(n.strip() for n in body.numbers if n.strip()):
        item = db.query(Incident).filter_by(number=number).first()
        if item is None:
            missing.append(number)
            continue
        if action == "ack":
            acknowledge(item, user.email)
        elif (item.status or "").upper() not in {"RESOLVED", "CLOSED"}:
            item.status = "RESOLVED"
            apply_status_fields(item, "RESOLVED", user.email)
        db.flush()
        refresh_asset_status(db, item.asset)
        audit(db, "incident.status", actor=user.email, object_type="incident", object_id=number, data={"status": (item.status or "").upper(), "bulk": True})
        done.append(number)
    db.commit()
    return {"done": done, "missing": missing}


class LinkBody(BaseModel):
    caused_by: str = ""


@router.post("/incidents/{number}/caused-by")
def incident_caused_by(number: str, body: LinkBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Mark this incident as a consequence of another one (empty ``caused_by`` removes the link)."""
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    target = None
    if body.caused_by.strip():
        target = db.query(Incident).filter_by(number=body.caused_by.strip()).first()
        if target is None:
            raise HTTPException(status_code=404, detail="no incident with that number")
        if target.id == item.id:
            raise HTTPException(status_code=400, detail="an incident cannot cause itself")
        seen, cur = {item.id}, target  # no loops: A caused by B caused by A
        while cur is not None and cur.caused_by_id:
            if cur.caused_by_id in seen:
                raise HTTPException(status_code=400, detail="that would make a loop of causes")
            seen.add(cur.id)
            cur = db.get(Incident, cur.caused_by_id)
    item.caused_by_id = target.id if target else None
    audit(db, "incident.caused_by", actor=user.email, object_type="incident", object_id=number, data={"caused_by": target.number if target else ""})
    db.commit()
    return {"number": number, "caused_by": target.number if target else ""}


class QuietBody(BaseModel):
    hours: float = Field(default=2, gt=0, le=72)
    reason: str = Field(default="", max_length=200)


@router.post("/incidents/{number}/quiet-host")
def incident_quiet_host(number: str, body: QuietBody, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    """Put the incident's asset in maintenance from now for ``hours`` (alarms muted, with an end), from the incident page."""
    from app.maintenance_edit import set_window

    item = db.query(Incident).filter_by(number=number).first()
    if item is None or item.asset is None:
        raise HTTPException(status_code=404, detail="incident or its asset not found")
    from app.settings import settings as cfg

    zone = ZoneInfo(cfg.timezone or "UTC")
    start = utcnow().astimezone(zone)
    end = start + timedelta(hours=body.hours)
    fmt = "%Y-%m-%dT%H:%M"
    try:
        set_window(db, [item.asset.asset_id], start.strftime(fmt), end.strftime(fmt), user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "incident.quiet_host", actor=user.email, object_type="incident", object_id=number,
          data={"asset": item.asset.asset_id, "hours": body.hours, "reason": body.reason}, commit=True)
    return {"asset": item.asset.asset_id, "until": end.isoformat()}


class ForgeAiAsk(BaseModel):
    task: str = "incident"
    skill: str = ""


@router.post("/incidents/{number}/forgeai")
def forgeai_ask(number: str, body: ForgeAiAsk, db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """Ask ForgeAI about this incident in the background: cause and first checks (optionally with another
    skill), explain the alarm, or the postmortem paragraph. Analyst and up; the model is local."""
    from app.forgeai import JOB_KIND, TASKS
    from app.jobs import enqueue
    from app.settings import settings

    if body.task not in TASKS or body.task in {"shift", "ask"}:
        raise HTTPException(status_code=400, detail="task is incident, investigate, explain or postmortem")
    if not (settings.ai_enabled and settings.llm_url):
        raise HTTPException(status_code=409, detail="ForgeAI is off: turn it on in Settings › RCA / AI")
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    if body.task == "incident":
        job = enqueue(db, "investigate", item.number, payload={"use_llm": True, "skill": body.skill, "actor": user.email})
    else:
        # one pending job per incident and task: an explain request does not swallow an investigation
        job = enqueue(db, JOB_KIND, f"{item.number}#{body.task}", object_type="incident",
                      payload={"task": body.task, "skill": body.skill, "actor": user.email})
    return {"queued": True, "job": getattr(job, "id", None)}


@router.get("/incidents/{number}/forgeai")
def forgeai_calls(number: str, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """ForgeAI answers for this incident, newest first; Analysts and up also get the exact prompts."""
    from app.forgeai import call_as_dict
    from app.models import ForgeAiCall

    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    rows = db.query(ForgeAiCall).filter_by(incident_id=item.id).order_by(ForgeAiCall.id.desc()).limit(20).all()
    return {"calls": [call_as_dict(c, prompts=can(user, "read_evidence")) for c in rows]}


class ForgeAiQuestion(BaseModel):
    skill: str = ""
    question: str = Field(default="", max_length=500)
    cloud: bool = False


ASK_PENDING_MAX = 3


@router.post("/incidents/{number}/forgeai/ask")
def forgeai_ask_trial(number: str, body: ForgeAiQuestion, db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """Ask again with another skill, or ask your own question, in the background (Analyst and up).

    With ``question`` the task is "ask"; without it the chosen skill re-runs "cause and first checks".
    The answer is kept as a trial: it never replaces the incident's answer unless an Engineer picks it.
    At most ASK_PENDING_MAX questions per person wait at once, so one operator cannot fill the LLM lane.
    """
    from uuid import uuid4

    from app.forgeai import JOB_KIND
    from app.jobs import enqueue
    from app.models import ForgeAiSkill, Job
    from app.settings import settings

    if body.cloud:
        from app.cloud_llm import not_ready

        if not can(user, "ai_cloud"):
            raise HTTPException(status_code=403, detail="only Engineers and Admins may send a question to the cloud model")
        why = not_ready()
        if why:
            raise HTTPException(status_code=409, detail=f"cloud model not available: {why}")
    elif not (settings.ai_enabled and settings.llm_url):
        raise HTTPException(status_code=409, detail="ForgeAI is off: turn it on in Settings › RCA / AI")
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    question = " ".join(body.question.split())
    task = "ask" if question else "incident"
    if body.skill:
        skill = db.query(ForgeAiSkill).filter_by(slug=body.skill).first()
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        if skill.task not in {"incident", "ask"}:
            raise HTTPException(status_code=400, detail="pick a 'cause and first checks' or 'question' skill")
        if not question and skill.task == "ask":
            raise HTTPException(status_code=400, detail="type a question for this skill")
    elif not question:
        raise HTTPException(status_code=400, detail="pick a skill or type a question")
    waiting = [j for j in db.query(Job).filter(Job.kind == JOB_KIND, Job.status.in_(["pending", "running"]), Job.object_id.like("%#ask-%")).all()
               if (j.payload or {}).get("actor") == user.email]
    if len(waiting) >= ASK_PENDING_MAX:
        raise HTTPException(status_code=429, detail=f"{len(waiting)} of your questions are still waiting; try again when one is answered")
    job = enqueue(db, JOB_KIND, f"{item.number}#ask-{uuid4().hex[:8]}", object_type="incident",
                  payload={"task": task, "skill": body.skill if body.skill else ("ask-generic" if task == "ask" else ""),
                           "question": question, "trial": True, "actor": user.email, "cloud": body.cloud})
    audit(db, "forgeai.cloud" if body.cloud else "forgeai.ask", actor=user.email, object_type="incident", object_id=item.number,
          data={"task": task, "skill": body.skill, "question": question[:200],
                **({"provider": settings.cloud_provider, "model": settings.cloud_model} if body.cloud else {})}, commit=True)
    return {"queued": True, "job": getattr(job, "id", None), "task": task}


@router.get("/forgeai/pulse")
def forgeai_pulse(db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """The small status line above every ForgeAI chat: model, connection, last test, queue, cloud."""
    from app.forgeai import pulse

    out = pulse(db)
    out["cloud"] = {**out["cloud"], "can_use": can(user, "ai_cloud")}
    out["can_test"] = can(user, "investigate")
    return out


@router.post("/forgeai/ping")
def forgeai_ping(db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """Queue one tiny timed question to the local model (LLM lane; one at a time)."""
    from app.forgeai import PING_KIND
    from app.jobs import enqueue
    from app.models import Job
    from app.settings import settings

    if not (settings.ai_enabled and settings.llm_url):
        raise HTTPException(status_code=409, detail="ForgeAI is off: turn it on in Settings › RCA / AI")
    waiting = db.query(Job).filter(Job.kind == PING_KIND, Job.status.in_(("pending", "running"))).first()
    if waiting is not None:
        return {"job": waiting.id, "already": True}
    job = enqueue(db, PING_KIND, f"ping-{int(time.time())}", object_type="forgeai", payload={"actor": user.email})
    db.commit()
    return {"job": job.id, "already": False}


class ForgeAiChatNew(BaseModel):
    title: str = Field(default="", max_length=255)
    incident: str = ""
    reuse: bool = False


class ForgeAiChatSay(BaseModel):
    text: str = Field(default="", max_length=2000)
    skill: str = ""
    cloud: bool = False


def _own_chat(db: Session, chat_id: int, user: User):
    from app.forgeai_chat import get_chat

    chat = get_chat(db, chat_id, user.email)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")
    return chat


@router.get("/forgeai/chats")
def forgeai_chats(db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """Your ForgeAI chats, newest first (Analyst and up; nobody sees anyone else's chats)."""
    from app.forgeai_chat import list_chats, pending_count, skills_for
    from app.settings import settings

    return {"chats": list_chats(db, user.email), "pending": pending_count(db, user.email),
            "skills": skills_for(db, incident=False), "local_on": bool(settings.ai_enabled and settings.llm_url),
            "can_cloud": can(user, "ai_cloud")}


@router.post("/forgeai/chats")
def forgeai_chat_new(body: ForgeAiChatNew, db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """Start a chat, optionally about one incident; ``reuse`` returns your latest chat about it."""
    from app.forgeai_chat import chat_detail, open_chat

    incident = None
    if body.incident:
        incident = db.query(Incident).filter_by(number=body.incident).first()
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
    return chat_detail(db, open_chat(db, user.email, title=body.title, incident=incident, reuse=body.reuse))


@router.get("/forgeai/chats/{chat_id}")
def forgeai_chat_get(chat_id: int, db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    from app.forgeai_chat import chat_detail, skills_for

    chat = _own_chat(db, chat_id, user)
    return {**chat_detail(db, chat), "skills": skills_for(db, incident=bool(chat.incident_id))}


@router.post("/forgeai/chats/{chat_id}/messages")
def forgeai_chat_say(chat_id: int, body: ForgeAiChatSay, db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """Send a message; the answer comes from a background job (poll the chat).

    The cloud model is for Engineers and up and only when it is set up; at most PENDING_MAX answers
    per person wait at once so one operator cannot fill the LLM lane.
    """
    from app.forgeai_chat import PENDING_MAX, ask, message_as_dict, pending_count
    from app.models import ForgeAiSkill
    from app.settings import settings

    chat = _own_chat(db, chat_id, user)
    if body.cloud:
        from app.cloud_llm import not_ready

        if not can(user, "ai_cloud"):
            raise HTTPException(status_code=403, detail="only Engineers and Admins may send a question to the cloud model")
        why = not_ready()
        if why:
            raise HTTPException(status_code=409, detail=f"cloud model not available: {why}")
    elif not (settings.ai_enabled and settings.llm_url):
        raise HTTPException(status_code=409, detail="ForgeAI is off: turn it on in Settings › RCA / AI")
    skill = None
    if body.skill:
        skill = db.query(ForgeAiSkill).filter_by(slug=body.skill, enabled=True).first()
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
    if pending_count(db, user.email) >= PENDING_MAX:
        raise HTTPException(status_code=429, detail="three of your answers are still coming; wait for one")
    try:
        question, answer = ask(db, chat, body.text, skill=skill, cloud=body.cloud, actor=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "forgeai.cloud" if body.cloud else "forgeai.chat", actor=user.email, object_type="forgeai_chat", object_id=str(chat.id),
          data={"skill": body.skill, "text": body.text[:200],
                **({"provider": settings.cloud_provider, "model": settings.cloud_model} if body.cloud else {})}, commit=True)
    return {"question": message_as_dict(question), "answer": message_as_dict(answer), "title": chat.title}


@router.delete("/forgeai/chats/{chat_id}")
def forgeai_chat_delete(chat_id: int, db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """Delete one of your chats (the logged model calls stay in Investigations)."""
    from app.models import ForgeAiChatMessage

    chat = _own_chat(db, chat_id, user)
    db.query(ForgeAiChatMessage).filter_by(chat_id=chat.id).delete()
    db.delete(chat)
    db.commit()
    return {"deleted": chat_id}


@router.post("/forgeai/calls/{call_id}/critique")
def forgeai_critique_call(call_id: int, db: Session = Depends(get_db), user: User = Depends(require("ai_cloud"))) -> dict:
    """Engineer: ask the paid cloud model for a second opinion on one answer (background job, audited)."""
    from uuid import uuid4

    from app.cloud_llm import not_ready
    from app.forgeai import JOB_KIND
    from app.jobs import enqueue
    from app.models import ForgeAiCall

    why = not_ready()
    if why:
        raise HTTPException(status_code=409, detail=f"cloud model not available: {why}")
    row = db.get(ForgeAiCall, call_id)
    if row is None or row.incident_id is None:
        raise HTTPException(status_code=404, detail="not found")
    if row.task != "incident" or row.status not in {"used", "trial"}:
        raise HTTPException(status_code=400, detail="only a checked 'cause and first checks' answer can get a second opinion")
    incident = db.get(Incident, row.incident_id)
    job = enqueue(db, JOB_KIND, f"{incident.number}#critic-{uuid4().hex[:8]}", object_type="incident",
                  payload={"task": "critic", "call_id": row.id, "actor": user.email, "cloud": True, "trial": True})
    audit(db, "forgeai.cloud", actor=user.email, object_type="incident", object_id=incident.number,
          data={"task": "critic", "call_id": row.id, "provider": settings.cloud_provider, "model": settings.cloud_model}, commit=True)
    return {"queued": True, "job": getattr(job, "id", None)}


@router.post("/forgeai/calls/{call_id}/use")
def forgeai_use_call(call_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Engineer: make a checked trial answer (another skill) this incident's ForgeAI answer."""
    from app.forgeai import use_trial_answer
    from app.models import ForgeAiCall

    row = db.get(ForgeAiCall, call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        use_trial_answer(db, row, user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": row.id, "status": row.status}


@router.get("/forgeai/docs")
def forgeai_docs(db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """Uploaded vendor documents with their indexing state, and whether search can run (Analyst and up)."""
    from app import embeddings
    from app.doc_search import doc_as_dict, vector_ready
    from app.models import DocFile

    rows = db.query(DocFile).order_by(DocFile.id.desc()).all()
    return {"docs": [doc_as_dict(d) for d in rows], "embedder": embeddings.available(), "embed_url": settings.embed_url or "",
            "model": embeddings.model_name(), "pgvector": vector_ready(db), "can_edit": can(user, "write_play")}


@router.post("/forgeai/docs")
def forgeai_docs_upload(file: UploadFile = File(...), title: str = "", db: Session = Depends(get_db),
                        user: User = Depends(require("write_play"))) -> dict:
    """Engineer: upload a PDF, text, Markdown or HTML file; the doc_index job reads and embeds it."""
    from pathlib import Path

    from app.doc_search import MAX_BYTES, SUFFIXES, doc_as_dict
    from app.jobs import enqueue
    from app.models import DocFile

    name = Path(file.filename or "").name[:255]
    if not name.lower().endswith(SUFFIXES):
        raise HTTPException(status_code=400, detail="upload a .pdf, .txt, .md or .html file")
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"files up to {MAX_BYTES // (1024 * 1024)} MB")
    if not data:
        raise HTTPException(status_code=400, detail="the file is empty")
    doc = DocFile(name=name, title=(title or Path(name).stem)[:255], size=len(data), raw=data, uploaded_by=user.email)
    db.add(doc)
    db.flush()
    enqueue(db, "doc_index", str(doc.id), object_type="doc", payload={"actor": user.email})
    audit(db, "forgeai.doc.upload", actor=user.email, object_type="doc", object_id=str(doc.id), data={"name": name, "size": len(data)}, commit=True)
    return doc_as_dict(doc)


@router.post("/forgeai/docs/{doc_id}/reindex")
def forgeai_docs_reindex(doc_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Engineer: run indexing again (after the embedding server was down). Needs the upload still waiting."""
    from app.jobs import enqueue
    from app.models import DocFile

    doc = db.get(DocFile, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    if doc.raw is None:
        raise HTTPException(status_code=400, detail="already indexed; upload the file again to re-read it")
    doc.status, doc.error = "pending", ""
    job = enqueue(db, "doc_index", str(doc.id), object_type="doc", payload={"actor": user.email})
    db.commit()
    return {"queued": True, "job": getattr(job, "id", None)}


@router.delete("/forgeai/docs/{doc_id}")
def forgeai_docs_delete(doc_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Engineer: remove a document and its chunks; ForgeAI stops citing it at once."""
    from app.models import DocChunk, DocFile

    doc = db.get(DocFile, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    db.query(DocChunk).filter_by(doc_id=doc.id).delete(synchronize_session=False)
    db.delete(doc)
    audit(db, "forgeai.doc.delete", actor=user.email, object_type="doc", object_id=str(doc_id), data={"name": doc.name}, commit=True)
    return {"deleted": doc_id}


@router.get("/forgeai/docs/search")
def forgeai_docs_search(q: str, db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """Try the search: the 5 closest excerpts for a phrase (one short local embedding call, 10 s timeout)."""
    from app.doc_search import search
    from app.embeddings import EmbeddingError

    try:
        return {"hits": search(db, q, k=5, min_score=0.0)}
    except EmbeddingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/forgeai/status")
def forgeai_status(db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Is ForgeAI on, does the model answer, how did the last week of calls go; the optional cloud model."""
    from app.cloud_llm import info
    from app.forgeai import status

    out = status(db)
    out["cloud"] = {**info(), "can_use": can(user, "ai_cloud")}
    return out


class ForgeAiTrial(BaseModel):
    incident: str = ""
    task: str = "incident"
    skill: str = ""
    instructions: str | None = None
    question: str = Field(default="", max_length=500)


@router.post("/forgeai/trial")
def forgeai_trial(body: ForgeAiTrial, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Try a skill (saved or unsaved text) on an incident, newest by default. The answer is kept as a
    trial and never shown as the incident's answer. Engineer and up."""
    from app.forgeai import JOB_KIND, TASKS
    from app.jobs import enqueue
    from app.settings import settings

    if body.task not in TASKS:
        raise HTTPException(status_code=400, detail="unknown task")
    if not (settings.ai_enabled and settings.llm_url):
        raise HTTPException(status_code=409, detail="ForgeAI is off: turn it on in Settings › RCA / AI")
    target = "shift"
    if body.task != "shift":
        q = db.query(Incident)
        item = q.filter_by(number=body.incident).first() if body.incident else q.order_by(Incident.started_at.desc()).first()
        if item is None:
            raise HTTPException(status_code=404, detail="no incident to try it on")
        target = item.number
    from uuid import uuid4

    # a unique object id: each trial (new text) is its own job, not deduplicated into a pending one
    job = enqueue(db, JOB_KIND, f"{target}#trial-{uuid4().hex[:8]}", object_type="forgeai",
                  payload={"task": body.task, "skill": body.skill, "trial": True, "instructions": body.instructions, "actor": user.email,
                           # a question skill is tried with the operator's text, or a plain default question
                           "question": body.question or ("What most likely caused this, and what should be checked first?" if body.task == "ask" else "")})
    return {"queued": True, "job": getattr(job, "id", None), "incident": None if target == "shift" else target}


@router.post("/forgeai/shift-summary")
def forgeai_shift(db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """Queue a shift handover summary (last 12 hours). Poll GET /api/v1/forgeai/calls/{id} via the job result."""
    from app.forgeai import JOB_KIND
    from app.jobs import enqueue
    from app.settings import settings

    if not (settings.ai_enabled and settings.llm_url):
        raise HTTPException(status_code=409, detail="ForgeAI is off: turn it on in Settings › RCA / AI")
    job = enqueue(db, JOB_KIND, "shift", object_type="forgeai", payload={"task": "shift", "actor": user.email})
    return {"queued": True, "job": getattr(job, "id", None)}


@router.get("/forgeai/calls/{call_id}")
def forgeai_call(call_id: int, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    from app.forgeai import call_as_dict
    from app.models import ForgeAiCall

    row = db.get(ForgeAiCall, call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return call_as_dict(row, prompts=can(user, "read_evidence"))


class ForgeAiRate(BaseModel):
    stars: int | None = Field(default=None, ge=0, le=5)
    value: int | None = Field(default=None, ge=-1, le=1)  # older thumbs API: 1 = 5 stars, -1 = 1 star


@router.post("/forgeai/calls/{call_id}/rate")
def forgeai_rate(call_id: int, body: ForgeAiRate, db: Session = Depends(get_db), user: User = Depends(require("investigate"))) -> dict:
    """1–5 stars (0 clears) on one ForgeAI answer; counted per skill. 4–5 also count as thumbs up,
    1–2 as thumbs down, so older reports keep working."""
    from app.models import ForgeAiCall

    row = db.get(ForgeAiCall, call_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if body.stars is None and body.value is None:
        raise HTTPException(status_code=400, detail="give stars (0–5)")
    stars = body.stars if body.stars is not None else {1: 5, -1: 1, 0: 0}[int(body.value or 0)]
    row.stars, row.rating, row.rated_by = stars, (1 if stars >= 4 else -1 if 0 < stars <= 2 else 0), user.email
    audit(db, "forgeai.rate", actor=user.email, object_type="forgeai_call", object_id=str(call_id), data={"stars": stars, "skill": row.skill_slug})
    db.commit()
    return {"id": row.id, "rating": row.rating, "stars": row.stars}


@router.get("/forgeai/skills")
def forgeai_skills(db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """All skills with their last-30-day results; ``can_edit`` for Engineers and up."""
    from app.forgeai import TASKS, seed_skills, skill_as_dict, skill_stats
    from app.models import ForgeAiSkill

    seed_skills(db)
    db.commit()
    stats = skill_stats(db)
    rows = db.query(ForgeAiSkill).order_by(ForgeAiSkill.task, ForgeAiSkill.priority, ForgeAiSkill.id).all()
    return {"skills": [skill_as_dict(s, stats.get(s.slug)) for s in rows], "tasks": {k: v["label"] for k, v in TASKS.items()},
            "can_edit": can(user, "write_play")}


@router.post("/forgeai/skills")
def forgeai_skill_create(body: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New skill (Engineer and up)."""
    from app.forgeai import create_skill, skill_as_dict

    try:
        skill = create_skill(db, body, user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "forgeai.skill.create", actor=user.email, object_type="forgeai_skill", object_id=skill.slug, data={"task": skill.task})
    db.commit()
    return skill_as_dict(skill)


@router.put("/forgeai/skills/{slug}")
def forgeai_skill_save(slug: str, body: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Save a skill: a new version; the old one stays in its history and in the audit log."""
    from app.forgeai import save_skill, skill_as_dict

    try:
        skill = save_skill(db, slug, body, user.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "forgeai.skill.update", actor=user.email, object_type="forgeai_skill", object_id=slug,
          data={"version": skill.version, "fields": sorted(k for k in body if k != "instructions") + (["instructions"] if "instructions" in body else [])})
    db.commit()
    return skill_as_dict(skill)


@router.post("/forgeai/skills/{slug}/reset")
def forgeai_skill_reset(slug: str, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Back to the shipped instructions (built-in skills only), as a new version."""
    from app.forgeai import save_skill, skill_as_dict
    from app.models import ForgeAiSkill

    row = db.query(ForgeAiSkill).filter_by(slug=slug).first()
    if row is None or not row.builtin:
        raise HTTPException(status_code=404, detail="only built-in skills have a default")
    skill = save_skill(db, slug, {"instructions": row.default_instructions}, user.email)
    audit(db, "forgeai.skill.reset", actor=user.email, object_type="forgeai_skill", object_id=slug, data={"version": skill.version})
    db.commit()
    return skill_as_dict(skill)


@router.delete("/forgeai/skills/{slug}")
def forgeai_skill_delete(slug: str, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Remove a skill you added. Built-in skills can only be turned off."""
    from app.models import ForgeAiSkill

    row = db.query(ForgeAiSkill).filter_by(slug=slug).first()
    if row is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if row.builtin:
        raise HTTPException(status_code=400, detail="built-in skills can be turned off, not removed")
    db.delete(row)
    audit(db, "forgeai.skill.delete", actor=user.email, object_type="forgeai_skill", object_id=slug)
    db.commit()
    return {"deleted": slug}


class ModelDownload(BaseModel):
    catalog_id: str = ""
    url: str = ""


@router.get("/llm/models")
def llm_models_api(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    """Settings › ForgeAI models: installed, active, catalog, disk and RAM, running downloads (Admin)."""
    from app.llm_models import overview

    return overview(db)


@router.post("/llm/models/download")
def llm_models_download(body: ModelDownload, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    """Download a catalog model or a Hugging Face .gguf link in the background (``llm_download`` job)."""
    from app.llm_models import queue_download

    try:
        job = queue_download(db, catalog_id=body.catalog_id, url=body.url, actor=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "forgeai.model.download", actor=user.email, object_type="model", object_id=body.catalog_id or body.url[:200], commit=True)
    return {"queued": True, "job": job}


@router.post("/llm/downloads/{job_id}/cancel")
def llm_models_cancel(job_id: int, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    """Stop a download; the part already fetched stays as .partial so Download again resumes."""
    from app.models import Job

    row = db.get(Job, job_id)
    if row is None or row.kind != "llm_download":
        raise HTTPException(status_code=404, detail="download not found")
    if row.status == "pending":
        row.status, row.error = "error", "cancelled"
    row.payload = {**(row.payload or {}), "cancel": True}
    db.commit()
    return {"cancelled": True}


@router.post("/llm/models/{name}/activate")
def llm_models_activate(name: str, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    """Run this model; llama.cpp restarts with it within a few seconds."""
    from app.llm_models import activate

    try:
        result = activate(db, name, user.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "forgeai.model.activate", actor=user.email, object_type="model", object_id=name, commit=True)
    return result


@router.delete("/llm/models/{name}")
def llm_models_remove(name: str, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    from app.llm_models import remove

    try:
        gone = remove(name)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "forgeai.model.remove", actor=user.email, object_type="model", object_id=name, commit=True)
    return {"removed": gone}


@router.post("/llm/models/upload")
async def llm_models_upload(request: Request, file: UploadFile, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    """Import a .gguf from the browser (streamed to disk). For very large files, copying into
    data/models on the VM is faster; it shows up here the same way."""
    from app.llm_models import save_upload

    try:
        saved = await save_upload(file, size_hint=int(request.headers.get("content-length") or 0))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "forgeai.model.upload", actor=user.email, object_type="model", object_id=saved["name"], commit=True)
    return saved


@router.get("/forgeai/jobs/{job_id}")
def forgeai_job(job_id: int, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """State of a ForgeAI job and, when done, its call id."""
    from app.models import Job

    row = db.get(Job, job_id)
    if row is None or row.kind not in ("forgeai", "investigate", "forgeai_replay"):
        raise HTTPException(status_code=404, detail="job not found")
    payload = row.payload or {}
    call_id = (payload.get("result") or {}).get("call_id") or payload.get("call_id")
    out = {"id": row.id, "status": row.status, "error": row.error or "", "call_id": call_id}
    if row.kind == "forgeai_replay":
        out["result"] = payload.get("result")
    return out


@router.get("/forgeai/skills/{slug}/tuning")
def forgeai_skill_tuning(slug: str, days: int = 60, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """A skill's answers next to the analysts' confirmed causes, ratings and rejection reasons."""
    del user
    from app.forgeai_tuning import tuning_report

    try:
        return tuning_report(db, slug, days=max(7, min(int(days or 60), 365)))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ReplayBody(BaseModel):
    limit: int = 10
    instructions: str | None = None


@router.post("/forgeai/skills/{slug}/replay")
def forgeai_skill_replay(slug: str, body: ReplayBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """Ask the skill again (saved or unsaved text) on the last confirmed incidents, as trials (Engineer+)."""
    from app.forgeai_tuning import REPLAY_KIND
    from app.jobs import enqueue
    from app.models import ForgeAiSkill
    from app.settings import settings

    skill = db.query(ForgeAiSkill).filter_by(slug=slug).first()
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if skill.task not in {"incident", "investigate"}:
        raise HTTPException(status_code=400, detail="replay is for skills that answer incidents")
    if not (settings.ai_enabled and settings.llm_url):
        raise HTTPException(status_code=409, detail="ForgeAI is off: turn it on in Settings › RCA / AI")
    text = body.instructions.strip() if body.instructions else None
    if text is not None and len(text) < 20:
        raise HTTPException(status_code=400, detail="instructions need at least a sentence")
    job = enqueue(db, REPLAY_KIND, slug, object_type="forgeai",
                  payload={"limit": max(1, min(body.limit, 20)), "instructions": text, "actor": user.email})
    return {"queued": True, "job": getattr(job, "id", None)}


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
    from app.jobs import enqueue

    # Runs on the jobs thread, never in the request (AGENTS.md §2); the ForgeAI rewrite
    # is queued by the job when ForgeRCA finishes.
    enqueue(db, "investigate", item.number, payload={"use_llm": False, "force": True, "actor": user.email})
    data = _incident(item, include_evidence=can(user, "read_evidence"))
    data["queued"] = True
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
    rows = db.query(Evidence).filter(Evidence.incident_id == row.incident_id).all()
    # Every immutable RCA item for the incident; supporting/contradicting ids are a hint, not a filter.
    return [
        _evidence_item(item, include_queries=include_queries)
        for item in rows
        if not item.evidence_id.startswith("ROLLUP-")
    ]


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
    if not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid sd token")
    return sd_targets(db)


@router.get("/sd/snmp")
def snmp_sd(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    if not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid sd token")
    return sd_snmp_targets(db)


@router.get("/sd/snmp-host")
def snmp_host_sd(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    if not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid sd token")
    return sd_snmp_host_targets(db)


@router.get("/sd/snmp-power")
def snmp_power_sd(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    if not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid sd token")
    return sd_snmp_power_targets(db)


@router.get("/sd/snmp-routing")
def snmp_routing_sd(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    if not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid sd token")
    return sd_snmp_routing_targets(db)


@router.get("/sd/synthetic")
def synthetic_sd(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    if not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid sd token")
    from app.synthetic import sd_targets

    return sd_targets(db)


@router.get("/sd/snmp-onboard")
def snmp_onboard_sd(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    if not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid sd token")
    return sd_snmp_onboard_targets(db)


@router.get("/sd/snmp-onboard-config")
def snmp_onboard_config(request: Request, db: Session = Depends(get_db)) -> dict:
    """Extra forgesre_onboard YAML for render_snmp_onboard.py. No SNMP secrets."""
    if not settings.webhook_token or not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid sd token")
    from app.snmp_onboard import onboard_config

    rows = db.query(Asset).order_by(Asset.asset_id).all()
    return onboard_config(rows)


@router.get("/sd/snmp-auths")
def snmp_sd_auths(request: Request, db: Session = Depends(get_db)) -> dict:
    """Per-asset snmp_exporter auths for scripts/render_snmp_auths.py. Holds community strings and
    v3 passwords: same bearer as SD, never logged, never shown in the GUI."""
    if not settings.webhook_token or not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid sd token")
    return {"auths": snmp_auths(db)}


class IdentifyBody(BaseModel):
    ip: str
    snmp_port: int = 161
    snmp: dict[str, Any] = Field(default_factory=dict)


ONBOARD_JOBS = ("onboard_identify", "asset_verify", "snmp_apply", "discovery_scan", "asset_import")


@router.post("/onboard/identify")
def onboard_identify(body: IdentifyBody, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    """Probe one address in the background; poll GET /api/v1/onboard/jobs/{id}. Nothing is saved."""
    from uuid import uuid4

    from app.asset_snmp import normalize_snmp
    from app.jobs import enqueue
    from app.onboarding import IDENTIFY_KIND, _check_ip

    try:
        ip = _check_ip(body.ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = enqueue(db, IDENTIFY_KIND, f"{ip}#{uuid4().hex[:8]}", object_type="address",
                  payload={"ip": ip, "snmp_port": body.snmp_port, "snmp": normalize_snmp(body.snmp), "actor": user.email})
    return {"job": job.id if job else None}


@router.post("/assets/{asset_id}/verify-job")
def onboard_verify(asset_id: str, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    """Check monitoring for one asset in the background (address → exporter/SNMP → Prometheus → Alertmanager → Core)."""
    from app.jobs import enqueue
    from app.onboarding import VERIFY_KIND
    from app.snmp_apply import queue_if_needed

    asset = db.query(Asset).filter_by(asset_id=asset_id).first()
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    applying = queue_if_needed(db, asset, actor=user.email)
    job = enqueue(db, VERIFY_KIND, asset_id, object_type="asset", payload={"actor": user.email})
    return {"job": job.id if job else None, "snmp_apply": applying}


@router.get("/onboard/jobs/{job_id}")
def onboard_job(job_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    """Status and result of an identify, verify, SNMP apply or scan job."""
    from app.models import Job

    row = db.get(Job, job_id)
    if row is None or row.kind not in ONBOARD_JOBS:
        raise HTTPException(status_code=404, detail="job not found")
    payload = row.payload or {}
    return {"id": row.id, "kind": row.kind, "status": row.status, "error": row.error or "",
            "result": payload.get("result"), "object_id": row.object_id}


@router.get("/discovery/status")
def discovery_status(db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    """Add devices › From LibreNMS: whether LibreNMS can be read, the running and last read, found counts."""
    from app.jobs import active_discovery_scan, latest_discovery_scan
    from app.librenms_discovery import not_ready

    job = active_discovery_scan(db)
    last = latest_discovery_scan(db)
    counts: dict[str, int] = {}
    for (status,) in db.query(DiscoveryCandidate.status).all():
        counts[status] = counts.get(status, 0) + 1
    last_out = None
    if last is not None:
        last_out = {"status": last.status, "at": last.finished_at.isoformat() if last.finished_at else None,
                    "error": (last.error or "")[:300], "result": (last.payload or {}).get("result")}
    return {"source": "librenms", "ready": not not_ready(), "why": not_ready(), "librenms_url": settings.librenms_url,
            "mode": settings.discovery_mode, "enabled": settings.discovery_enabled,
            "scan": {"id": job.id, "status": job.status} if job else None, "last": last_out, "counts": counts}


@router.get("/discovery/candidates")
def list_candidates(db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> list[dict]:
    rows = db.query(DiscoveryCandidate).order_by(DiscoveryCandidate.id.desc()).all()
    return [_candidate(item) for item in rows]


@router.post("/discovery/scan")
def discovery_scan(db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    """Queue a read of LibreNMS's devices and links. ForgeSRE does not sweep networks itself."""
    from app.jobs import active_discovery_scan, enqueue_discovery_scan
    from app.librenms_discovery import not_ready

    why = not_ready()
    if why:
        raise HTTPException(status_code=400, detail=why)
    already = active_discovery_scan(db) is not None
    job = enqueue_discovery_scan(db, actor=user.email)
    audit(db, "discovery.scan", actor=user.email, data={"queued": True, "job_id": job.id if job else None, "already": already}, commit=True)
    return {"queued": True, "already": already, "job_id": job.id if job else None,
            "status": job.status if job else "pending", "kind": "discovery_scan"}


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


@router.post("/discovery/zabbix-sync")
def discovery_zabbix_sync(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    from app.zabbix_sync import sync_zabbix

    return sync_zabbix(db, force=True)


@router.post("/discovery/glpi-sync")
def discovery_glpi_sync(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    from app.glpi_sync import sync_glpi

    return sync_glpi(db, force=True)


class AssetBody(BaseModel):
    hostname: str
    asset_id: str = ""
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
    snmp_port: int | None = None
    alarms: dict | None = None
    extras: dict | None = None
    playrule_ids: list[int] | None = None
    playrule_off: list[int] | None = None


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
    snmp_port: int | None = None
    alarms: dict | None = None
    extras: dict | None = None
    playrule_ids: list[int] | None = None
    playrule_off: list[int] | None = None


class AssetCloneBody(BaseModel):
    hostname: str | None = None
    asset_id: str | None = None
    ip: str | None = None
    type: str | None = None
    environment: str | None = None
    owner: str | None = None
    contact_name: str | None = None
    owner_email: str | None = None
    owner_phone: str | None = None
    notes: str | None = None
    scrape_address: str | None = None
    snmp_port: int | None = None
    alarms: dict | None = None
    extras: dict | None = None
    playrule_ids: list[int] | None = None
    playrule_off: list[int] | None = None


@router.post("/assets")
async def create_asset_api(
    body: AssetBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_assets")),
) -> dict:
    try:
        async with get_db_session() as session:
            asset = await add_asset(
                session,
                write_db=db,
                hostname=body.hostname,
                asset_id=body.asset_id,
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
                extras=body.extras,
                playrule_ids=body.playrule_ids,
                playrule_off=body.playrule_off,
                snmp_port=body.snmp_port,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _queue_snmp_apply(db, asset, user.email)
    return _asset(asset)


class AssetImportBody(BaseModel):
    csv: str


@router.get("/assets/import/template")
def asset_import_template(user: User = Depends(require("write_assets"))) -> Response:
    """Empty CSV with every column Import CSV reads, plus one example row."""
    del user
    from app.asset_import import COLUMNS

    example = {"hostname": "web-01", "ip": "10.0.0.11", "type": "Linux Server", "environment": "Production",
               "owner": "platform", "site": "DC1", "rack": "A07"}
    body = ",".join(COLUMNS) + "\n" + ",".join(example.get(c, "") for c in COLUMNS) + "\n"
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="forgesre-assets-template.csv"'})


@router.post("/assets/import/preview")
def asset_import_preview(body: AssetImportBody, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    """Check a CSV against the Add asset rules; writes nothing."""
    del user
    from app.asset_import import preview, public

    try:
        return public(preview(db, body.csv))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/assets/import")
def asset_import_start(body: AssetImportBody, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    """Check the CSV again on the server, then add the accepted rows in the asset_import job."""
    from app.asset_import import IMPORT_KIND, preview
    from app.jobs import enqueue

    try:
        result = preview(db, body.csv)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = [r for r in result["rows"] if r["action"] == "create"]
    if not rows:
        raise HTTPException(status_code=400, detail="Nothing to add: every row already exists or has an error.")
    job = enqueue(db, IMPORT_KIND, f"csv-{int(time.time() * 1000)}", object_type="asset",
                  payload={"actor": user.email, "rows": rows})
    return {"job": job.id if job else None, "counts": result["counts"]}


def _queue_snmp_apply(db: Session, asset: Any, actor: str) -> None:
    """A saved custom community / v3 user reaches snmp_exporter without ./forgesre snmp-auths."""
    from app.snmp_apply import queue_if_needed

    try:
        fresh = db.query(Asset).filter_by(asset_id=asset.asset_id).first()
        if fresh is not None:
            queue_if_needed(db, fresh, actor=actor)
    except Exception:
        log.exception("could not queue snmp_apply for %s", getattr(asset, "asset_id", "?"))


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
    if body.ip is not None:
        from app.inventory import validate_ip_field

        try:
            validate_ip_field(body.ip)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        extras=body.extras,
        playrule_ids=body.playrule_ids,
        playrule_off=body.playrule_off,
        snmp_port=body.snmp_port,
    )
    _queue_snmp_apply(db, asset, user.email)
    return _asset(asset)


@router.post("/assets/{asset_id}/clone")
async def clone_asset_api(
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
        posted_host = (body.hostname or "").strip()
        posted_id = (body.asset_id or "").strip() if body.asset_id is not None else ""
        if posted_id:
            new_id = posted_id
        elif posted_host:
            new_id = ""
        else:
            new_id = defaults.get("asset_id") or ""
        async with get_db_session() as session:
            asset = await add_asset(
                session,
                write_db=db,
                hostname=posted_host or defaults["hostname"],
                asset_id=new_id,
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
                extras=body.extras if body.extras is not None else defaults["extras"],
                playrule_ids=body.playrule_ids if body.playrule_ids is not None else defaults["playrule_ids"],
                playrule_off=body.playrule_off,
                snmp_port=body.snmp_port if body.snmp_port is not None else defaults["snmp_port"],
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
async def detect_exporter_api(
    ip: str = "",
    hint_type: str = "",
    user: User = Depends(require("write_assets")),
) -> dict[str, Any]:
    del user
    from app.exporter_detect import detect_exporter_type

    return (await detect_exporter_type(ip, hint_type=hint_type, snmp_prober=_snmp_answer)).as_dict()


def _ingest_numbers(db: Session, payload: dict[str, Any], source: str = "prometheus") -> list[str]:
    return [item.number for item in ingest_alertmanager(db, payload, source=source)]


def _background_verify_and_route(payload: dict[str, Any], source: str) -> None:
    """After 202. Async session (asyncpg / aiosqlite) on a worker thread.

    Request ``get_db()`` is already closed. Starlette runs this sync task in a
    threadpool so SMTP/RCA cannot block the event loop. The session is committed
    or rolled back and always returned to the pool.
    """
    from app.asset_verify import drop_librenms_flaps
    from app.db import get_db_session
    from app.librenms import run_sync
    from app.services import ingest_alertmanager

    async def _job() -> None:
        async with get_db_session() as session:

            def _sync(db: Session) -> None:
                body = payload
                if source == "librenms":
                    body = {**payload, "alerts": drop_librenms_flaps(list(payload.get("alerts") or []))}
                ingest_alertmanager(db, body, source=source)

            await session.run_sync(_sync)

    try:
        run_sync(_job())
    except Exception as exc:
        log.exception("webhook ingest failed source=%s", source)
        try:
            from app.db import SessionLocal
            from app.journal import report

            journal_db = SessionLocal()
            try:
                report(
                    journal_db,
                    "incident",
                    "ingest",
                    "error",
                    summary=f"Webhook ingest failed ({source})",
                    detail=str(exc)[:400],
                    object_type="webhook",
                    object_id=source,
                )
            finally:
                journal_db.close()
        except Exception:
            log.exception("webhook ingest journal failed source=%s", source)


def _queued_alert_count(payload: dict[str, Any]) -> int:
    alerts = payload.get("alerts")
    return len(alerts) if isinstance(alerts, list) else 0


@router.post("/webhooks/alertmanager", status_code=202)
@router.post("/alerts/alertmanager", status_code=202)
async def alertmanager_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Alertmanager group → 202, then ingest_alertmanager in the background.

    The whole payload stays one job so group-aware resolve still works. Bearer is
    ALERTMANAGER_WEBHOOK_TOKEN (same constant-time check as HTTP SD).
    """
    if not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid webhook token")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="body is not JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Alertmanager body must be a JSON object")
    queued = _queued_alert_count(payload)
    background_tasks.add_task(_background_verify_and_route, payload, "prometheus")
    return {"accepted": True, "source": "prometheus", "queued": queued}


@router.post("/webhooks/zabbix")
async def zabbix_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Zabbix media type (Webhook) → incidents with source=zabbix. Own Bearer: ZABBIX_WEBHOOK_TOKEN."""
    from app.zabbix import parse_webhook

    expected = settings.zabbix_webhook_token
    if not expected:
        raise HTTPException(status_code=503, detail="Zabbix webhook is off: set ZABBIX_WEBHOOK_TOKEN in secrets/secrets.env")
    auth = request.headers.get("authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
    if not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid zabbix webhook token")
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="body is not JSON") from exc
    events = body if isinstance(body, list) else [body]
    alerts = []
    try:
        for item in events[:50]:
            alerts.append(parse_webhook(item))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    numbers = await run_in_threadpool(_ingest_numbers, db, {"alerts": alerts}, "zabbix")
    return {
        "accepted": True,
        "source": "zabbix",
        "status": [alert["status"] for alert in alerts],
        "incidents": numbers,
    }


@router.post("/webhooks/librenms", status_code=202)
@router.post("/alerts/librenms", status_code=202)
async def librenms_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """LibreNMS API/alert transport → 202, then verify_and_route_alert.

    Flat keys (rule_name, hostname, msg, device) are normalized to the
    Alertmanager-shaped schema. Same Bearer as Alertmanager. Flap confirm
    and ingest run after the 202.
    """
    from app.librenms import parse_webhook as parse_librenms

    if not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="invalid webhook token")
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="body is not JSON") from exc
    events = body if isinstance(body, list) else [body]
    alerts = []
    try:
        for item in events[:50]:
            alerts.append(parse_librenms(item))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    queued = len(alerts)
    background_tasks.add_task(_background_verify_and_route, {"alerts": alerts}, "librenms")
    return {
        "accepted": True,
        "source": "librenms",
        "queued": queued,
        "status": [alert["status"] for alert in alerts],
    }


class DemoRunBody(BaseModel):
    scenario: str


@router.get("/demo/scenarios")
def demo_scenarios(user: User = Depends(require("admin"))) -> dict:
    """Lab scenarios for Settings › Demo."""
    del user
    from app.demo_view import scenario_list

    return {"scenarios": scenario_list()}


@router.post("/demo/run")
def demo_run(body: DemoRunBody, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    """Queue one lab scenario as the demo_run job; poll GET /api/v1/demo/jobs/{id} for the incident number."""
    from app.demo_view import DEMO_KIND, scenario_list
    from app.jobs import enqueue

    if body.scenario not in {s["key"] for s in scenario_list()}:
        raise HTTPException(status_code=400, detail="unknown demo scenario")
    job = enqueue(db, DEMO_KIND, body.scenario, object_type="demo", payload={"actor": user.email})
    return {"job": job.id if job else None}


@router.get("/demo/jobs/{job_id}")
def demo_job(job_id: int, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    del user
    from app.models import Job

    row = db.get(Job, job_id)
    if row is None or row.kind != "demo_run":
        raise HTTPException(status_code=404, detail="job not found")
    return {"id": row.id, "status": row.status, "error": row.error or "", "result": (row.payload or {}).get("result")}


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
            "escalated": int(incident_counts.get("ESCALATED") or 0),
            "resolved": int(incident_counts.get("RESOLVED") or 0),
        },
        "monitoring": doctor_payload()["components"],
    }


@router.get("/system/doctor")
def doctor(request: Request, user: User | None = Depends(current_user)) -> dict:
    if user is None and not _webhook_token_ok(request):
        raise HTTPException(status_code=401, detail="authentication required")
    return doctor_payload()


@router.get("/system/resources")
def system_resources(user: User = Depends(require_user)) -> dict:
    """This appliance VM (node_exporter / /proc), not a random inventory asset."""
    del user
    return appliance_resources()


@router.get("/ticker")
def ticker(db: Session = Depends(get_db), user: User = Depends(require_user)) -> dict:
    """Newest active production incidents for the top blink bar."""
    del user
    return alarm_ticker(db)


@router.get("/jobs", response_model=None)
def list_jobs_api(
    with_counts: int = 0, db: Session = Depends(get_db), user: User = Depends(require("read_play"))
) -> list[dict] | dict:
    """Newest 50 jobs. ``with_counts=1`` wraps them as {"counts": {status: n}, "jobs": [...]}."""
    from app.jobs import job_counts, list_jobs

    rows = [
        {
            "id": row.id,
            "kind": row.kind,
            "status": row.status,
            "object_id": row.object_id,
            "error": row.error,
            "attempts": row.attempts,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }
        for row in list_jobs(db)
    ]
    if with_counts:
        return {"counts": job_counts(db), "jobs": rows}
    return rows


@router.post("/demo-reset")
def demo_reset(user: User = Depends(require("admin"))) -> dict:
    del user
    reset_demo_gauges()
    return {"ok": True, "cpu": 12, "disk": 35}


class BackupBody(BaseModel):
    include_secrets: bool = True
    include_models: bool = False


@router.get("/backups")
def backups_api(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict[str, Any]:
    """New UI Settings › Backup: archives on disk (newest first) and any backup job still running."""
    from app.backup import list_archives
    from app.models import Job

    from app.models import JournalEntry

    running = db.query(Job).filter(Job.kind == "backup_create", Job.status.in_(["pending", "running"])).first()
    last = db.query(Job).filter(Job.kind == "backup_create").order_by(Job.id.desc()).first()
    restoring = db.query(Job).filter(Job.kind == "backup_restore", Job.status.in_(["pending", "running"])).first()
    # the restored database has no row for the job that restored it: the journal line written after it is the answer
    restored = db.query(JournalEntry).filter_by(module="backup", action="restore").order_by(JournalEntry.id.desc()).first()
    return {
        "archives": list_archives(),
        "running": bool(running),
        "restoring": bool(restoring),
        "last_restore": None if restored is None else {"at": restored.at.isoformat() if restored.at else None, "status": restored.status,
                                                       "summary": restored.summary, "detail": restored.detail},
        "last_job": None if last is None else {"status": last.status, "error": last.error or "", "result": (last.payload or {}).get("result", "")},
    }


@router.post("/backups")
def backup_create_api(body: BackupBody, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict[str, Any]:
    """New UI: start a backup as a background job (it can take minutes); the list shows it when done."""
    from app.jobs import enqueue

    job = enqueue(db, "backup_create", "backup", object_type="backup",
                  payload={"include_secrets": body.include_secrets, "include_models": body.include_models, "actor": user.email})
    audit(db, "backup.create", actor=user.email, object_type="backup", object_id=f"job-{job.id}")
    db.commit()
    return {"job": job.id, "status": job.status}


@router.delete("/backups/{name}")
def backup_delete_api(name: str, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict[str, Any]:
    """New UI: remove one backup archive from disk."""
    from app.backup import delete_backup

    try:
        deleted = delete_backup(name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="backup not found") from exc
    audit(db, "backup.remove", actor=user.email, object_type="backup", object_id=deleted.name)
    db.commit()
    return {"removed": deleted.name}


@router.get("/backups/{name}/download")
def backup_download_api(name: str, request: Request, db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    """Download one backup archive (Admin). Audited."""
    from fastapi.responses import FileResponse

    from app.backup import download_name, resolve_archive

    try:
        path = resolve_archive(name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="backup not found") from exc
    audit(db, "backup.download", actor=user.email, object_type="backup", object_id=name, ip=request.client.host if request.client else "", commit=True)
    return FileResponse(path, filename=download_name(path), media_type="application/gzip")


class RestoreBody(BaseModel):
    confirm: str = ""
    acknowledged: bool = False


@router.post("/backups/{name}/restore")
def backup_restore_api(name: str, body: RestoreBody, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict[str, Any]:
    """Restore a backup over this appliance (Admin), in the background. Needs the warning ticked and RESTORE typed."""
    from app.backup import CONFIRM_WORD, resolve_archive
    from app.jobs import enqueue

    if not body.acknowledged or body.confirm.strip() != CONFIRM_WORD:
        raise HTTPException(status_code=400, detail=f"Restore refused. Tick the warning and type {CONFIRM_WORD}.")
    try:
        resolve_archive(name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="backup not found") from exc
    audit(db, "backup.restore.start", actor=user.email, object_type="backup", object_id=name)
    db.commit()
    job = enqueue(db, "backup_restore", name, object_type="backup", payload={"actor": user.email})
    return {"job": job.id if job else None}


@router.post("/backups/import")
async def backup_import_api(archive: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict[str, Any]:
    """New UI: upload a backup archive from another appliance. Restoring stays a separate, confirmed step."""
    from app.backup import backup_ident, save_upload

    try:
        path = save_upload(await archive.read(), archive.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ident = backup_ident(path)
    audit(db, "backup.import", actor=user.email, object_type="backup", object_id=ident)
    db.commit()
    return {"imported": ident}


@router.get("/users-page")
def users_page_api(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict[str, Any]:
    """New UI Settings › Users: every account, its role, and why it cannot be removed (if so)."""
    from app.security import ROLE_HELP, ROLE_LABELS, ROLES, role_key
    from app.users import delete_blocked, is_last_admin

    rows = []
    for row in db.query(User).order_by(User.email).all():
        rows.append({
            "id": row.id, "email": row.email, "email2": row.email2 or "", "name": row.name, "surname": row.surname or "",
            "phone": row.phone or "", "phone2": row.phone2 or "", "role": role_key(row.role), "active": bool(row.is_active),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "me": row.id == user.id, "last_admin": is_last_admin(db, row), "delete_blocked": delete_blocked(user, row, db),
        })
    return {"users": rows, "roles": [{"key": r, "label": ROLE_LABELS[r], "help": ROLE_HELP[r]} for r in ROLES]}


@router.post("/users")
def create_user(body: UserBody, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    from app.users import create_user as add_user

    try:
        row = add_user(
            db,
            user,
            email=body.email,
            email2=body.email2,
            name=body.name,
            surname=body.surname,
            password=body.password,
            role=body.role,
            phone=body.phone,
            phone2=body.phone2,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"id": row.id, "email": row.email, "role": row.role}


@router.post("/users/{user_id}")
def update_user_api(
    user_id: int,
    body: UserUpdateBody,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(require("admin")),
) -> dict:
    """Save a user. A new password signs that user out everywhere; an Admin who changes
    their own password gets a fresh cookie so this browser stays signed in."""
    from app.security import set_session_cookie
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
            email2=body.email2,
            name=body.name,
            surname=body.surname,
            password=body.password or "",
            role=body.role,
            phone=body.phone,
            phone2=body.phone2,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    if row.id == user.id:
        set_session_cookie(response, row)
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


def _proc_port_listening(port: int, proto: str) -> bool:
    """True when /proc/net shows this host port bound. TCP requires state LISTEN (0A)."""
    needle = f":{port:04X}"
    paths = ("/proc/net/udp", "/proc/net/udp6") if proto == "udp" else ("/proc/net/tcp", "/proc/net/tcp6")
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[1].upper()
            state = parts[3].upper()
            if not local.endswith(needle):
                continue
            if proto == "tcp" and state != "0A":
                continue
            return True
    return False


def syslog_listen_ports(port: int = 514) -> dict[str, bool]:
    """Whether Alloy's syslog sockets are bound. Does not open the port."""
    return {"udp": _proc_port_listening(port, "udp"), "tcp": _proc_port_listening(port, "tcp")}


def _loki_check() -> dict[str, str]:
    """Loki /ready. Graphs are not this hop; a down Loki is logs only."""
    if not settings.loki_enabled:
        return _ok("disabled")
    url = f"{settings.loki_url.rstrip('/')}/ready"
    result = _http(url, "GET")
    result.setdefault("test", f"curl -fsS {url}")
    if result.get("status") == "ok":
        result["why"] = "Loki ready."
        return result
    result.setdefault("fix", "docker compose logs loki && docker compose up -d loki")
    return result


def _alloy_check() -> dict[str, str]:
    """Alloy process (metrics) plus syslog UDP/TCP 514. A closed 514 is yellow, not an alarm-path failure."""
    if not settings.loki_enabled:
        return _ok("disabled")
    result = _http("http://127.0.0.1:12345/metrics", "GET")
    result.setdefault("test", "curl -fsS http://127.0.0.1:12345/metrics")
    if result.get("status") != "ok":
        result.setdefault("fix", "docker compose up -d alloy")
        return result
    ports = syslog_listen_ports()
    if ports["udp"] and ports["tcp"]:
        result["why"] = "Alloy up. Syslog listening on UDP/514 and TCP/514."
        return result
    missing = []
    if not ports["udp"]:
        missing.append("UDP/514")
    if not ports["tcp"]:
        missing.append("TCP/514")
    result["status"] = "warn"
    result["why"] = (
        "Alloy is up, but syslog is not listening on "
        + " and ".join(missing)
        + ". Devices cannot ship logs until the port is bound. This is not the alarm path."
    )
    result["test"] = "ss -ulpn | grep ':514'; ss -tlpn | grep ':514'"
    result["fix"] = (
        "docker compose up -d --force-recreate --no-deps alloy"
        "  (host UDP/TCP 514; Loki and Prometheus stay on 127.0.0.1)"
    )
    return result


_DOCTOR_TTL = 8.0
_doctor_lock = threading.Lock()
_doctor_in_flight: threading.Event | None = None
_doctor_cache: dict[str, Any] = {"at": 0.0, "payload": None}


def _doctor_cache_fresh(now: float) -> bool:
    cached = _doctor_cache.get("payload")
    return cached is not None and now - float(_doctor_cache.get("at") or 0) < _DOCTOR_TTL


def doctor_payload(*, force: bool = False) -> dict[str, Any]:
    """Cached System Health probe. Overlapping callers share one in-flight run."""
    now = time.monotonic()
    if not force and _doctor_cache_fresh(now):
        return _doctor_cache["payload"]
    wait_for: threading.Event | None = None
    mine: threading.Event | None = None
    with _doctor_lock:
        now = time.monotonic()
        if not force and _doctor_cache_fresh(now):
            return _doctor_cache["payload"]
        global _doctor_in_flight
        if _doctor_in_flight is not None:
            wait_for = _doctor_in_flight
        else:
            mine = threading.Event()
            _doctor_in_flight = mine
    if wait_for is not None:
        wait_for.wait(timeout=60)
        return _doctor_cache.get("payload") or {"overall": "DEGRADED", "components": {}, "failed": []}
    try:
        payload = _doctor_payload_fresh()
        with _doctor_lock:
            _doctor_cache["at"] = time.monotonic()
            _doctor_cache["payload"] = payload
        return payload
    finally:
        with _doctor_lock:
            done = _doctor_in_flight
            _doctor_in_flight = None
        if done is not None:
            done.set()


def _chat_check() -> dict[str, Any]:
    """Slack / Teams / Discord: configured, and whether the last message to each went through."""
    from app.chat import doctor_check
    from app.db import SessionLocal

    try:
        db = SessionLocal()
        try:
            return doctor_check(db)
        finally:
            db.close()
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}


def _k8s_check() -> dict[str, Any]:
    """Configured clusters, and whether the last sync could read each one."""
    from app.db import SessionLocal
    from app.kubernetes import clusters, token_key

    names = [c["name"] for c in clusters()]
    if not names:
        return {"status": "disabled", "why": "no cluster in kubernetes.clusters"}
    missing = [token_key(n) for n in names if not settings._secret(token_key(n))]
    if missing:
        return {"status": "warn", "why": "no token: " + ", ".join(missing)}
    try:
        db = SessionLocal()
        try:
            down = [i.asset.hostname for i in db.query(Incident).filter(Incident.title.like("%KubeApiUnreachable%"), Incident.status.notin_(["RESOLVED", "CLOSED"])).all() if i.asset]
        finally:
            db.close()
    except Exception as exc:
        return {"status": "error", "why": str(exc)[:200]}
    return {"status": "warn", "why": "API unreachable: " + ", ".join(down)} if down else {"status": "ok", "why": ", ".join(names)}


def _mcp_check() -> dict[str, Any]:
    """Read-only MCP endpoint: off, or on with a token."""
    from app.mcp_server import enabled, token

    if not enabled():
        return {"status": "disabled", "why": "mcp.enabled is false"}
    return {"status": "ok", "why": "/mcp on (bearer MCP_TOKEN)"} if token() else {"status": "warn", "why": "mcp.enabled but MCP_TOKEN is empty"}


def _checks_check() -> dict[str, Any]:
    """Certificates, ForgeSRE backup age, heartbeats: any failing check makes this row red."""
    from app.db import SessionLocal
    from app.health_checks import doctor_check

    try:
        db = SessionLocal()
        try:
            return doctor_check(db)
        finally:
            db.close()
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}


def _power_check() -> dict[str, Any]:
    """UPS / PDU / sensor assets: disabled when none, warn while a forgesre-power alert is open."""
    from app.db import SessionLocal
    from app.power import power_metrics_on

    names = ("UpsOnBattery", "UpsBatteryLow", "UpsLoadHigh", "PowerTemperatureHigh", "PowerSnmpUnreachable")
    try:
        db = SessionLocal()
        try:
            assets = [a.asset_id for a in db.query(Asset).all() if power_metrics_on(a)]
            if not assets:
                return {"status": "disabled", "why": "no UPS, PDU or Environment sensor asset"}
            q = db.query(Incident).filter(Incident.status.notin_(["RESOLVED", "CLOSED"]))
            open_ = [i.title for i in q.all() if any(n in (i.title or "") for n in names)]
        finally:
            db.close()
    except Exception as exc:
        return {"status": "error", "why": str(exc)[:200]}
    if open_:
        return {"status": "warn", "why": f"{len(open_)} open: " + "; ".join(open_[:3]), "test": "./forgesre power"}
    return {"status": "ok", "why": f"{len(assets)} power asset(s), job forgesre-snmp-power"}


def _blackbox_check() -> dict[str, Any]:
    """blackbox_exporter: disabled with no synthetic check, else :9115 must answer."""
    from app.db import SessionLocal
    from app.models import SyntheticCheck

    try:
        db = SessionLocal()
        try:
            count = db.query(SyntheticCheck).filter(SyntheticCheck.enabled.is_(True)).count()
        finally:
            db.close()
    except Exception as exc:
        return {"status": "error", "why": str(exc)[:200]}
    if not count:
        return {"status": "disabled", "why": "no synthetic check (Settings › Checks)"}
    result = _http("http://127.0.0.1:9115/-/healthy", "GET")
    if result.get("status") != "ok":
        result["fix"] = "docker compose up -d blackbox-exporter"
        return result
    return {"status": "ok", "why": f"{count} synthetic check(s)"}


def _oxidized_check() -> dict[str, Any]:
    """Oxidized config changes: off, or the result of the last sync job."""
    from app.db import SessionLocal
    from app.models import Job
    from app.oxidized import JOB_KIND, ready

    why = ready()
    if why:
        return {"status": "disabled", "why": why}
    try:
        db = SessionLocal()
        try:
            last = db.query(Job).filter(Job.kind == JOB_KIND).order_by(Job.id.desc()).first()
        finally:
            db.close()
    except Exception as exc:
        return {"status": "error", "why": str(exc)[:200]}
    if last is None or last.status in {"pending", "running"}:
        return {"status": "starting", "why": "first sync not finished yet"}
    if last.status == "error":
        return {"status": "error", "why": (last.error or "sync failed")[:200], "test": "./forgesre settings test oxidized"}
    result = (last.payload or {}).get("result") or {}
    failed = int(result.get("failed_backups") or 0)
    note = f"{result.get('nodes', 0)} nodes" + (f", {failed} failed backups in Oxidized" if failed else "")
    return {"status": "warn" if failed else "ok", "why": note}


def _backup_verify_check() -> dict[str, Any]:
    """Last ./forgesre backup verify (also run by the nightly timer): disabled until the first one."""
    import json as _json
    import os
    from pathlib import Path

    path = Path(os.environ.get("FORGESRE_BACKUP_DIR") or "/backups") / ".verify-status.json"
    try:
        last = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "disabled", "why": "never run (./forgesre backup verify, or backup schedule on)"}
    if not last.get("ok"):
        return {"status": "error", "why": "; ".join(last.get("problems") or ["failed"])[:200], "test": "./forgesre backup verify"}
    name = Path(str(last.get("archive") or "")).parent.name
    return {"status": "ok", "why": f"{name} readable, {last.get('tables', 0)} tables ({str(last.get('at', ''))[:16]})"}


def _offbox_check() -> dict[str, Any]:
    """Off-box backup copy: disabled when FORGESRE_OFFBOX is empty, else the last push result."""
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    from app import offbox

    cfg = offbox.config()
    if not cfg["kind"]:
        return {"status": "disabled", "why": "off (FORGESRE_OFFBOX=dir or s3)"}
    last = offbox.status(Path(os.environ.get("FORGESRE_BACKUP_DIR") or "/backups"))
    if not last:
        return {"status": "warn", "why": f"{cfg['kind']} configured, no copy yet", "test": "./forgesre backup offbox test"}
    if not last.get("ok"):
        return {"status": "error", "why": str(last.get("detail") or "last copy failed")[:200], "test": "./forgesre backup offbox test"}
    try:
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(str(last["at"]))).total_seconds() / 3600
    except (KeyError, ValueError):
        age_h = 0.0
    limit = float(((settings.yaml or {}).get("checks") or {}).get("backup_max_age_hours") or 26)
    if age_h > limit:
        return {"status": "warn", "why": f"last off-box copy {age_h:.0f} h ago (limit {limit:.0f} h)"}
    return {"status": "ok", "why": f"{last.get('detail', '')[:160]}, {age_h:.0f} h ago"}


def _https_check() -> dict[str, Any]:
    """HTTPS proxy: disabled when off (.env FORGESRE_HTTPS), else the certificate it serves and its expiry."""
    import os

    if str(os.environ.get("FORGESRE_HTTPS") or "").lower() not in {"on", "1", "true", "yes"}:
        return {"status": "disabled", "why": "HTTPS is off (./forgesre https on)"}
    port = int(os.environ.get("FORGESRE_HTTPS_PORT") or 443)
    from app.health_checks import cert_days_left

    try:
        days = int(cert_days_left("127.0.0.1", port))
        detail = f"https://127.0.0.1:{port}"
    except Exception as exc:
        return {"status": "error", "why": f"127.0.0.1:{port}: {str(exc)[:160]}", "fix": "./forgesre https status ; ./forgesre logs proxy"}
    if days < 0:
        return {"status": "error", "why": f"certificate expired ({detail})", "fix": "./forgesre https cert, or put a new forgesre.crt in data/tls"}
    if days < 30:
        return {"status": "warn", "why": f"certificate expires in {days} days ({detail})", "fix": "./forgesre https cert"}
    return {"status": "ok", "why": f"port {port}, certificate valid {days} more days ({detail})"}


def _doctor_payload_fresh() -> dict[str, Any]:
    components = {
        # Machine key "core" = compose service. Display: Core (container).
        # Same GET /api/v1/health as doctor.sh (that CLI row is labeled Core API).
        "core": _core_check(),
        "postgres": _probe_sql(),
        "prometheus": _http(f"{settings.prometheus_url}/-/ready", "GET"),
        "alertmanager": _http(f"{settings.alertmanager_url}/-/ready", "GET"),
        "loki": _loki_check(),
        "alloy": _alloy_check(),
        "grafana": _grafana_check(),
        "snmp": _snmp_check(),
        "llm": _llm_check(),
        "embed": _embed_check(),
        "cloud": _cloud_check(),
        "netbox": _netbox_check(),
        "zabbix": _zabbix_check(),
        "glpi": _glpi_check(),
        "librenms": _librenms_check(),
        "discovery": _discovery_check(),
        "worker": _worker_check(),
        "chat": _chat_check(),
        "checks": _checks_check(),
        "mcp": _mcp_check(),
        "kubernetes": _k8s_check(),
        "power": _power_check(),
        "blackbox": _blackbox_check(),
        "oxidized": _oxidized_check(),
        "https": _https_check(),
        "offbox": _offbox_check(),
        "backup_verify": _backup_verify_check(),
    }
    for name, item in components.items():
        item["label"] = component_label(name)
    failed = [name for name, item in components.items() if not doctor_soft_status(item["status"])]
    payload = {
        "overall": "HEALTHY" if not failed else "DEGRADED",
        "components": components,
        "failed": failed,
    }
    _maybe_journal_doctor(components)
    return payload


def _maybe_journal_doctor(components: dict[str, Any]) -> None:
    """Skip under FORGESRE_DEV (pytest). Live Core journals Prom/AM hops only."""
    if os.environ.get("FORGESRE_DEV", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    try:
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            journal_doctor_alarm_path(db, components)
        finally:
            db.close()
    except Exception:
        log.debug("doctor journal skipped", exc_info=True)


def grafana_health_url() -> str:
    """Grafana listens on GRAFANA_PORT (compose GF_SERVER_HTTP_PORT); else the GRAFANA_PUBLIC_URL port; else 3000."""
    from urllib.parse import urlparse

    port = str(os.environ.get("GRAFANA_PORT") or "").strip()
    if not port.isdigit():
        try:
            port = str(urlparse(settings.grafana_public_url or "").port or 3000)
        except ValueError:
            port = "3000"
    return f"http://127.0.0.1:{port}/api/health"


def _grafana_check() -> dict[str, str]:
    """Grafana is graphs only. Down is yellow warn — not a Prometheus / alarm-path FAIL."""
    if not settings.grafana_enabled:
        return _ok("disabled")
    url = grafana_health_url()
    result = _http(url, "GET")
    if result.get("status") == "ok":
        return result
    why = str(result.get("why") or "unreachable")
    result["status"] = "warn"
    result["why"] = (
        "Grafana is graphs only (not Prometheus → Alertmanager → Core). "
        f"Yellow, not a Prometheus outage. {why}"
    )
    result.setdefault("test", f"curl -fsS {url}")
    result.setdefault(
        "fix",
        "docker compose up -d grafana  (optional; incidents still fire without Grafana)",
    )
    return result


def _core_check() -> dict[str, str]:
    """Same /api/v1/health probe as doctor.sh Core API, labeled Core (container) in the payload."""
    port = str(os.environ.get("FORGESRE_HTTP_PORT") or "8080").strip() or "8080"
    url = f"http://127.0.0.1:{port}/api/v1/health"
    result = _http(url, "GET")
    result.setdefault("test", f"curl -fsS {url}")
    if result.get("status") != "ok":
        result.setdefault("fix", "docker compose logs core && ./forgesre secrets-check")
    return result


def _worker_check() -> dict[str, Any]:
    """Background loops (jobs, ForgeAI, escalation, discovery): age of each beat and the process running them."""
    from app.loop_beats import status

    try:
        row = status()
    except Exception as exc:
        return {"status": "warn", "why": f"Could not read loop heartbeats: {exc}", "fix": "./forgesre logs core"}
    row["test"] = "loop_beats table: last beat of each background loop"
    return row


def _discovery_check() -> dict[str, str]:
    """Discovery reads LibreNMS: is it reachable with a token, last journal row, loop heartbeat."""
    if not settings.discovery_enabled:
        return _ok("disabled")
    from app.librenms_discovery import not_ready

    blocked = not_ready()
    if blocked:
        return {"status": "warn", "why": blocked, "test": "LibreNMS URL and API token",
                "fix": "./forgesre settings set-secret LIBRENMS_API_TOKEN (or turn LibreNMS on), then ./forgesre discovery read"}
    from app.db import SessionLocal
    from app.inventory import discovery_loop_age_seconds
    from app.journal import list_entries
    from app.loop_beats import age_seconds

    test = "journal module=discovery; discovery loop heartbeat"
    # the loop may run in another process (web workers, separate worker): the beat in the database counts
    age = age_seconds("discovery")
    if age is None:
        age = discovery_loop_age_seconds()
    last = None
    try:
        db = SessionLocal()
        try:
            rows = list_entries(db, module="discovery", limit=1)
            last = rows[0] if rows else None
        finally:
            db.close()
    except Exception as exc:
        return {
            "status": "warn",
            "why": f"Could not read discovery journal: {exc}",
            "test": test,
            "fix": "./forgesre journal discovery",
        }
    if last is not None and str(last.status or "") == "error":
        return {
            "status": "warn",
            "why": last.summary or "Last discovery journal row is error",
            "test": test,
            "fix": "./forgesre journal discovery",
        }
    if age is None:
        return {
            "status": "warn",
            "why": "Discovery loop has not reported yet (first LibreNMS read waits ~30s, then every 6 hours).",
            "test": test,
            "fix": "Watch ./forgesre journal discovery after Core is up.",
        }
    if age > (6 * 3600 + 120):
        return {
            "status": "warn",
            "why": f"Discovery loop last heartbeat {int(age)}s ago (expected at least every 6 hours).",
            "test": test,
            "fix": "docker compose logs core",
        }
    why = "Discovery loop is alive."
    if last is not None:
        why = f"{why} Last journal: {last.action} {last.status} — {last.summary}"
    else:
        why = f"{why} No LibreNMS read in the journal yet."
    return {"status": "ok", "why": why, "test": test}


def _snmp_check() -> dict[str, str]:
    """snmp-exporter: running when :9116 answers *and* there are targets.

    Zero real network SNMP targets → paused (not DOWN), even if :9116 is up.
    Do not start the exporter or call the tile broken just to un-pause it.
    """
    if not settings.snmp_enabled:
        return _ok("disabled")
    url = f"{settings.snmp_exporter_url}/metrics"
    targets = snmp_target_count()
    if targets <= 0:
        return {
            "status": "paused",
            "why": (
                "paused (no SNMP targets) — no Network device + IP yet; "
                "snmp-exporter is idle (not down)."
            ),
            "test": f"curl -fsS {url}",
            "fix": (
                "Add a Network device with an IP when you have one. "
                "Do not add devices just to un-pause this tile."
            ),
        }
    result = _http(url, "GET")
    if result.get("status") == "ok":
        return result
    result["fix"] = "docker compose up -d snmp-exporter"
    result["test"] = f"curl -fsS {url}"
    return result


def _netbox_check() -> dict[str, str]:
    """Align with Discovery traffic light: ok / warn / starting — never paused when UI is up."""
    if not settings.netbox_enabled:
        return _ok("disabled")
    from app.netbox import looks_like_v2_token, netbox_status, token_presence

    url = (settings.netbox_url or "http://127.0.0.1:8001").rstrip("/")
    token = settings.netbox_token
    result = netbox_status(url, token)
    presence = f"API token: {token_presence(token)}"
    test = f"curl -fsS {url}/login/"
    if result.get("ok"):
        return {"status": "ok", "why": presence}
    why = str(result.get("why") or "NetBox unreachable").strip()
    if presence not in why:
        if why and not why.endswith("."):
            why += "."
        why = f"{why} {presence}."
    if result.get("starting") and not result.get("ui_up"):
        return {
            "status": "starting",
            "why": why,
            "test": test,
            "fix": "Wait for first-boot migrations (can take several minutes). Then: docker compose logs netbox. External override: inventory.netbox.url + NETBOX_API_TOKEN.",
        }
    if result.get("ui_up") or result.get("degraded") or "403" in why:
        if token_presence(token) != "yes":
            fix = "Set NETBOX_API_TOKEN in secrets/secrets.env, then recreate core."
        elif looks_like_v2_token(token):
            fix = (
                "Confirm NETBOX_API_TOKEN is the full v2 secret shown once in the NetBox UI "
                "(nbt_…, not the 12-character key), then recreate core. Launch will not replace a v2 token with v1."
            )
        else:
            fix = (
                "Recreate netbox+core after changing secrets.env. Core is a token on the NetBox superuser "
                "(NETBOX_SUPERUSER_NAME), not a separate UI login."
            )
        return {
            "status": "warn",
            "why": why,
            "test": test,
            "fix": fix,
        }
    return {
        "status": "error",
        "why": why,
        "test": test,
        "fix": "docker compose logs netbox && docker compose up -d netbox netbox-redis netbox-db-init",
    }


def _zabbix_check() -> dict[str, str]:
    """Cached Zabbix API probe. Down is yellow warn (optional source), never red."""
    from app.zabbix import api_endpoint
    from app.zabbix_sync import current_status, note_health

    if not settings.zabbix_enabled:
        return {
            "status": "disabled",
            "why": "Not configured (ZABBIX_URL + ZABBIX_API_TOKEN in secrets/secrets.env).",
            "fix": "See operator handbook §18 Zabbix. Optional — Prometheus path does not need it.",
        }
    try:
        status = current_status()
    except Exception as exc:
        status = {"configured": True, "ok": False, "label": "API error", "why": str(exc)[:300]}
    test = f"POST {api_endpoint(settings.zabbix_url)} apiinfo.version (read-only)"
    try:
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            note_health(db, status)
        finally:
            db.close()
    except Exception:
        log.debug("zabbix health journal skipped", exc_info=True)
    if status.get("ok") and status.get("light") == "green":
        return {"status": "ok", "why": str(status.get("why") or "Zabbix API answered."), "test": test}
    return {
        "status": "warn",
        "why": f"{status.get('label') or 'Unreachable'}: {status.get('why') or ''}".strip(" :"),
        "test": test,
        "fix": (
            "Check ZABBIX_URL / ZABBIX_API_TOKEN and that user forgesre-ro has Read on host groups. "
            "Optional source — incidents, mail and the UI keep working."
        ),
    }


def _glpi_check() -> dict[str, str]:
    """Cached GLPI API probe. Down is yellow warn (optional source), never red."""
    from app.glpi import api_root
    from app.glpi_sync import current_status, note_health

    if not settings.glpi_enabled:
        return {
            "status": "disabled",
            "why": "Not configured (GLPI_URL + GLPI_APP_TOKEN + GLPI_USER_TOKEN in secrets/secrets.env).",
            "fix": "See docs/glpi.md. Optional — Prometheus path does not need it.",
        }
    try:
        status = current_status()
    except Exception as exc:
        status = {"configured": True, "ok": False, "label": "API error", "why": str(exc)[:300]}
    test = f"GET {api_root(settings.glpi_url)}/initSession (read-only)"
    try:
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            note_health(db, status)
        finally:
            db.close()
    except Exception:
        log.debug("glpi health journal skipped", exc_info=True)
    if status.get("ok") and status.get("light") == "green":
        return {"status": "ok", "why": str(status.get("why") or "GLPI API answered."), "test": test}
    return {
        "status": "warn",
        "why": f"{status.get('label') or 'Unreachable'}: {status.get('why') or ''}".strip(" :"),
        "test": test,
        "fix": (
            "Check GLPI_URL / GLPI_APP_TOKEN / GLPI_USER_TOKEN and that the API user can read "
            "Computer and NetworkEquipment. Optional source — incidents, mail and the UI keep working."
        ),
    }


def _librenms_check() -> dict[str, str]:
    """Cached LibreNMS API probe. Down is yellow warn (discovery only), never red."""
    from app.librenms import api_root, status as librenms_status

    if not settings.librenms_enabled:
        return _ok("disabled")
    try:
        result = librenms_status()
    except Exception as exc:
        result = {"configured": False, "ok": False, "label": "API error", "why": str(exc)[:300]}
    url = (settings.librenms_url or "http://127.0.0.1:8000").rstrip("/")
    test = f"GET {api_root(url)}/devices (read-only after Identify disables polling)"
    if result.get("ok") and result.get("light") == "green":
        return {"status": "ok", "why": str(result.get("why") or "LibreNMS API answered."), "test": test}
    label = str(result.get("label") or "")
    if label == "Disabled":
        return _ok("disabled")
    if label == "Starting":
        return {
            "status": "starting",
            "why": str(result.get("why") or "LibreNMS UI is not answering yet."),
            "test": f"curl -fsS {url}/login",
            "fix": "Wait for first-boot MariaDB migrations (can take several minutes). Then: docker compose logs librenms.",
        }
    return {
        "status": "warn",
        "why": f"{label or 'Unreachable'}: {result.get('why') or ''}".strip(" :"),
        "test": test,
        "fix": (
            "Set LIBRENMS_API_TOKEN in secrets/secrets.env (LibreNMS → API Access), then recreate core. "
            "Optional — Prometheus still scrapes snmp_exporter without LibreNMS."
        ),
    }


def _candidate(item: DiscoveryCandidate) -> dict[str, Any]:
    return {
        "id": item.id,
        "ip": item.ip,
        "hostname": item.hostname or "",
        "proposed_role": item.proposed_role,
        "open_ports": item.open_ports,
        "snmp_ok": bool(getattr(item, "snmp_ok", False)),
        "node_exporter": bool(getattr(item, "node_exporter", False)),
        "windows_exporter": bool(getattr(item, "windows_exporter", False)),
        "status": item.status,
        "source": item.source,
        "asset_id": item.asset_id,
        "seen_at": item.seen_at.isoformat() if item.seen_at else None,
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


DOCTOR_HTTP_TIMEOUT = 5.0


async def _http_async(url: str, method: str) -> dict[str, str]:
    """Probe a hop without blocking the FastAPI loop. Network blips become error rows."""
    test = f"curl -fsS {url}"
    try:
        async with httpx.AsyncClient(timeout=DOCTOR_HTTP_TIMEOUT) as client:
            response = await client.request(method, url)
        if response.status_code < 400:
            return _ok("ok")
        return {
            "status": "error",
            "why": f"{url} returned {response.status_code}",
            "test": test,
            "fix": "Check the container logs and config/forgesre.yml",
        }
    except Exception as exc:
        return {
            "status": "error",
            "why": f"{url}: {exc}",
            "test": test,
            "fix": "Confirm the service is running: ./doctor.sh",
        }


def _llm_check() -> dict[str, str]:
    """ForgeAI's model server. When it does not answer, say how to install or start it, not just "running?"."""
    if not settings.llm_url:
        return _ok("disabled")
    row = _http(settings.llm_url.rstrip("/") + "/models", "GET")
    if row.get("status") == "error" and settings.llm_mode == "bundled":
        row["fix"] = (
            "llama.cpp is not running. Never installed (data/models/ empty, COMPOSE_PROFILES without ai): "
            "./forgesre fetch-llm. Installed: docker compose --profile ai up -d llm, then docker compose logs --tail 40 llm"
        )
    elif row.get("status") == "error":
        row["fix"] = "The external model server does not answer: check ai.llm.url in Settings › RCA / AI"
    return row


def _cloud_check() -> dict[str, str]:
    """The optional paid cloud model: configuration only; doctor never sends anything out."""
    from app.cloud_llm import not_ready

    if not settings.cloud_enabled:
        return _ok("disabled")
    why = not_ready()
    if why:
        return {"status": "warn", "why": why, "fix": "Settings › RCA / AI, or ./forgesre settings set-secret CLOUD_LLM_API_KEY"}
    return {"status": "ok", "why": f"{settings.cloud_provider} {settings.cloud_model} configured; Settings › Test sends one tiny request"}


def _embed_check() -> dict[str, str]:
    """The embedding server for document search. Down only matters once documents are uploaded."""
    if not settings.embed_url:
        return _ok("disabled")
    row = _http(settings.embed_url.rstrip("/").removesuffix("/v1") + "/health", "GET")
    if row.get("status") == "error":
        from app.db import SessionLocal
        from app.models import DocFile

        db = SessionLocal()
        try:
            has_docs = db.query(DocFile.id).first() is not None
        finally:
            db.close()
        fix = "./forgesre fetch-llm (downloads the ~150 MB embedding model), then docker compose --profile ai up -d embed"
        if not has_docs:
            return {"status": "paused", "why": "No documents uploaded yet; the embedding server is not needed.", "fix": fix}
        row["fix"] = fix
    return row


def _http(url: str, method: str) -> dict[str, str]:
    """Sync wrapper for System Health / pytest. A live loop uses a worker thread."""
    from app.metrics import run_sync

    return run_sync(_http_async(url, method))


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
        "zabbix_hostid": item.zabbix_hostid or "",
        "zabbix_agent": item.zabbix_agent or "",
        "glpi_id": getattr(item, "glpi_id", "") or "",
        "scrape_address": item.scrape_address,
        "snmp_port": snmp_port_for(item) if is_snmp_asset(item) else 0,
        "alarms": getattr(item, "alarms", None) or {},
        "extras": public_extras(getattr(item, "extras", None)),
        "playrule_ids": asset_playrule_ids(item),
        "playrule_off": muted_playrule_ids(item),
        "parent_asset_id": item.parent.asset_id if getattr(item, "parent", None) is not None else "",
        "support_status": {key: value for key, value in support_status(item).items() if key in {"state", "label", "detail"}},
        "snmp": is_snmp_asset(item),
        "ping": reach["ping"],
        "ping_detail": reach["ping_detail"],
        "exporter": reach["exporter"],
        "exporter_detail": reach["exporter_detail"],
        "exporter_label": reach["exporter_label"],
        "snmp_label": reach["snmp_label"] if is_snmp_asset(item) else "",
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
        "source": incident_source(item),
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


class SettingBody(BaseModel):
    value: Any


@router.get("/settings")
def settings_list(group: str = "", user: User = Depends(require("admin"))) -> dict[str, Any]:
    """Every setting an Admin can see, grouped. Secrets show only whether they are set."""
    from app.system_settings import GROUPS, list_settings

    return {"groups": GROUPS, "settings": list_settings(group or None)}


@router.put("/settings/{key:path}")
def settings_update(
    key: str, body: SettingBody, db: Session = Depends(get_db), user: User = Depends(require("admin"))
) -> dict[str, Any]:
    """Change one YAML setting; 400 with the reason when the value or the key is not accepted."""
    from app.system_settings import set_setting

    try:
        return set_setting(db, key, body.value, actor=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not write config: {exc}") from exc


@router.post("/settings/test/{component}")
def settings_test(component: str, user: User = Depends(require("run_checks"))) -> dict[str, Any]:
    """Check one service or integration now (each check has its own timeout)."""
    from app.system_settings import test_connection

    try:
        return test_connection(component)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/integrations")
def integrations_api(request: Request, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict[str, Any]:
    """Settings › Integrations: status, web UI links, last sync and settings per integration."""
    from app.integrations_view import integrations

    return integrations(db, request.headers.get("host", ""))


@router.post("/integrations/{target}/sync")
def integrations_sync(target: str, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict[str, Any]:
    """Sync one integration now in the background (``integration_sync`` job). The result lands in the journal."""
    from app.integrations_view import SYNC_KINDS
    from app.jobs import enqueue

    if target not in SYNC_KINDS:
        raise HTTPException(status_code=404, detail="nothing to sync for this integration")
    job = enqueue(db, "integration_sync", target, object_type="integration", payload={"actor": user.email})
    audit(db, "integration.sync", actor=user.email, object_type="integration", object_id=target)
    db.commit()
    return {"queued": True, "job": getattr(job, "id", None)}


@router.get("/tv")
def tv_api(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict[str, Any]:
    """Wall screen: the NOC overview, the incident clock, 14 days per day, and the day cards, 24-hour
    curve and tiles of the weather-style layout (app.tv_view)."""
    from app.noc import incident_clock, noc_overview
    from app.reports_view import incidents_per_day

    from app.tv_view import tv_overview

    data = noc_overview(db)
    data["clock"] = incident_clock(db)
    data["daily"] = incidents_per_day(db, 14)
    data.update(tv_overview(db))
    return data


@router.get("/tv/day")
def tv_day_api(date: str, db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict[str, Any]:
    """Day clock for the wall screen: incidents per hour of one day (last 31 days), AM and PM."""
    from datetime import date as Date
    from zoneinfo import ZoneInfo

    from app.noc import DAY_LOOKBACK, incident_day

    try:
        day = Date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    try:
        today = utcnow().astimezone(ZoneInfo(settings.timezone or "UTC")).date()
    except Exception:
        today = utcnow().date()
    if day > today or (today - day).days > DAY_LOOKBACK:
        raise HTTPException(status_code=400, detail=f"date must be within the last {DAY_LOOKBACK} days")
    return incident_day(db, day)


@router.get("/noc")
def noc_api(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict[str, Any]:
    """NOC overview for the new UI: counts, incidents by tab, health by group, on-call, AI mode."""
    from app.noc import noc_overview

    return noc_overview(db)


class ChecklistBody(BaseModel):
    step: int
    done: bool


@router.get("/incidents-page")
def incidents_page_api(
    tab: str = "active",
    severity: str = "",
    q: str = "",
    limit: int = 50,
    offset: int = 0,
    date_from: str = "",
    date_to: str = "",
    team: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require("read_incidents")),
) -> dict[str, Any]:
    """Incident list for the new UI: one tab, paged, with counts for every tab. Days are YYYY-MM-DD, local."""
    from app.incident_view import incidents_page

    try:
        return incidents_page(db, tab=tab, severity=severity, q=q, limit=limit, offset=offset, date_from=date_from, date_to=date_to, team=team)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="dates are YYYY-MM-DD") from exc


class IncidentMailBody(BaseModel):
    to: str | list[str]
    subject: str = ""
    body: str = ""


def _incident_or_404(db: Session, number: str) -> Incident:
    item = db.query(Incident).options(joinedload(Incident.asset)).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return item


@router.get("/incidents/{number}/mail")
def incident_mail_draft(number: str, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Subject, generated report and address suggestions for Send report (Analyst+)."""
    del user
    from app.incident_mail import mail_draft

    return mail_draft(db, _incident_or_404(db, number))


@router.post("/incidents/{number}/mail")
def incident_mail_send(number: str, body: IncidentMailBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Queue the incident report to up to 10 addresses; the incident_mail job sends them."""
    from app.incident_mail import queue_mail

    try:
        job = queue_mail(db, _incident_or_404(db, number), body.to, body.subject, body.body, user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": job.id if job else None}


@router.get("/incidents/{number}/mail/{job_id}")
def incident_mail_job(number: str, job_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))) -> dict:
    """Status of a Send report job: per address sent, failed (with the SMTP error) or generated (SMTP off)."""
    del user
    from app.models import Job

    row = db.get(Job, job_id)
    if row is None or row.kind != "incident_mail" or not row.object_id.startswith(f"{number}#"):
        raise HTTPException(status_code=404, detail="job not found")
    return {"id": row.id, "kind": row.kind, "status": row.status, "error": row.error or "", "result": (row.payload or {}).get("result")}


@router.get("/incidents-page.csv")
def incidents_page_csv(
    tab: str = "all", severity: str = "", q: str = "", date_from: str = "", date_to: str = "", team: str = "",
    db: Session = Depends(get_db), user: User = Depends(require("read_incidents")),
) -> Response:
    """The incident list with the same filters as CSV (UTF-8 with BOM)."""
    from app.incident_view import incidents_csv

    try:
        body = incidents_csv(db, tab=tab, severity=severity, q=q, date_from=date_from, date_to=date_to, team=team)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="dates are YYYY-MM-DD") from exc
    return Response(body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="forgesre-incidents.csv"'})


@router.get("/outbox")
def outbox_api(
    status: str = "", q: str = "", limit: int = 50, offset: int = 0,
    db: Session = Depends(get_db), user: User = Depends(require("read_play")),
) -> dict[str, Any]:
    """Every mail and chat message ForgeSRE produced (alarms, reports), newest first, with its delivery status."""
    from app.models import Notification

    query = db.query(Notification).outerjoin(Incident, Notification.incident_id == Incident.id)
    if status == "failed":
        query = query.filter(Notification.status.notin_(("sent", "generated", "queued")))
    elif status:
        query = query.filter(Notification.status == status)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(Notification.target.ilike(like), Notification.subject.ilike(like), Incident.number.ilike(like)))
    total = query.count()
    rows = query.order_by(Notification.id.desc()).offset(max(0, offset)).limit(max(1, min(limit, 200))).all()
    numbers = {i.id: i.number for i in db.query(Incident.id, Incident.number).filter(Incident.id.in_([r.incident_id for r in rows if r.incident_id])).all()}
    return {
        "total": total,
        "rows": [
            {"id": r.id, "at": r.created_at.isoformat() if r.created_at else None, "channel": r.channel, "target": r.target,
             "subject": r.subject, "status": r.status, "error": r.error or "", "step": r.step_key or "", "incident": numbers.get(r.incident_id, "")}
            for r in rows
        ],
    }


@router.get("/audit")
def audit_api(
    q: str = "", action: str = "", limit: int = 50, offset: int = 0,
    db: Session = Depends(get_db), user: User = Depends(require("admin")),
) -> dict[str, Any]:
    """Who changed what: rules, settings, users, assets, incidents. Admin only."""
    from app.models import AuditLog

    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action.ilike(f"{action}%"))
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(AuditLog.actor.ilike(like), AuditLog.object_id.ilike(like), AuditLog.action.ilike(like)))
    total = query.count()
    rows = query.order_by(AuditLog.id.desc()).offset(max(0, offset)).limit(max(1, min(limit, 200))).all()
    return {
        "total": total,
        "rows": [{"id": r.id, "at": r.at.isoformat() if r.at else None, "actor": r.actor, "action": r.action,
                  "object": f"{r.object_type}:{r.object_id}" if r.object_type else r.object_id, "ip": r.ip or "", "data": r.data or {}} for r in rows],
    }


@router.get("/incidents/{number}/postmortem")
def incident_postmortem_api(number: str, download: int = 0, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))):
    """Postmortem draft (Markdown) from the incident's facts. ``download=1`` returns the .md file."""
    from app.postmortem import postmortem_markdown

    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    text = postmortem_markdown(db, item, user)
    name = f"postmortem-{item.number}.md"
    if download:
        return Response(text, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return {"markdown": text, "filename": name}


@router.get("/incidents/{number}/syslog")
def incident_syslog_api(number: str, db: Session = Depends(get_db), user: User = Depends(require("read_evidence"))) -> dict[str, Any]:
    """Up to 20 syslog lines from Loki for the incident's asset, 30 minutes either side of the start."""
    from app.dc_noc import incident_syslog_lines

    incident = db.query(Incident).options(joinedload(Incident.asset)).filter_by(number=number).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    lines = incident_syslog_lines(incident)
    return {"lines": lines or [], "note": "" if lines else "No syslog lines from this asset around the start (or the device does not send syslog to port 514)."}


@router.get("/incidents/{number}/view")
def incident_view_api(number: str, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict[str, Any]:
    """Incident detail page of the new UI in one call."""
    from app.incident_view import incident_view

    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return incident_view(db, item, user)


@router.post("/incidents/{number}/checklist")
def incident_checklist_api(
    number: str, body: ChecklistBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))
) -> dict[str, Any]:
    """Tick or untick one playbook step on this incident."""
    from app.incident_view import set_checklist_step

    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    try:
        return {"done": set_checklist_step(db, item, body.step, body.done, actor=user.email)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/inventory")
def inventory_api(
    q: str = "",
    site: str = "",
    rack: str = "",
    type: str = "",
    environment: str = "",
    customer: str = "",
    view: str = "",
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(require("read_assets")),
) -> dict[str, Any]:
    """Inventory page of the new UI: library counts plus one filtered page of assets."""
    from app.inventory_view import inventory_page

    data = inventory_page(
        db, q=q, site=site, rack=rack, type_=type, environment=environment, customer=customer, view=view, limit=limit, offset=offset
    )
    data["can_edit"] = can(user, "write_assets")
    return data


@router.get("/inventory/tree")
def inventory_tree_api(db: Session = Depends(get_db), user: User = Depends(require("read_assets"))) -> dict:
    """Inventory › Tree: site → row → rack → asset, children under their parent, worst state per node."""
    from app.inventory_view import inventory_tree

    return inventory_tree(db)


@router.get("/inventory.csv")
def inventory_csv_api(
    q: str = "", site: str = "", rack: str = "", type: str = "", environment: str = "", customer: str = "", view: str = "",
    db: Session = Depends(get_db), user: User = Depends(require("read_assets")),
) -> Response:
    """The filtered inventory as CSV, in the columns Import CSV reads."""
    del user
    from app.inventory_view import inventory_csv

    body = inventory_csv(db, q=q, site=site, rack=rack, type_=type, environment=environment, customer=customer, view=view)
    return Response(body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="forgesre-inventory.csv"'})


class BulkAssets(BaseModel):
    ids: list[str] = Field(default_factory=list)


@router.post("/inventory/bulk-delete")
def assets_bulk_delete(body: BulkAssets, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict[str, Any]:
    """Remove several assets (Engineer+). Database only; an asset that may not be removed is listed with the reason."""
    ids = list(dict.fromkeys(i for i in body.ids if i))[:200]
    if not ids:
        raise HTTPException(status_code=400, detail="pick at least one asset")
    removed, refused = [], []
    for asset_id in ids:
        item = db.query(Asset).filter_by(asset_id=asset_id).first()
        if item is None:
            refused.append({"asset_id": asset_id, "reason": "not found"})
            continue
        try:
            delete_asset(db, item, actor=user.email)
            removed.append(asset_id)
        except ValueError as exc:
            db.rollback()
            refused.append({"asset_id": asset_id, "reason": str(exc)})
    return {"removed": removed, "refused": refused}


@router.get("/inventory-form")
def inventory_form_api(db: Session = Depends(get_db), user: User = Depends(require("read_assets"))) -> dict[str, Any]:
    """Choices for the asset form in the new UI."""
    from app.inventory_view import asset_form_options

    return asset_form_options(db, user)


class ConflictDecision(BaseModel):
    action: str  # ours | theirs


@router.get("/inventory/sources")
def inventory_sources_api(db: Session = Depends(get_db), user: User = Depends(require("read_assets"))) -> dict[str, Any]:
    """Each feeder (GLPI, Zabbix, NetBox, LibreNMS): last sync, linked assets, open conflicts; and the open conflicts."""
    from app.asset_sources import conflict_rows, sources_view

    return {"sources": sources_view(db), "conflicts": conflict_rows(db), "can_edit": can(user, "write_assets"),
            "new_devices": settings.inventory_new_from_sources}


@router.post("/inventory/conflicts/{conflict_id}")
def inventory_conflict_decide(conflict_id: int, body: ConflictDecision, db: Session = Depends(get_db),
                              user: User = Depends(require("write_assets"))) -> dict[str, Any]:
    """Keep the inventory value (ours) or take the source's (theirs). Audited."""
    from app.asset_sources import resolve
    from app.models import AssetConflict

    row = db.get(AssetConflict, conflict_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conflict not found")
    try:
        resolve(db, row, body.action, user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": row.id, "status": row.status}


@router.get("/inventory/{asset_id}")
def inventory_card_api(asset_id: str, db: Session = Depends(get_db), user: User = Depends(require("read_assets"))) -> dict[str, Any]:
    """Asset card of the new UI."""
    from app.inventory_view import asset_card

    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return asset_card(db, item, user)


@router.get("/automation/export")
def automation_export(db: Session = Depends(get_db), user: User = Depends(require("read_play"))) -> Response:
    """Routing rules, playbooks, escalation policies, noise filters, on-call, SLOs and ForgeAI skills as one JSON file,
    for review and safekeeping. Read only; secrets are not part of any of these."""
    import json

    from sqlalchemy import inspect as sa_inspect

    from app.forgeai import seed_skills
    from app.models import ForgeAiSkill, NoiseFilter, OncallSlot, Slo

    seed_skills(db)  # built-in skills exist even before anyone opened the skills page

    def rows(model) -> list[dict[str, Any]]:
        cols = [c.key for c in sa_inspect(model).mapper.column_attrs]
        return [{c: getattr(r, c) for c in cols} for r in db.query(model).order_by(model.id).all()]

    data = {
        "forgesre": "2.0.0", "exported_at": utcnow().isoformat(), "exported_by": user.email,
        "routing_rules": rows(Playrule), "playbooks": rows(Playbook), "escalation_policies": rows(EscalationPolicy),
        "noise_filters": rows(NoiseFilter), "oncall": rows(OncallSlot), "slos": rows(Slo), "forgeai_skills": rows(ForgeAiSkill),
    }
    audit(db, "automation.export", actor=user.email, object_type="automation", object_id="all", commit=True)
    body = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="forgesre-automation-{utcnow():%Y%m%d}.json"'})


class SimulateBody(BaseModel):
    alertname: str
    asset: str = ""
    value: str = ""
    at: str = ""


class RuleEditBody(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    severity: str = "warning"
    alertname: str = ""
    metric: str = ""
    operator: str = ">"
    value: float | str | None = None
    playbook_id: int | None = None
    escalation_policy_id: int | None = None


class PlaybookEditBody(BaseModel):
    name: str
    slug: str = ""
    description: str = ""
    steps: list[str] = Field(default_factory=list)


def _rule_or_404(db: Session, rule_id: int) -> Playrule:
    row = db.get(Playrule, rule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="playrule not found")
    return row


def _book_or_404(db: Session, book_id: int) -> Playbook:
    row = db.get(Playbook, book_id)
    if row is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    return row


@router.post("/automation/rules")
def automation_rule_create(body: RuleEditBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: create a playrule (alert → playbook + escalation)."""
    from app.automation_edit import save_playrule

    try:
        return _playrule(save_playrule(db, body.model_dump(), user.email))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/automation/rules/{rule_id}")
def automation_rule_update(rule_id: int, body: RuleEditBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: update a playrule."""
    from app.automation_edit import save_playrule

    try:
        return _playrule(save_playrule(db, body.model_dump(), user.email, _rule_or_404(db, rule_id)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/automation/rules/{rule_id}")
def automation_rule_delete(rule_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: remove a playrule; incidents keep their playbook."""
    from app.automation_edit import delete_playrule

    delete_playrule(db, _rule_or_404(db, rule_id), user.email)
    return {"removed": rule_id}


@router.post("/automation/playbooks")
def automation_playbook_create(body: PlaybookEditBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: create a playbook from a list of step titles."""
    from app.automation_edit import save_playbook

    try:
        return _playbook(save_playbook(db, body.model_dump(), user.email))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/automation/playbooks/{book_id}")
def automation_playbook_update(book_id: int, body: PlaybookEditBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: update a playbook's name, description and steps (the slug stays)."""
    from app.automation_edit import save_playbook

    try:
        return _playbook(save_playbook(db, body.model_dump(), user.email, _book_or_404(db, book_id)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/automation/playbooks/{book_id}")
def automation_playbook_delete(book_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: remove a playbook; rules and incidents that used it lose the checklist link."""
    from app.automation_edit import delete_playbook

    delete_playbook(db, _book_or_404(db, book_id), user.email)
    return {"removed": book_id}


class PolicyEditBody(BaseModel):
    name: str
    slug: str = ""
    levels: list[dict[str, Any]] = Field(default_factory=list)


class OncallBody(BaseModel):
    weekday: int
    start: str
    end: str
    name: str = ""
    surname: str = ""
    email: str = ""
    phone: str = ""
    phone2: str = ""


def _policy_or_404(db: Session, policy_id: int) -> EscalationPolicy:
    row = db.get(EscalationPolicy, policy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="escalation policy not found")
    return row


def _policy_out(row: EscalationPolicy) -> dict[str, Any]:
    return {"id": row.id, "slug": row.slug, "name": row.name, "steps": row.steps or []}


@router.post("/automation/policies")
def automation_policy_create(body: PolicyEditBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: create an escalation policy from L1–L4 levels."""
    from app.automation_edit import save_policy

    try:
        return _policy_out(save_policy(db, body.model_dump(), user.email))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/automation/policies/{policy_id}")
def automation_policy_update(policy_id: int, body: PolicyEditBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: change a policy's name and L1–L4 levels (the slug stays)."""
    from app.automation_edit import save_policy

    try:
        return _policy_out(save_policy(db, body.model_dump(), user.email, _policy_or_404(db, policy_id)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/automation/policies/{policy_id}")
def automation_policy_delete(policy_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: remove a policy; its rules move to default-warning."""
    from app.automation_edit import delete_policy

    try:
        return {"removed": policy_id, "rules_moved": delete_policy(db, _policy_or_404(db, policy_id), user.email)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/oncall-week")
def oncall_week_api(db: Session = Depends(get_db), user: User = Depends(require("read_play"))) -> dict[str, Any]:
    """New UI: the weekly on-call roster and who is on call now."""
    from app.oncall_view import oncall_week

    return oncall_week(db, user)


def _slot_or_404(db: Session, slot_id: int):
    from app.models import OncallSlot

    row = db.get(OncallSlot, slot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="on-call slot not found")
    return row


@router.post("/oncall-slots")
def oncall_slot_create(body: OncallBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: add one weekday window to the roster."""
    from app.dc_noc import add_oncall_slot, oncall_slot_view

    try:
        row = add_oncall_slot(db, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "oncall.add", actor=user.email, object_type="oncall", object_id=row.email, data={"weekday": row.weekday, "name": row.name})
    db.commit()
    return oncall_slot_view(row)


@router.put("/oncall-slots/{slot_id}")
def oncall_slot_update(slot_id: int, body: OncallBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: rewrite one roster window; on/off stays on the toggle."""
    from app.dc_noc import oncall_slot_view, update_oncall_slot

    row = _slot_or_404(db, slot_id)
    try:
        update_oncall_slot(db, row, **body.model_dump())
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "oncall.update", actor=user.email, object_type="oncall", object_id=str(slot_id), data={"name": row.name})
    db.commit()
    return oncall_slot_view(row)


@router.post("/oncall-slots/{slot_id}/toggle")
def oncall_slot_toggle(slot_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: switch a roster window on or off."""
    from app.dc_noc import oncall_slot_view, toggle_oncall_slot

    row = toggle_oncall_slot(_slot_or_404(db, slot_id))
    audit(db, "oncall.toggle", actor=user.email, object_type="oncall", object_id=str(slot_id), data={"enabled": bool(row.enabled)})
    db.commit()
    return oncall_slot_view(row)


@router.delete("/oncall-slots/{slot_id}")
def oncall_slot_delete(slot_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: remove a roster window."""
    db.delete(_slot_or_404(db, slot_id))
    audit(db, "oncall.remove", actor=user.email, object_type="oncall", object_id=str(slot_id))
    db.commit()
    return {"removed": slot_id}


@router.post("/oncall-slots/take")
def oncall_take(db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict:
    """New UI: cover the rest of today as the signed-in user."""
    from app.dc_noc import take_duty

    try:
        slot = take_duty(db, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "oncall.take", actor=user.email, object_type="oncall", object_id=user.email, data={"name": slot.get("name")})
    db.commit()
    return slot


class ScheduledReportBody(BaseModel):
    name: str
    recipients: list[str] = Field(default_factory=list)
    schedule: str = "24h"
    every_n: str = ""
    every_unit: str = "minutes"
    weekdays: list[int] = Field(default_factory=list)
    cron: str = ""
    asset_ids: list[str] = Field(default_factory=list)
    kind: str = "performance"  # "performance" (NOC summary) or "forgeai" (ForgeAI digest)


def _report_writer(user: User) -> None:
    from app.security import can_send_ops

    if not can_send_ops(user):
        raise HTTPException(status_code=403, detail="needs Analyst or higher")


def _report_or_404(db: Session, report_id: int):
    from app.models import ScheduledReport

    row = db.get(ScheduledReport, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="scheduled report not found")
    return row


def _parsed_report(db: Session, body: ScheduledReportBody, actor: str) -> dict:
    from app.report_forms import parse_scheduled_report_form

    return parse_scheduled_report_form(
        db, name=body.name, to_email=list(body.recipients), new_email="", interval_hours=0, asset_id=list(body.asset_ids),
        actor=actor, schedule=body.schedule, every_n=body.every_n, every_unit=body.every_unit,
        weekday=[str(d) for d in body.weekdays], cron=body.cron,
    )


def _report_out(row) -> dict[str, Any]:
    from app.services import report_recipients, report_schedule, schedule_label

    from app.services import report_kind

    return {"id": row.id, "name": row.name, "kind": report_kind(row), "recipients": report_recipients(row), "schedule": schedule_label(row),
            "spec": report_schedule(row), "enabled": bool(row.enabled), "asset_ids": list(row.asset_ids or []),
            "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None}


@router.post("/scheduled-reports")
def scheduled_report_create(body: ScheduledReportBody, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """New UI: schedule a NOC summary or a ForgeAI digest by preset, interval, cron expression or once."""
    from app.models import ScheduledReport
    from app.services import initial_next_run

    _report_writer(user)
    parsed = _parsed_report(db, body, user.email)
    row = ScheduledReport(
        name=parsed["name"], kind="forgeai" if body.kind == "forgeai" else "performance", to_email=parsed["email"], recipients=parsed["emails"],
        interval_hours=parsed["hours"], schedule=parsed["schedule"], asset_ids=parsed["ids"], enabled=True,
        created_by=user.email, next_run_at=initial_next_run(utcnow(), parsed["schedule"]),
    )
    db.add(row)
    audit(db, "report.create", actor=user.email, object_type="report", object_id=row.name, data={"to": parsed["emails"], "schedule": parsed["schedule"]})
    db.commit()
    return _report_out(row)


@router.put("/scheduled-reports/{report_id}")
def scheduled_report_update(report_id: int, body: ScheduledReportBody, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """New UI: change a scheduled report; a new schedule recomputes the next run."""
    from app.services import initial_next_run

    _report_writer(user)
    row = _report_or_404(db, report_id)
    parsed = _parsed_report(db, body, user.email)
    changed = (row.schedule or {}) != parsed["schedule"]
    row.name, row.to_email, row.recipients = parsed["name"], parsed["email"], parsed["emails"]
    row.interval_hours, row.schedule, row.asset_ids = parsed["hours"], parsed["schedule"], parsed["ids"]
    row.kind = "forgeai" if body.kind == "forgeai" else "performance"
    if changed:
        row.next_run_at = initial_next_run(utcnow(), parsed["schedule"])
    audit(db, "report.update", actor=user.email, object_type="report", object_id=str(row.id), data={"name": row.name, "schedule": parsed["schedule"]})
    db.commit()
    return _report_out(row)


@router.post("/scheduled-reports/{report_id}/toggle")
def scheduled_report_toggle(report_id: int, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """New UI: switch a scheduled report on or off (off = kept, not sent)."""
    _report_writer(user)
    row = _report_or_404(db, report_id)
    row.enabled = not bool(row.enabled)
    audit(db, "report.toggle", actor=user.email, object_type="report", object_id=str(row.id), data={"enabled": bool(row.enabled)})
    db.commit()
    return _report_out(row)


@router.delete("/scheduled-reports/{report_id}")
def scheduled_report_delete(report_id: int, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """New UI: remove a scheduled report. Mail already in the outbox stays."""
    _report_writer(user)
    row = _report_or_404(db, report_id)
    audit(db, "report.delete", actor=user.email, object_type="report", object_id=str(row.id), data={"name": row.name})
    db.delete(row)
    db.commit()
    return {"removed": report_id}


@router.get("/scheduled-reports/preview")
def scheduled_report_sample_preview(db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """What a weekly report mail looks like with today's data. Nothing is sent."""
    from app.message_previews import scheduled_report

    return scheduled_report(db)


@router.get("/scheduled-reports/{report_id}/preview")
def scheduled_report_preview(report_id: int, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """The mail this scheduled report would send now, with its recipients. Nothing is sent."""
    from app.message_previews import scheduled_report

    try:
        return scheduled_report(db, report_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc


@router.post("/scheduled-reports/{report_id}/send")
def scheduled_report_send(report_id: int, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Send this report now, in the background (``report_send`` job). The schedule does not move."""
    from app.jobs import enqueue

    from app.services import _report_days, report_kind, report_recipients

    _report_writer(user)
    row = _report_or_404(db, report_id)
    if report_kind(row) == "forgeai":
        # The digest asks the model: LLM lane, same job and dedupe key as the scheduler.
        job = enqueue(db, "forgeai_digest", f"report-{row.id}", object_type="report",
                      payload={"actor": user.email, "recipients": report_recipients(row), "name": row.name,
                               "hours": min(_report_days(row), 7) * 24})
    else:
        job = enqueue(db, "report_send", str(row.id), object_type="report", payload={"actor": user.email})
    audit(db, "report.send_now", actor=user.email, object_type="report", object_id=str(row.id), data={"name": row.name})
    db.commit()
    return {"queued": True, "job": getattr(job, "id", None)}


@router.get("/message-previews")
def message_previews_api(incident: str = "", db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Settings › Messages: the alert mail, chat messages and incident report for one incident
    (the newest when none is named, a sample on an empty appliance). Rendered only, never sent."""
    from app import message_previews as mp

    from app.ack_links import public_url

    row, sample = mp.pick_incident(db, incident)
    return {
        "incident": None if sample else row.number,
        "public_url": public_url(),
        "note": mp.SAMPLE_NOTE if sample else "",
        "alert_email": mp.alert_email(row),
        "chat": mp.chat_messages(row),
        "incident_report": mp.incident_report(db, row),
    }


class MaintenanceBody(BaseModel):
    assets: list[str] = Field(default_factory=list)
    starts_at: str = ""
    ends_at: str = ""


@router.post("/maintenance")
def maintenance_set_api(body: MaintenanceBody, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    """New UI: put one or more assets in maintenance (all or nothing)."""
    from app.maintenance_edit import set_window

    try:
        rows = set_window(db, body.assets, body.starts_at, body.ends_at, user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"assets": [r.asset_id for r in rows]}


@router.delete("/maintenance/{asset_id}")
def maintenance_clear_api(asset_id: str, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict:
    """New UI: end an asset's maintenance window now."""
    from app.maintenance_edit import clear_window

    try:
        return clear_window(db, asset_id, user.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="asset not found") from exc


class ParentBody(BaseModel):
    asset_id: str
    parent_asset_id: str = ""


@router.get("/topology")
def topology_api(db: Session = Depends(get_db), user: User = Depends(require("read_assets"))) -> dict[str, Any]:
    """New UI Inventory › Dependency map: the parent tree and parent suggestions from LLDP/CDP."""
    from app.topology import suggestions, tree

    data = tree(db)
    data["suggestions"] = suggestions(db)
    data["can_edit"] = can(user, "write_assets")
    return data


@router.post("/topology/sync")
def topology_sync_api(db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict[str, Any]:
    """New UI: read LLDP/CDP links from LibreNMS in the background."""
    from app.jobs import enqueue

    job = enqueue(db, "topology_sync", "librenms", object_type="topology")
    audit(db, "topology.sync", actor=user.email, object_type="topology", object_id=f"job-{job.id}")
    db.commit()
    return {"job": job.id, "status": job.status}


@router.post("/topology/parent")
def topology_parent_api(body: ParentBody, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict[str, Any]:
    """New UI: set or clear one asset's parent (loops refused). Parent-down suppression uses it at once."""
    from app.dc_noc import set_asset_parent

    asset = db.query(Asset).filter_by(asset_id=body.asset_id).first()
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    try:
        set_asset_parent(db, asset, body.parent_asset_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "asset.parent", actor=user.email, object_type="asset", object_id=asset.asset_id, data={"parent": body.parent_asset_id})
    db.commit()
    return {"asset_id": asset.asset_id, "parent_asset_id": body.parent_asset_id}


class ChangeBody(BaseModel):
    title: str
    source: str = "api"
    kind: str = "change"
    detail: str = ""
    url: str = ""
    asset: str = ""
    actor: str = ""
    at: datetime | None = None


@router.post("/changes")
def changes_post_api(body: ChangeBody, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Record a deploy or change. Bearer CHANGES_WEBHOOK_TOKEN (CI/CD, GLPI) or a signed-in Engineer."""
    from app.changes import record

    auth = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    expected = settings.changes_webhook_token
    actor = body.actor
    if auth and expected and hmac.compare_digest(auth.encode(), expected.encode()):
        source = body.source or "api"
    else:
        user = current_user(request, db)
        if user is None or not can(user, "write_assets"):
            raise HTTPException(status_code=401, detail="needs CHANGES_WEBHOOK_TOKEN or an Engineer session")
        source, actor = "manual", user.email
    try:
        row = record(db, title=body.title, source=source, kind=body.kind, detail=body.detail, url=body.url,
                     actor=actor, asset_ref=body.asset, at=body.at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": row.id, "asset_matched": row.asset_id is not None}


@router.get("/changes")
def changes_list_api(db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict[str, Any]:
    """Latest recorded changes (deploys, config pushes, tickets)."""
    from app.changes import recent

    return {"changes": recent(db), "can_add": can(user, "write_assets")}


def _token_or_engineer(request: Request, db: Session) -> str:
    """Actor for machine-or-human writes: CHANGES_WEBHOOK_TOKEN bearer, else an Engineer session."""
    auth = (request.headers.get("authorization") or "").replace("Bearer ", "").strip()
    expected = settings.changes_webhook_token
    if auth and expected and hmac.compare_digest(auth.encode(), expected.encode()):
        return "token"
    user = current_user(request, db)
    if user is None or not can(user, "write_assets"):
        raise HTTPException(status_code=401, detail="needs CHANGES_WEBHOOK_TOKEN or an Engineer session")
    return user.email


@router.get("/checks")
def checks_api(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict[str, Any]:
    """Latest TLS, backup and heartbeat results (the CLI's ./forgesre checks reads this too)."""
    from app.health_checks import results

    data = results(db)
    data["can_edit"] = can(user, "write_assets")
    return data


@router.post("/checks/run")
def checks_run_api(db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict[str, Any]:
    """Run the checks now in the background instead of waiting for the 15-minute round."""
    from app.jobs import enqueue

    job = enqueue(db, "health_checks", "appliance", object_type="checks")
    db.commit()
    return {"job": job.id, "status": job.status}


@router.post("/heartbeats/{name}")
def heartbeat_ping_api(name: str, request: Request, every_hours: float | None = None, asset: str = "", note: str = "",
                       db: Session = Depends(get_db)) -> dict[str, Any]:
    """A backup or cron job calls this after each success. First call creates the heartbeat."""
    from app.health_checks import ping

    _token_or_engineer(request, db)
    try:
        row = ping(db, name, every_hours=every_hours, asset=asset, note=note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"name": row.name, "every_hours": row.every_hours, "last_at": row.last_at.isoformat()}


@router.delete("/heartbeats/{name}")
def heartbeat_delete_api(name: str, db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict[str, Any]:
    """Stop watching a heartbeat and resolve its alarm."""
    from app.models import CheckResult, Heartbeat

    row = db.query(Heartbeat).filter_by(name=name).first()
    if row is None:
        raise HTTPException(status_code=404, detail="heartbeat not found")
    from app.health_checks import _alert
    from app.services import ingest_alertmanager

    asset = row.asset_ref
    db.delete(row)
    db.query(CheckResult).filter_by(kind="heartbeat", target=name).delete()
    audit(db, "heartbeat.remove", actor=user.email, object_type="heartbeat", object_id=name)
    db.commit()
    # Close its alarm now; nothing will check it again.
    ingest_alertmanager(db, {"status": "resolved", "alerts": [_alert("heartbeat", name, "ok", "heartbeat removed", asset)]})
    db.commit()
    return {"removed": name}


class SloBody(BaseModel):
    name: str
    scope: str = "all"
    scope_value: str = ""
    target: float = 99.9
    window_days: int = 30
    severities: list[str] = Field(default_factory=lambda: ["CRITICAL"])
    owner: str = ""


def _slo_or_404(db: Session, slo_id: int):
    from app.models import Slo

    row = db.get(Slo, slo_id)
    if row is None:
        raise HTTPException(status_code=404, detail="SLO not found")
    return row


@router.get("/slos")
def slos_api(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict[str, Any]:
    """Every SLO with availability, error budget and 24 h burn rate (also ./forgesre slo)."""
    from app.slo import business_services, evaluate_all

    return {"slos": evaluate_all(db), "can_edit": can(user, "write_play"), "services": business_services(db)}


@router.post("/slos")
def slo_create_api(body: SloBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict[str, Any]:
    """New SLO (Engineer+)."""
    from app.slo import evaluate, save

    try:
        return evaluate(db, save(db, body.model_dump(), user.email))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/slos/{slo_id}")
def slo_update_api(slo_id: int, body: SloBody, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict[str, Any]:
    """Change an SLO; history is recomputed from incidents, nothing is stored per day."""
    from app.slo import evaluate, save

    try:
        return evaluate(db, save(db, body.model_dump(), user.email, _slo_or_404(db, slo_id)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/slos/{slo_id}")
def slo_delete_api(slo_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict[str, Any]:
    row = _slo_or_404(db, slo_id)
    audit(db, "slo.remove", actor=user.email, object_type="slo", object_id=row.name)
    db.delete(row)
    db.commit()
    return {"removed": slo_id}


@router.get("/kubernetes")
def kubernetes_api(db: Session = Depends(get_db), user: User = Depends(require("read_assets"))) -> dict[str, Any]:
    """Configured clusters with node and workload counts and open Kubernetes incidents."""
    from app.kubernetes import CLUSTER, NODE, WORKLOAD, clusters, token_key
    from app.services import ACTIVE_INCIDENT_STATUSES

    out = []
    for c in clusters():
        root = db.query(Asset).filter_by(asset_id=f"k8s-{c['name']}").first()
        kids = db.query(Asset).filter(Asset.parent_id == root.id).all() if root else []
        ids = [root.id] + [k.id for k in kids] if root else []
        open_ = db.query(Incident).filter(Incident.asset_id.in_(ids or [-1]), Incident.status.in_(ACTIVE_INCIDENT_STATUSES)).count()
        out.append({"name": c["name"], "url": c["url"], "token_set": bool(settings._secret(token_key(c["name"]))),
                    "token_key": token_key(c["name"]), "version": root.notes if root else "",
                    "nodes": sum(1 for k in kids if k.type == NODE), "workloads": sum(1 for k in kids if k.type == WORKLOAD),
                    "open_incidents": open_, "seen": root is not None and root.type == CLUSTER})
    return {"clusters": out, "can_edit": can(user, "write_assets")}


@router.post("/kubernetes/sync")
def kubernetes_sync_api(db: Session = Depends(get_db), user: User = Depends(require("write_assets"))) -> dict[str, Any]:
    """Read every cluster now, in the background."""
    from app.jobs import enqueue

    job = enqueue(db, "k8s_sync", "clusters", object_type="kubernetes")
    db.commit()
    return {"job": job.id, "status": job.status}


@router.get("/automation")
def automation_api(db: Session = Depends(get_db), user: User = Depends(require("read_play"))) -> dict[str, Any]:
    """Automation page of the new UI: pipeline counts, rules, playbooks, policies, maintenance."""
    from app.automation_view import automation_overview

    data = automation_overview(db)
    data["can_edit"] = can(user, "write_play")
    data["can_edit_maintenance"] = can(user, "write_assets")
    return data


@router.post("/automation/simulate")
def automation_simulate_api(body: SimulateBody, db: Session = Depends(get_db), user: User = Depends(require("read_play"))) -> dict[str, Any]:
    """Try an alarm: what would match and who would be called. Read-only."""
    from datetime import datetime as _dt

    from app.automation_view import simulate_alarm

    at = None
    if body.at.strip():
        try:
            at = _dt.fromisoformat(body.at.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="time must be ISO 8601, e.g. 2026-11-19T01:30") from exc
        if at.tzinfo is None:
            from app.dc_noc import _zone

            at = at.replace(tzinfo=_zone())
    if not body.alertname.strip():
        raise HTTPException(status_code=400, detail="alertname is required")
    return simulate_alarm(db, body.alertname.strip(), asset_ref=body.asset, value=body.value, at=at)


class ReviewBody(BaseModel):
    root_cause: str
    rca_rating: str = ""
    ai_rating: str = ""


@router.post("/incidents/{number}/review")
def incident_review_api(
    number: str, body: ReviewBody, db: Session = Depends(get_db), user: User = Depends(require("write_incidents"))
) -> dict[str, Any]:
    """Analyst confirms the cause and rates ForgeRCA / ForgeAI (correct, partly, wrong)."""
    from app.investigations_view import review_incident

    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    try:
        return review_incident(db, item, user.email, body.root_cause, body.rca_rating, body.ai_rating)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/investigations-page")
def investigations_page_api(
    days: int = 90,
    q: str = "",
    only_ai: bool = False,
    only_reviewed: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(require("read_incidents")),
) -> dict[str, Any]:
    """Investigations page: resolved incidents with analyses and reviews, accuracy, recurring causes."""
    from app.investigations_view import investigations_page

    return investigations_page(db, days=days, q=q, only_ai=only_ai, only_reviewed=only_reviewed, limit=limit, offset=offset)


@router.get("/reports-page")
def reports_page_api(days: int = 30, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict[str, Any]:
    """Reports page of the new UI: period KPIs vs the previous period, breakdowns, scheduled reports."""
    from app.reports_view import reports_overview

    from app.security import can_send_ops

    data = reports_overview(db, days=days)
    data["can_edit"] = can_send_ops(user)
    return data


@router.get("/reports/alert-quality")
def alert_quality(days: int = 30, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    """Per alert name: incidents, % acknowledged, % that cleared by themselves, flapping, advice."""
    from app.alert_quality import report

    return report(db, days=days)


@router.get("/reports/incidents.csv")
def reports_csv_api(days: int = 30, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> Response:
    """Incidents of the last ``days`` as a CSV download."""
    from app.reports_view import incidents_csv

    return Response(
        incidents_csv(db, days=days),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="forgesre-incidents-{max(1, min(days, 365))}d.csv"'},
    )


@router.get("/noise-filters")
def noise_filters_list(db: Session = Depends(get_db), user: User = Depends(require("read_play"))) -> list[dict[str, Any]]:
    """Every noise filter with hit counts."""
    from app.models import NoiseFilter
    from app.noise_filters import as_dict

    return [as_dict(f) for f in db.query(NoiseFilter).order_by(NoiseFilter.id).all()]


@router.post("/noise-filters")
def noise_filters_create(body: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict[str, Any]:
    """Create a noise filter (Engineer+); audited. 400 with the reason when invalid."""
    from app.models import NoiseFilter
    from app.noise_filters import apply_fields, as_dict

    try:
        row = apply_fields(NoiseFilter(created_by=user.email), body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(row)
    db.flush()
    audit(db, "noise_filter.create", actor=user.email, object_type="noise_filter", object_id=str(row.id), data=as_dict(row))
    db.commit()
    return as_dict(row)


@router.put("/noise-filters/{filter_id}")
def noise_filters_update(
    filter_id: int, body: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(require("write_play"))
) -> dict[str, Any]:
    """Change a noise filter (Engineer+); audited."""
    from app.models import NoiseFilter
    from app.noise_filters import apply_fields, as_dict

    row = db.get(NoiseFilter, filter_id)
    if row is None:
        raise HTTPException(status_code=404, detail="noise filter not found")
    try:
        apply_fields(row, body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, "noise_filter.update", actor=user.email, object_type="noise_filter", object_id=str(row.id), data=as_dict(row))
    db.commit()
    return as_dict(row)


@router.delete("/noise-filters/{filter_id}")
def noise_filters_delete(filter_id: int, db: Session = Depends(get_db), user: User = Depends(require("write_play"))) -> dict[str, Any]:
    """Remove a noise filter. Incidents it quieted open on the next promotion pass if still firing."""
    from app.models import NoiseFilter

    row = db.get(NoiseFilter, filter_id)
    if row is None:
        raise HTTPException(status_code=404, detail="noise filter not found")
    audit(db, "noise_filter.delete", actor=user.email, object_type="noise_filter", object_id=str(row.id), data={"name": row.name})
    db.delete(row)
    db.commit()
    return {"deleted": filter_id}
