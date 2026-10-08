"""Selected-row Delete and Export for the operator lists.

Delete uses the same permission as the row's existing Remove button.
Escalation mail is not a row you can delete: that notification is the
send ledger, and removing it makes the jobs loop mail the step again.
Audit has no prune, so it exports only.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.audit import audit
from app.db import get_db
from app.inventory import delete_asset, delete_candidate
from app.journal import report
from app.models import (
    Asset,
    AuditLog,
    DiscoveryCandidate,
    Evidence,
    Incident,
    IncidentEvent,
    IncidentNote,
    Investigation,
    Job,
    JournalEntry,
    Notification,
    Playrule,
    ScheduledReport,
    User,
    utcnow,
)
from app.security import can, can_send_ops, user_from_session
from app.services import incident_host, refresh_asset_status
from app.web import NotAuthenticated

router = APIRouter()

MAX_SELECTED = 100
# Operator mail. Anything else tied to an incident is an escalation step.
OPERATOR_MAIL_KEYS = frozenset({"manual", "report", "incident-report"})
_NEXT = {
    "/",
    "/#journal",
    "/incidents",
    "/history",
    "/assets",
    "/discovery",
    "/playrules",
    "/journal",
    "/ops",
    "/ops#mail",
    "/ops#reports",
    "/admin",
}


def _user(request: Request, db: Session = Depends(get_db)) -> User:
    user = user_from_session(db, request.cookies.get("forgesre_session"))
    if user is None:
        raise NotAuthenticated()
    return user


def _forbid(user: User, ok: bool) -> None:
    if not ok:
        raise HTTPException(status_code=403, detail="forbidden")


def selected_values(raw: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        text = str(item or "").strip()
        if not text or text in seen or len(text) > 80:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= MAX_SELECTED:
            break
    return out


def _ints(raw: list[str]) -> list[int]:
    return [int(item) for item in raw if item.isdigit()]


def _back(raw: str, default: str) -> RedirectResponse:
    target = raw if raw in _NEXT else default
    return RedirectResponse(target, status_code=303)


def _download(filename: str, lines: list[str]) -> PlainTextResponse:
    body = "\n".join(lines)
    if not body.endswith("\n"):
        body += "\n"
    return PlainTextResponse(
        body,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _need(raw: list[str]) -> list[str]:
    values = selected_values(raw)
    if not values:
        raise HTTPException(status_code=400, detail="Select at least one row")
    return values


def _cell(value: object) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text or "—"


def mail_row_deletable(row: Notification) -> bool:
    """Escalation steps are the send ledger. Deleting one makes the jobs loop mail it again."""
    if row.incident_id and (row.step_key or "") not in OPERATOR_MAIL_KEYS:
        return False
    return True


def delete_incidents(db: Session, numbers: list[str], actor: str) -> int:
    rows = db.query(Incident).filter(Incident.number.in_(numbers)).all() if numbers else []
    if not rows:
        return 0
    ids = [row.id for row in rows]
    nums = [row.number for row in rows]
    assets: list[Asset] = []
    seen: set[int] = set()
    for row in rows:
        asset = row.asset
        if asset is not None and asset.id not in seen:
            assets.append(asset)
            seen.add(asset.id)
    now = utcnow()
    db.query(Job).filter(Job.object_id.in_(nums), Job.status == "pending").update(
        {Job.status: "error", Job.error: "incident deleted", Job.finished_at: now},
        synchronize_session=False,
    )
    # Keep the outbox row, but detach it. The incident is gone, so the ladder cannot send it again.
    db.query(Notification).filter(Notification.incident_id.in_(ids)).update(
        {Notification.incident_id: None},
        synchronize_session=False,
    )
    for model in (Evidence, Investigation, IncidentEvent, IncidentNote):
        db.query(model).filter(model.incident_id.in_(ids)).delete(synchronize_session=False)
    for row in rows:
        audit(
            db,
            "incident.delete",
            actor=actor,
            object_type="incident",
            object_id=row.number,
            data={"title": row.title, "status": row.status},
        )
        db.delete(row)
    db.commit()
    for asset in assets:
        refresh_asset_status(db, asset)
    db.commit()
    report(
        db,
        "incidents",
        "delete",
        "ok",
        summary=f"Removed {len(rows)} incident(s)",
        detail=" ".join(nums)[:400],
        object_type="incident",
    )
    return len(rows)


def incidents_text(db: Session, numbers: list[str]) -> list[str]:
    rows = (
        db.query(Incident)
        .options(joinedload(Incident.asset))
        .filter(Incident.number.in_(numbers))
        .all()
    )
    by = {row.number: row for row in rows}
    lines = [f"ForgeSRE incidents ({len(by)})"]
    for number in numbers:
        row = by.get(number)
        if row is None:
            continue
        lines.append(
            "\t".join(
                [
                    _cell(row.number),
                    _cell(row.severity),
                    _cell(row.status),
                    _cell(incident_host(row)),
                    _cell(row.title),
                ]
            )
        )
    return lines


def delete_assets(db: Session, asset_ids: list[str], actor: str) -> int:
    removed = 0
    for asset_id in asset_ids:
        item = db.query(Asset).filter_by(asset_id=asset_id).first()
        if item is None:
            continue
        try:
            delete_asset(db, item, actor=actor)
        except ValueError:
            continue
        removed += 1
    return removed


def assets_text(db: Session, asset_ids: list[str]) -> list[str]:
    rows = db.query(Asset).filter(Asset.asset_id.in_(asset_ids)).all()
    by = {row.asset_id: row for row in rows}
    lines = [f"ForgeSRE assets ({len(by)})"]
    for asset_id in asset_ids:
        row = by.get(asset_id)
        if row is None:
            continue
        extras = row.extras if isinstance(row.extras, dict) else {}
        lines.append(
            "\t".join(
                [
                    _cell(row.number),
                    _cell(row.asset_id),
                    _cell(row.hostname),
                    _cell(row.ip),
                    _cell(row.type),
                    _cell(row.status),
                    _cell(row.owner_email),
                    _cell(extras.get("site")),
                    _cell(extras.get("customer")),
                ]
            )
        )
    return lines


def delete_found(db: Session, ids: list[int], actor: str) -> int:
    removed = 0
    for pk in ids:
        row = db.get(DiscoveryCandidate, pk)
        if row is None:
            continue
        delete_candidate(db, row, actor=actor)
        removed += 1
    return removed


def found_text(db: Session, ids: list[int]) -> list[str]:
    rows = db.query(DiscoveryCandidate).filter(DiscoveryCandidate.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    lines = [f"ForgeSRE found assets ({len(by)})"]
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        ports = ", ".join(str(port) for port in (row.open_ports or [])) or "—"
        lines.append(
            "\t".join(
                [
                    _cell(row.ip),
                    _cell(row.hostname),
                    _cell(row.proposed_role),
                    _cell(ports),
                    _cell(row.status),
                    _cell(row.source),
                ]
            )
        )
    return lines


def delete_playrules(db: Session, ids: list[int], actor: str) -> int:
    rows = db.query(Playrule).filter(Playrule.id.in_(ids)).all() if ids else []
    for row in rows:
        db.query(Incident).filter(Incident.playrule_id == row.id).update(
            {Incident.playrule_id: None},
            synchronize_session=False,
        )
        name = row.name
        audit(db, "playrule.remove", actor=actor, object_type="playrule", object_id=name)
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


def playrules_text(db: Session, ids: list[int]) -> list[str]:
    rows = db.query(Playrule).filter(Playrule.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    lines = [f"ForgeSRE playrules ({len(by)})"]
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        cond = row.condition if isinstance(row.condition, dict) else {}
        book = row.playbook.name if row.playbook else ""
        lines.append(
            "\t".join(
                [
                    _cell(row.name),
                    _cell(cond.get("alertname")),
                    _cell(row.severity),
                    "ON" if row.enabled else "OFF",
                    _cell(book),
                ]
            )
        )
    return lines


def delete_journal_rows(db: Session, ids: list[int], actor: str) -> int:
    rows = db.query(JournalEntry).filter(JournalEntry.id.in_(ids)).all() if ids else []
    if not rows:
        return 0
    for row in rows:
        db.delete(row)
    audit(db, "journal.delete", actor=actor, object_type="journal", object_id=str(len(rows)))
    db.commit()
    report(
        db,
        "journal",
        "delete",
        "ok",
        summary=f"Removed {len(rows)} journal row(s)",
        object_type="journal",
        object_id=str(len(rows)),
    )
    return len(rows)


def journal_text(db: Session, ids: list[int]) -> list[str]:
    rows = db.query(JournalEntry).filter(JournalEntry.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    lines = [f"ForgeSRE journal ({len(by)})"]
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        lines.append(
            "\t".join(
                [
                    _cell(row.at),
                    _cell(row.module),
                    _cell(row.action),
                    _cell(row.status),
                    _cell(row.summary),
                    _cell(row.object_type),
                    _cell(row.object_id),
                ]
            )
        )
    return lines


def delete_mail(db: Session, ids: list[int], actor: str) -> int:
    rows = db.query(Notification).filter(Notification.id.in_(ids)).all() if ids else []
    removed = 0
    for row in rows:
        if not mail_row_deletable(row):
            continue
        audit(
            db,
            "mail.delete",
            actor=actor,
            object_type="notification",
            object_id=str(row.id),
            data={"to": row.target, "step": row.step_key},
        )
        db.delete(row)
        removed += 1
    if removed:
        db.commit()
    return removed


def mail_text(db: Session, ids: list[int]) -> list[str]:
    rows = db.query(Notification).filter(Notification.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    lines = [f"ForgeSRE mail ({len(by)})"]
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        lines.append(
            "\t".join(
                [
                    _cell(row.created_at),
                    _cell(row.target),
                    _cell(row.status),
                    _cell(row.step_key),
                    _cell(row.subject),
                ]
            )
        )
        body = (row.body or "").strip()
        if body:
            lines.append(body[:4000])
            lines.append("")
    return lines


def delete_reports(db: Session, ids: list[int], actor: str) -> int:
    rows = db.query(ScheduledReport).filter(ScheduledReport.id.in_(ids)).all() if ids else []
    for row in rows:
        audit(
            db,
            "report.delete",
            actor=actor,
            object_type="report",
            object_id=str(row.id),
            data={"name": row.name, "to": row.to_email},
        )
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


def reports_text(db: Session, ids: list[int]) -> list[str]:
    rows = db.query(ScheduledReport).filter(ScheduledReport.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    lines = [f"ForgeSRE scheduled reports ({len(by)})"]
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        assets = ", ".join(str(item) for item in (row.asset_ids or [])) or "all"
        lines.append(
            "\t".join(
                [
                    _cell(row.name),
                    _cell(row.to_email),
                    f"{int(row.interval_hours or 0)}h",
                    "ON" if row.enabled else "OFF",
                    _cell(assets),
                    _cell(row.next_run_at),
                ]
            )
        )
    return lines


def audit_text(db: Session, ids: list[int]) -> list[str]:
    rows = db.query(AuditLog).filter(AuditLog.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    lines = [f"ForgeSRE audit ({len(by)})"]
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        lines.append(
            "\t".join(
                [
                    _cell(row.at),
                    _cell(row.actor),
                    _cell(row.action),
                    _cell(row.object_type),
                    _cell(row.object_id),
                ]
            )
        )
    return lines


@router.post("/incidents/bulk-delete")
def incidents_bulk_delete(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    _forbid(user, can(user, "write_incidents"))
    delete_incidents(db, selected_values(selected), user.email)
    return _back(nxt, "/incidents")


@router.post("/incidents/export")
def incidents_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
):
    _forbid(user, can(user, "read_incidents"))
    return _download("incidents.txt", incidents_text(db, _need(selected)))


@router.post("/assets/bulk-delete")
def assets_bulk_delete(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    _forbid(user, can(user, "write_assets"))
    delete_assets(db, selected_values(selected), user.email)
    return _back(nxt, "/assets")


@router.post("/assets/export")
def assets_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
):
    _forbid(user, can(user, "read_assets"))
    return _download("assets.txt", assets_text(db, _need(selected)))


@router.post("/discovery/bulk-delete")
def discovery_bulk_delete(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    _forbid(user, can(user, "write_assets"))
    delete_found(db, _ints(selected_values(selected)), user.email)
    return _back(nxt, "/discovery")


@router.post("/discovery/export")
def discovery_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
):
    _forbid(user, can(user, "write_assets"))
    return _download("found-assets.txt", found_text(db, _ints(_need(selected))))


@router.post("/playrules/bulk-delete")
def playrules_bulk_delete(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    _forbid(user, can(user, "write_play"))
    delete_playrules(db, _ints(selected_values(selected)), user.email)
    return _back(nxt, "/playrules")


@router.post("/playrules/export")
def playrules_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
):
    _forbid(user, can(user, "read_play"))
    return _download("playrules.txt", playrules_text(db, _ints(_need(selected))))


@router.post("/journal/bulk-delete")
def journal_bulk_delete(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    _forbid(user, can(user, "read_play"))
    delete_journal_rows(db, _ints(selected_values(selected)), user.email)
    return _back(nxt, "/journal")


@router.post("/journal/export")
def journal_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
):
    _forbid(user, can(user, "read_play"))
    return _download("journal.txt", journal_text(db, _ints(_need(selected))))


@router.post("/ops/mail/bulk-delete")
def mail_bulk_delete(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    _forbid(user, can_send_ops(user))
    delete_mail(db, _ints(selected_values(selected)), user.email)
    return _back(nxt, "/ops#mail")


@router.post("/ops/mail/export")
def mail_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
):
    return _download("mail.txt", mail_text(db, _ints(_need(selected))))


@router.post("/ops/reports/bulk-delete")
def reports_bulk_delete(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    _forbid(user, can_send_ops(user))
    delete_reports(db, _ints(selected_values(selected)), user.email)
    return _back(nxt, "/ops#reports")


@router.post("/ops/reports/export")
def reports_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
):
    _forbid(user, can_send_ops(user))
    return _download("scheduled-reports.txt", reports_text(db, _ints(_need(selected))))


@router.post("/admin/audit/export")
def audit_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
):
    _forbid(user, can(user, "admin"))
    return _download("audit.txt", audit_text(db, _ints(_need(selected))))
