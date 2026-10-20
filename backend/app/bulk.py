"""Selected-row Delete and Export for the operator lists.

Delete uses the same permission as the row's existing Remove button.
Escalation mail is not a row you can delete: that notification is the
send ledger, and removing it makes the jobs loop mail the step again.
Audit has no prune, so it exports only.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Annotated
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session, joinedload, selectinload

from app.asset_extras import extras_rows, support_status
from app.audit import audit
from app.db import get_db
from app.history import audit_for, notes_for
from app.asset_types import snmp_port_for
from app.inventory import delete_asset, delete_candidate, is_snmp_asset, similar_incident_groups
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
    OncallSlot,
    Playbook,
    Playrule,
    ScheduledReport,
    User,
    utcnow,
)
from app.security import can, can_send_ops, user_from_session
from app.services import (
    compact_when_text,
    format_started_at,
    incident_host,
    incident_short_label,
    incident_source_label,
    incident_when,
    is_demo_incident,
    refresh_asset_status,
)
from app.web import NotAuthenticated, asset_playrules, evidence_row

router = APIRouter()

MAX_SELECTED = 100
# Operator mail. Anything else tied to an incident is an escalation step.
OPERATOR_MAIL_KEYS = frozenset({"manual", "report", "incident-report"})
_NEXT = {
    "/",
    "/#journal",
    "/incidents",
    "/assets",
    "/discovery",
    "/playrules",
    "/playbooks",
    "/escalation",
    "/escalation#oncall",
    "/journal",
    "/health-ui",
    "/health-ui#journal",
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


def _select_one_target(raw: str, default: str) -> str:
    """Stay on the list. Nothing is written when Edit does not have exactly one row."""
    target = raw if raw in _NEXT else default
    base, _, frag = target.partition("#")
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}edit_note=select-one"
    if frag:
        url += "#" + frag
    return url


def _safe_assets_url(raw: str) -> str:
    """List URL only. Query is kept when it is a normal Assets filter string."""
    text = (raw or "").replace("\n", "").replace("\r", "").strip().split("#", 1)[0]
    path, sep, query = text.partition("?")
    if path != "/assets":
        return "/assets"
    if any(ch in query for ch in " <>\"'\\"):
        return "/assets"
    return text if sep else "/assets"


def _assets_query(raw: str, **put: str) -> str:
    base = _safe_assets_url(raw)
    _, _, query = base.partition("?")
    drop = {"edit", "clone", "view", "edit_note", "notice"}
    pairs = [(key, value) for key, value in parse_qsl(query, keep_blank_values=True) if key not in drop and key not in put]
    for key, value in put.items():
        if value:
            pairs.append((key, value))
    encoded = urlencode(pairs)
    return "/assets" + (("?" + encoded) if encoded else "")


def _safe_discovery_url(raw: str) -> str:
    """List URL only. Query is kept when it is a normal Discovery filter string."""
    text = (raw or "").replace("\n", "").replace("\r", "").strip().split("#", 1)[0]
    path, sep, query = text.partition("?")
    if path != "/discovery":
        return "/discovery"
    if any(ch in query for ch in " <>\"'\\"):
        return "/discovery"
    return text if sep else "/discovery"


def _discovery_query(raw: str, **put: str) -> str:
    base = _safe_discovery_url(raw)
    _, _, query = base.partition("?")
    drop = {"edit", "clone", "import", "notice", "edit_note"}
    pairs = [(key, value) for key, value in parse_qsl(query, keep_blank_values=True) if key not in drop and key not in put]
    for key, value in put.items():
        if value:
            pairs.append((key, value))
    encoded = urlencode(pairs)
    return "/discovery" + (("?" + encoded) if encoded else "")


_LIST_PATHS = frozenset({"/playrules", "/playbooks", "/escalation"})


def _safe_list_url(raw: str, path: str) -> str:
    """List URL only. Query is kept when it is a normal filter string."""
    if path not in _LIST_PATHS:
        return "/playbooks" if path == "/playbooks" else "/playrules"
    text = (raw or "").replace("\n", "").replace("\r", "").strip().split("#", 1)[0]
    base, sep, query = text.partition("?")
    if base != path:
        return path
    if any(ch in query for ch in " <>\"'\\"):
        return path
    return text if sep else path


def _list_query(raw: str, path: str, **put: str) -> str:
    base = _safe_list_url(raw, path)
    _, _, query = base.partition("?")
    drop = {"edit", "clone", "view", "edit_note", "notice", "oncall_edit", "oncall_clone"}
    pairs = [(key, value) for key, value in parse_qsl(query, keep_blank_values=True) if key not in drop and key not in put]
    for key, value in put.items():
        if value:
            pairs.append((key, value))
    encoded = urlencode(pairs)
    return path + (("?" + encoded) if encoded else "")


def _one_pk(db: Session, model: type, selected: list[str]):
    ids = _ints(selected_values(selected))
    if len(ids) != 1:
        return None
    return db.get(model, ids[0])


def _one_asset(db: Session, selected: list[str]) -> Asset | None:
    ids = selected_values(selected)
    if len(ids) != 1:
        return None
    return db.query(Asset).filter(Asset.asset_id == ids[0]).first()


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


def _export_format(raw: str) -> str:
    """TXT is the default. CSV is an explicit opt-in from the pager dropdown."""
    return "csv" if str(raw or "").strip().lower() == "csv" else "txt"


def _csv_cell(value: object) -> str:
    if isinstance(value, datetime):
        return compact_when_text(value) or "—"
    if value is None:
        return "—"
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text if text.strip() else "—"


def _download_csv(filename: str, headers: list[str], rows: list[list[object]]) -> Response:
    """Table-shaped CSV. UTF-8 with BOM so Excel keeps Serbian Latin notes."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_csv_cell(cell) for cell in row])
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _ladder_csv(cubes: list[dict] | None) -> str:
    words = {"red": "mailed", "green": "ready"}
    parts = []
    for cube in cubes or []:
        level = f"L{cube.get('level')}"
        word = words.get(str(cube.get("tone") or ""), "—")
        parts.append(f"{level} {word}")
    return " · ".join(parts) if parts else "—"


def _yesno(flag: object) -> str:
    return "yes" if flag else "no"


def _need(raw: list[str]) -> list[str]:
    values = selected_values(raw)
    if not values:
        raise HTTPException(status_code=400, detail="Select at least one row")
    return values


_LABEL_W = 18


def _plain(value: object, *, keep_lines: bool = False) -> str:
    if isinstance(value, datetime):
        return compact_when_text(value) or "—"
    if value is None:
        return "—"
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    if keep_lines:
        lines = [line.rstrip() for line in text.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines) if any(line.strip() for line in lines) else "—"
    text = " ".join(text.split())
    return text or "—"


def _field(label: str, value: object) -> str:
    """One labeled line. Values stay on the right; nothing is joined with tabs or commas."""
    text = _plain(value)
    if len(label) >= _LABEL_W:
        return f"{label}  {text}"
    return f"{label:<{_LABEL_W}}{text}"


def _block(label: str, value: object) -> list[str]:
    raw = _plain(value, keep_lines=True)
    if raw == "—":
        return [_field(label, "—")]
    parts = raw.split("\n")
    if len(parts) == 1:
        return [_field(label, parts[0])]
    indent = " " * _LABEL_W
    return [_field(label, parts[0])] + [f"{indent}{line}" if line else "" for line in parts[1:]]


def _break_label(value: object, limit: int = 72) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text or "record"


def _join_docs(title: str, records: list[list[str]], labels: list[str] | None = None) -> list[str]:
    """Title, then each record as labeled lines.

    Two or more records get a titled break so the file is not one mashed blob.
    One record stays a single block. Values stay on vertical labeled lines.
    """
    lines = [title, ""]
    bodies: list[list[str]] = []
    names: list[str] = []
    for index, record in enumerate(records):
        body = list(record)
        while body and body[-1] == "":
            body.pop()
        if not body:
            continue
        bodies.append(body)
        raw = labels[index] if labels and index < len(labels) else ""
        names.append(_break_label(raw))
    total = len(bodies)
    for index, body in enumerate(bodies):
        if total >= 2:
            lines.append(f"======== {index + 1} of {total}  {names[index]} ========")
            lines.append("")
        elif index:
            lines.append("")
        lines.extend(body)
        if index < total - 1:
            lines.append("")
    return lines


def _alertname(row: Incident) -> str:
    payload = row.alert_payload if isinstance(row.alert_payload, dict) else {}
    labels = payload.get("labels") if isinstance(payload, dict) else None
    if isinstance(labels, dict) and labels.get("alertname"):
        return str(labels.get("alertname") or "")
    return ""


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


def _step_title(step: object) -> str:
    if isinstance(step, dict):
        return str(step.get("title") or step.get("name") or step)
    return str(step)


def _rca_lines(items: object, *keys: str) -> list[str]:
    lines: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            text = ""
            for key in keys:
                if item.get(key):
                    text = str(item.get(key))
                    break
            if not text:
                text = str(item)
        else:
            text = str(item)
        text = " ".join(text.split())
        if text:
            lines.append(f"    - {text}")
    return lines


def _incident_record(db: Session, row: Incident) -> list[str]:
    """Vertical snapshot of the incident page, through engineer evidence."""
    asset = row.asset
    when = incident_when(row)
    lines = [
        _field("id", row.number),
        _field("hostname", incident_host(row) or "—"),
        _field("incident", row.title or row.number),
        _field("short", incident_short_label(row.number)),
        _field("status", row.status),
        _field("severity", row.severity),
        _field("source", incident_source_label(row)),
        _field("first", format_started_at(row.started_at) or compact_when_text(row.started_at)),
    ]
    if when.get("duration"):
        if when.get("live"):
            lines.append(_field("duration", f"{when['duration']} (still open)"))
        else:
            ended = when.get("ended_full") or ""
            lines.append(_field("duration", f"{when['duration']} (ended {ended})".rstrip()))
    if when.get("ended"):
        lines.append(_field("ended", when.get("ended_full") or when.get("ended")))
    if row.ack_by:
        lines.append(_field("ack", f"{row.ack_by} {compact_when_text(row.ack_at)}".strip()))
    if row.resolved_by:
        lines.append(
            _field(
                "resolved by",
                f"{row.resolved_by} {compact_when_text(row.resolved_at or row.ended_at)}".strip(),
            )
        )
    lines.append(_field("alertname", _alertname(row)))
    lines.append(_field("demo", "yes" if is_demo_incident(row) else "no"))

    if asset is not None:
        lines.extend(["", "Who to call"])
        lines.append(_field("owner", asset.owner))
        lines.append(_field("contact", asset.contact_name))
        lines.append(_field("email", asset.owner_email or "No owner email"))
        lines.append(_field("phone", asset.owner_phone))
        support = support_status(asset)
        lines.append(_field("support", f"{support['label']} — {support['call_note']}"))
        for key, label, value in extras_rows(asset):
            if key == "runbook_note":
                continue
            lines.extend(_block(label, value))
        rules = asset_playrules(db, asset)
        if rules:
            shown = []
            for rule in rules:
                mark = " (used)" if row.playrule_id == rule.id else ""
                shown.append(f"{rule.name}{mark}")
            lines.append(_field("playrules", ", ".join(shown)))
        for key, label, value in extras_rows(asset):
            if key == "runbook_note":
                lines.extend(["", label])
                lines.extend(_block("note", value))

    lines.extend(["", "What happened"])
    lines.extend(_block("summary", row.summary or "Awaiting evidence."))
    investigations = list(row.investigations or [])
    investigation = investigations[-1] if investigations else None
    if investigation is not None:
        lines.extend(_block("rca", investigation.summary))
        if investigation.disclaimer:
            lines.extend(_block("disclaimer", investigation.disclaimer))
        engine = f"{investigation.engine or 'forgerca'} {investigation.engine_version or ''}".strip()
        lines.append(_field("engine", engine))
        lines.extend(_block("likely cause", investigation.likely_cause))
        lines.append(_field("confidence", f"{int(investigation.confidence or 0)}%"))
        lines.extend(_block("recommendation", investigation.recommended_action))
        rca = investigation.result if isinstance(investigation.result, dict) else {}
        for heading, key, sub in (
            ("Facts", "facts", "text"),
            ("Anomalies", "anomalies", "summary"),
            ("Candidate causes", "hypotheses", "summary"),
            ("Limitations", "limitations", "text"),
        ):
            bullets = _rca_lines(rca.get(key) or [], sub, "text")
            if bullets:
                lines.append(heading)
                lines.extend(bullets)
    else:
        lines.append(_field("rca", "ForgeRCA has not been run yet."))

    if row.playrule or row.playbook:
        lines.extend(["", "Workflow"])
        lines.append(_field("playrule", row.playrule.name if row.playrule else "—"))
        lines.append(_field("playbook", row.playbook.name if row.playbook else "—"))
        steps = list(row.playbook.steps or []) if row.playbook is not None else []
        if steps:
            lines.append("steps")
            for step in steps:
                lines.append(f"    - {_plain(_step_title(step))}")

    if asset is not None:
        groups = similar_incident_groups(db, asset)
        if groups:
            lines.extend(["", "Similar on this asset"])
            for group in groups:
                name = group.get("alertname") or group.get("title") or "Incident"
                lines.append(
                    _field(
                        "alert",
                        f"{name} — {group.get('count')} times ({group.get('open_count')} open), "
                        f"last {group.get('last_number')} {group.get('last_status')}",
                    )
                )

    lines.extend(["", "Who did what"])
    audits = audit_for(db, row.number)
    if not audits:
        lines.append(_field("audit", "No audit rows yet."))
    for entry in audits:
        lines.append(_field("when", entry.at))
        lines.append(_field("who", entry.actor))
        lines.append(_field("action", entry.action))
        detail = _audit_detail(entry)
        if detail:
            lines.extend(_block("detail", detail))
        lines.append("")

    lines.append("Operator notes")
    notes = notes_for(db, row)
    if not notes:
        lines.append(_field("notes", "No notes yet."))
    for note in notes:
        lines.append(_field("when", note.at))
        lines.append(_field("who", note.actor))
        lines.extend(_block("note", note.body))
        lines.append("")

    lines.append("Engineer evidence")
    evidence = sorted(row.evidence or [], key=lambda item: item.id or 0)
    if not evidence:
        lines.append(_field("evidence", "No evidence collected yet."))
    for item in evidence:
        view = evidence_row(item)
        lines.append(_field("when", f"{view['when']} {view['when_time']}".strip()))
        lines.append(_field("source", view["actor"]))
        lines.extend(_block("action", view["action"]))
        lines.extend(_block("full", view["full"]))
        lines.append("")
    return lines


def incidents_text(db: Session, numbers: list[str]) -> list[str]:
    rows = (
        db.query(Incident)
        .options(
            joinedload(Incident.asset),
            joinedload(Incident.playbook),
            joinedload(Incident.playrule),
        )
        .filter(Incident.number.in_(numbers))
        .all()
    )
    by = {row.number: row for row in rows}
    records = []
    labels = []
    for number in numbers:
        row = by.get(number)
        if row is None:
            continue
        records.append(_incident_record(db, row))
        labels.append(number)
    return _join_docs(f"ForgeSRE incidents ({len(records)})", records, labels)


def _incident_notes_csv(row: Incident) -> str:
    notes = list(getattr(row, "operator_notes", None) or [])
    notes.sort(key=lambda note: int(getattr(note, "id", None) or 0))
    bodies = []
    for note in notes:
        body = str(getattr(note, "body", "") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if body:
            bodies.append(body)
    if not bodies:
        return "—"
    return " | ".join(reversed(bodies))


def _incident_status_csv(row: Incident) -> str:
    from app.dc_noc import in_maintenance
    from app.web import incident_status_primary

    label = incident_status_primary(row.status, row.severity).get("label") or row.status or "—"
    bits = [label]
    if str(row.status or "").upper() == "MAINT" or (row.asset is not None and in_maintenance(row.asset)):
        bits.append("Maintenance")
    if row.ack_at:
        bits.append("Acknowledged")
    return " / ".join(bits)


def incidents_csv(db: Session, numbers: list[str]) -> tuple[list[str], list[list[object]]]:
    """Same columns as the Incidents table, including Notes as written."""
    from app.ladder_cubes import incident_ladder_cubes

    rows = (
        db.query(Incident)
        .options(
            joinedload(Incident.asset),
            selectinload(Incident.operator_notes),
        )
        .filter(Incident.number.in_(numbers))
        .all()
    )
    by = {row.number: row for row in rows}
    out: list[list[object]] = []
    for number in numbers:
        row = by.get(number)
        if row is None:
            continue
        out.append(
            [
                row.title or row.number,
                incident_host(row) or "—",
                _incident_status_csv(row),
                _ladder_csv(incident_ladder_cubes(row, db)),
                compact_when_text(row.started_at) or "—",
                row.severity or "—",
                row.resolved_by or "—",
                _incident_notes_csv(row),
            ]
        )
    return (
        ["Incident", "Hostname", "Status", "L1–L4", "When", "Severity", "Resolved by", "Notes"],
        out,
    )


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


def _asset_record(row: Asset) -> list[str]:
    lines = [
        _field("number", row.number),
        _field("id", row.asset_id),
        _field("hostname", row.hostname),
        _field("ip", row.ip),
        _field("type", row.type),
        _field("environment", row.environment),
        _field("status", row.status),
        _field("source", row.source),
        _field("owner", row.owner),
        _field("contact", row.contact_name),
        _field("email", row.owner_email),
        _field("phone", row.owner_phone),
        _field("scrape", row.scrape_address),
    ]
    try:
        snmp = is_snmp_asset(row)
    except AttributeError:
        snmp = False
    if snmp:
        lines.append(_field("snmp port", snmp_port_for(row) or "—"))
    lines.extend(_block("notes", row.notes))
    for key, label, value in extras_rows(row):
        lines.extend(_block(label, value))
    support = support_status(row)
    if support["state"] != "unknown":
        lines.append(_field("support", f"{support['label']} — {support['call_note']}"))
    return lines


def assets_text(db: Session, asset_ids: list[str]) -> list[str]:
    rows = db.query(Asset).filter(Asset.asset_id.in_(asset_ids)).all()
    by = {row.asset_id: row for row in rows}
    records = []
    labels = []
    for asset_id in asset_ids:
        row = by.get(asset_id)
        if row is None:
            continue
        records.append(_asset_record(row))
        labels.append(row.hostname or asset_id)
    return _join_docs(f"ForgeSRE assets ({len(records)})", records, labels)


def assets_csv(db: Session, asset_ids: list[str]) -> tuple[list[str], list[list[object]]]:
    from app.dc_noc import in_maintenance
    from app.inventory import asset_extra, asset_type_abbrev
    from app.ladder_cubes import asset_ladder_cubes

    rows = db.query(Asset).filter(Asset.asset_id.in_(asset_ids)).all()
    by = {row.asset_id: row for row in rows}
    out: list[list[object]] = []
    for asset_id in asset_ids:
        row = by.get(asset_id)
        if row is None:
            continue
        status = row.status or "—"
        if in_maintenance(row):
            status = f"{status} / Maintenance"
        contact = " ".join(part for part in (row.contact_name, row.owner_email) if part) or "—"
        out.append(
            [
                row.hostname or "—",
                status,
                _ladder_csv(asset_ladder_cubes(row, db)),
                row.number or "—",
                row.asset_id,
                row.ip or "—",
                row.owner or "—",
                contact,
                asset_type_abbrev(row.type) or row.type or "—",
                row.environment or "—",
                row.source or "—",
                asset_extra(row, "runbook_note") or "—",
            ]
        )
    return (
        ["Hostname", "Status", "L1–L4", "#", "ID", "IP", "Owner", "Contact", "Type", "Environment", "Source", "Notes"],
        out,
    )


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
    records = []
    labels = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        ports = ", ".join(str(port) for port in (row.open_ports or [])) or "—"
        records.append(
            [
                _field("ip", row.ip),
                _field("hostname", row.hostname),
                _field("role", row.proposed_role),
                _field("ports", ports),
                _field("status", row.status),
                _field("source", row.source),
            ]
        )
        labels.append(row.hostname or row.ip or str(pk))
    return _join_docs(f"ForgeSRE found assets ({len(records)})", records, labels)


def found_csv(db: Session, ids: list[int]) -> tuple[list[str], list[list[object]]]:
    rows = db.query(DiscoveryCandidate).filter(DiscoveryCandidate.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    out: list[list[object]] = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        ports = ", ".join(str(port) for port in (row.open_ports or [])) or "—"
        out.append(
            [
                row.hostname or "—",
                row.ip or "—",
                row.proposed_role or "—",
                ports,
                _yesno(row.node_exporter),
                _yesno(row.windows_exporter),
                _yesno(row.snmp_ok),
                row.status or "—",
                row.source or "—",
            ]
        )
    return (
        ["Hostname", "IP", "Proposed role", "Open ports", "node_exporter", "windows_exporter", "SNMP", "Status", "Source"],
        out,
    )


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
    """Name, alertname, stored rule text, and the alerts.yml expr when one exists.

    Playrules match alertname after ingest. The PromQL is the file text, not something this row runs.
    """
    from app.alert_rules import rules_for

    rows = db.query(Playrule).filter(Playrule.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    records = []
    labels = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        labels.append(row.name or str(pk))
        cond = row.condition if isinstance(row.condition, dict) else {}
        alertname = str(cond.get("alertname") or "")
        book = row.playbook.name if row.playbook else ""
        policy = row.escalation_policy.name if row.escalation_policy else ""
        exprs = [str(item.get("expr") or "").strip() for item in rules_for(alertname)]
        exprs = [item for item in exprs if item]
        parts: list[str] = []
        if exprs:
            parts.append("alerts.yml (not executed by the playrule): " + " | ".join(exprs))
        if cond.get("metric") or cond.get("operator") or cond.get("value") not in (None, ""):
            note = " ".join(
                str(part)
                for part in (cond.get("metric") or "", cond.get("operator") or "", cond.get("value") if cond.get("value") is not None else "")
                if str(part).strip()
            )
            if note:
                parts.append(f"stored note (not executed): {note}")
        records.append(
            [
                _field("name", row.name),
                _field("alertname", alertname),
                *_block("formula", " | ".join(parts)),
                _field("severity", row.severity),
                _field("escalation", policy),
                _field("enabled", "ON" if row.enabled else "OFF"),
                _field("playbook", book),
            ]
        )
    return _join_docs(f"ForgeSRE playrules ({len(records)})", records, labels)


def playrules_csv(db: Session, ids: list[int]) -> tuple[list[str], list[list[object]]]:
    from app.alert_rules import rules_for

    rows = db.query(Playrule).filter(Playrule.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    out: list[list[object]] = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        cond = row.condition if isinstance(row.condition, dict) else {}
        alertname = str(cond.get("alertname") or "")
        exprs = [str(item.get("expr") or "").strip() for item in rules_for(alertname)]
        fires = " | ".join(item for item in exprs if item) or "—"
        book = row.playbook.name if row.playbook else "—"
        out.append(
            [
                row.name or "—",
                alertname or "—",
                fires,
                row.severity or "—",
                book,
                "ON" if row.enabled else "OFF",
            ]
        )
    return (["Rule", "Alertname", "Fires when", "Severity", "Playbook", "Enabled"], out)


def delete_oncall_slots(db: Session, ids: list[int], actor: str) -> int:
    """Same as the per-row Remove: the roster loses that weekday window."""
    rows = db.query(OncallSlot).filter(OncallSlot.id.in_(ids)).all() if ids else []
    for row in rows:
        audit(db, "oncall.remove", actor=actor, object_type="oncall", object_id=str(row.id), data={"name": row.name})
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


def oncall_text(db: Session, ids: list[int]) -> list[str]:
    from app.dc_noc import oncall_slot_view

    rows = db.query(OncallSlot).filter(OncallSlot.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    records = []
    labels = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        view = oncall_slot_view(row)
        labels.append(view["window"] or str(pk))
        records.append(
            [
                _field("when", view["window"]),
                _field("name", view["name"]),
                _field("email", view["email"]),
                _field("phone", view["phone"] or "—"),
                _field("phone2", view["phone2"] or "—"),
                _field("enabled", "ON" if view["enabled"] else "OFF"),
            ]
        )
    return _join_docs(f"ForgeSRE on-call ({len(records)})", records, labels)


def oncall_csv(db: Session, ids: list[int]) -> tuple[list[str], list[list[object]]]:
    from app.dc_noc import oncall_slot_view

    rows = db.query(OncallSlot).filter(OncallSlot.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    out: list[list[object]] = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        view = oncall_slot_view(row)
        out.append(
            [
                view["window"] or "—",
                view["name"] or "—",
                view["email"] or "—",
                view["phone"] or "—",
                view["phone2"] or "—",
                "ON" if view["enabled"] else "OFF",
            ]
        )
    return (["When", "Name", "Email", "Phone", "Phone 2", "Enabled"], out)


def delete_playbooks(db: Session, ids: list[int], actor: str) -> int:
    """Same unlink as the per-card Remove: rules and incidents keep working without the book."""
    rows = db.query(Playbook).filter(Playbook.id.in_(ids)).all() if ids else []
    for row in rows:
        db.query(Playrule).filter(Playrule.playbook_id == row.id).update({Playrule.playbook_id: None})
        db.query(Incident).filter(Incident.playbook_id == row.id).update({Incident.playbook_id: None})
        slug = row.slug
        audit(db, "playbook.remove", actor=actor, object_type="playbook", object_id=slug)
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


def playbooks_text(db: Session, ids: list[int]) -> list[str]:
    rows = db.query(Playbook).filter(Playbook.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    records = []
    labels = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        labels.append(row.name or row.slug or str(pk))
        steps = "\n".join(_step_title(step).strip() for step in (row.steps or []) if _step_title(step).strip())
        body = [
            _field("name", row.name),
            _field("slug", row.slug),
        ]
        if (row.description or "").strip():
            body.extend(_block("description", row.description))
        body.extend(_block("steps", steps))
        records.append(body)
    return _join_docs(f"ForgeSRE playbooks ({len(records)})", records, labels)


def playbooks_csv(db: Session, ids: list[int]) -> tuple[list[str], list[list[object]]]:
    rows = db.query(Playbook).filter(Playbook.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    out: list[list[object]] = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        steps = " | ".join(_step_title(step).strip() for step in (row.steps or []) if _step_title(step).strip())
        out.append(
            [
                row.name or "—",
                row.slug or "—",
                row.description or "—",
                steps or "—",
            ]
        )
    return (["Name", "Slug", "Description", "Steps"], out)


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
    records = []
    labels = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        records.append(
            [
                _field("when", row.at),
                _field("module", row.module),
                _field("action", row.action),
                _field("status", row.status),
                *_block("summary", row.summary),
                *_block("detail", row.detail),
                _field("object", f"{row.object_type} {row.object_id}".strip()),
                _field("duration", f"{row.duration_ms} ms" if row.duration_ms else "—"),
            ]
        )
        labels.append(row.summary or f"{row.module} {row.action}".strip())
    return _join_docs(f"ForgeSRE journal ({len(records)})", records, labels)


def journal_csv(db: Session, ids: list[int]) -> tuple[list[str], list[list[object]]]:
    rows = db.query(JournalEntry).filter(JournalEntry.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    out: list[list[object]] = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        report = row.summary or ""
        if row.object_id:
            report = f"{report} · {row.object_type} {row.object_id}".strip()
        if row.detail:
            report = f"{report}\n{row.detail}".strip()
        out.append(
            [
                compact_when_text(row.at) or "—",
                row.module or "—",
                row.action or "—",
                row.status or "—",
                report or "—",
            ]
        )
    return (["When", "Module", "Action", "Status", "Report"], out)


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
    records = []
    labels = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        body = [
            _field("when", row.created_at),
            _field("to", row.target),
            _field("status", row.status),
            _field("step", row.step_key),
            _field("subject", row.subject),
        ]
        if (row.body or "").strip():
            body.extend(_block("body", (row.body or "")[:4000]))
        records.append(body)
        labels.append(row.subject or row.target)
    return _join_docs(f"ForgeSRE mail ({len(records)})", records, labels)


def mail_csv(db: Session, ids: list[int]) -> tuple[list[str], list[list[object]]]:
    rows = db.query(Notification).filter(Notification.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    out: list[list[object]] = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        out.append(
            [
                compact_when_text(row.created_at) or "—",
                row.target or "—",
                row.subject or "—",
                row.status or "—",
                (row.body or "")[:4000] or "—",
            ]
        )
    return (["When", "To", "Subject", "Status", "Body"], out)


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
    from app.services import report_recipients, schedule_label

    rows = db.query(ScheduledReport).filter(ScheduledReport.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    records = []
    labels = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        labels.append(row.name or str(pk))
        assets = ", ".join(str(item) for item in (row.asset_ids or [])) or "all"
        records.append(
            [
                _field("name", row.name),
                _field("to", ", ".join(report_recipients(row))),
                _field("schedule", schedule_label(row)),
                _field("enabled", "ON" if row.enabled else "OFF"),
                _field("assets", assets),
                _field("next", row.next_run_at),
            ]
        )
    return _join_docs(f"ForgeSRE scheduled reports ({len(records)})", records, labels)


def reports_csv(db: Session, ids: list[int]) -> tuple[list[str], list[list[object]]]:
    from app.services import report_recipients, schedule_label

    rows = db.query(ScheduledReport).filter(ScheduledReport.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    out: list[list[object]] = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        assets = ", ".join(str(item) for item in (row.asset_ids or [])) or "all"
        out.append(
            [
                row.name or "—",
                ", ".join(report_recipients(row)) or row.to_email or "—",
                schedule_label(row),
                assets,
                compact_when_text(row.last_run_at) or "—",
                compact_when_text(row.next_run_at) or "—",
                "ON" if row.enabled else "OFF",
            ]
        )
    return (["Name", "To", "Every", "Assets", "Last", "Next", "Enabled"], out)


def _audit_detail(row: AuditLog) -> str:
    data = row.data if isinstance(row.data, dict) else {}
    if not data:
        return ""
    parts = []
    for key, value in data.items():
        parts.append(f"{key}={value}")
    return ", ".join(parts)


def audit_text(db: Session, ids: list[int]) -> list[str]:
    rows = db.query(AuditLog).filter(AuditLog.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    records = []
    labels = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        records.append(
            [
                _field("when", row.at),
                _field("who", row.actor),
                _field("action", row.action),
                _field("object", f"{row.object_type} {row.object_id}".strip()),
                *_block("detail", _audit_detail(row)),
            ]
        )
        labels.append(row.action or str(pk))
    return _join_docs(f"ForgeSRE audit ({len(records)})", records, labels)


def audit_csv(db: Session, ids: list[int]) -> tuple[list[str], list[list[object]]]:
    rows = db.query(AuditLog).filter(AuditLog.id.in_(ids)).all() if ids else []
    by = {row.id: row for row in rows}
    out: list[list[object]] = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        out.append(
            [
                compact_when_text(row.at) or "—",
                row.actor or "—",
                row.action or "—",
                f"{row.object_type} {row.object_id}".strip() or "—",
            ]
        )
    return (["When", "Actor", "Action", "Object"], out)


def backups_text(names: list[str]) -> list[str]:
    """Selected backup folder names and compact times. Not the archive contents."""
    from app.backup import backup_compact, list_archives

    rows = {str(row.get("name") or ""): row for row in list_archives()}
    records = []
    labels = []
    for name in names:
        row = rows.get(name)
        if row is None:
            continue
        when = backup_compact(row)
        stamp = " ".join(part for part in (when.get("date"), when.get("time")) if part)
        labels.append(name)
        records.append(
            [
                _field("name", name),
                _field("when", stamp or None),
            ]
        )
    return _join_docs(f"ForgeSRE backups ({len(records)})", records, labels)


def backups_csv(names: list[str]) -> tuple[list[str], list[list[object]]]:
    from app.backup import backup_compact, format_size, list_archives

    rows = {str(row.get("name") or ""): row for row in list_archives()}
    out: list[list[object]] = []
    for name in names:
        row = rows.get(name)
        if row is None:
            continue
        when = backup_compact(row)
        stamp = " ".join(part for part in (when.get("date"), when.get("time")) if part) or "—"
        out.append([stamp, format_size(int(row.get("size") or 0)), name])
    return (["When", "Size", "Name"], out)


def incident_audit_text(db: Session, number: str, ids: list[int]) -> list[str]:
    """Who did what for one incident: when, who, action, and the extra audit fields."""
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.object_type == "incident", AuditLog.object_id == number, AuditLog.id.in_(ids))
        .all()
        if ids
        else []
    )
    by = {row.id: row for row in rows}
    records = []
    labels = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        records.append(
            [
                _field("when", row.at),
                _field("who", row.actor),
                _field("action", row.action),
                *_block("detail", _audit_detail(row)),
            ]
        )
        labels.append(row.action or str(pk))
    return _join_docs(f"ForgeSRE who did what {number} ({len(records)})", records, labels)


def incident_audit_csv(db: Session, number: str, ids: list[int]) -> tuple[list[str], list[list[object]]]:
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.object_type == "incident", AuditLog.object_id == number, AuditLog.id.in_(ids))
        .all()
        if ids
        else []
    )
    by = {row.id: row for row in rows}
    out: list[list[object]] = []
    for pk in ids:
        row = by.get(pk)
        if row is None:
            continue
        out.append(
            [
                compact_when_text(row.at) or "—",
                row.actor or "—",
                row.action or "—",
                _audit_detail(row) or "—",
            ]
        )
    return (["When", "Actor", "Action", "Detail"], out)


@router.post("/incidents/edit")
def incidents_edit(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    """Open one incident's notes. Zero or many rows do not write anything."""
    _forbid(user, can(user, "write_incidents"))
    ids = selected_values(selected)
    if len(ids) != 1:
        return RedirectResponse(_select_one_target(nxt, "/incidents"), status_code=303)
    row = db.query(Incident).filter(Incident.number == ids[0]).first()
    if row is None:
        return RedirectResponse(_select_one_target(nxt, "/incidents"), status_code=303)
    return RedirectResponse(f"/incidents/{row.number}?focus=notes#notes", status_code=303)


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


@router.post("/incidents/{number}/audit/export")
def incident_audit_export(
    number: str,
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can(user, "read_incidents"))
    row = db.query(Incident).filter_by(number=number).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    ids = _ints(_need(selected))
    safe = number.replace(":", "-")
    if _export_format(fmt) == "csv":
        headers, rows = incident_audit_csv(db, number, ids)
        return _download_csv(f"{safe}-who-did-what.csv", headers, rows)
    return _download(f"{safe}-who-did-what.txt", incident_audit_text(db, number, ids))


@router.post("/incidents/export")
def incidents_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can(user, "read_incidents"))
    ids = _need(selected)
    if _export_format(fmt) == "csv":
        headers, rows = incidents_csv(db, ids)
        return _download_csv("incidents.csv", headers, rows)
    return _download("incidents.txt", incidents_text(db, ids))


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


@router.post("/assets/open-view")
def assets_open_view(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    stay: Annotated[str, Form()] = "",
):
    """Open the list pane (metrics + form) for exactly one selected asset. Viewers can View."""
    _forbid(user, can(user, "read_assets"))
    row = _one_asset(db, selected)
    if row is None:
        return RedirectResponse(_assets_query(stay, edit_note="select-one"), status_code=303)
    return RedirectResponse(_assets_query(stay, view=row.asset_id), status_code=303)


@router.post("/assets/open-edit")
def assets_open_edit(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    stay: Annotated[str, Form()] = "",
):
    """Open Add/Edit for exactly one selected asset. Zero or many do not write."""
    _forbid(user, can(user, "write_assets"))
    row = _one_asset(db, selected)
    if row is None:
        return RedirectResponse(_assets_query(stay, edit_note="select-one"), status_code=303)
    return RedirectResponse(_assets_query(stay, edit=row.asset_id), status_code=303)


@router.post("/assets/open-clone")
def assets_open_clone(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    stay: Annotated[str, Form()] = "",
):
    _forbid(user, can(user, "write_assets"))
    row = _one_asset(db, selected)
    if row is None:
        return RedirectResponse(_assets_query(stay, edit_note="select-one"), status_code=303)
    return RedirectResponse(_assets_query(stay, clone=row.asset_id), status_code=303)


@router.post("/assets/open-verify")
def assets_open_verify(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    stay: Annotated[str, Form()] = "",
):
    """Verify the checked assets (one or more) on the existing verify page.

    Zero checked rows stay on the list. This does not probe the rest of inventory.
    """
    _forbid(user, can(user, "write_assets"))
    ids = selected_values(selected)
    if not ids:
        return RedirectResponse(_assets_query(stay, edit_note="select-one"), status_code=303)
    found = {
        row.asset_id
        for row in db.query(Asset).filter(Asset.asset_id.in_(ids)).all()
    }
    ordered = [item for item in ids if item in found]
    if not ordered:
        return RedirectResponse(_assets_query(stay, edit_note="select-one"), status_code=303)
    return RedirectResponse("/assets/verify?" + urlencode([("selected", item) for item in ordered]), status_code=303)


@router.post("/assets/export")
def assets_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can(user, "read_assets"))
    ids = _need(selected)
    if _export_format(fmt) == "csv":
        headers, rows = assets_csv(db, ids)
        return _download_csv("assets.csv", headers, rows)
    return _download("assets.txt", assets_text(db, ids))


@router.post("/discovery/open-import")
def discovery_open_import(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    stay: Annotated[str, Form()] = "",
):
    """Prefill Add asset from exactly one Found-assets row. Zero or many do not write."""
    _forbid(user, can(user, "write_assets"))
    row = _one_pk(db, DiscoveryCandidate, selected)
    if row is None:
        return RedirectResponse(_discovery_query(stay, edit_note="select-one"), status_code=303)
    return RedirectResponse(_discovery_query(stay, **{"import": str(row.id)}), status_code=303)


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
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can(user, "write_assets"))
    ids = _ints(_need(selected))
    if _export_format(fmt) == "csv":
        headers, rows = found_csv(db, ids)
        return _download_csv("found-assets.csv", headers, rows)
    return _download("found-assets.txt", found_text(db, ids))


@router.post("/playrules/open-edit")
def playrules_open_edit(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    stay: Annotated[str, Form()] = "",
):
    """Open the existing rule form for exactly one row. Zero or many do not write."""
    _forbid(user, can(user, "write_play"))
    row = _one_pk(db, Playrule, selected)
    if row is None:
        return RedirectResponse(_list_query(stay, "/playrules", edit_note="select-one"), status_code=303)
    return RedirectResponse(_list_query(stay, "/playrules", edit=str(row.id)) + "#playrule-form", status_code=303)


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
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can(user, "read_play"))
    ids = _ints(_need(selected))
    if _export_format(fmt) == "csv":
        headers, rows = playrules_csv(db, ids)
        return _download_csv("playrules.csv", headers, rows)
    return _download("playrules.txt", playrules_text(db, ids))


@router.post("/playbooks/open-edit")
def playbooks_open_edit(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    stay: Annotated[str, Form()] = "",
):
    """Open the existing playbook form for exactly one row. Zero or many do not write."""
    _forbid(user, can(user, "write_play"))
    row = _one_pk(db, Playbook, selected)
    if row is None:
        return RedirectResponse(_list_query(stay, "/playbooks", edit_note="select-one"), status_code=303)
    return RedirectResponse(_list_query(stay, "/playbooks", edit=str(row.id)) + "#playbook-form", status_code=303)


@router.post("/playbooks/bulk-delete")
def playbooks_bulk_delete(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    _forbid(user, can(user, "write_play"))
    delete_playbooks(db, _ints(selected_values(selected)), user.email)
    return _back(nxt, "/playbooks")


@router.post("/playbooks/export")
def playbooks_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can(user, "read_play"))
    ids = _ints(_need(selected))
    if _export_format(fmt) == "csv":
        headers, rows = playbooks_csv(db, ids)
        return _download_csv("playbooks.csv", headers, rows)
    return _download("playbooks.txt", playbooks_text(db, ids))


@router.post("/escalation/oncall/open-edit")
def oncall_open_edit(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    stay: Annotated[str, Form()] = "",
):
    """Open the existing on-call form for exactly one row. Zero or many do not write."""
    _forbid(user, can(user, "write_play"))
    row = _one_pk(db, OncallSlot, selected)
    if row is None:
        return RedirectResponse(_list_query(stay, "/escalation", edit_note="select-one") + "#oncall", status_code=303)
    return RedirectResponse(_list_query(stay, "/escalation", oncall_edit=str(row.id)) + "#oncall-form", status_code=303)


@router.post("/escalation/oncall/open-clone")
def oncall_open_clone(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    stay: Annotated[str, Form()] = "",
):
    """Prefill Add on-call from exactly one row. Zero or many do not write."""
    _forbid(user, can(user, "write_play"))
    row = _one_pk(db, OncallSlot, selected)
    if row is None:
        return RedirectResponse(_list_query(stay, "/escalation", edit_note="select-one") + "#oncall", status_code=303)
    return RedirectResponse(_list_query(stay, "/escalation", oncall_clone=str(row.id)) + "#oncall-form", status_code=303)


@router.post("/escalation/oncall/bulk-delete")
def oncall_bulk_delete(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    _forbid(user, can(user, "write_play"))
    delete_oncall_slots(db, _ints(selected_values(selected)), user.email)
    return _back(nxt, "/escalation#oncall")


@router.post("/escalation/oncall/export")
def oncall_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can(user, "read_play"))
    ids = _ints(_need(selected))
    if _export_format(fmt) == "csv":
        headers, rows = oncall_csv(db, ids)
        return _download_csv("oncall.csv", headers, rows)
    return _download("oncall.txt", oncall_text(db, ids))


@router.post("/journal/bulk-delete")
def journal_bulk_delete(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    nxt: Annotated[str, Form(alias="next")] = "",
):
    _forbid(user, can(user, "read_play"))
    delete_journal_rows(db, _ints(selected_values(selected)), user.email)
    return _back(nxt, "/health-ui#journal")


@router.post("/journal/export")
def journal_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can(user, "read_play"))
    ids = _ints(_need(selected))
    if _export_format(fmt) == "csv":
        headers, rows = journal_csv(db, ids)
        return _download_csv("journal.csv", headers, rows)
    return _download("journal.txt", journal_text(db, ids))


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
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can_send_ops(user))
    ids = _ints(_need(selected))
    if _export_format(fmt) == "csv":
        headers, rows = mail_csv(db, ids)
        return _download_csv("mail.csv", headers, rows)
    return _download("mail.txt", mail_text(db, ids))


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
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can_send_ops(user))
    ids = _ints(_need(selected))
    if _export_format(fmt) == "csv":
        headers, rows = reports_csv(db, ids)
        return _download_csv("scheduled-reports.csv", headers, rows)
    return _download("scheduled-reports.txt", reports_text(db, ids))


@router.post("/admin/audit/export")
def audit_export(
    db: Session = Depends(get_db),
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    _forbid(user, can(user, "admin"))
    ids = _ints(_need(selected))
    if _export_format(fmt) == "csv":
        headers, rows = audit_csv(db, ids)
        return _download_csv("audit.csv", headers, rows)
    return _download("audit.txt", audit_text(db, ids))


@router.post("/admin/backups/export")
def backups_export(
    user: User = Depends(_user),
    selected: Annotated[list[str], Form()] = [],
    fmt: Annotated[str, Form(alias="format")] = "txt",
):
    """Names and times of the selected backup folders. Archive bytes stay on Download."""
    _forbid(user, can(user, "admin"))
    names = _need(selected)
    if _export_format(fmt) == "csv":
        headers, rows = backups_csv(names)
        return _download_csv("backups.csv", headers, rows)
    return _download("backups.txt", backups_text(names))
