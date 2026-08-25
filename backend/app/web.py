from __future__ import annotations

import json
import os
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.asset_probe import reachability_snapshot
from app.seed import is_demo_asset_id
from app.exporter_detect import AUTO_ASSET_TYPE
from app.asset_alarms import alarms_from_form, saved_alarm_hostnames
from app.inventory import (
    ASSET_TYPE_CHOICES,
    approve_candidate,
    asset_form_values,
    clone_prefill,
    create_manual_asset,
    delete_asset,
    delete_blocked,
    ignore_candidate,
    run_scan,
    similar_incident_groups,
    sync_netbox,
    update_asset,
    is_snmp_asset,
)
from app.journal import MODULES, error_banner_entries, list_entries, module_counts, next_error_ack_id
from app.models import (
    Asset,
    AuditLog,
    DiscoveryCandidate,
    EscalationPolicy,
    Incident,
    Job,
    MailContact,
    Notification,
    Playbook,
    Playrule,
    ScheduledReport,
    User,
)
from app.security import can, distinct_who_name, make_session_token, role_label, user_from_session, verify_password
from app.api import doctor_payload, run_asset_verify
from app.asset_metrics import safe_asset_metric_panel
from app.metrics import reset_demo_gauges
from app.services import (
    is_demo_incident,
    is_demo_journal,
    is_demo_mail,
    list_host_down_incidents,
    run_demo,
    run_demo_host,
    run_demo_network,
    run_demo_nodecpu,
    run_demo_rca,
    run_demo_windows,
    run_investigation,
)
from app.stack import enrich_components, rewrite_host
from app.history import (
    add_note,
    apply_status_fields,
    audit_for,
    clamp_days,
    list_history,
    notes_for,
    notifications_for,
    reported_to_for,
)
from rca.catalog import PLAYRULE_PRESETS
from app.settings import settings


def health_class(status: str) -> str:
    """Map doctor status to pill/stat CSS: ok green, disabled yellow, error red."""
    value = str(status or "").lower()
    if value in {"ok", "healthy", "running"}:
        return "ok"
    if value in {"disabled", "warn", "warning", "paused", "starting"}:
        return "warn"
    return "crit"


def incident_tone(status: str, severity: str = "") -> str:
    """INC link color: green resolved, yellow in progress, red critical."""
    st = str(status or "").upper()
    sev = str(severity or "").upper()
    if st in {"RESOLVED", "CLOSED"}:
        return "inc-ok"
    if sev in {"CRITICAL", "CRIT", "FATAL", "EMERGENCY"}:
        return "inc-crit"
    return "inc-warn"


def can_send_ops(user: User) -> bool:
    return can(user, "write_play") or can(user, "write_incidents") or can(user, "admin")


def _snmp_answer(ip: str) -> bool:
    from discovery import probe_snmp_udp

    return bool(probe_snmp_udp(ip))


def smtp_provider_id() -> str:
    """Which documented SMTP path Core is using. Display only — does not send mail."""
    if not (settings.email_enabled and settings.smtp_host):
        return "off"
    host = (settings.smtp_host or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return "mailbox" if int(settings.smtp_port or 0) == 587 else "other"
    if "gmail" in host:
        return "gmail"
    if "office365" in host or "outlook" in host or "hotmail" in host:
        return "outlook"
    return "other"


def _webmail_url(request: Request) -> str:
    """Roundcube link when the optional mailbox profile is in use or hinted in env."""
    host = (settings.smtp_host or "").lower()
    local = host in {"127.0.0.1", "localhost", "::1"} and int(settings.smtp_port or 0) == 587
    hinted = bool(os.environ.get("MAIL_DOMAIN") or os.environ.get("ROUNDCUBEMAIL_DES_KEY"))
    if not (local or hinted):
        return ""
    hostname = (request.headers.get("host") or "localhost").split(":")[0] or "localhost"
    port = os.environ.get("ROUNDCUBE_PORT", "8081")
    return f"http://{hostname}:{port}/"


def ops_mail_ctx(db: Session, user: User, incident: Incident | None = None) -> dict:
    from app.services import list_mail_addresses

    owner = ""
    if incident and incident.asset and incident.asset.owner_email:
        owner = incident.asset.owner_email.strip()
    return {
        "addresses": list_mail_addresses(db),
        "default_to": owner,
        "can_send": can_send_ops(user),
        "smtp_on": settings.email_enabled and bool(settings.smtp_host),
    }

router = APIRouter()
templates = Jinja2Templates(directory=str(settings.frontend_dir / "templates"))


def render(request: Request, name: str, user, **extra):
    return templates.TemplateResponse(request, name, ctx(request, user, **extra))


def ctx(request: Request, user: User | None, **extra):
    data = {
        "request": request,
        "user": user,
        "can": lambda perm: can(user, perm),
        "role_label": role_label,
        "distinct_who_name": distinct_who_name,
        "grafana_url": settings.grafana_public_url,
        "grafana_enabled": settings.grafana_enabled,
        "ai_enabled": settings.ai_enabled,
        "health_class": health_class,
        "incident_tone": incident_tone,
        "is_demo_incident": is_demo_incident,
        "is_demo_mail": is_demo_mail,
        "is_demo_journal": is_demo_journal,
    }
    data.update(extra)
    return data


def llm_job_error(db: Session, number: str) -> bool:
    row = (
        db.query(Job)
        .filter(Job.kind == "investigate", Job.object_id == number, Job.status == "error")
        .order_by(Job.id.desc())
        .first()
    )
    return bool(row and (row.payload or {}).get("use_llm") is not False)


def llm_job_pending(db: Session, number: str) -> bool:
    return (
        db.query(Job)
        .filter(
            Job.kind == "investigate",
            Job.object_id == number,
            Job.status.in_(["pending", "running"]),
        )
        .first()
        is not None
    )


def tool_status(investigation, pending: bool, llm_error: bool = False) -> dict:
    """ForgeRCA = builtin (always first). ForgeAI = local LLM rewrite."""
    rca_class = "ok" if investigation else "ignored"
    rca_hint = "Python builtin. Always first." if investigation else "Not run yet."
    provider = getattr(investigation, "provider", "") if investigation else ""
    enabled = bool(settings.ai_enabled and settings.llm_url)
    if provider == "forgerca-llm":
        ai_class, ai_hint = "ok", "Local LLM rewrote the prose. Facts stay from ForgeRCA."
    elif pending:
        ai_class, ai_hint = "warn", "ForgeAI rewrite is running. Can take several minutes on CPU."
    elif llm_error:
        ai_class, ai_hint = "crit", "ForgeAI rewrite failed. Showing ForgeRCA builtin."
    elif not enabled:
        ai_class, ai_hint = "crit", "ForgeAI is off (ai.enabled false or no LLM URL)."
    else:
        ai_class, ai_hint = "warn", "ForgeAI is enabled. Latest text is still ForgeRCA."
    return {
        "rca_class": rca_class,
        "rca_hint": rca_hint,
        "ai_class": ai_class,
        "ai_hint": ai_hint,
    }


def get_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return user_from_session(db, request.cookies.get("forgesre_session"))


class NotAuthenticated(Exception):
    pass


def login_required(user: User | None = Depends(get_user)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require_page(*permissions: str):
    def _inner(user: User = Depends(login_required)) -> User:
        if permissions and not any(can(user, perm) for perm in permissions):
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return _inner


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: User | None = Depends(get_user)):
    if user:
        return RedirectResponse("/", status_code=302)
    return render(request, "login.html", None, error=None)


@router.post("/login")
def login_submit(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(...),
    password: str = Form(...),
):
    user = db.query(User).filter_by(email=email).first()
    if user is None or not verify_password(password, user.password_hash):
        return render(request, "login.html", None, error="Invalid email or password.")
    audit(db, "login", actor=user.email, ip=request.client.host if request.client else "", commit=True)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        "forgesre_session",
        make_session_token(user.id),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=60 * 60 * 12,
    )
    return response


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db), user: User | None = Depends(get_user)):
    if user:
        audit(db, "logout", actor=user.email, ip=request.client.host if request.client else "", commit=True)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("forgesre_session")
    return response


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    from sqlalchemy import func

    pending = db.query(func.count(DiscoveryCandidate.id)).filter_by(status="new").scalar() or 0
    stats = {
        "assets_total": db.query(func.count(Asset.id)).scalar() or 0,
        "healthy": db.query(func.count(Asset.id)).filter_by(status="healthy").scalar() or 0,
        "warning": db.query(func.count(Asset.id)).filter_by(status="warning").scalar() or 0,
        "critical": db.query(func.count(Asset.id)).filter_by(status="critical").scalar() or 0,
        "offline": db.query(func.count(Asset.id)).filter_by(status="offline").scalar() or 0,
        "open": db.query(func.count(Incident.id)).filter_by(status="OPEN").scalar() or 0,
        "inc_critical": db.query(func.count(Incident.id)).filter(Incident.severity == "CRITICAL", Incident.status.notin_(["RESOLVED", "CLOSED"])).scalar() or 0,
        "investigating": db.query(func.count(Incident.id)).filter_by(status="INVESTIGATING").scalar() or 0,
        "resolved": db.query(func.count(Incident.id)).filter_by(status="RESOLVED").scalar() or 0,
        "pending_discovery": pending,
    }
    doctor = doctor_payload()
    recent = db.query(Incident).order_by(Incident.id.desc()).limit(8).all()
    journal_error = error_banner_entries(db, getattr(user, "journal_error_ack_id", 0), limit=5)
    journal_recent = list_entries(db, limit=8)
    down_incidents = list_host_down_incidents(db)
    return render(
        request,
        "dashboard.html",
        user,
        stats=stats,
        doctor=doctor,
        recent=recent,
        journal_error=journal_error,
        journal_recent=journal_recent,
        down_incidents=down_incidents,
    )


@router.post("/dashboard/journal-ack")
def dashboard_journal_ack(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    until_id: str = Form(""),
):
    if not can(user, "read_play"):
        raise HTTPException(status_code=403, detail="forbidden")
    shown = list_entries(db, status="error", limit=5)
    raw = (until_id or "").strip()
    requested = int(raw) if raw.isdigit() else None
    new_ack = next_error_ack_id(requested, user.journal_error_ack_id, [row.id for row in shown])
    if new_ack > int(user.journal_error_ack_id or 0):
        user.journal_error_ack_id = new_ack
        db.commit()
    return RedirectResponse("/", status_code=302)


@router.get("/assets", response_class=HTMLResponse)
def assets_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    edit: str = "",
    clone: str = "",
):
    rows = db.query(Asset).order_by(Asset.hostname).all()
    form_mode = "add"
    selected = None
    form = asset_form_values()
    clone_source = ""
    notice = request.query_params.get("notice") or ""
    if edit.strip():
        selected = db.query(Asset).filter_by(asset_id=edit.strip()).first()
        if selected is not None:
            form_mode = "edit"
            form = asset_form_values(selected)
    elif clone.strip():
        selected = db.query(Asset).filter_by(asset_id=clone.strip()).first()
        if selected is not None:
            form_mode = "clone"
            form = clone_prefill(db, selected)
            clone_source = selected.asset_id
    return render(
        request,
        "assets.html",
        user,
        assets=rows,
        form_mode=form_mode,
        form=form,
        selected=selected,
        clone_source=clone_source,
        type_choices=ASSET_TYPE_CHOICES,
        delete_blocked=delete_blocked,
        notice=notice,
        reachability_snapshot=reachability_snapshot,
    )



@router.get("/assets/verify", response_class=HTMLResponse)
def assets_verify_all(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("write_assets")),
    demo: str = "",
):
    include_demo = demo.strip().lower() in {"1", "true", "yes", "demo"}
    reports: list[dict] = []
    skipped_demo = 0
    for asset in db.query(Asset).order_by(Asset.hostname).all():
        if is_demo_asset_id(asset.asset_id) and not include_demo:
            skipped_demo += 1
            continue
        reports.append(run_asset_verify(db, asset))
    return render(
        request,
        "assets_verify.html",
        user,
        reports=reports,
        skipped_demo=skipped_demo,
        include_demo=include_demo,
    )


@router.post("/assets")
def asset_create(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    hostname: str = Form(...),
    ip: str = Form(""),
    type: str = Form(AUTO_ASSET_TYPE),
    environment: str = Form("Production"),
    owner: str = Form("platform"),
    contact_name: str = Form(""),
    owner_email: str = Form(""),
    owner_phone: str = Form(""),
    notes: str = Form(""),
    scrape_address: str = Form(""),
    clone_of: str = Form(""),
    alarms_present: str = Form(""),
    alarm_up_enabled: str = Form(""),
    alarm_cpu_enabled: str = Form(""),
    alarm_cpu_threshold: str = Form(""),
    alarm_memory_enabled: str = Form(""),
    alarm_memory_threshold: str = Form(""),
    alarm_disk_enabled: str = Form(""),
    alarm_disk_threshold: str = Form(""),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    cloned_from = (clone_of or "").strip()
    posted_alarms = alarms_from_form(
        present=alarms_present,
        up_enabled=alarm_up_enabled,
        cpu_enabled=alarm_cpu_enabled,
        cpu_threshold=alarm_cpu_threshold,
        memory_enabled=alarm_memory_enabled,
        memory_threshold=alarm_memory_threshold,
        disk_enabled=alarm_disk_enabled,
        disk_threshold=alarm_disk_threshold,
    )
    try:
        asset = create_manual_asset(
            db,
            hostname=hostname,
            ip=ip,
            type=type,
            environment=environment,
            owner=owner,
            contact_name=contact_name,
            owner_email=owner_email,
            owner_phone=owner_phone,
            notes=notes,
            scrape_address=scrape_address,
            actor=user.email,
            require_new=bool(cloned_from),
            cloned_from=cloned_from,
            snmp_prober=_snmp_answer,
            alarms=posted_alarms,
        )
    except ValueError as exc:
        if cloned_from:
            return RedirectResponse(
                f"/assets?clone={quote(cloned_from)}&notice={quote(str(exc))}",
                status_code=302,
            )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    notice = getattr(asset, "_detect_message", "") or ""
    suffix = f"?notice={quote(notice)}" if notice else ""
    return RedirectResponse(f"/assets/{asset.asset_id}{suffix}", status_code=302)


@router.get("/discovery", response_class=HTMLResponse)
def discovery_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_page("write_assets"))):
    rows = db.query(DiscoveryCandidate).order_by(DiscoveryCandidate.id.desc()).all()
    pending = [row for row in rows if row.status == "new"]
    return render(
        request,
        "discovery.html",
        user,
        candidates=rows,
        pending=pending,
        discovery_enabled=settings.discovery_enabled,
        discovery_mode=settings.discovery_mode,
        discovery_cidrs=settings.discovery_cidrs,
        netbox_enabled=settings.netbox_enabled,
        netbox_url=settings.netbox_url,
    )


@router.post("/discovery/scan")
def discovery_scan_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    run_scan(db)
    audit(db, "discovery.scan", actor=user.email, commit=True)
    return RedirectResponse("/discovery", status_code=302)


@router.post("/discovery/{candidate_id}/approve")
def discovery_approve_page(
    candidate_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    row = db.get(DiscoveryCandidate, candidate_id)
    if row is None:
        raise HTTPException(status_code=404)
    asset = approve_candidate(db, row, actor=user.email)
    return RedirectResponse(f"/assets/{asset.asset_id}", status_code=302)


@router.post("/discovery/{candidate_id}/ignore")
def discovery_ignore_page(
    candidate_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    row = db.get(DiscoveryCandidate, candidate_id)
    if row is None:
        raise HTTPException(status_code=404)
    ignore_candidate(db, row, actor=user.email)
    return RedirectResponse("/discovery", status_code=302)


@router.post("/discovery/netbox-sync")
def discovery_netbox_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    sync_netbox(db)
    return RedirectResponse("/discovery", status_code=302)


@router.get("/assets/{asset_id}", response_class=HTMLResponse)
def asset_detail(asset_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404)
    related = db.query(Incident).filter_by(asset_id=item.id).order_by(Incident.id.desc()).all()
    similar = similar_incident_groups(db, item)
    return render(
        request,
        "asset_detail.html",
        user,
        asset=item,
        incidents=related,
        similar=similar,
        snmp_target=is_snmp_asset(item),
        snmp_enabled=settings.snmp_enabled,
        can_remove=can(user, "write_assets") and not delete_blocked(item),
        remove_blocked=delete_blocked(item) if can(user, "write_assets") else "",
        reachability_snapshot=reachability_snapshot,
        metrics=safe_asset_metric_panel(item),
    )



@router.get("/assets/{asset_id}/verify", response_class=HTMLResponse)
def asset_verify_page(
    asset_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("write_assets")),
):
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404)
    report = run_asset_verify(db, item)
    return render(
        request,
        "asset_verify.html",
        user,
        asset=item,
        report=report,
    )


@router.post("/assets/{asset_id}/update")
def asset_update(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    hostname: str = Form(""),
    ip: str = Form(""),
    type: str = Form("Linux Server"),
    environment: str = Form("Production"),
    owner: str = Form("platform"),
    contact_name: str = Form(""),
    owner_email: str = Form(""),
    owner_phone: str = Form(""),
    notes: str = Form(""),
    scrape_address: str = Form(""),
    alarms_present: str = Form(""),
    alarm_up_enabled: str = Form(""),
    alarm_cpu_enabled: str = Form(""),
    alarm_cpu_threshold: str = Form(""),
    alarm_memory_enabled: str = Form(""),
    alarm_memory_threshold: str = Form(""),
    alarm_disk_enabled: str = Form(""),
    alarm_disk_threshold: str = Form(""),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404)
    posted_alarms = alarms_from_form(
        present=alarms_present,
        up_enabled=alarm_up_enabled,
        cpu_enabled=alarm_cpu_enabled,
        cpu_threshold=alarm_cpu_threshold,
        memory_enabled=alarm_memory_enabled,
        memory_threshold=alarm_memory_threshold,
        disk_enabled=alarm_disk_enabled,
        disk_threshold=alarm_disk_threshold,
    )
    update_asset(
        db,
        item,
        hostname=hostname,
        ip=ip,
        type=type,
        environment=environment,
        owner=owner,
        contact_name=contact_name,
        owner_email=owner_email,
        owner_phone=owner_phone,
        notes=notes,
        scrape_address=scrape_address,
        actor=user.email,
        snmp_prober=_snmp_answer,
        alarms=posted_alarms,
    )
    notice = getattr(item, "_detect_message", "") or ""
    suffix = f"?notice={quote(notice)}" if notice else ""
    return RedirectResponse(f"/assets/{asset_id}{suffix}", status_code=302)


@router.post("/assets/{asset_id}/detect")
def asset_detect(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404)
    update_asset(db, item, detect=True, actor=user.email, snmp_prober=_snmp_answer)
    notice = getattr(item, "_detect_message", "") or "No exporter detected."
    return RedirectResponse(f"/assets/{asset_id}?notice={quote(notice)}", status_code=302)


@router.post("/assets/{asset_id}/delete")
def asset_delete(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404)
    try:
        result = delete_asset(db, item, actor=user.email)
    except ValueError as exc:
        return RedirectResponse(f"/assets/{asset_id}?notice={quote(str(exc))}", status_code=302)
    unlinked = result.get("unlinked_incidents") or 0
    notice = f"Removed {result['deleted']}."
    if unlinked:
        notice += f" {unlinked} incident(s) stay in History without this host."
    return RedirectResponse(f"/assets?notice={quote(notice)}", status_code=302)


@router.get("/incidents", response_class=HTMLResponse)
def incidents_page(request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    rows = db.query(Incident).order_by(Incident.id.desc()).limit(200).all()
    return render(request, "incidents.html", user, incidents=rows, reported_to=reported_to_for(db, rows))


@router.get("/history", response_class=HTMLResponse)
def history_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    days: str = "90",
    status: str = "",
    asset: str = "",
    number: str = "",
    page: str = "1",
):
    days_n = clamp_days(days)
    try:
        page_n = max(1, int(page))
    except (TypeError, ValueError):
        page_n = 1
    limit = 200
    offset = (page_n - 1) * limit
    rows, total = list_history(
        db,
        days=days_n,
        status=status,
        asset=asset,
        number=number,
        limit=limit,
        offset=offset,
    )
    pages = max(1, (total + limit - 1) // limit)
    return render(
        request,
        "history.html",
        user,
        incidents=rows,
        days=days_n,
        status=status,
        asset=asset,
        number=number,
        page=page_n,
        pages=pages,
        total=total,
        reported_to=reported_to_for(db, rows),
    )


@router.get("/incidents/{number}", response_class=HTMLResponse)
def incident_detail(number: str, request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404)
    investigation = item.investigations[-1] if item.investigations else None
    rca = (investigation.result if investigation else None) or {}
    similar = similar_incident_groups(db, item.asset) if item.asset else []
    pending = llm_job_pending(db, number)
    return render(
            request,
            "incident_detail.html",
            user,
            incident=item,
            investigation=investigation,
            rca=rca,
            similar=similar,
            timeline_json=json.dumps(item.timeline or []),
            engineer=can(user, "read_evidence"),
            mail=notifications_for(db, item),
            audit_rows=audit_for(db, item.number),
            operator_notes=notes_for(db, item),
            llm_pending=pending,
            tools=tool_status(investigation, pending, llm_job_error(db, number)),
            **ops_mail_ctx(db, user, item),
        )


@router.post("/incidents/{number}/status")
def incident_status(
    number: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    status: str = Form(...),
):
    if not can(user, "write_incidents") and not (status == "INVESTIGATING" and can(user, "ack_incidents")):
        if status.upper() not in {"INVESTIGATING"} or not can(user, "ack_incidents"):
            raise HTTPException(status_code=403)
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404)
    item.status = status.upper()
    apply_status_fields(item, item.status, user.email)
    if item.status in {"RESOLVED", "CLOSED"} and item.asset and item.asset.asset_id == "forge-demo-01":
        from app.metrics import reset_demo_gauges

        reset_demo_gauges()
    audit(db, "incident.status", actor=user.email, object_type="incident", object_id=number, data={"status": item.status})
    db.commit()
    return RedirectResponse(f"/incidents/{number}", status_code=302)


@router.post("/incidents/{number}/investigate")
def incident_investigate(
    number: str,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can(user, "investigate") and not can(user, "read_ai"):
        raise HTTPException(status_code=403)
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404)
    from app.services import queue_llm_rewrite, run_investigation

    run_investigation(db, item, actor=user.email, use_llm=False)
    queue_llm_rewrite(db, item, actor=user.email)
    return RedirectResponse(f"/ai/{number}", status_code=303)


@router.post("/incidents/{number}/notes")
def incident_note_page(
    number: str,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    body: str = Form(""),
):
    if not can(user, "write_incidents"):
        raise HTTPException(status_code=403)
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404)
    try:
        add_note(db, item, actor=user.email, body=body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/incidents/{number}#notes", status_code=302)


@router.post("/incidents/{number}/mail")
def incident_send_report(
    number: str,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    target: str = Form(""),
    new_email: str = Form(""),
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404)
    from app.services import send_incident_report

    chosen = (new_email or "").strip() or (target or "").strip()
    try:
        send_incident_report(db, item, chosen, actor=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/incidents/{number}#mail", status_code=303)


@router.get("/ai/{number}", response_class=HTMLResponse)
def ai_page(number: str, request: Request, db: Session = Depends(get_db), user: User = Depends(require_page("read_ai"))):
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404)
    investigation = item.investigations[-1] if item.investigations else None
    rca = (investigation.result if investigation else None) or {}
    pending = llm_job_pending(db, number)
    return render(
        request,
        "ai.html",
        user,
        incident=item,
        investigation=investigation,
        rca=rca,
        engineer=can(user, "read_evidence"),
        llm_pending=pending,
        tools=tool_status(investigation, pending, llm_job_error(db, number)),
        mail=notifications_for(db, item),
        **ops_mail_ctx(db, user, item),
    )


@router.get("/playrules", response_class=HTMLResponse)
def playrules_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_page("read_play"))):
    rows = db.query(Playrule).order_by(Playrule.name).all()
    books = db.query(Playbook).order_by(Playbook.name).all()
    hosts = saved_alarm_hostnames(db.query(Asset).order_by(Asset.hostname).all())
    return render(
        request,
        "playrules.html",
        user,
        playrules=rows,
        playbooks=books,
        presets=PLAYRULE_PRESETS,
        asset_alarm_hosts=hosts,
    )


@router.post("/playrules")
def playrule_create(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
    alertname: str = Form(""),
    metric: str = Form("filesystem_usage"),
    operator: str = Form(">"),
    value: float = Form(80),
    severity: str = Form("warning"),
    playbook_id: int | None = Form(None),
):
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    policy = db.query(EscalationPolicy).filter_by(slug="default-warning").first()
    row = Playrule(
        name=name,
        enabled=True,
        severity=severity,
        condition={"metric": metric, "operator": operator, "value": value, "alertname": (alertname or name).strip()},
        playbook_id=playbook_id or None,
        escalation_policy_id=policy.id if policy else None,
    )
    db.add(row)
    audit(db, "playrule.create", actor=user.email, object_type="playrule", object_id=name)
    db.commit()
    return RedirectResponse("/playrules", status_code=302)


@router.post("/playrules/{rule_id}/toggle")
def playrule_toggle(rule_id: int, db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    row = db.get(Playrule, rule_id)
    if row is None:
        raise HTTPException(status_code=404)
    row.enabled = not row.enabled
    audit(db, "playrule.update", actor=user.email, object_type="playrule", object_id=row.name)
    db.commit()
    return RedirectResponse("/playrules", status_code=302)


@router.get("/playbooks", response_class=HTMLResponse)
def playbooks_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_page("read_play"))):
    rows = db.query(Playbook).order_by(Playbook.name).all()
    return render(request, "playbooks.html", user, playbooks=rows)


@router.post("/playbooks")
def playbook_create(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
    slug: str = Form(...),
    steps: str = Form(""),
):
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    step_rows = []
    for line in steps.splitlines():
        line = line.strip()
        if line:
            step_rows.append({"title": line.lstrip("0123456789.-) ").strip()})
    row = Playbook(name=name, slug=slug, steps=step_rows)
    db.add(row)
    audit(db, "playbook.create", actor=user.email, object_type="playbook", object_id=slug)
    db.commit()
    return RedirectResponse("/playbooks", status_code=302)


@router.get("/escalation", response_class=HTMLResponse)
def escalation_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_page("read_play"))):
    policies = db.query(EscalationPolicy).all()
    notes = db.query(Notification).order_by(Notification.id.desc()).limit(20).all()
    return render(request, "escalation.html", user, policies=policies, notifications=notes)


@router.get("/journal", response_class=HTMLResponse)
def journal_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("read_play")),
    module: str = "",
    status: str = "",
    q: str = "",
):
    rows = list_entries(db, module=module or None, status=status or None, q=q or None, limit=200)
    counts = module_counts(db)
    return render(
        request,
        "journal.html",
        user,
        entries=rows,
        counts=counts,
        modules=MODULES,
        filter_module=module,
        filter_status=status,
        filter_q=q,
    )


@router.get("/health-ui", response_class=HTMLResponse)
def health_page(request: Request, user: User = Depends(login_required)):
    payload = doctor_payload()
    host = request.headers.get("host") or "localhost"
    return render(
        request,
        "health.html",
        user,
        doctor=payload,
        stack=enrich_components(payload.get("components") or {}, host),
        grafana_open=rewrite_host(settings.grafana_public_url, host.split(":")[0]),
    )


@router.post("/health-ui/refresh")
def health_refresh(user: User = Depends(login_required)):
    doctor_payload(force=True)
    return RedirectResponse("/health-ui", status_code=303)


@router.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    from app.services import list_mail_addresses

    mail = db.query(Notification).order_by(Notification.id.desc()).limit(80).all()
    reports = db.query(ScheduledReport).order_by(ScheduledReport.id.desc()).all()
    assets = db.query(Asset).order_by(Asset.hostname).all()
    contacts = db.query(MailContact).order_by(MailContact.email).all()
    return render(
        request,
        "ops.html",
        user,
        mail=mail,
        reports=reports,
        assets=assets,
        contacts=contacts,
        addresses=list_mail_addresses(db),
        smtp_on=settings.email_enabled and bool(settings.smtp_host),
        can_send=can_send_ops(user),
        webmail_url=_webmail_url(request),
        smtp_provider=smtp_provider_id(),
    )


@router.post("/ops/contacts")
def ops_add_contact(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    email: str = Form(...),
    name: str = Form(""),
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    from app.services import remember_mail_contact

    row = remember_mail_contact(db, email, name=name, actor=user.email)
    if row is None:
        raise HTTPException(status_code=400, detail="Need a valid email address")
    audit(db, "mail.contact", actor=user.email, object_type="mail", object_id=row.email, data={"name": row.name})
    db.commit()
    return RedirectResponse("/ops#send", status_code=303)


@router.post("/ops/mail")
def ops_send_mail(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    target: str = Form(""),
    new_email: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    from app.services import remember_mail_contact, send_outbound_mail

    chosen = (new_email or "").strip() or (target or "").strip()
    row = remember_mail_contact(db, chosen, actor=user.email)
    if row is None:
        raise HTTPException(status_code=400, detail="Pick a saved address or enter a new email")
    send_outbound_mail(db, target=row.email, subject=subject, body=body, actor=user.email, step_key="manual")
    return RedirectResponse("/ops#mail", status_code=303)


def _ops_send_report_now(
    db: Session,
    user: User,
    *,
    to_email: str,
    new_email: str,
    asset_id: list[str],
    name: str = "send-now",
):
    from app.services import send_performance_report

    ids = [str(item).strip() for item in (asset_id or []) if str(item).strip()]
    chosen = (new_email or "").strip() or (to_email or "").strip()
    try:
        send_performance_report(
            db,
            asset_ids=ids,
            to_email=chosen,
            actor=user.email,
            name=(name or "").strip() or "send-now",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/ops#mail", status_code=303)


@router.post("/ops/reports")
def ops_create_report(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(""),
    to_email: str = Form(""),
    new_email: str = Form(""),
    interval_hours: str = Form("6"),
    custom_at: str = Form(""),
    asset_id: Annotated[list[str], Form()] = [],
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    from datetime import timedelta

    from app.services import next_custom_report_at, remember_mail_contact, utcnow

    when = (interval_hours or "6").strip().lower()
    if when in {"now", "0"}:
        return _ops_send_report_now(
            db,
            user,
            to_email=to_email,
            new_email=new_email,
            asset_id=asset_id,
            name=name,
        )
    ids = [str(item).strip() for item in (asset_id or []) if str(item).strip()]
    chosen = (new_email or "").strip() or (to_email or "").strip()
    contact = remember_mail_contact(db, chosen, actor=user.email)
    if contact is None:
        raise HTTPException(status_code=400, detail="Pick a saved address or enter a new email")
    if when == "custom":
        hours = 24
        try:
            nxt = next_custom_report_at(custom_at)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        try:
            hours = max(1, min(168, int(when or 6)))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Pick now, 1h, 6h, 24h, or custom") from exc
        nxt = utcnow() + timedelta(hours=hours)
    row = ScheduledReport(
        name=name.strip() or "performance",
        to_email=contact.email,
        interval_hours=hours,
        asset_ids=ids,
        enabled=True,
        created_by=user.email,
        next_run_at=nxt,
    )
    db.add(row)
    audit(db, "report.create", actor=user.email, object_type="report", object_id=row.name, data={"to": row.to_email, "hours": hours})
    db.commit()
    return RedirectResponse("/ops#reports", status_code=303)


@router.post("/ops/reports/send-now")
def ops_send_report_now(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    to_email: str = Form(""),
    new_email: str = Form(""),
    asset_id: Annotated[list[str], Form()] = [],
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    return _ops_send_report_now(db, user, to_email=to_email, new_email=new_email, asset_id=asset_id)


@router.post("/ops/reports/{report_id}/run")
def ops_run_report(
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    from app.services import run_scheduled_report

    row = db.get(ScheduledReport, report_id)
    if row is None:
        raise HTTPException(status_code=404)
    run_scheduled_report(db, row, actor=user.email)
    return RedirectResponse("/ops#reports", status_code=303)


@router.post("/ops/reports/{report_id}/toggle")
def ops_toggle_report(
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    row = db.get(ScheduledReport, report_id)
    if row is None:
        raise HTTPException(status_code=404)
    row.enabled = not bool(row.enabled)
    db.commit()
    return RedirectResponse("/ops#reports", status_code=303)


@router.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    selected: int | None = Query(None),
):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    users = db.query(User).order_by(User.email).all()
    audits = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(30).all()
    chosen = db.get(User, selected) if selected else None
    from app.backup import format_size, list_archives, layout_from_env
    from app.users import delete_blocked, edit_blocked

    lay = layout_from_env()
    return render(
        request,
        "admin.html",
        user,
        users=users,
        audits=audits,
        selected=chosen,
        can_edit_selected=bool(chosen) and not edit_blocked(user, chosen),
        can_delete_selected=bool(chosen) and not delete_blocked(user, chosen),
        backups=list_archives(lay),
        backup_files_writable=lay.files_writable,
        format_size=format_size,
    )


@router.post("/admin/users")
def admin_create_user(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    role: str = Form("analyst"),
):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    from app.journal import report
    from app.users import create_user

    try:
        row = create_user(db, user, email=email, name=name, password=password, role=role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    report(db, "core", "user.create", "ok", summary=f"Created {row.role} {row.email}", object_type="user", object_id=row.email)
    return RedirectResponse(f"/admin?selected={row.id}", status_code=303)


@router.post("/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(""),
    role: str = Form("analyst"),
):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    from app.journal import report
    from app.users import update_user

    try:
        update_user(db, user, target, email=email, name=name, password=password, role=role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    report(db, "core", "user.update", "ok", summary=f"Updated {target.email}", object_type="user", object_id=target.email)
    return RedirectResponse(f"/admin?selected={target.id}", status_code=303)


@router.post("/admin/users/{user_id}/delete")
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    from app.journal import report
    from app.users import delete_user

    try:
        email = delete_user(db, user, target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    report(db, "core", "user.delete", "ok", summary=f"Removed {email}", object_type="user", object_id=email)
    return RedirectResponse("/admin", status_code=303)


def _require_admin(user: User) -> None:
    if not can(user, "admin"):
        raise HTTPException(status_code=403)


@router.post("/admin/backups")
def admin_create_backup(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    include_models: str = Form(""),
    include_secrets: str = Form("1"),
):
    _require_admin(user)
    from app.backup import create_backup
    from app.journal import report

    result = create_backup(include_secrets=include_secrets != "0", include_models=bool(include_models))
    audit(db, "backup.create", actor=user.email, object_type="backup", object_id=result.name, ip=request.client.host if request.client else "")
    report(db, "backup", "create", "ok", summary=f"Wrote {result.name}", object_type="backup", object_id=result.name)
    db.commit()
    return RedirectResponse(f"/admin?backup={quote(result.name)}", status_code=303)


@router.get("/admin/backups/{name}")
def admin_download_backup(
    name: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    _require_admin(user)
    from app.backup import download_name, resolve_archive

    try:
        path = resolve_archive(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="backup not found") from exc
    ident = name if name else download_name(path)
    audit(db, "backup.download", actor=user.email, object_type="backup", object_id=ident, ip=request.client.host if request.client else "", commit=True)
    return FileResponse(
        path,
        filename=download_name(path),
        media_type="application/gzip",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/admin/backups/import")
async def admin_import_backup(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    archive: UploadFile = File(...),
):
    _require_admin(user)
    from app.backup import backup_ident, save_upload
    from app.journal import report

    data = await archive.read()
    try:
        path = save_upload(data, archive.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ident = backup_ident(path)
    audit(db, "backup.import", actor=user.email, object_type="backup", object_id=ident, ip=request.client.host if request.client else "")
    report(db, "backup", "import", "ok", summary=f"Imported {ident}", object_type="backup", object_id=ident)
    db.commit()
    return RedirectResponse(f"/admin?imported={quote(ident)}", status_code=303)


@router.post("/admin/backups/remove")
def admin_remove_backup(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
):
    _require_admin(user)
    from app.backup import delete_backup
    from app.journal import report

    try:
        deleted = delete_backup(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="backup not found") from exc
    ident = deleted.name
    audit(db, "backup.remove", actor=user.email, object_type="backup", object_id=ident, ip=request.client.host if request.client else "")
    report(db, "backup", "remove", "ok", summary=f"Removed {ident}", object_type="backup", object_id=ident)
    db.commit()
    return RedirectResponse(f"/admin?removed={quote(ident)}", status_code=303)


@router.post("/admin/backups/restore")
def admin_restore_backup(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
    confirm: str = Form(""),
    acknowledged: str = Form(""),
):
    _require_admin(user)
    from app.backup import CONFIRM_WORD, backup_ident, restore_archive, resolve_archive
    from app.journal import report

    if acknowledged != "1" or confirm.strip() != CONFIRM_WORD:
        raise HTTPException(
            status_code=400,
            detail="Restore refused. Tick the warning and type RESTORE.",
        )
    try:
        path = resolve_archive(name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        outcome = restore_archive(path, confirm=CONFIRM_WORD, stop_core=False)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ident = backup_ident(path)
    audit(db, "backup.restore", actor=user.email, object_type="backup", object_id=ident, ip=request.client.host if request.client else "")
    report(
        db,
        "backup",
        "restore",
        "ok",
        summary=f"Restored {ident}",
        detail="\n".join(outcome.get("notes") or []),
        object_type="backup",
        object_id=ident,
    )
    db.commit()
    return RedirectResponse(f"/admin?restored={quote(ident)}", status_code=303)


@router.post("/demo")
def demo_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    incident = run_demo(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-rca")
def demo_rca_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    incident = run_demo_rca(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/ai/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-host")
def demo_host_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    incident = run_demo_host(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-windows")
def demo_windows_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    incident = run_demo_windows(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-network")
def demo_network_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    incident = run_demo_network(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-nodecpu")
def demo_nodecpu_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    incident = run_demo_nodecpu(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-reset")
def demo_reset_page(user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    reset_demo_gauges()
    return RedirectResponse("/?demo=1", status_code=303)
