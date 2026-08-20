from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.inventory import (
    approve_candidate,
    create_manual_asset,
    ignore_candidate,
    run_scan,
    similar_incident_groups,
    sync_netbox,
    update_asset,
    is_snmp_asset,
)
from app.journal import MODULES, list_entries, module_counts
from app.models import Asset, AuditLog, DiscoveryCandidate, EscalationPolicy, Incident, Notification, Playbook, Playrule, User
from app.security import can, hash_password, make_session_token, role_label, user_from_session, verify_password
from app.api import doctor_payload
from app.services import run_demo, run_demo_rca, run_investigation
from app.history import (
    add_note,
    apply_status_fields,
    audit_for,
    clamp_days,
    list_history,
    notes_for,
    notifications_for,
)
from app.settings import settings

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
        "grafana_url": settings.grafana_public_url,
        "grafana_enabled": settings.grafana_enabled,
        "ai_enabled": settings.ai_enabled,
    }
    data.update(extra)
    return data


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
    journal_error = list_entries(db, status="error", limit=5)
    journal_recent = list_entries(db, limit=8)
    return render(
        request,
        "dashboard.html",
        user,
        stats=stats,
        doctor=doctor,
        recent=recent,
        journal_error=journal_error,
        journal_recent=journal_recent,
    )


@router.get("/assets", response_class=HTMLResponse)
def assets_page(request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    rows = db.query(Asset).order_by(Asset.hostname).all()
    return render(request, "assets.html", user, assets=rows)


@router.post("/assets")
def asset_create(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    hostname: str = Form(...),
    ip: str = Form(""),
    type: str = Form("Linux Server"),
    environment: str = Form("Production"),
    owner: str = Form("platform"),
    contact_name: str = Form(""),
    owner_email: str = Form(""),
    owner_phone: str = Form(""),
    notes: str = Form(""),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
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
            actor=user.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/assets/{asset.asset_id}", status_code=302)


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
    )


@router.post("/assets/{asset_id}/update")
def asset_update(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    ip: str = Form(""),
    type: str = Form("Linux Server"),
    environment: str = Form("Production"),
    owner: str = Form("platform"),
    contact_name: str = Form(""),
    owner_email: str = Form(""),
    owner_phone: str = Form(""),
    notes: str = Form(""),
    scrape_address: str = Form(""),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404)
    update_asset(
        db,
        item,
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
    )
    return RedirectResponse(f"/assets/{asset_id}", status_code=302)


@router.get("/incidents", response_class=HTMLResponse)
def incidents_page(request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    rows = db.query(Incident).order_by(Incident.id.desc()).limit(200).all()
    return render(request, "incidents.html", user, incidents=rows)


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
    )


@router.get("/incidents/{number}", response_class=HTMLResponse)
def incident_detail(number: str, request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404)
    investigation = item.investigations[-1] if item.investigations else None
    rca = (investigation.result if investigation else None) or {}
    similar = similar_incident_groups(db, item.asset) if item.asset else []
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
    run_investigation(db, item, actor=user.email)
    return RedirectResponse(f"/incidents/{number}#ai", status_code=302)


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


@router.get("/ai/{number}", response_class=HTMLResponse)
def ai_page(number: str, request: Request, db: Session = Depends(get_db), user: User = Depends(require_page("read_ai"))):
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404)
    investigation = item.investigations[-1] if item.investigations else None
    rca = (investigation.result if investigation else None) or {}
    return render(
        request,
        "ai.html",
        user,
        incident=item,
        investigation=investigation,
        rca=rca,
        engineer=can(user, "read_evidence"),
    )


@router.get("/playrules", response_class=HTMLResponse)
def playrules_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_page("read_play"))):
    rows = db.query(Playrule).order_by(Playrule.name).all()
    books = db.query(Playbook).order_by(Playbook.name).all()
    return render(request, "playrules.html", user, playrules=rows, playbooks=books)


@router.post("/playrules")
def playrule_create(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
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
        condition={"metric": metric, "operator": operator, "value": value, "alertname": name},
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
    return render(request, "health.html", user, doctor=doctor_payload())


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    users = db.query(User).order_by(User.email).all()
    audits = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(30).all()
    return render(request, "admin.html", user, users=users, audits=audits)


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
    if role not in {"admin", "engineer", "analyst", "viewer"}:
        raise HTTPException(status_code=400, detail="invalid role")
    db.add(User(email=email, name=name, password_hash=hash_password(password), role=role))
    audit(db, "user.create", actor=user.email, object_type="user", object_id=email)
    db.commit()
    from app.journal import report

    report(db, "core", "user.create", "ok", summary=f"Created {role} {email}", object_type="user", object_id=email)
    return RedirectResponse("/admin", status_code=302)


@router.post("/demo")
def demo_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    incident = run_demo(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=302)


@router.post("/demo-rca")
def demo_rca_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    incident = run_demo_rca(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/ai/{number}" if number else "/incidents", status_code=302)
