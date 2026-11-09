"""Asset GUI routes. Same URLs; attached to app.web.router."""
from __future__ import annotations

from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.asset_alarms import alarms_from_form
from app.asset_extras import extras_from_form, playrule_ids_from_form
from app.asset_probe import reachability_snapshot
from app.asset_snmp import snmp_from_form
from app.asset_types import DEFAULT_SNMP_PORT, parse_snmp_port
from app.demo_ids import is_lab_inventory_row
from app.db import get_db, get_db_session
from app.exporter_detect import AUTO_ASSET_TYPE
from app.inventory import (
    ASSET_SOURCE_FILTERS,
    add_asset,
    asset_filter_options,
    asset_form_values,
    asset_in_zabbix,
    asset_missing_email,
    assets_matching,
    attach_imported_candidate,
    clone_prefill,
    delete_asset,
    set_asset_playrules,
    update_asset,
    validate_ip_field,
    zabbix_agent_state,
)
from app.models import Asset, DiscoveryCandidate, User
from app.playrule_mute import playrule_off_from_form
from app.security import can
from app.settings import settings
from app.snmp_families import families_from_form, sources_from_form
from app.api import run_asset_verify, verify_assets
from app.history import paginate
from app.web import (
    _asset_form_notice,
    _picked_email,
    _posted_extras,
    _posted_ladder,
    _posted_snmp,
    _snmp_answer,
    asset_form_context,
    asset_saved_url,
    login_required,
    per_page,
    render,
    require_page,
    router,
)


@router.get("/assets", response_class=HTMLResponse)
def assets_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    edit: str = "",
    clone: str = "",
    view: str = "",
    q: str = "",
    status: str = "",
    flag: str = "",
    source: str = "",
    agent: str = "",
    asset_type: str = Query("", alias="type"),
    site: str = "",
    customer: str = "",
    vlan: str = "",
    row: str = "",
    rack: str = "",
    hostname: str = "",
    ip: str = "",
    lab: str = "",
    page: str = "1",
):
    every = db.query(Asset).order_by(Asset.number, Asset.hostname).all()
    zabbix_assets = any(asset_in_zabbix(row) for row in every)
    rows = assets_matching(
        every,
        q,
        status,
        flag,
        source,
        agent,
        type=asset_type,
        site=site,
        customer=customer,
        vlan=vlan,
        hostname=hostname,
        ip=ip,
        row=row,
        rack=rack,
        lab=lab,
    )
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
        "row": row,
        "rack": rack,
        "hostname": hostname,
        "ip": ip,
        "lab": lab,
    }
    filter_tail = "".join(f"&{urlencode({key: value})}" for key, value in filters.items() if value)
    form_mode = ""
    selected = None
    form = asset_form_values()
    clone_source = ""
    notice = request.query_params.get("notice") or ""
    open_id = edit.strip() or view.strip()
    if open_id:
        selected = db.query(Asset).filter_by(asset_id=open_id).first()
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
        import_of="",
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
        row=row,
        rack=rack,
        hostname=hostname,
        ip=ip,
        lab=lab,
        filter_tail=filter_tail,
        source_filters=ASSET_SOURCE_FILTERS,
        filter_options=asset_filter_options(every),
        zabbix_filters=zabbix_assets or settings.zabbix_enabled,
        zabbix_agent_state=zabbix_agent_state,
        asset_missing_email=asset_missing_email,
        reachability_snapshot=reachability_snapshot,
        pager=pager,
        **asset_form_context(db, every=every, form_mode=form_mode, selected=selected),
    )

@router.get("/assets/verify", response_class=HTMLResponse)
def assets_verify_all(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("write_assets")),
    demo: str = "",
    page: str = "1",
    selected: Annotated[list[str], Query()] = [],
):
    include_demo = demo.strip().lower() in {"1", "true", "yes", "demo"}
    picked: list[str] = []
    seen: set[str] = set()
    for item in selected:
        text = str(item or "").strip()
        if not text or text in seen or len(text) > 80:
            continue
        seen.add(text)
        picked.append(text)
        if len(picked) >= 100:
            break
    if picked:
        rows = db.query(Asset).filter(Asset.asset_id.in_(picked)).all()
        by_id = {row.asset_id: row for row in rows}
        chosen = [by_id[item] for item in picked if item in by_id]
        skipped_demo = 0
        include_demo = False
    else:
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
async def asset_create(
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
    import_of: str = Form(""),
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
    extra_row: str = Form(""),
    extra_rack: str = Form(""),
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
    extra_maint_from: str = Form(""),
    extra_maint_until: str = Form(""),
    snmp_poll: str = Form(""),
    source_present: str = Form(""),
    source_prometheus: str = Form(""),
    source_snmp: str = Form(""),
    snmp_metrics_present: str = Form(""),
    snmp_metric_net: str = Form(""),
    snmp_metric_cpu: str = Form(""),
    snmp_metric_load: str = Form(""),
    snmp_metric_mem: str = Form(""),
    snmp_metric_swap: str = Form(""),
    snmp_metric_disk: str = Form(""),
    snmp_metric_proc: str = Form(""),
    parent_asset_id: str = Form(""),
    playrules_present: str = Form(""),
    playrule_ids: list[str] = Form([]),
    playrule_add: str = Form(""),
    playrule_switches: str = Form(""),
    playrule_on: list[str] = Form([]),
    ladder_present: str = Form(""),
    ladder_on_1: str = Form(""),
    ladder_minutes_1: str = Form(""),
    ladder_target_1: str = Form(""),
    ladder_email_1: list[str] = Form([]),
    ladder_email_1_pick: list[str] = Form([]),
    ladder_on_2: str = Form(""),
    ladder_minutes_2: str = Form(""),
    ladder_target_2: str = Form(""),
    ladder_email_2: list[str] = Form([]),
    ladder_email_2_pick: list[str] = Form([]),
    ladder_on_3: str = Form(""),
    ladder_minutes_3: str = Form(""),
    ladder_target_3: str = Form(""),
    ladder_email_3: list[str] = Form([]),
    ladder_email_3_pick: list[str] = Form([]),
    ladder_on_4: str = Form(""),
    ladder_minutes_4: str = Form(""),
    ladder_target_4: str = Form(""),
    ladder_email_4: list[str] = Form([]),
    ladder_email_4_pick: list[str] = Form([]),
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
    try:
        posted_extras = _posted_extras(
            extras_present,
            customer=extra_customer,
            site=extra_site,
            vlan=extra_vlan,
            row=extra_row,
            rack=extra_rack,
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
            maint_from=extra_maint_from,
            maint_until=extra_maint_until,
            snmp_poll=snmp_poll,
        )
    except ValueError as exc:
        return _asset_form_notice(exc, clone_of=clone_of, import_of=import_of)
    if posted_extras is None and str(snmp_poll or "").strip():
        posted_extras = extras_from_form("1", snmp_poll=snmp_poll)
    picked_metrics = families_from_form(
        snmp_metrics_present,
        net=snmp_metric_net,
        cpu=snmp_metric_cpu,
        load=snmp_metric_load,
        mem=snmp_metric_mem,
        swap=snmp_metric_swap,
        disk=snmp_metric_disk,
        proc=snmp_metric_proc,
    )
    if picked_metrics is not None:
        posted_extras = dict(posted_extras or {})
        posted_extras["snmp_metrics"] = picked_metrics
    posted_sources = sources_from_form(source_present, prometheus=source_prometheus, snmp=source_snmp)
    posted_playrules = playrule_ids_from_form(playrules_present, playrule_ids, playrule_add)
    posted_off = playrule_off_from_form(playrule_switches, playrule_ids, playrule_on)
    posted_ladder = _posted_ladder(
        ladder_present,
        on=[ladder_on_1, ladder_on_2, ladder_on_3, ladder_on_4],
        minutes=[ladder_minutes_1, ladder_minutes_2, ladder_minutes_3, ladder_minutes_4],
        emails=[ladder_email_1, ladder_email_2, ladder_email_3, ladder_email_4],
        picks=[ladder_email_1_pick, ladder_email_2_pick, ladder_email_3_pick, ladder_email_4_pick],
        targets=[ladder_target_1, ladder_target_2, ladder_target_3, ladder_target_4],
    )
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
        async with get_db_session() as session:
            asset = await add_asset(
                session,
                write_db=db,
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
                playrule_off=posted_off,
                snmp_port=snmp_port,
                snmp=posted_snmp,
                ladder=posted_ladder,
                parent_asset_id=parent_asset_id,
                source_prometheus=None if posted_sources is None else posted_sources["prometheus"],
                source_snmp=None if posted_sources is None else posted_sources["snmp"],
            )
    except ValueError as exc:
        return _asset_form_notice(exc, clone_of=cloned_from, import_of=import_of)
    imported = (import_of or "").strip()
    if imported.isdigit():
        candidate = db.get(DiscoveryCandidate, int(imported))
        if candidate is not None:
            attach_imported_candidate(db, candidate, asset, actor=user.email)
    notice = getattr(asset, "_detect_message", "") or ""
    return RedirectResponse(asset_saved_url(asset.asset_id, notice), status_code=302)

@router.get("/assets/{asset_id}", response_class=HTMLResponse)
def asset_detail(asset_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(login_required)):
    """Old fact-page URL. Edit lives on the Assets list."""
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404)
    return RedirectResponse(asset_saved_url(item.asset_id), status_code=303)


@router.post("/assets/{asset_id}/playrules")
def asset_playrules_update(
    asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    playrules_present: str = Form(""),
    playrule_ids: list[str] = Form([]),
    playrule_add: str = Form(""),
    playrule_switches: str = Form(""),
    playrule_on: list[str] = Form([]),
):
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404)
    posted = playrule_ids_from_form(playrules_present or "1", playrule_ids, playrule_add)
    posted_off = playrule_off_from_form(playrule_switches, playrule_ids, playrule_on)
    set_asset_playrules(db, item, posted, actor=user.email, playrule_off=posted_off)
    count = len(item.playrule_ids or [])
    notice = f"Custom alarms saved ({count})." if count else "Custom alarms cleared — Rules apply."
    return RedirectResponse(asset_saved_url(asset_id, notice) + "#client-playrules", status_code=302)



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


@router.post("/assets/snmp-probe")
def asset_snmp_probe(
    user: User = Depends(login_required),
    ip: str = Form(""),
    snmp_port: str = Form(""),
    community: str = Form(""),
):
    """v2c GET sysDescr against the typed IP. Any device class — not vendor-specific."""
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    from app.asset_probe import probe_snmp
    from discovery import probe_snmp_udp

    host = (ip or "").strip()
    port = parse_snmp_port(snmp_port) or DEFAULT_SNMP_PORT
    comm = "".join(ch for ch in (community or "").strip() if ch not in "\r\n")[:128]
    if not host:
        return JSONResponse({"ok": False, "detail": "no IP", "port": port})

    def _prober(target: str, timeout: float, port: int = port) -> bool:
        return bool(probe_snmp_udp(target, community=comm or None, timeout=timeout, port=port))

    result = probe_snmp(host, timeout=1.5, prober=_prober, port=port)
    return JSONResponse({"ok": result.ok is True, "detail": result.detail, "port": port})


@router.post("/assets/http-probe")
def asset_http_probe(
    user: User = Depends(login_required),
    ip: str = Form(""),
    scrape_address: str = Form(""),
    type: str = Form(""),
):
    """GET /metrics on the typed scrape address or the Linux/Windows default port."""
    if not can(user, "write_assets"):
        raise HTTPException(status_code=403)
    from app.asset_probe import default_exporter_port, parse_host_port, probe_metrics

    host, port = parse_host_port(scrape_address)
    host = host or (ip or "").strip()
    if port is None:
        port = default_exporter_port(type)
    if not host or port is None:
        return JSONResponse({"ok": False, "detail": "no scrape address", "port": port or 0})
    result = probe_metrics(host, port, timeout=1.5)
    return JSONResponse({"ok": result.ok is True, "detail": result.detail, "port": port})


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
    notes: str | None = Form(None),
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
    extra_row: str = Form(""),
    extra_rack: str = Form(""),
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
    extra_maint_from: str = Form(""),
    extra_maint_until: str = Form(""),
    snmp_poll: str = Form(""),
    source_present: str = Form(""),
    source_prometheus: str = Form(""),
    source_snmp: str = Form(""),
    snmp_metrics_present: str = Form(""),
    snmp_metric_net: str = Form(""),
    snmp_metric_cpu: str = Form(""),
    snmp_metric_load: str = Form(""),
    snmp_metric_mem: str = Form(""),
    snmp_metric_swap: str = Form(""),
    snmp_metric_disk: str = Form(""),
    snmp_metric_proc: str = Form(""),
    parent_asset_id: str = Form(""),
    playrules_present: str = Form(""),
    playrule_ids: list[str] = Form([]),
    playrule_add: str = Form(""),
    playrule_switches: str = Form(""),
    playrule_on: list[str] = Form([]),
    ladder_present: str = Form(""),
    ladder_on_1: str = Form(""),
    ladder_minutes_1: str = Form(""),
    ladder_target_1: str = Form(""),
    ladder_email_1: list[str] = Form([]),
    ladder_email_1_pick: list[str] = Form([]),
    ladder_on_2: str = Form(""),
    ladder_minutes_2: str = Form(""),
    ladder_target_2: str = Form(""),
    ladder_email_2: list[str] = Form([]),
    ladder_email_2_pick: list[str] = Form([]),
    ladder_on_3: str = Form(""),
    ladder_minutes_3: str = Form(""),
    ladder_target_3: str = Form(""),
    ladder_email_3: list[str] = Form([]),
    ladder_email_3_pick: list[str] = Form([]),
    ladder_on_4: str = Form(""),
    ladder_minutes_4: str = Form(""),
    ladder_target_4: str = Form(""),
    ladder_email_4: list[str] = Form([]),
    ladder_email_4_pick: list[str] = Form([]),
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
    try:
        posted_extras = _posted_extras(
            extras_present,
            customer=extra_customer,
            site=extra_site,
            vlan=extra_vlan,
            row=extra_row,
            rack=extra_rack,
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
            maint_from=extra_maint_from,
            maint_until=extra_maint_until,
            snmp_poll=snmp_poll,
        )
    except ValueError as exc:
        return _asset_form_notice(exc, asset_id=asset_id)
    picked_metrics = families_from_form(
        snmp_metrics_present,
        net=snmp_metric_net,
        cpu=snmp_metric_cpu,
        load=snmp_metric_load,
        mem=snmp_metric_mem,
        swap=snmp_metric_swap,
        disk=snmp_metric_disk,
        proc=snmp_metric_proc,
    )
    if picked_metrics is not None:
        posted_extras = dict(posted_extras or {})
        posted_extras["snmp_metrics"] = picked_metrics
    posted_sources = sources_from_form(source_present, prometheus=source_prometheus, snmp=source_snmp)
    posted_playrules = playrule_ids_from_form(playrules_present, playrule_ids, playrule_add)
    posted_off = playrule_off_from_form(playrule_switches, playrule_ids, playrule_on)
    posted_ladder = _posted_ladder(
        ladder_present,
        on=[ladder_on_1, ladder_on_2, ladder_on_3, ladder_on_4],
        minutes=[ladder_minutes_1, ladder_minutes_2, ladder_minutes_3, ladder_minutes_4],
        emails=[ladder_email_1, ladder_email_2, ladder_email_3, ladder_email_4],
        picks=[ladder_email_1_pick, ladder_email_2_pick, ladder_email_3_pick, ladder_email_4_pick],
        targets=[ladder_target_1, ladder_target_2, ladder_target_3, ladder_target_4],
    )
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
        return _asset_form_notice(exc, asset_id=asset_id)
    try:
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
            playrule_off=posted_off,
            snmp_port=snmp_port if comms_present.strip() else None,
            snmp=posted_snmp,
            ladder=posted_ladder,
            parent_asset_id=parent_asset_id,
            source_prometheus=None if posted_sources is None else posted_sources["prometheus"],
            source_snmp=None if posted_sources is None else posted_sources["snmp"],
        )
    except ValueError as exc:
        return _asset_form_notice(exc, asset_id=asset_id)
    notice = getattr(item, "_detect_message", "") or ""
    return RedirectResponse(asset_saved_url(asset_id, notice), status_code=302)


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
    return RedirectResponse(asset_saved_url(asset_id, notice), status_code=302)


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
        return RedirectResponse(asset_saved_url(asset_id, str(exc)), status_code=302)
    unlinked = result.get("unlinked_incidents") or 0
    notice = f"Removed {result['deleted']}."
    if unlinked:
        notice += f" {unlinked} incident(s) stay on Incidents without this host."
    return RedirectResponse(f"/assets?notice={quote(notice)}", status_code=302)
