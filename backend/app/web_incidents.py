"""Incident GUI routes (list, detail, RCA, history redirect). Same URLs; attached to app.web.router."""
from __future__ import annotations

import json
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.dc_noc import incident_syslog_lines
from app.history import (
    acknowledge,
    add_note,
    apply_status_fields,
    audit_for,
    incident_list_filters,
    incident_neighbors,
    list_history,
    notes_for,
    notifications_for,
    paginate,
    pager_state,
    reported_to_for,
)
from app.incident_report_mail import build_incident_report
from app.inventory import asset_filter_options, similar_incident_groups
from app.models import Asset, Incident, User
from app.security import can, can_send_ops
from app.services import incident_alarm_subject
from app.web import (
    asset_playrules,
    evidence_row,
    llm_job_error,
    llm_job_pending,
    login_required,
    ops_mail_ctx,
    per_page,
    render,
    require_page,
    router,
    tool_status,
)
from rca.specialists import specialists_from_evidence


@router.get("/incidents", response_class=HTMLResponse)
def incidents_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    open_filter: str = Query("", alias="open"),
    status: str = "",
    severity: str = "",
    days: str = "",
    window: str = "",
    started_from: str = Query("", alias="from"),
    started_to: str = Query("", alias="to"),
    site: str = "",
    customer: str = "",
    row: str = "",
    rack: str = "",
    q: str = "",
    demo: str = "",
    page: str = "1",
):
    filters = incident_list_filters(
        status=status,
        severity=severity,
        open_filter=open_filter,
        days=days,
        window=window,
        started_from=started_from,
        started_to=started_to,
        site=site,
        customer=customer,
        dc_row=row,
        rack=rack,
        q=q,
        demo=demo,
    )
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
        window=filters["window"],
        date_from=filters["date_from"],
        date_to=filters["date_to"],
        site=filters["site"],
        customer=filters["customer"],
        row=filters["row"],
        rack=filters["rack"],
        q=filters["q"],
        filter_options=asset_filter_options(db.query(Asset).all()),
        pager=pager,
        nav_qs=filters["qs"],
    )


_HISTORY_QUERY_KEYS = (
    "status",
    "severity",
    "open",
    "days",
    "window",
    "from",
    "to",
    "site",
    "customer",
    "row",
    "rack",
    "page",
    "per_page",
    "q",
)


def history_redirect_target(request: Request) -> str:
    """Old History URL. The list is Incidents; keep filters that page understands.

    History's asset / number search becomes Incidents ``q`` when ``q`` is empty.
    A bare ``/history`` does not force the old 90-day window.
    """
    kept: list[tuple[str, str]] = []
    asset = ""
    number = ""
    for key, value in request.query_params.multi_items():
        text = str(value or "")
        if key == "asset":
            asset = text.strip()
            continue
        if key == "number":
            number = text.strip()
            continue
        if key in _HISTORY_QUERY_KEYS and text.strip():
            kept.append((key, text))
    if not any(key == "q" for key, _value in kept):
        synthesized = number or asset
        if synthesized:
            kept.append(("q", synthesized))
    qs = urlencode(kept)
    return f"/incidents?{qs}" if qs else "/incidents"


@router.get("/history")
def history_page(request: Request, user: User = Depends(login_required)):
    """Old History page. The incident list is Incidents."""
    del user
    return RedirectResponse(history_redirect_target(request), status_code=302)


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
        status=q.get("status", ""),
        severity=q.get("severity", ""),
        open_filter=q.get("open", ""),
        days=q.get("days", ""),
        window=q.get("window", ""),
        started_from=q.get("from", ""),
        started_to=q.get("to", ""),
        site=q.get("site", ""),
        customer=q.get("customer", ""),
        dc_row=q.get("row", ""),
        rack=q.get("rack", ""),
        q=q.get("q", ""),
        demo=q.get("demo", ""),
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
            specialists=specialists_from_evidence(item.evidence),
            audit_pager=audit_pager,
            notes_pager=notes_pager,
            nav=incident_neighbors(db, item, filters),
            evidence_rows=evidence,
            evidence_pager=evidence_pager,
            client_playrules=asset_playrules(db, item.asset),
            syslog_lines=incident_syslog_lines(item),
            report_preview=build_incident_report(db, item),
            report_subject=incident_alarm_subject(item),
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
    from app.services import refresh_asset_status

    db.flush()
    refresh_asset_status(db, item.asset)
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

    run_investigation(db, item, actor=user.email, force=True, use_llm=False)
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
    subject: str = Form(""),
    body: str = Form(""),
):
    if not can_send_ops(user):
        raise HTTPException(status_code=403)
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404)
    from app.services import send_incident_report

    chosen = (new_email or "").strip() or (target or "").strip()
    try:
        send_incident_report(db, item, chosen, actor=user.email, subject=subject, body=body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/incidents/{number}", status_code=303)


@router.post("/incidents/{number}/report/export")
def incident_report_export(
    number: str,
    db: Session = Depends(get_db),
    user: User = Depends(login_required),
    fmt: Annotated[str, Form(alias="format")] = "txt",
    filename: Annotated[str, Form()] = "",
    subject: str = Form(""),
    body: str = Form(""),
):
    if not can_send_ops(user) and not can(user, "read_incidents"):
        raise HTTPException(status_code=403)
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404)
    from app.bulk import _download, _download_csv, _export_format, _safe_export_name

    text = (body or "").strip() or build_incident_report(db, item)
    title = (subject or "").strip() or incident_alarm_subject(item)
    stem = filename or f"{number}-report"
    if _export_format(fmt) == "csv":
        rows: list[list[object]] = [["Subject", title]]
        for line in text.splitlines():
            if ": " in line and not line.startswith(("#", " ", "-")):
                label, value = line.split(": ", 1)
                rows.append([label, value])
            elif line.strip():
                rows.append(["", line])
        return _download_csv(_safe_export_name(stem, f"{number}-report", fmt), ["Field", "Value"], rows)
    return _download(_safe_export_name(stem, f"{number}-report", fmt), text.splitlines())


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
        specialists=specialists_from_evidence(item.evidence),
        mail=notifications_for(db, item),
        **ops_mail_ctx(db, user, item),
    )
