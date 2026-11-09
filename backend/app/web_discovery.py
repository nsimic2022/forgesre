"""Discovery GUI routes (scan, NetBox/Zabbix/GLPI/LibreNMS cubes, SNMP devices).

Same URLs; attached to app.web.router.
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.asset_snmp import effective_auth, snmp_settings
from app.asset_types import DEFAULT_SNMP_PORT, snmp_port_for, snmp_target
from app.audit import audit
from app.db import get_db
from app.demo_ids import DEMO_CANDIDATE_IP
from app.inventory import (
    CANDIDATE_ROLE_CHOICES,
    approve_candidate,
    asset_for_candidate,
    asset_form_values,
    asset_missing_email,
    candidate_form_values,
    clone_candidate,
    delete_candidate,
    ignore_candidate,
    suggest_clone_candidate_ip,
    sync_netbox,
    update_candidate,
)
from app.journal import report
from app.models import Asset, DiscoveryCandidate, User
from app.netbox import is_local_netbox_url, status_label, sync_cta, token_presence
from app.security import can
from app.services import short_when_label
from app.settings import settings
from app.snmp_onboard import (
    ONBOARD_MODULE,
    apply_onboard_module,
    attach_onboard_oids,
    cardinality_hint,
    clamp_timeout,
    community_for_asset,
    exporter_scrape,
    identify_from_librenms,
    librenms_hostname_for,
    onboard_oids_for,
    prometheus_onboard_status,
    recommend_from_librenms,
    reload_exporter,
    restore_onboard_module,
    run_snmp_generator,
    save_mib_file,
    search_catalog,
    snmp_get,
    union_onboard_oids,
    write_generator_yml,
)
from app.history import paginate
from app.web import (
    asset_form_context,
    asset_saved_url,
    log,
    login_required,
    per_page,
    render,
    require_page,
    router,
)


def _render_discovery(
    request: Request,
    db: Session,
    user: User,
    *,
    page: str = "1",
    edit: str = "",
    clone: str = "",
    imported: str = "",
    asset: str = "",
    notice: str | None = None,
    snmp: dict | None = None,
):
    from discovery import suggested_management_cidr
    from app.jobs import active_discovery_scan, latest_discovery_scan, scan_ports_view

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
    candidate_mode = ""
    selected = None
    selected_candidate = None
    candidate_form = {"ip": "", "hostname": "", "proposed_role": "Unknown device"}
    form_mode = "add"
    form = asset_form_values()
    import_of = ""
    if notice is None:
        notice = request.query_params.get("notice") or ""
    if imported.strip().isdigit():
        selected_candidate = db.get(DiscoveryCandidate, int(imported.strip()))
        if selected_candidate is not None:
            existing = asset_for_candidate(db, selected_candidate)
            if existing is not None:
                form_mode = "edit"
                form = asset_form_values(existing)
                selected = existing
            else:
                form = candidate_form_values(db, selected_candidate)
                import_of = str(selected_candidate.id)
    elif (asset or "").strip():
        existing = db.query(Asset).filter_by(asset_id=asset.strip()).first()
        if existing is not None:
            form_mode = "edit"
            form = asset_form_values(existing)
            selected = existing
            selected_candidate = (
                db.query(DiscoveryCandidate).filter_by(asset_id=existing.asset_id).first()
                or db.query(DiscoveryCandidate).filter_by(ip=existing.ip).first()
            )
    elif edit.strip().isdigit():
        selected_candidate = db.get(DiscoveryCandidate, int(edit.strip()))
        if selected_candidate is not None:
            candidate_mode = "edit"
            candidate_form = {
                "ip": selected_candidate.ip,
                "hostname": selected_candidate.hostname or "",
                "proposed_role": selected_candidate.proposed_role or "Unknown device",
            }
    elif clone.strip().isdigit():
        source = db.get(DiscoveryCandidate, int(clone.strip()))
        if source is not None:
            selected_candidate = source
            candidate_mode = "clone"
            candidate_form = {
                "ip": suggest_clone_candidate_ip(db, source.ip),
                "hostname": source.hostname or "",
                "proposed_role": source.proposed_role or "Unknown device",
            }
    saved_cidrs = list(settings.discovery_cidrs)
    suggested = suggested_management_cidr()
    cidr_prefill = ", ".join(saved_cidrs) if saved_cidrs else (suggested or "")
    inventory = db.query(Asset).order_by(Asset.number, Asset.hostname).all()
    return render(
        request,
        "discovery.html",
        user,
        candidates=rows,
        pending=pending,
        candidate_mode=candidate_mode,
        candidate_form=candidate_form,
        form_mode=form_mode,
        form=form,
        selected=selected,
        selected_candidate=selected_candidate,
        clone_source="",
        import_of=import_of,
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
        librenms=librenms_discovery_ctx(),
        zabbix=zabbix_discovery_ctx(db),
        glpi=glpi_discovery_ctx(db),
        demo_candidate_ip=DEMO_CANDIDATE_IP,
        scan_job=active_discovery_scan(db),
        scan_ports=scan_ports_view(
            active_discovery_scan(db) or latest_discovery_scan(db),
            zabbix_on=settings.zabbix_enabled,
        ),
        pager=pager,
        found_assets={a.asset_id: a for a in inventory if a.asset_id},
        found_by_ip={a.ip: a for a in inventory if a.ip},
        **asset_form_context(
            db,
            every=inventory,
            form_mode=form_mode,
            selected=selected if form_mode == "edit" else None,
        ),
        **(snmp or _snmp_onboard_ctx(db, inventory=inventory)),
    )


@router.get("/discovery", response_class=HTMLResponse)
def discovery_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("write_assets")),
    page: str = "1",
    edit: str = "",
    clone: str = "",
    imported: str = Query("", alias="import"),
    asset: str = "",
):
    return _render_discovery(
        request, db, user, page=page, edit=edit, clone=clone, imported=imported, asset=asset
    )


def _snmp_onboard_target(db: Session, asset_id: str, ip: str, port: str) -> tuple[Asset | None, str, int]:
    asset = db.query(Asset).filter_by(asset_id=(asset_id or "").strip()).first() if (asset_id or "").strip() else None
    host = (ip or "").strip() or ((asset.ip or "").strip() if asset is not None else "")
    if not host:
        raise ValueError("Need an IP or an asset with an IP")
    raw_port = (port or "").strip()
    if raw_port:
        try:
            udp = int(raw_port)
        except ValueError as exc:
            raise ValueError("UDP port must be a number") from exc
    else:
        udp = snmp_port_for(asset) if asset is not None else DEFAULT_SNMP_PORT
    if udp <= 0:
        udp = DEFAULT_SNMP_PORT
    if udp > 65535:
        raise ValueError("UDP port out of range")
    return asset, host, udp


def _snmp_onboard_ctx(
    db: Session,
    *,
    inventory: list | None = None,
    form: dict | None = None,
    identify: dict | None = None,
    probe: dict | None = None,
    recommendations: list | None = None,
    cardinality: str = "",
    apply_result: dict | None = None,
    scrape: dict | None = None,
    mib: dict | None = None,
    search: list | None = None,
    snmp_notice: str = "",
    librenms: dict | None = None,
) -> dict:
    from app.librenms import status as librenms_status, token_presence as librenms_token_presence

    rows = inventory if inventory is not None else db.query(Asset).order_by(Asset.hostname, Asset.asset_id).all()
    sticky = {
        "asset_id": "",
        "ip": "",
        "port": str(DEFAULT_SNMP_PORT),
        "version": "v2c",
        "timeout": "0.8",
        "oid": "",
        "get_next": "",
        "oid_pick": [],
        "mib_q": "",
    }
    sticky.update(form or {})
    devices = []
    for row in rows:
        if not (row.ip or "").strip():
            continue
        extra = onboard_oids_for(row)
        devices.append(
            {
                "row": row,
                "oids": extra,
                "on": bool(extra),
                "librenms": librenms_hostname_for(row),
            }
        )
    return {
        "snmp_assets": [row for row in rows if (row.ip or "").strip()],
        "snmp_devices": devices,
        "snmp_form": sticky,
        "snmp_identify": identify or {},
        "snmp_probe": probe or {},
        "snmp_recommendations": recommendations or [],
        "snmp_cardinality": cardinality,
        "snmp_apply": apply_result or {},
        "snmp_scrape": scrape or {},
        "snmp_mib": mib or {},
        "snmp_search": search or [],
        "snmp_notice": snmp_notice,
        "snmp_librenms": librenms if librenms is not None else librenms_status(),
        "librenms_token_present": librenms_token_presence(settings.librenms_token),
    }


@router.get("/discovery/snmp")
def discovery_snmp_page(user: User = Depends(require_page("write_assets"))):
    return RedirectResponse("/discovery#snmp", status_code=302)


@router.post("/discovery/snmp", response_class=HTMLResponse)
async def discovery_snmp_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("write_assets")),
    action: str = Form(""),
    asset_id: str = Form(""),
    ip: str = Form(""),
    port: str = Form("161"),
    version: str = Form("v2c"),
    community: str = Form(""),
    timeout: str = Form("0.8"),
    oid: str = Form(""),
    get_next: str = Form(""),
    oid_pick: list[str] = Form([]),
    mib_q: str = Form(""),
):
    notice = ""
    identify: dict = {}
    probe: dict = {}
    recommendations: list = []
    cardinality = ""
    apply_result: dict = {}
    scrape: dict = {}
    search: list = []
    wait = clamp_timeout(timeout)
    ver = (version or "v2c").strip().lower()
    if ver not in {"v1", "v2c", "v3"}:
        ver = "v2c"
    form = {
        "asset_id": (asset_id or "").strip(),
        "ip": (ip or "").strip(),
        "port": (port or "").strip() or str(DEFAULT_SNMP_PORT),
        "version": ver,
        "timeout": str(wait),
        "oid": (oid or "").strip(),
        "get_next": "1" if (get_next or "").strip() else "",
        "oid_pick": list(oid_pick or []),
        "mib_q": (mib_q or "").strip(),
    }
    try:
        if (action or "").startswith("pick-"):
            form["asset_id"] = action.split("pick-", 1)[1]
            action = "pick"
        if action == "search":
            search = search_catalog(form["mib_q"] or form["oid"])
            if not search:
                notice = "No catalog names matched."
        else:
            asset, host, udp = _snmp_onboard_target(db, form["asset_id"], form["ip"], form["port"])
            form["ip"] = host
            form["port"] = str(udp)
            comm = community_for_asset(asset, community)
            kwargs = {
                "community": comm,
                "port": udp,
                "version": ver,
                "timeout": wait,
            }
            if action == "pick":
                if asset is None:
                    raise ValueError("Pick an asset from the SNMP devices list.")
                form["asset_id"] = asset.asset_id
                form["ip"] = (asset.ip or "").strip()
                extra = onboard_oids_for(asset)
                form["oid_pick"] = extra
            elif action == "identify":
                from app.librenms import discover_device, maybe_await

                stored = snmp_settings(getattr(asset, "extras", None) if asset is not None else None)
                inventory = await maybe_await(discover_device(
                    host,
                    version=ver,
                    community=comm,
                    port=udp,
                    v3_user=stored.get("snmp_v3_user") or "",
                    v3_level=stored.get("snmp_v3_level") or "",
                    v3_auth_proto=stored.get("snmp_v3_auth_proto") or "",
                    v3_auth_pass=stored.get("snmp_v3_auth_pass") or "",
                    v3_priv_proto=stored.get("snmp_v3_priv_proto") or "",
                    v3_priv_pass=stored.get("snmp_v3_priv_pass") or "",
                    disable_polling=True,
                ))
                identify = identify_from_librenms(inventory)
                recommendations = recommend_from_librenms(inventory)
                if recommendations:
                    identify["profile"] = recommendations[0].get("profile") or ""
                if_number = next((row["value"] for row in recommendations if row.get("key") == "ifNumber" and row.get("ok")), "")
                extra_cols = sum(1 for row in recommendations if row.get("ok") and row.get("kind") == "table" and row.get("recommend"))
                cardinality = cardinality_hint(if_number, extra_cols)
                if asset is not None:
                    attach_onboard_oids(asset, onboard_oids_for(asset), librenms_hostname=str(inventory.get("hostname") or host))
                    db.add(asset)
                    db.flush()
                notice = (
                    f"LibreNMS has {identify.get('hostname') or host}. "
                    "SNMP polling there is off — snmp_exporter collects Prometheus series."
                )
            elif action == "probe":
                if ver == "v3":
                    raise ValueError("OID tester is v1/v2c. For v3 use Test scrape after ./forgesre snmp-auths.")
                if not form["oid"]:
                    raise ValueError("Need a numeric OID")
                probe = await asyncio.to_thread(snmp_get, host, form["oid"], get_next=bool(form["get_next"]), **kwargs)
                if not probe.get("ok"):
                    notice = probe.get("error") or "no SNMP reply"
            elif action == "apply":
                if asset is None:
                    match = db.query(Asset).filter(Asset.ip == host).order_by(Asset.asset_id).all()
                    if len(match) == 1:
                        asset = match[0]
                        form["asset_id"] = asset.asset_id
                if asset is None:
                    raise ValueError("Pick an asset so Prometheus HTTP SD can scrape forgesre_onboard.")
                picks = list(oid_pick or [])
                if form["oid"]:
                    picks.append(form["oid"])
                if not picks:
                    raise ValueError("Select an OID LibreNMS recommended, or type one in OID tester.")
                attach_onboard_oids(
                    asset,
                    list(onboard_oids_for(asset)) + picks,
                    librenms_hostname=librenms_hostname_for(asset) or host,
                )
                db.add(asset)
                db.flush()
                all_oids = union_onboard_oids(db.query(Asset).order_by(Asset.asset_id).all())
                apply_result = apply_onboard_module(all_oids)
                if apply_result.get("ok"):
                    gen = run_snmp_generator()
                    apply_result["generator"] = (
                        "snmp-generator ran."
                        if gen.get("ok")
                        else (gen.get("error") or "generator skipped")
                    )
                    audit(
                        db,
                        "snmp.onboard.apply",
                        actor=user.email,
                        object_type="snmp",
                        object_id=asset.asset_id,
                        data={"module": ONBOARD_MODULE, "oids": len(all_oids)},
                        commit=True,
                    )
                    apply_result["reload"] = (
                        "snmp_exporter reloaded."
                        if reload_exporter(settings.snmp_exporter_url)
                        else "snmp_exporter not reloaded — docker compose restart snmp-exporter."
                    )
                else:
                    notice = apply_result.get("error") or "Apply failed"
            elif action == "apply_profile":
                from app.librenms import discover_device, maybe_await

                stored = snmp_settings(getattr(asset, "extras", None) if asset is not None else None)
                inventory = await maybe_await(discover_device(
                    host,
                    version=ver,
                    community=comm,
                    port=udp,
                    v3_user=stored.get("snmp_v3_user") or "",
                    v3_level=stored.get("snmp_v3_level") or "",
                    v3_auth_proto=stored.get("snmp_v3_auth_proto") or "",
                    v3_auth_pass=stored.get("snmp_v3_auth_pass") or "",
                    v3_priv_proto=stored.get("snmp_v3_priv_proto") or "",
                    v3_priv_pass=stored.get("snmp_v3_priv_pass") or "",
                ))
                identify = identify_from_librenms(inventory)
                recommendations = recommend_from_librenms(inventory)
                if recommendations:
                    identify["profile"] = recommendations[0].get("profile") or ""
                picks = [row["oid"] for row in recommendations if row.get("applyable") and row.get("profile_match")]
                if not picks:
                    raise ValueError("Identify first — no profile OIDs to apply (already in Prometheus if_mib/host).")
                if asset is None:
                    match = db.query(Asset).filter(Asset.ip == host).order_by(Asset.asset_id).all()
                    if len(match) == 1:
                        asset = match[0]
                        form["asset_id"] = asset.asset_id
                if asset is None:
                    raise ValueError("Pick an asset so Prometheus HTTP SD can scrape forgesre_onboard.")
                attach_onboard_oids(
                    asset,
                    list(onboard_oids_for(asset)) + picks,
                    librenms_hostname=str(inventory.get("hostname") or host),
                )
                db.add(asset)
                db.flush()
                all_oids = union_onboard_oids(db.query(Asset).order_by(Asset.asset_id).all())
                apply_result = apply_onboard_module(all_oids)
                if apply_result.get("ok"):
                    audit(
                        db,
                        "snmp.onboard.apply",
                        actor=user.email,
                        object_type="snmp",
                        object_id=asset.asset_id,
                        data={"module": ONBOARD_MODULE, "oids": len(all_oids), "profile": True},
                        commit=True,
                    )
                    apply_result["reload"] = (
                        "snmp_exporter reloaded."
                        if reload_exporter(settings.snmp_exporter_url)
                        else "snmp_exporter not reloaded — docker compose restart snmp-exporter."
                    )
                else:
                    notice = apply_result.get("error") or "Apply failed"
            elif action in {"enable", "disable", "remove"}:
                from app.librenms import delete_device, maybe_await, set_disabled

                name = librenms_hostname_for(asset) if asset is not None else host
                if action == "remove":
                    await maybe_await(delete_device(name))
                    if asset is not None:
                        attach_onboard_oids(asset, [], librenms_hostname="")
                        db.add(asset)
                        db.flush()
                        remaining = union_onboard_oids(db.query(Asset).order_by(Asset.asset_id).all())
                        apply_onboard_module(remaining)
                        db.commit()
                    notice = f"Removed {name} from LibreNMS."
                else:
                    await maybe_await(set_disabled(name, disabled=(action == "disable")))
                    notice = f"LibreNMS polling {'off' if action == 'disable' else 'on'} for {name}."
            elif action == "restore":
                if asset is not None:
                    attach_onboard_oids(asset, [], librenms_hostname=librenms_hostname_for(asset))
                    db.add(asset)
                    db.flush()
                    remaining = union_onboard_oids(db.query(Asset).order_by(Asset.asset_id).all())
                    apply_result = apply_onboard_module(remaining)
                else:
                    apply_result = restore_onboard_module()
                if apply_result.get("ok"):
                    audit(db, "snmp.onboard.restore", actor=user.email, object_type="snmp", object_id=host, commit=True)
                    apply_result["reload"] = (
                        "snmp_exporter reloaded."
                        if reload_exporter(settings.snmp_exporter_url)
                        else "snmp_exporter not reloaded — docker compose restart snmp-exporter."
                    )
                else:
                    notice = apply_result.get("error") or "Restore failed"
            elif action == "scrape":
                auth = effective_auth(asset) if asset is not None else "public_v2"
                scrape = exporter_scrape(
                    snmp_target(host, udp),
                    module=ONBOARD_MODULE,
                    auth=auth,
                    timeout=max(wait, 2.0),
                    url=settings.snmp_exporter_url,
                )
                if not scrape.get("ok"):
                    scrape = exporter_scrape(
                        snmp_target(host, udp),
                        module="if_mib",
                        auth=auth,
                        timeout=max(wait, 2.0),
                        url=settings.snmp_exporter_url,
                    )
                scrape["prom"] = prometheus_onboard_status(
                    snmp_target(host, udp),
                    asset_id=(asset.asset_id if asset is not None else ""),
                )
                if not scrape.get("ok"):
                    notice = scrape.get("error") or "scrape failed"
            else:
                raise ValueError("Unknown SNMP action")
    except ValueError as exc:
        notice = str(exc)
    except Exception as exc:
        from app.librenms import LibreNmsError, LibreNmsUnavailable

        if isinstance(exc, (LibreNmsError, LibreNmsUnavailable)):
            notice = str(exc)
        else:
            raise
    return _render_discovery(
        request,
        db,
        user,
        notice=notice,
        snmp=_snmp_onboard_ctx(
            db,
            form=form,
            identify=identify,
            probe=probe,
            recommendations=recommendations,
            cardinality=cardinality,
            apply_result=apply_result,
            scrape=scrape,
            search=search,
            snmp_notice=notice,
        ),
    )


@router.post("/discovery/snmp/mib", response_class=HTMLResponse)
def discovery_snmp_mib(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("write_assets")),
    asset_id: str = Form(""),
    ip: str = Form(""),
    port: str = Form("161"),
    version: str = Form("v2c"),
    timeout: str = Form("0.8"),
    oid: str = Form(""),
    mib_file: UploadFile = File(...),
):
    notice = ""
    mib: dict = {}
    form = {
        "asset_id": (asset_id or "").strip(),
        "ip": (ip or "").strip(),
        "port": (port or "").strip() or str(DEFAULT_SNMP_PORT),
        "version": (version or "v2c").strip().lower() or "v2c",
        "timeout": str(clamp_timeout(timeout)),
        "oid": (oid or "").strip(),
        "get_next": "",
        "oid_pick": [],
    }
    try:
        data = mib_file.file.read()
        mib = save_mib_file(data, mib_file.filename or "")
        asset, host, _udp = None, form["ip"], DEFAULT_SNMP_PORT
        try:
            asset, host, _udp = _snmp_onboard_target(db, form["asset_id"], form["ip"], form["port"])
            form["ip"] = host
        except ValueError:
            asset = None
        oids = onboard_oids_for(asset) if asset is not None else []
        if form["oid"]:
            oids = list(oids) + [form["oid"]]
        write_generator_yml(oids)
        gen = run_snmp_generator()
        mib["generator"] = "snmp-generator ran." if gen.get("ok") else (gen.get("error") or "generator skipped")
        audit(
            db,
            "snmp.onboard.mib",
            actor=user.email,
            object_type="snmp",
            object_id=mib.get("name") or "mib",
            data={"objects": mib.get("object_count") or 0},
            commit=True,
        )
    except ValueError as exc:
        notice = str(exc)
        mib = {"ok": False, "error": str(exc)}
    return _render_discovery(
        request,
        db,
        user,
        notice=notice,
        snmp=_snmp_onboard_ctx(db, form=form, mib=mib),
    )


def librenms_discovery_ctx() -> dict:
    """Discovery LibreNMS cube: traffic light + API token, same shape as NetBox/Zabbix/GLPI."""
    from app.librenms import status as librenms_status, token_presence as librenms_token_presence

    enabled = settings.librenms_enabled
    present = librenms_token_presence(settings.librenms_token)
    if not enabled:
        return {
            "enabled": False,
            "url": settings.librenms_url,
            "light": "grey",
            "label": "Disabled",
            "why": "inventory.librenms.enabled is false",
            "hosts": 0,
            "clickable": False,
            "token_present": present,
        }
    try:
        status = librenms_status()
    except Exception as exc:
        status = {
            "ok": False,
            "light": "yellow",
            "label": "API error",
            "why": str(exc)[:300],
            "hosts": 0,
        }
    light = str(status.get("light") or "grey")
    return {
        "enabled": True,
        "url": settings.librenms_url,
        "light": light,
        "label": str(status.get("label") or "Not configured"),
        "why": "" if light == "green" else str(status.get("why") or ""),
        "hosts": int(status.get("hosts") or 0),
        "clickable": present == "yes",
        "token_present": present,
    }


def zabbix_discovery_ctx(db: Session) -> dict:
    """Discovery Zabbix block: cached status chip, last sync / last error. Never blocks on a dead Zabbix twice."""
    from app.zabbix import NOT_CONFIGURED_WHY, token_presence as zabbix_token_presence
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
        "token_present": zabbix_token_presence(settings.zabbix_token),
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


def glpi_discovery_ctx(db: Session) -> dict:
    """Discovery GLPI block: cached status chip, last sync / last error."""
    from app.glpi import NOT_CONFIGURED_WHY, token_presence as glpi_token_presence
    from app.glpi_sync import current_status, last_sync_entry

    enabled = settings.glpi_enabled
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
        "url": settings.glpi_url,
        "auto_sync": settings.glpi_auto_sync,
        "light": str(status.get("light") or "grey"),
        "label": str(status.get("label") or "Not configured"),
        "why": "" if status.get("light") == "green" else str(status.get("why") or ""),
        "hosts": int(status.get("hosts") or 0),
        "clickable": bool(enabled),
        "last_ok": last_ok,
        "last_error": last_error,
        "token_present": glpi_token_presence(settings.glpi_app_token, settings.glpi_user_token),
    }


@router.post("/discovery/glpi-sync")
def discovery_glpi_page(db: Session = Depends(get_db), user: User = Depends(login_required)):
    if not can(user, "admin"):
        raise HTTPException(status_code=403)
    from app.glpi_sync import sync_glpi

    result = sync_glpi(db, force=True) or {}
    if result.get("error"):
        notice = f"GLPI sync failed: {result['error']}"
    elif result.get("skipped"):
        notice = "GLPI is not configured (GLPI_URL + GLPI_APP_TOKEN + GLPI_USER_TOKEN in secrets/secrets.env)."
    else:
        notice = (
            f"GLPI sync: {int(result.get('created') or 0)} new, {int(result.get('linked') or 0)} linked, "
            f"{int(result.get('skipped') or 0)} skipped. New items are Auto type with no scrape — "
            "set Type / owner email on Assets."
        )
    return RedirectResponse(f"/discovery?notice={quote(notice)}", status_code=302)


@router.get("/discovery/scan-ports", response_class=HTMLResponse)
def discovery_scan_ports_fragment(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page("write_assets")),
):
    """HTMX poll for Scan now cubes. Same reach-sq size as Assets Port."""
    from app.jobs import active_discovery_scan, latest_discovery_scan, scan_ports_view

    return render(
        request,
        "_discovery_scan_ports.html",
        user,
        scan_ports=scan_ports_view(
            active_discovery_scan(db) or latest_discovery_scan(db),
            zabbix_on=settings.zabbix_enabled,
        ),
    )


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
    notice = ""
    if asset_missing_email(asset):
        notice = "Approved. Add an owner email (Edit) so escalation mail has a recipient."
    return RedirectResponse(asset_saved_url(asset.asset_id, notice), status_code=302)


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
