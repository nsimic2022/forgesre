from __future__ import annotations

import json
import logging
import os
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.asset_probe import reachability_snapshot
from app.demo_ids import DEMO_CANDIDATE_IP, is_lab_inventory_row
from app.exporter_detect import AUTO_ASSET_TYPE
from app.asset_alarms import alarms_from_form, saved_alarm_hostnames
from app.asset_extras import (
    EXTRA_FIELDS,
    SUPPORT_CHOICES,
    SUPPORT_LEAD_CHOICES,
    SUPPORT_LEAD_DEFAULT,
    asset_playrule_ids,
    extras_from_form,
    extras_rows,
    form_extras,
    playrule_ids_from_form,
    support_status,
)
from app.asset_snmp import AUTH_PROTOCOLS, PRIV_PROTOCOLS, SNMP_VERSIONS, V3_LEVELS, auth_status, snmp_from_form
from app.asset_types import ASSET_TYPE_GROUPS, DEFAULT_SNMP_PORT, snmp_family, snmp_port_for
from app.inventory import (
    ASSET_SOURCE_FILTERS,
    ASSET_TYPE_CHOICES,
    CANDIDATE_ROLE_CHOICES,
    approve_candidate,
    asset_filter_options,
    asset_form_values,
    asset_in_zabbix,
    asset_missing_email,
    asset_tiles,
    asset_type_abbrev,
    assets_matching,
    clone_candidate,
    clone_prefill,
    create_manual_asset,
    delete_asset,
    delete_blocked,
    delete_candidate,
    ignore_candidate,
    set_asset_playrules,
    similar_incident_groups,
    suggest_clone_candidate_ip,
    sync_netbox,
    update_asset,
    update_candidate,
    is_snmp_asset,
    validate_ip_field,
    zabbix_agent_state,
)
from app.journal import MODULES, count_entries, list_entries, module_counts, report
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
from app.netbox import is_local_netbox_url, status_label, sync_cta, token_presence
from app.security import CREATABLE_ROLES, can, distinct_who_name, make_session_token, role_label, user_from_session, verify_password
from app.api import doctor_payload, run_asset_verify, verify_assets
from app.asset_metrics import safe_asset_metric_panel
from app.metrics import reset_demo_gauges
from app.services import (
    format_started_at,
    incident_host,
    incident_short_label,
    incident_source,
    incident_source_label,
    incident_when,
    incident_when_label,
    is_demo_incident,
    is_demo_journal,
    is_demo_mail,
    severity_pill,
    short_when_label,
    parse_policy_steps,
    policy_step_problems,
    policy_steps_text,
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
    ack_circle,
    acknowledge,
    add_note,
    apply_status_fields,
    audit_for,
    clamp_days,
    dashboard_incident_tiles,
    incident_heat,
    incident_list_filters,
    incident_neighbors,
    list_history,
    notes_for,
    notifications_for,
    paginate,
    paginate_query,
    pager_state,
    parse_per_page,
    per_page_param,
    reported_to_for,
)
from app.host_resources import RESOURCE_CRIT_PERCENT, RESOURCE_WARN_PERCENT
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


def _parse_id_query(raw: str) -> int | None:
    text = (raw or "").strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if value > 0 else None


def parse_scheduled_report_form(
    db: Session,
    *,
    name: str,
    to_email: str,
    new_email: str,
    interval_hours: int,
    asset_id: list[str],
    actor: str,
) -> tuple[str, str, int, list[str]]:
    """Name, recipient, interval, asset ids for a scheduled report. SMTP path unchanged."""
    from app.services import remember_mail_contact

    hours = max(1, min(168, int(interval_hours or 6)))
    ids = [str(item).strip() for item in (asset_id or []) if str(item).strip()]
    chosen = (new_email or "").strip() or (to_email or "").strip()
    contact = remember_mail_contact(db, chosen, actor=actor)
    if contact is None:
        raise HTTPException(status_code=400, detail="Pick a saved address or enter a new email")
    return (name.strip() or "performance", contact.email, hours, ids)


def scheduled_report_form_values(row: ScheduledReport | None = None, *, clone: bool = False) -> dict:
    """Prefill the /ops create form for edit or clone. Clone is a draft until Save."""
    if row is None:
        return {"name": "storage-6h", "to_email": "", "interval_hours": 6, "asset_ids": []}
    name = (row.name or "performance").strip() or "performance"
    if clone:
        name = f"{name}-copy"
    return {
        "name": name,
        "to_email": row.to_email or "",
        "interval_hours": max(1, int(row.interval_hours or 6)),
        "asset_ids": list(row.asset_ids or []),
    }


def _snmp_answer(ip: str) -> bool:
    from discovery import probe_snmp_udp

    return bool(probe_snmp_udp(ip))


def _picked_email(picked: str, typed: str) -> str:
    """Address-book pick wins; the "type a new email" box is used when nothing is picked."""
    return (picked or "").strip() or (typed or "").strip()


def _posted_extras(present: str, **values: str) -> dict | None:
    try:
        return extras_from_form(present, **values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _posted_snmp(present: str, previous=None, **values: str) -> dict | None:
    try:
        return snmp_from_form(present, previous, **values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def pager_href(request: Request, page: int, param: str = "page", fragment: str = "") -> str:
    """Keep current filters, replace only the page query key."""
    items = [(key, value) for key, value in request.query_params.multi_items() if key != param]
    items.append((param, str(page)))
    qs = urlencode(items)
    frag = fragment or ""
    if frag and not frag.startswith("#"):
        frag = "#" + frag
    return f"{request.url.path}?{qs}{frag}"


def per_page(request: Request, page_param: str = "page") -> int:
    """Rows per page for one list, from its ?per_page= (or audit_per_page=, …) query key."""
    return parse_per_page(request.query_params.get(per_page_param(page_param)))


def pager_keep(pager: dict | None, *, page: bool = True) -> str:
    """'&page=2&per_page=20' tail for Edit/Clone/filter links so they land on the same slice. Empty when default."""
    if not pager:
        return ""
    items = []
    if page and int(pager.get("page") or 1) > 1:
        items.append((pager["param"], str(pager["page"])))
    if pager.get("size") and pager["size"] != pager.get("default_size"):
        items.append((pager["size_param"], str(pager["size"])))
    return "&" + urlencode(items) if items else ""


EVIDENCE_SOURCE_LABELS = {
    "alertmanager": "Alertmanager",
    "forgesre": "ForgeSRE",
    "prometheus": "Prometheus",
    "loki": "Loki",
    "zabbix": "Zabbix",
}
EVIDENCE_SUMMARY_MAX = 240


def _evidence_brief(value) -> str:
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if isinstance(item, dict):
                item = "{…}" if item else "{}"
            elif isinstance(item, list):
                item = f"[{len(item)}]"
            parts.append(f"{key}={item}")
        return ", ".join(parts)
    if isinstance(value, list):
        return f"{len(value)} item{'' if len(value) == 1 else 's'}"
    text = str(value if value is not None else "").strip()
    return text.splitlines()[0] if text else ""


def evidence_row(ev) -> dict:
    """One Engineer evidence row: When / Source / short Action, plus the full stored payload for the expanded view. Display only."""
    payload = ev.payload if ev.payload is not None else {}
    body = payload.get("content", payload) if isinstance(payload, dict) and "evidence_id" in payload else payload
    brief = _evidence_brief(body)
    action = f"{ev.title} · {brief}" if brief else (ev.title or ev.kind or "")
    if len(action) > EVIDENCE_SUMMARY_MAX:
        action = action[: EVIDENCE_SUMMARY_MAX - 1].rstrip() + "…"
    source = (ev.source or "").strip()
    lines = []
    if ev.evidence_id:
        lines.append(f"ID: {ev.evidence_id}")
    if ev.query:
        lines.append(f"Query: {ev.query}")
    dump = payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    lines.append(dump)
    return {
        "anchor": ev.evidence_id or ev.kind or "",
        "when": short_when_label(ev.captured_at),
        "when_full": format_started_at(ev.captured_at),
        "actor": EVIDENCE_SOURCE_LABELS.get(source.lower(), source) or "ForgeRCA",
        "action": action,
        "full": "\n".join(lines),
    }


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
log = logging.getLogger("forgesre")


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
        "incident_short_label": incident_short_label,
        "incident_when_label": incident_when_label,
        "incident_when": incident_when,
        "incident_host": incident_host,
        "format_started_at": format_started_at,
        "short_when_label": short_when_label,
        "severity_pill": severity_pill,
        "is_demo_incident": is_demo_incident,
        "incident_source": incident_source,
        "incident_source_label": incident_source_label,
        "is_demo_mail": is_demo_mail,
        "is_demo_journal": is_demo_journal,
        "asset_type_abbrev": asset_type_abbrev,
        "demo_candidate_ip": DEMO_CANDIDATE_IP,
        "llm_timeout": settings.llm_timeout,
        "pager_href": pager_href,
        "pager_keep": pager_keep,
        "ack_circle": ack_circle,
        "extras_rows": extras_rows,
        "support_status": support_status,
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
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    page: str = "1",
    journal_page: str = "1",
):
    asset_rows = asset_tiles(db.query(Asset).all())
    incident_rows = dashboard_incident_tiles(db)
    size = per_page(request)
    recent, total = list_history(db, days=None, open_only=False, limit=size, page=page)
    pager = pager_state(page, total=total, size=size)
    journal_recent: list = []
    journal_pager = None
    if can(user, "read_play"):
        journal_pager = pager_state(
            journal_page,
            total=count_entries(db),
            size=per_page(request, "journal_page"),
            param="journal_page",
            fragment="#journal",
        )
        journal_recent = list_entries(db, limit=journal_pager["size"], offset=journal_pager["offset"])
    stack = enrich_components(doctor_payload().get("components") or {}, request.headers.get("host") or "localhost")
    return render(
        request,
        "dashboard.html",
        user,
        stack=stack,
        asset_tiles=asset_rows,
        incident_tiles=incident_rows,
        incident_heat=incident_heat(incident_rows),
        resource_warn=RESOURCE_WARN_PERCENT,
        resource_crit=RESOURCE_CRIT_PERCENT,
        recent=recent,
        journal_recent=journal_recent,
        journal_pager=journal_pager,
        pager=pager,
    )


@router.get("/assets", response_class=HTMLResponse)
def assets_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    edit: str = "",
    clone: str = "",
    q: str = "",
    status: str = "",
    flag: str = "",
    source: str = "",
    agent: str = "",
    asset_type: str = Query("", alias="type"),
    site: str = "",
    customer: str = "",
    vlan: str = "",
    page: str = "1",
):
    from app.services import list_mail_addresses

    every = db.query(Asset).order_by(Asset.number, Asset.hostname).all()
    zabbix_assets = any(asset_in_zabbix(row) for row in every)
    rows = assets_matching(every, q, status, flag, source, agent, type=asset_type, site=site, customer=customer, vlan=vlan)
    rows, pager = paginate(rows, page, size=per_page(request))
    filters = {
        "status": status,
        "flag": flag,
        "source": source,
        "agent": agent,
        "type": asset_type,
        "site": site,
        "vlan": vlan,
        "customer": customer,
    }
    filter_tail = "".join(f"&{urlencode({key: value})}" for key, value in filters.items() if value)
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
        type_groups=ASSET_TYPE_GROUPS,
        snmp_family=snmp_family,
        default_snmp_port=DEFAULT_SNMP_PORT,
        mail_addresses=list_mail_addresses(db),
        delete_blocked=delete_blocked,
        notice=notice,
        q=q,
        status=status,
        flag=flag,
        source=source,
        agent=agent,
        asset_type=asset_type,
        site=site,
        vlan=vlan,
        customer=customer,
        filter_tail=filter_tail,
        source_filters=ASSET_SOURCE_FILTERS,
        filter_options=asset_filter_options(every),
        zabbix_filters=zabbix_assets or settings.zabbix_enabled,
        zabbix_agent_state=zabbix_agent_state,
        asset_missing_email=asset_missing_email,
        reachability_snapshot=reachability_snapshot,
        pager=pager,
        extra_fields=EXTRA_FIELDS,
        playrule_choices=playrule_choices(db),
        support_choices=SUPPORT_CHOICES,
        support_lead_choices=SUPPORT_LEAD_CHOICES,
        support_lead_default=SUPPORT_LEAD_DEFAULT,
        snmp_versions=SNMP_VERSIONS,
        snmp_v3_levels=V3_LEVELS,
        snmp_auth_protocols=AUTH_PROTOCOLS,
        snmp_priv_protocols=PRIV_PROTOCOLS,
    )


def playrule_choices(db: Session) -> list[dict]:
    """Client playrule picker rows: every playrule with its alertname and playbook. Read-only."""
    rows = db.query(Playrule).order_by(Playrule.name).all()
    return [
        {
            "id": rule.id,
            "name": rule.name,
            "alertname": str((rule.condition or {}).get("alertname") or ""),
            "enabled": bool(rule.enabled),
            "playbook": rule.playbook.name if rule.playbook else "",
        }
        for rule in rows
    ]


def asset_playrules(db: Session, asset: Asset | None) -> list[Playrule]:
    """Client playrules saved on this asset, in saved order. Missing ids are skipped."""
    from app.asset_extras import asset_playrule_ids

    ids = asset_playrule_ids(asset)
    if not ids:
        return []
    by_id = {rule.id: rule for rule in db.query(Playrule).filter(Playrule.id.in_(ids)).all()}
    return [by_id[pk] for pk in ids if pk in by_id]


@router.get("/assets/verify", response_class=HTMLResponse)
def assets_verify_all(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("write_assets")),
    demo: str = "",
    page: str = "1",
):
    include_demo = demo.strip().lower() in {"1", "true", "yes", "demo"}
    assets = db.query(Asset).order_by(Asset.hostname).all()
    chosen = [asset for asset in assets if include_demo or not is_lab_inventory_row(asset)]
    skipped_demo = len(assets) - len(chosen)
    chosen, pager = paginate(chosen, page, size=per_page(request))
    reports = verify_assets(db, chosen)
    return render(
        request,
        "assets_verify.html",
        user,
        reports=reports,
        skipped_demo=skipped_demo,
        include_demo=include_demo,
        pager=pager,
    )


@router.post("/assets")
def asset_create(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    hostname: str = Form(...),
    asset_id: str = Form(""),
    ip: str = Form(""),
    type: str = Form(AUTO_ASSET_TYPE),
    environment: str = Form("Production"),
    owner: str = Form("platform"),
    contact_name: str = Form(""),
    owner_email: str = Form(""),
    owner_phone: str = Form(""),
    notes: str = Form(""),
    scrape_address: str = Form(""),
    snmp_port: str = Form(""),
    comms_present: str = Form(""),
    snmp_version: str = Form(""),
    snmp_community_mode: str = Form(""),
    snmp_community: str = Form(""),
    snmp_v3_user: str = Form(""),
    snmp_v3_level: str = Form(""),
    snmp_v3_auth_proto: str = Form(""),
    snmp_v3_auth_pass: str = Form(""),
    snmp_v3_priv_proto: str = Form(""),
    snmp_v3_priv_pass: str = Form(""),
    owner_email_pick: str = Form(""),
    extra_backup_email_pick: str = Form(""),
    clone_of: str = Form(""),
    alarms_present: str = Form(""),
    alarm_up_enabled: str = Form(""),
    alarm_cpu_enabled: str = Form(""),
    alarm_cpu_threshold: str = Form(""),
    alarm_memory_enabled: str = Form(""),
    alarm_memory_threshold: str = Form(""),
    alarm_disk_enabled: str = Form(""),
    alarm_disk_threshold: str = Form(""),
    extras_present: str = Form(""),
    extra_customer: str = Form(""),
    extra_site: str = Form(""),
    extra_vlan: str = Form(""),
    extra_backup_name: str = Form(""),
    extra_backup_phone: str = Form(""),
    extra_backup_email: str = Form(""),
    extra_support_hours: str = Form(""),
    extra_timezone: str = Form(""),
    extra_contract: str = Form(""),
    extra_runbook_note: str = Form(""),
    extra_support: str = Form(""),
    extra_support_custom: str = Form(""),
    extra_support_from: str = Form(""),
    extra_support_to: str = Form(""),
    extra_support_lead_days: str = Form(""),
    playrules_present: str = Form(""),
    playrule_ids: list[str] = Form([]),
    playrule_add: str = Form(""),
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
    owner_email = _picked_email(owner_email_pick, owner_email)
    posted_extras = _posted_extras(
        extras_present,
        customer=extra_customer,
        site=extra_site,
        vlan=extra_vlan,
        backup_name=extra_backup_name,
        backup_phone=extra_backup_phone,
        backup_email=_picked_email(extra_backup_email_pick, extra_backup_email),
        support_hours=extra_support_hours,
        timezone=extra_timezone,
        contract=extra_contract,
        runbook_note=extra_runbook_note,
        support=extra_support,
        support_custom=extra_support_custom,
        support_from=extra_support_from,
        support_to=extra_support_to,
        support_lead_days=extra_support_lead_days,
    )
    posted_playrules = playrule_ids_from_form(playrules_present, playrule_ids, playrule_add)
    clone_row = db.query(Asset).filter_by(asset_id=cloned_from).first() if cloned_from else None
    try:
        posted_snmp = snmp_from_form(
            comms_present,
            clone_row.extras if clone_row is not None else None,
            version=snmp_version,
            community_mode=snmp_community_mode,
            community=snmp_community,
            v3_user=snmp_v3_user,
            v3_level=snmp_v3_level,
            v3_auth_proto=snmp_v3_auth_proto,
            v3_auth_pass=snmp_v3_auth_pass,
            v3_priv_proto=snmp_v3_priv_proto,
            v3_priv_pass=snmp_v3_priv_pass,
        )
        asset = create_manual_asset(
            db,
            hostname=hostname,
            asset_id=asset_id,
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
            extras=posted_extras,
            playrule_ids=posted_playrules,
            snmp_port=snmp_port,
            snmp=posted_snmp,
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
def discovery_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("write_assets")),
    page: str = "1",
    edit: str = "",
    clone: str = "",
):
    from discovery import suggested_management_cidr
    from app.jobs import active_discovery_scan

    rows = db.query(DiscoveryCandidate).order_by(DiscoveryCandidate.id.desc()).all()
    rows = sorted(
        rows,
        key=lambda row: (
            0 if (getattr(row, "source", "") == "demo" or row.ip == DEMO_CANDIDATE_IP) else 1,
            -(row.id or 0),
        ),
    )
    pending = [row for row in rows if row.status == "new"]
    rows, pager = paginate(rows, page, size=per_page(request))
    token = settings.netbox_token
    netbox_sync = sync_cta(settings.netbox_url, token, settings.netbox_enabled)
    form_mode = ""
    selected = None
    form = {"ip": "", "hostname": "", "proposed_role": "Unknown device"}
    notice = request.query_params.get("notice") or ""
    if edit.strip().isdigit():
        selected = db.get(DiscoveryCandidate, int(edit.strip()))
        if selected is not None:
            form_mode = "edit"
            form = {
                "ip": selected.ip,
                "hostname": selected.hostname or "",
                "proposed_role": selected.proposed_role or "Unknown device",
            }
    elif clone.strip().isdigit():
        source = db.get(DiscoveryCandidate, int(clone.strip()))
        if source is not None:
            selected = source
            form_mode = "clone"
            form = {
                "ip": suggest_clone_candidate_ip(db, source.ip),
                "hostname": source.hostname or "",
                "proposed_role": source.proposed_role or "Unknown device",
            }
    saved_cidrs = list(settings.discovery_cidrs)
    suggested = suggested_management_cidr()
    cidr_prefill = ", ".join(saved_cidrs) if saved_cidrs else (suggested or "")
    return render(
        request,
        "discovery.html",
        user,
        candidates=rows,
        pending=pending,
        form_mode=form_mode,
        selected=selected,
        form=form,
        role_choices=CANDIDATE_ROLE_CHOICES,
        notice=notice,
        discovery_enabled=settings.discovery_enabled,
        discovery_mode=settings.discovery_mode,
        discovery_cidrs=saved_cidrs,
        discovery_has_cidrs=bool(saved_cidrs),
        discovery_warnings=[],
        suggested_cidr=suggested or "",
        cidr_prefill=cidr_prefill,
        netbox_enabled=settings.netbox_enabled,
        netbox_auto_sync=settings.netbox_auto_sync,
        netbox_url=settings.netbox_url,
        netbox_url_is_local=is_local_netbox_url(settings.netbox_url),
        netbox_token_present=token_presence(token),
        netbox_sync_ready=bool(netbox_sync.get("ready")),
        netbox_sync_clickable=bool(netbox_sync.get("clickable", netbox_sync.get("ready"))),
        netbox_sync_light=str(netbox_sync.get("light") or "grey"),
        netbox_sync_label=str(
            netbox_sync.get("label")
            or status_label(
                str(netbox_sync.get("light") or "grey"),
                str(netbox_sync.get("why") or ""),
            )
        ),
        netbox_sync_count=int(netbox_sync.get("count") or 0),
        netbox_sync_why=str(netbox_sync.get("why") or ""),
        zabbix=zabbix_discovery_ctx(db),
        demo_candidate_ip=DEMO_CANDIDATE_IP,
        scan_job=active_discovery_scan(db),
        pager=pager,
    )


def zabbix_discovery_ctx(db: Session) -> dict:
    """Discovery Zabbix block: cached status chip, last sync / last error. Never blocks on a dead Zabbix twice."""
    from app.zabbix import NOT_CONFIGURED_WHY
    from app.zabbix_sync import current_status, last_sync_entry

    enabled = settings.zabbix_enabled
    if enabled:
        try:
            status = current_status()
        except Exception as exc:
            status = {"configured": True, "ok": False, "light": "grey", "label": "API error", "why": str(exc)[:300]}
    else:
        status = {"configured": False, "ok": False, "light": "grey", "label": "Not configured", "why": NOT_CONFIGURED_WHY}
    last = last_sync_entry(db) if enabled else None
    last_error = ""
    last_ok = ""
    if last is not None:
        stamp = short_when_label(last.at)
        if last.status == "error":
            last_error = f"{stamp} — {last.detail or last.summary}"
        else:
            last_ok = f"{stamp} — {last.summary}"
    return {
        "enabled": enabled,
        "url": settings.zabbix_url,
        "auto_sync": settings.zabbix_auto_sync,
        "webhook_on": bool(settings.zabbix_webhook_token),
        "light": str(status.get("light") or "grey"),
        "label": str(status.get("label") or "Not configured"),
        "why": "" if status.get("light") == "green" else str(status.get("why") or ""),
        "hosts": int(status.get("hosts") or 0),
        "clickable": bool(enabled),
        "last_ok": last_ok,
        "last_error": last_error,
    }


@router.post("/discovery/zabbix-sync")
def discovery_zabbix_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    from app.zabbix_sync import sync_zabbix

    result = sync_zabbix(db, force=True) or {}
    if result.get("error"):
        notice = f"Zabbix sync failed: {result['error']}"
    elif result.get("skipped"):
        notice = "Zabbix is not configured (ZABBIX_URL + ZABBIX_API_TOKEN in secrets/secrets.env)."
    else:
        notice = (
            f"Zabbix sync: {int(result.get('created') or 0)} new, {int(result.get('linked') or 0)} linked, "
            f"{int(result.get('skipped') or 0)} skipped. New hosts are Auto type with no scrape — "
            "set Type / owner email on Assets."
        )
    return RedirectResponse(f"/discovery?notice={quote(notice)}", status_code=302)


@router.post("/discovery/scan")
def discovery_scan_page(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    cidrs: str = Form(""),
    confirm: str = Form(""),
):
    """Confirm CIDRs and/or queue a background probe. Never call run_scan inline."""
    notice = "Scan queued. Candidates appear when the background job finishes."
    try:
        if not can(user, "write_assets"):
            raise HTTPException(status_code=403)
        from discovery import normalize_cidrs
        from app.jobs import enqueue_discovery_scan, active_discovery_scan

        want_confirm = (confirm or "").strip().lower() in {
            "1", "true", "yes", "on", "confirm", "save",
        }
        parsed = normalize_cidrs(cidrs)
        if want_confirm:
            if not parsed:
                return RedirectResponse(
                    f"/discovery?notice={quote('Confirm a management CIDR first (suggested /24 from this VM). Empty cidrs = no scan.')}",
                    status_code=302,
                )
            persist_note = ""
            try:
                settings.set_discovery_cidrs(parsed)
            except OSError as exc:
                log.exception("discovery.cidrs persist failed")
                persist_note = (
                    f" Could not write config/forgesre.yml ({type(exc).__name__}); "
                    "scan still queued with in-memory CIDRs."
                )
            already = active_discovery_scan(db) is not None
            job = enqueue_discovery_scan(
                db, actor=user.email, cidrs=parsed, saved=True,
            )
            audit(
                db,
                "discovery.scan",
                actor=user.email,
                data={
                    "queued": True,
                    "job_id": job.id if job else None,
                    "cidrs": parsed,
                    "confirm": True,
                    "already": already,
                },
                commit=True,
            )
            if already:
                notice = "Confirmed discovery.cidrs; scan already queued."
            else:
                notice = (
                    f"Confirmed discovery.cidrs={', '.join(parsed)}; "
                    "scan queued. Candidates appear when the background job finishes."
                )
            if persist_note:
                notice = notice + persist_note
            return RedirectResponse(f"/discovery?notice={quote(notice)}", status_code=302)

        saved = list(settings.discovery_cidrs)
        if not saved:
            return RedirectResponse(
                f"/discovery?notice={quote('No discovery.cidrs yet. Confirm the suggested management /24 first.')}",
                status_code=302,
            )
        already = active_discovery_scan(db) is not None
        job = enqueue_discovery_scan(
            db, actor=user.email, cidrs=saved, saved=False,
        )
        audit(
            db,
            "discovery.scan",
            actor=user.email,
            data={
                "queued": True,
                "job_id": job.id if job else None,
                "cidrs": saved,
                "confirm": False,
                "already": already,
            },
            commit=True,
        )
        if already:
            notice = "Scan already queued. It will use the latest saved CIDRs."
        else:
            notice = "Scan queued. Candidates appear when the background job finishes."
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("discovery scan POST failed")
        report(
            db,
            "discovery",
            "scan",
            "error",
            summary="Could not queue discovery scan",
            detail=str(exc),
        )
        notice = f"Could not queue scan ({type(exc).__name__}). Core stayed up — see Journal."
    return RedirectResponse(f"/discovery?notice={quote(notice)}", status_code=302)




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
    suffix = ""
    if asset_missing_email(asset):
        notice = "Approved. Add an owner email (Edit) so escalation mail has a recipient."
        suffix = f"?notice={quote(notice)}"
    return RedirectResponse(f"/assets/{asset.asset_id}{suffix}", status_code=302)


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


@router.post("/discovery/{candidate_id}/update")
def discovery_update_page(
    candidate_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    ip: str = Form(...),
    hostname: str = Form(""),
    proposed_role: str = Form(""),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    row = db.get(DiscoveryCandidate, candidate_id)
    if row is None:
        raise HTTPException(status_code=404)
    try:
        update_candidate(db, row, ip=ip, hostname=hostname, proposed_role=proposed_role, actor=user.email)
    except ValueError as exc:
        return RedirectResponse(
            f"/discovery?edit={candidate_id}&notice={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse("/discovery", status_code=302)


@router.post("/discovery/{candidate_id}/clone")
def discovery_clone_page(
    candidate_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    ip: str = Form(...),
    hostname: str = Form(""),
    proposed_role: str = Form(""),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    row = db.get(DiscoveryCandidate, candidate_id)
    if row is None:
        raise HTTPException(status_code=404)
    try:
        clone_candidate(db, row, ip=ip, hostname=hostname, proposed_role=proposed_role, actor=user.email)
    except ValueError as exc:
        return RedirectResponse(
            f"/discovery?clone={candidate_id}&notice={quote(str(exc))}",
            status_code=302,
        )
    return RedirectResponse("/discovery", status_code=302)


@router.post("/discovery/{candidate_id}/delete")
def discovery_delete_page(
    candidate_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    row = db.get(DiscoveryCandidate, candidate_id)
    if row is None:
        raise HTTPException(status_code=404)
    delete_candidate(db, row, actor=user.email)
    return RedirectResponse("/discovery", status_code=302)


@router.post("/discovery/netbox-sync")
def discovery_netbox_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    result = sync_netbox(db) or {}
    if result.get("error"):
        notice = f"NetBox sync failed: {result['error']}"
    elif result.get("skipped"):
        notice = "NetBox sync is off in config."
    else:
        notice = (
            f"NetBox sync: {int(result.get('created') or 0)} new, {int(result.get('linked') or 0)} already linked. "
            "New devices are Auto type with no scrape — set Type / owner email on Assets."
        )
    return RedirectResponse(f"/discovery?notice={quote(notice)}", status_code=302)


@router.get("/assets/{asset_id}", response_class=HTMLResponse)
def asset_detail(asset_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404)
    related = db.query(Incident).filter_by(asset_id=item.id).order_by(Incident.id.desc()).all()
    similar = similar_incident_groups(db, item)
    related, pager = paginate(related, request.query_params.get("page", "1"), size=per_page(request))
    similar, similar_pager = paginate(
        similar,
        request.query_params.get("similar_page", "1"),
        size=per_page(request, "similar_page"),
        param="similar_page",
    )
    return render(
        request,
        "asset_detail.html",
        user,
        asset=item,
        incidents=related,
        similar=similar,
        snmp_target=is_snmp_asset(item),
        snmp_port=snmp_port_for(item),
        snmp_enabled=settings.snmp_enabled,
        can_remove=can(user, "write_assets") and not delete_blocked(item),
        remove_blocked=delete_blocked(item) if can(user, "write_assets") else "",
        reachability_snapshot=reachability_snapshot,
        metrics=safe_asset_metric_panel(item),
        pager=pager,
        similar_pager=similar_pager,
        playrule_choices=playrule_choices(db),
        picked_playrules=asset_playrule_ids(item),
        ex=form_extras(item),
        support_labels=dict(SUPPORT_CHOICES),
        snmp_auth=auth_status(item),
    )


@router.post("/assets/{asset_id}/playrules")
def asset_playrules_update(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    playrules_present: str = Form(""),
    playrule_ids: list[str] = Form([]),
    playrule_add: str = Form(""),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404)
    posted = playrule_ids_from_form(playrules_present or "1", playrule_ids, playrule_add)
    set_asset_playrules(db, item, posted, actor=user.email)
    count = len(item.playrule_ids or [])
    notice = f"Client playrules saved ({count})." if count else "Client playrules cleared — global Playrules apply."
    return RedirectResponse(f"/assets/{asset_id}?notice={quote(notice)}#client-playrules", status_code=302)



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
    snmp_port: str = Form(""),
    comms_present: str = Form(""),
    snmp_version: str = Form(""),
    snmp_community_mode: str = Form(""),
    snmp_community: str = Form(""),
    snmp_v3_user: str = Form(""),
    snmp_v3_level: str = Form(""),
    snmp_v3_auth_proto: str = Form(""),
    snmp_v3_auth_pass: str = Form(""),
    snmp_v3_priv_proto: str = Form(""),
    snmp_v3_priv_pass: str = Form(""),
    owner_email_pick: str = Form(""),
    extra_backup_email_pick: str = Form(""),
    alarms_present: str = Form(""),
    alarm_up_enabled: str = Form(""),
    alarm_cpu_enabled: str = Form(""),
    alarm_cpu_threshold: str = Form(""),
    alarm_memory_enabled: str = Form(""),
    alarm_memory_threshold: str = Form(""),
    alarm_disk_enabled: str = Form(""),
    alarm_disk_threshold: str = Form(""),
    extras_present: str = Form(""),
    extra_customer: str = Form(""),
    extra_site: str = Form(""),
    extra_vlan: str = Form(""),
    extra_backup_name: str = Form(""),
    extra_backup_phone: str = Form(""),
    extra_backup_email: str = Form(""),
    extra_support_hours: str = Form(""),
    extra_timezone: str = Form(""),
    extra_contract: str = Form(""),
    extra_runbook_note: str = Form(""),
    extra_support: str = Form(""),
    extra_support_custom: str = Form(""),
    extra_support_from: str = Form(""),
    extra_support_to: str = Form(""),
    extra_support_lead_days: str = Form(""),
    playrules_present: str = Form(""),
    playrule_ids: list[str] = Form([]),
    playrule_add: str = Form(""),
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
    owner_email = _picked_email(owner_email_pick, owner_email)
    posted_extras = _posted_extras(
        extras_present,
        customer=extra_customer,
        site=extra_site,
        vlan=extra_vlan,
        backup_name=extra_backup_name,
        backup_phone=extra_backup_phone,
        backup_email=_picked_email(extra_backup_email_pick, extra_backup_email),
        support_hours=extra_support_hours,
        timezone=extra_timezone,
        contract=extra_contract,
        runbook_note=extra_runbook_note,
        support=extra_support,
        support_custom=extra_support_custom,
        support_from=extra_support_from,
        support_to=extra_support_to,
        support_lead_days=extra_support_lead_days,
    )
    posted_playrules = playrule_ids_from_form(playrules_present, playrule_ids, playrule_add)
    posted_snmp = _posted_snmp(
        comms_present,
        item.extras,
        version=snmp_version,
        community_mode=snmp_community_mode,
        community=snmp_community,
        v3_user=snmp_v3_user,
        v3_level=snmp_v3_level,
        v3_auth_proto=snmp_v3_auth_proto,
        v3_auth_pass=snmp_v3_auth_pass,
        v3_priv_proto=snmp_v3_priv_proto,
        v3_priv_pass=snmp_v3_priv_pass,
    )
    try:
        validate_ip_field(ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        extras=posted_extras,
        playrule_ids=posted_playrules,
        snmp_port=snmp_port if comms_present.strip() else None,
        snmp=posted_snmp,
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
def incidents_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    open_filter: str = Query("", alias="open"),
    status: str = "",
    severity: str = "",
    days: str = "",
    page: str = "1",
):
    filters = incident_list_filters(status=status, severity=severity, open_filter=open_filter, days=days)
    size = per_page(request)
    rows, total = list_history(db, **filters["query"], limit=size, page=page)
    pager = pager_state(page, total=total, size=size)
    return render(
        request,
        "incidents.html",
        user,
        incidents=rows,
        reported_to=reported_to_for(db, rows),
        open_only=filters["query"]["open_only"],
        status_group=filters["status_group"],
        severity_group=filters["severity_group"],
        days=filters["days"],
        pager=pager,
        nav_qs=filters["qs"],
    )


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
    size = per_page(request)
    rows, total = list_history(
        db,
        days=days_n,
        status=status,
        asset=asset,
        number=number,
        limit=size,
        page=page,
    )
    pager = pager_state(page, total=total, size=size)
    return render(
        request,
        "history.html",
        user,
        incidents=rows,
        days=days_n,
        status=status,
        asset=asset,
        number=number,
        pager=pager,
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
    audit_rows, audit_pager = paginate(
        audit_for(db, item.number),
        request.query_params.get("audit_page", "1"),
        size=per_page(request, "audit_page"),
        param="audit_page",
        fragment="#audit",
    )
    notes, notes_pager = paginate(
        notes_for(db, item),
        request.query_params.get("notes_page", "1"),
        size=per_page(request, "notes_page"),
        param="notes_page",
    )
    q = request.query_params
    filters = incident_list_filters(
        status=q.get("status", ""), severity=q.get("severity", ""), open_filter=q.get("open", ""), days=q.get("days", "")
    )
    evidence, evidence_pager = paginate(
        [evidence_row(ev) for ev in sorted(item.evidence, key=lambda ev: ev.id or 0)] if can(user, "read_evidence") else [],
        q.get("ev_page", "1"),
        size=per_page(request, "ev_page"),
        param="ev_page",
        fragment="#evidence",
    )
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
            audit_rows=audit_rows,
            operator_notes=notes,
            llm_pending=pending,
            tools=tool_status(investigation, pending, llm_job_error(db, number)),
            audit_pager=audit_pager,
            notes_pager=notes_pager,
            nav=incident_neighbors(db, item, filters),
            evidence_rows=evidence,
            evidence_pager=evidence_pager,
            client_playrules=asset_playrules(db, item.asset),
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
    if status.upper() == "INVESTIGATING":
        acknowledge(item, user.email)
        data = {"status": item.status, "ack": True}
    else:
        item.status = status.upper()
        apply_status_fields(item, item.status, user.email)
        data = {"status": item.status}
        if item.status in {"RESOLVED", "CLOSED"} and item.asset and item.asset.asset_id == "forge-demo-01":
            from app.metrics import reset_demo_gauges

            reset_demo_gauges()
    audit(db, "incident.status", actor=user.email, object_type="incident", object_id=number, data=data)
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
    return RedirectResponse(f"/incidents/{number}", status_code=303)


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


def playrule_form_values(rule: Playrule | None = None) -> dict:
    condition = dict(rule.condition or {}) if rule is not None else {}
    return {
        "name": rule.name if rule is not None else "",
        "alertname": str(condition.get("alertname") or ""),
        "metric": str(condition.get("metric") or ""),
        "operator": str(condition.get("operator") or ""),
        "value": "" if condition.get("value") in (None, "") else str(condition.get("value")),
        "severity": (rule.severity if rule is not None else "warning") or "warning",
        "playbook_id": rule.playbook_id if rule is not None else None,
        "escalation_policy_id": rule.escalation_policy_id if rule is not None else None,
    }


def playrule_condition(alertname: str, name: str, metric: str, operator: str, value: str) -> dict:
    """alertname is the only matched key. metric/operator/value are a free-text note, never evaluated."""
    condition: dict = {"alertname": (alertname or name).strip()}
    note_value: float | str | None = None
    raw = (value or "").strip()
    if raw:
        try:
            note_value = float(raw)
        except ValueError:
            note_value = raw
    if (metric or "").strip():
        condition["metric"] = metric.strip()
        condition["operator"] = (operator or ">").strip()
        if note_value is not None:
            condition["value"] = note_value
    return condition


def _playrule_policy(db: Session, escalation_policy_id: int | None) -> EscalationPolicy | None:
    policy = db.get(EscalationPolicy, escalation_policy_id) if escalation_policy_id else None
    if policy is None:
        policy = db.query(EscalationPolicy).filter_by(slug="default-warning").first()
    return policy


def _optional_id(raw: str) -> int | None:
    text = (raw or "").strip()
    return int(text) if text.isdigit() else None


@router.get("/playrules", response_class=HTMLResponse)
def playrules_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("read_play")),
    page: str = "1",
    edit: str = "",
):
    from app.alert_rules import load_alert_rules, rules_for

    rows = db.query(Playrule).order_by(Playrule.name).all()
    rows, pager = paginate(rows, page, size=per_page(request))
    books = db.query(Playbook).order_by(Playbook.name).all()
    hosts = saved_alarm_hostnames(db.query(Asset).order_by(Asset.hostname).all())
    policies = db.query(EscalationPolicy).order_by(EscalationPolicy.name).all()
    alert_rules = load_alert_rules()
    selected = db.get(Playrule, int(edit)) if edit.strip().isdigit() else None
    form = playrule_form_values(selected)
    return render(
        request,
        "playrules.html",
        user,
        playrules=rows,
        playbooks=books,
        presets=PLAYRULE_PRESETS,
        asset_alarm_hosts=hosts,
        policies=policies,
        pager=pager,
        alert_rules=alert_rules,
        rules_for=lambda name: rules_for(name, alert_rules),
        selected=selected,
        form=form,
        form_mode="edit" if selected is not None else "create",
        notice=request.query_params.get("notice") or "",
    )


@router.post("/playrules")
def playrule_create(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
    alertname: str = Form(""),
    metric: str = Form(""),
    operator: str = Form(">"),
    value: str = Form(""),
    severity: str = Form("warning"),
    playbook_id: str = Form(""),
    escalation_policy_id: str = Form(""),
):
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    clean = name.strip()
    if not clean:
        raise HTTPException(status_code=400, detail="name required")
    if db.query(Playrule).filter_by(name=clean).first() is not None:
        return RedirectResponse(f"/playrules?notice={quote('A playrule with that name already exists.')}", status_code=302)
    policy = _playrule_policy(db, _optional_id(escalation_policy_id))
    row = Playrule(
        name=clean,
        enabled=True,
        severity=severity,
        condition=playrule_condition(alertname, clean, metric, operator, value),
        playbook_id=_optional_id(playbook_id),
        escalation_policy_id=policy.id if policy else None,
    )
    db.add(row)
    audit(db, "playrule.create", actor=user.email, object_type="playrule", object_id=clean)
    db.commit()
    return RedirectResponse("/playrules", status_code=302)


@router.post("/playrules/{rule_id}/update")
def playrule_update(
    rule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
    alertname: str = Form(""),
    metric: str = Form(""),
    operator: str = Form(">"),
    value: str = Form(""),
    severity: str = Form("warning"),
    playbook_id: str = Form(""),
    escalation_policy_id: str = Form(""),
):
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    row = db.get(Playrule, rule_id)
    if row is None:
        raise HTTPException(status_code=404)
    clean = name.strip() or row.name
    clash = db.query(Playrule).filter(Playrule.name == clean, Playrule.id != row.id).first()
    if clash is not None:
        return RedirectResponse(
            f"/playrules?edit={row.id}&notice={quote('A playrule with that name already exists.')}",
            status_code=302,
        )
    policy = _playrule_policy(db, _optional_id(escalation_policy_id))
    row.name = clean
    row.severity = severity
    row.condition = playrule_condition(alertname, clean, metric, operator, value)
    row.playbook_id = _optional_id(playbook_id)
    row.escalation_policy_id = policy.id if policy else None
    audit(db, "playrule.update", actor=user.email, object_type="playrule", object_id=clean)
    db.commit()
    return RedirectResponse("/playrules", status_code=302)


@router.post("/playrules/{rule_id}/delete")
def playrule_delete(rule_id: int, db: Session = Depends(get_db), user: User = Depends(login_required)):
    """Existing incidents keep their playbook; only the playrule link is cleared."""
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    row = db.get(Playrule, rule_id)
    if row is None:
        raise HTTPException(status_code=404)
    name = row.name
    db.query(Incident).filter(Incident.playrule_id == row.id).update({Incident.playrule_id: None})
    db.delete(row)
    audit(db, "playrule.remove", actor=user.email, object_type="playrule", object_id=name)
    db.commit()
    return RedirectResponse(f"/playrules?notice={quote(f'Removed playrule {name}.')}", status_code=302)


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


def _playbook_steps(text: str) -> list[dict]:
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            rows.append({"title": line.lstrip("0123456789.-) ").strip()})
    return rows


def _playbook_steps_text(book: Playbook) -> str:
    lines = []
    for step in book.steps or []:
        if isinstance(step, dict):
            title = str(step.get("title") or "").strip()
        else:
            title = str(step).strip()
        if title:
            lines.append(title)
    return "\n".join(lines)


def _playbook_slug(raw: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in (raw or "").strip().lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "playbook"


def _unique_playbook_slug(db: Session, base: str) -> str:
    slug = _playbook_slug(base)
    n = 2
    candidate = slug
    while db.query(Playbook).filter_by(slug=candidate).first() is not None:
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


@router.get("/playbooks", response_class=HTMLResponse)
def playbooks_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("read_play")),
    page: str = "1",
    edit: str = "",
    clone: str = "",
):
    rows = db.query(Playbook).order_by(Playbook.name).all()
    rows, pager = paginate(rows, page, size=per_page(request))
    form_mode = "create"
    selected = None
    form = {
        "name": "",
        "slug": "",
        "steps": "Verify disk usage\nCheck growth\nIdentify owner\nNotify responsible engineer\nEscalate if not acknowledged",
    }
    notice = request.query_params.get("notice") or ""
    if edit.strip().isdigit():
        selected = db.get(Playbook, int(edit.strip()))
        if selected is not None:
            form_mode = "edit"
            form = {"name": selected.name, "slug": selected.slug, "steps": _playbook_steps_text(selected)}
    elif clone.strip().isdigit():
        source = db.get(Playbook, int(clone.strip()))
        if source is not None:
            selected = source
            form_mode = "clone"
            form = {
                "name": f"{source.name} (copy)",
                "slug": _unique_playbook_slug(db, f"{source.slug}-copy"),
                "steps": _playbook_steps_text(source),
            }
    return render(
        request,
        "playbooks.html",
        user,
        playbooks=rows,
        pager=pager,
        form_mode=form_mode,
        selected=selected,
        form=form,
        notice=notice,
    )


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
    slug = _playbook_slug(slug)
    if db.query(Playbook).filter_by(slug=slug).first() is not None:
        raise HTTPException(status_code=400, detail="playbook slug already exists")
    row = Playbook(name=name.strip(), slug=slug, steps=_playbook_steps(steps))
    db.add(row)
    audit(db, "playbook.create", actor=user.email, object_type="playbook", object_id=slug)
    db.commit()
    return RedirectResponse("/playbooks", status_code=302)


@router.post("/playbooks/{book_id}/update")
def playbook_update(
    book_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
    slug: str = Form(""),
    steps: str = Form(""),
):
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    row = db.get(Playbook, book_id)
    if row is None:
        raise HTTPException(status_code=404)
    row.name = name.strip()
    row.steps = _playbook_steps(steps)
    audit(db, "playbook.update", actor=user.email, object_type="playbook", object_id=row.slug)
    db.commit()
    return RedirectResponse("/playbooks", status_code=302)


@router.post("/playbooks/{book_id}/delete")
def playbook_delete(
    book_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    row = db.get(Playbook, book_id)
    if row is None:
        raise HTTPException(status_code=404)
    slug = row.slug
    db.query(Playrule).filter(Playrule.playbook_id == row.id).update({Playrule.playbook_id: None})
    db.query(Incident).filter(Incident.playbook_id == row.id).update({Incident.playbook_id: None})
    db.delete(row)
    audit(db, "playbook.remove", actor=user.email, object_type="playbook", object_id=slug)
    db.commit()
    return RedirectResponse("/playbooks", status_code=302)


DEFAULT_POLICY_SLUG = "default-warning"
ESCALATION_FORM_DEFAULT = {"name": "", "slug": "", "steps": "0 team\n15 team-lead\n30 engineer"}


def _escalation_view(
    request: Request,
    db: Session,
    user: User,
    *,
    page: str = "1",
    form_mode: str = "create",
    selected: EscalationPolicy | None = None,
    form: dict | None = None,
    error: str = "",
    status_code: int = 200,
):
    policies = db.query(EscalationPolicy).order_by(EscalationPolicy.name).all()
    policies, pager = paginate(policies, page, size=per_page(request))
    usage = dict(
        db.query(Playrule.escalation_policy_id, func.count(Playrule.id))
        .filter(Playrule.escalation_policy_id.isnot(None))
        .group_by(Playrule.escalation_policy_id)
        .all()
    )
    response = render(
        request,
        "escalation.html",
        user,
        policies=policies,
        pager=pager,
        form_mode=form_mode,
        selected=selected,
        form=form or dict(ESCALATION_FORM_DEFAULT),
        error=error,
        usage=usage,
        default_slug=DEFAULT_POLICY_SLUG,
    )
    response.status_code = status_code
    return response


@router.get("/escalation", response_class=HTMLResponse)
def escalation_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("read_play")),
    page: str = "1",
    edit: str = "",
):
    selected = None
    form = None
    edit_id = _parse_id_query(edit)
    if edit_id is not None and can(user, "write_play"):
        selected = db.get(EscalationPolicy, edit_id)
    if selected is not None:
        form = {"name": selected.name, "slug": selected.slug, "steps": policy_steps_text(selected.steps)}
    return _escalation_view(
        request,
        db,
        user,
        page=page,
        form_mode="edit" if selected is not None else "create",
        selected=selected,
        form=form,
    )


@router.post("/escalation")
def escalation_create(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
    slug: str = Form(...),
    steps: str = Form(""),
):
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    clean_slug = (slug or "").strip().lower().replace(" ", "-")
    form = {"name": name, "slug": slug, "steps": steps}
    error = ""
    if not clean_slug:
        error = "Slug is required."
    elif db.query(EscalationPolicy).filter_by(slug=clean_slug).first():
        error = f"Slug {clean_slug} already exists. Edit that policy instead, or pick another slug."
    else:
        error = " ".join(policy_step_problems(steps))
    if error:
        return _escalation_view(request, db, user, form=form, error=error, status_code=400)
    row = EscalationPolicy(name=name.strip(), slug=clean_slug, steps=parse_policy_steps(steps))
    db.add(row)
    audit(db, "escalation.create", actor=user.email, object_type="escalation_policy", object_id=clean_slug)
    db.commit()
    return RedirectResponse("/escalation", status_code=302)


@router.post("/escalation/{policy_id}/update")
def escalation_update(
    policy_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
    steps: str = Form(""),
):
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    row = db.get(EscalationPolicy, policy_id)
    if row is None:
        raise HTTPException(status_code=404)
    problems = policy_step_problems(steps)
    if problems or not name.strip():
        return _escalation_view(
            request,
            db,
            user,
            form_mode="edit",
            selected=row,
            form={"name": name, "slug": row.slug, "steps": steps},
            error=" ".join(problems) or "Name is required.",
            status_code=400,
        )
    row.name = name.strip()
    row.steps = parse_policy_steps(steps)
    audit(db, "escalation.update", actor=user.email, object_type="escalation_policy", object_id=row.slug)
    db.commit()
    return RedirectResponse("/escalation", status_code=302)


@router.post("/escalation/{policy_id}/delete")
def escalation_delete(
    policy_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can(user, "write_play"):
        raise HTTPException(status_code=403)
    row = db.get(EscalationPolicy, policy_id)
    if row is None:
        raise HTTPException(status_code=404)
    if row.slug == DEFAULT_POLICY_SLUG:
        raise HTTPException(
            status_code=400,
            detail="Default warning is the ladder new playrules attach (Core start re-creates it). Edit its steps instead.",
        )
    fallback = db.query(EscalationPolicy).filter_by(slug=DEFAULT_POLICY_SLUG).first()
    moved = (
        db.query(Playrule)
        .filter(Playrule.escalation_policy_id == row.id)
        .update({Playrule.escalation_policy_id: fallback.id if fallback else None}, synchronize_session=False)
    )
    slug = row.slug
    db.delete(row)
    audit(
        db,
        "escalation.remove",
        actor=user.email,
        object_type="escalation_policy",
        object_id=slug,
        data={"playrules_moved": int(moved or 0), "to": fallback.slug if fallback else ""},
    )
    db.commit()
    return RedirectResponse("/escalation", status_code=302)


@router.get("/journal", response_class=HTMLResponse)
def journal_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("read_play")),
    module: str = "",
    status: str = "",
    q: str = "",
    page: str = "1",
):
    total = count_entries(db, module=module or None, status=status or None, q=q or None)
    pager = pager_state(page, total=total, size=per_page(request))
    rows = list_entries(
        db,
        module=module or None,
        status=status or None,
        q=q or None,
        limit=pager["size"],
        offset=pager["offset"],
    )
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
        pager=pager,
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
def ops_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    page: str = "1",
    reports_page: str = "1",
    edit: str = "",
    clone: str = "",
):
    from app.services import list_mail_addresses

    mail, mail_pager = paginate_query(
        db.query(Notification).order_by(Notification.id.desc()),
        page,
        size=per_page(request),
        fragment="#mail",
    )
    reports = db.query(ScheduledReport).order_by(ScheduledReport.id.desc()).all()
    reports, reports_pager = paginate(
        reports,
        reports_page,
        size=per_page(request, "reports_page"),
        param="reports_page",
        fragment="#reports",
    )
    assets = db.query(Asset).order_by(Asset.hostname).all()
    report_form_mode = "add"
    report_form = scheduled_report_form_values()
    editing_report = None
    clone_source_name = ""
    edit_id = _parse_id_query(edit)
    clone_id = _parse_id_query(clone)
    if edit_id is not None:
        editing_report = db.get(ScheduledReport, edit_id)
        if editing_report is not None:
            report_form_mode = "edit"
            report_form = scheduled_report_form_values(editing_report)
    elif clone_id is not None:
        source = db.get(ScheduledReport, clone_id)
        if source is not None:
            report_form_mode = "clone"
            report_form = scheduled_report_form_values(source, clone=True)
            clone_source_name = source.name or "report"
    return render(
        request,
        "ops.html",
        user,
        mail=mail,
        reports=reports,
        assets=assets,
        addresses=list_mail_addresses(db),
        smtp_on=settings.email_enabled and bool(settings.smtp_host),
        can_send=can_send_ops(user),
        webmail_url=_webmail_url(request),
        smtp_provider=smtp_provider_id(),
        mail_pager=mail_pager,
        reports_pager=reports_pager,
        report_form_mode=report_form_mode,
        report_form=report_form,
        editing_report=editing_report,
        clone_source_name=clone_source_name,
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


@router.post("/ops/reports")
def ops_create_report(
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
    to_email: str = Form(""),
    new_email: str = Form(""),
    interval_hours: int = Form(6),
    asset_id: Annotated[list[str], Form()] = [],
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    from datetime import timedelta

    from app.services import utcnow

    label, email, hours, ids = parse_scheduled_report_form(
        db,
        name=name,
        to_email=to_email,
        new_email=new_email,
        interval_hours=interval_hours,
        asset_id=asset_id,
        actor=user.email,
    )
    row = ScheduledReport(
        name=label,
        to_email=email,
        interval_hours=hours,
        asset_ids=ids,
        enabled=True,
        created_by=user.email,
        next_run_at=utcnow() + timedelta(hours=hours),
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
    from app.services import send_performance_report

    ids = [str(item).strip() for item in (asset_id or []) if str(item).strip()]
    chosen = (new_email or "").strip() or (to_email or "").strip()
    try:
        send_performance_report(
            db,
            asset_ids=ids,
            to_email=chosen,
            actor=user.email,
            name="send-now",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/ops#mail", status_code=303)


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


@router.post("/ops/reports/{report_id}/update")
def ops_update_report(
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    name: str = Form(...),
    to_email: str = Form(""),
    new_email: str = Form(""),
    interval_hours: int = Form(6),
    asset_id: Annotated[list[str], Form()] = [],
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    from datetime import timedelta

    from app.services import utcnow

    row = db.get(ScheduledReport, report_id)
    if row is None:
        raise HTTPException(status_code=404)
    label, email, hours, ids = parse_scheduled_report_form(
        db,
        name=name,
        to_email=to_email,
        new_email=new_email,
        interval_hours=interval_hours,
        asset_id=asset_id,
        actor=user.email,
    )
    interval_changed = int(row.interval_hours or 0) != hours
    row.name = label
    row.to_email = email
    row.interval_hours = hours
    row.asset_ids = ids
    if interval_changed:
        row.next_run_at = utcnow() + timedelta(hours=hours)
    audit(
        db,
        "report.update",
        actor=user.email,
        object_type="report",
        object_id=str(row.id),
        data={"name": row.name, "to": row.to_email, "hours": hours},
    )
    db.commit()
    return RedirectResponse("/ops#reports", status_code=303)


@router.post("/ops/reports/{report_id}/delete")
def ops_delete_report(
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    row = db.get(ScheduledReport, report_id)
    if row is None:
        raise HTTPException(status_code=404)
    audit(
        db,
        "report.delete",
        actor=user.email,
        object_type="report",
        object_id=str(row.id),
        data={"name": row.name, "to": row.to_email},
    )
    db.delete(row)
    db.commit()
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
    audit(
        db,
        "report.toggle",
        actor=user.email,
        object_type="report",
        object_id=str(row.id),
        data={"enabled": bool(row.enabled)},
    )
    db.commit()
    return RedirectResponse("/ops#reports", status_code=303)


@router.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    selected: int | None = Query(None),
    clone: int | None = Query(None),
    page: str = "1",
    audit_page: str = "1",
    backup_page: str = "1",
):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    users = db.query(User).order_by(User.email).all()
    users, users_pager = paginate(users, page, size=per_page(request))
    audits, audit_pager = paginate_query(
        db.query(AuditLog).order_by(AuditLog.id.desc()),
        audit_page,
        size=per_page(request, "audit_page"),
        param="audit_page",
    )
    chosen = db.get(User, selected) if selected else None
    clone_of = db.get(User, clone) if clone else None
    from app.backup import format_size, list_archives, layout_from_env
    from app.users import delete_blocked, edit_blocked

    lay = layout_from_env()
    backups = list_archives(lay)
    backup_choices = backups
    backups, backup_pager = paginate(backups, backup_page, size=per_page(request, "backup_page"), param="backup_page")
    return render(
        request,
        "admin.html",
        user,
        users=users,
        audits=audits,
        selected=chosen,
        clone_of=clone_of,
        clone_role=(clone_of.role if clone_of and clone_of.role in CREATABLE_ROLES else "analyst") if clone_of else "",
        can_edit_selected=bool(chosen) and not edit_blocked(user, chosen),
        can_delete_selected=bool(chosen) and not delete_blocked(user, chosen),
        backups=backups,
        backup_choices=backup_choices,
        backup_files_writable=lay.files_writable,
        format_size=format_size,
        pager=users_pager,
        audit_pager=audit_pager,
        backup_pager=backup_pager,
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
def demo_page(db: Session = Depends(get_db), user: User = Depends(require_page("admin"))):
    incident = run_demo(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-rca")
def demo_rca_page(db: Session = Depends(get_db), user: User = Depends(require_page("admin"))):
    incident = run_demo_rca(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/ai/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-host")
def demo_host_page(db: Session = Depends(get_db), user: User = Depends(require_page("admin"))):
    incident = run_demo_host(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-windows")
def demo_windows_page(db: Session = Depends(get_db), user: User = Depends(require_page("admin"))):
    incident = run_demo_windows(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-network")
def demo_network_page(db: Session = Depends(get_db), user: User = Depends(require_page("admin"))):
    incident = run_demo_network(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-nodecpu")
def demo_nodecpu_page(db: Session = Depends(get_db), user: User = Depends(require_page("admin"))):
    incident = run_demo_nodecpu(db)
    number = incident.number if incident else ""
    return RedirectResponse(f"/incidents/{number}" if number else "/incidents", status_code=303)


@router.post("/demo-reset")
def demo_reset_page(user: User = Depends(require_page("admin"))):
    reset_demo_gauges()
    return RedirectResponse("/?demo=1", status_code=303)
