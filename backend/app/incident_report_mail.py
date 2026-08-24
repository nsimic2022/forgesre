"""Incident report: plain-text + HTML bodies for Send incident report."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.email_html import DASH, esc, html_list, prose_to_html, render_email
from app.models import Asset, Incident

SNAPSHOT_FOOTER = "This is a snapshot. ForgeSRE does not execute playbooks."


def _demo(incident: Incident) -> tuple[bool, str]:
    from app.services import demo_body_line, is_demo_incident

    if is_demo_incident(incident):
        return True, demo_body_line(incident)
    return False, ""


def _attach_asset(db: Session | None, incident: Incident) -> Asset | None:
    if incident.asset is not None:
        return incident.asset
    if db is not None and incident.asset_id:
        incident.asset = db.get(Asset, incident.asset_id)
    return incident.asset


def _rca_payload(incident: Incident) -> tuple[Any, dict[str, Any]]:
    investigation = incident.investigations[-1] if incident.investigations else None
    rca = (investigation.result if investigation else None) or {}
    if not isinstance(rca, dict):
        rca = {}
    return investigation, rca


def _item_text(item: Any, *keys: str) -> str:
    if isinstance(item, dict):
        for key in keys:
            value = item.get(key)
            if value:
                return str(value)
        return str(item)
    return str(item)


def _list_texts(items: Any, *keys: str) -> list[str]:
    out: list[str] = []
    for item in items or []:
        text = _item_text(item, *keys).strip()
        if text:
            out.append(text)
    return out


def build_incident_report(db: Session, incident: Incident) -> str:
    """Text report for one INC. Includes ForgeRCA when it has already run."""
    asset = _attach_asset(db, incident)
    investigation, rca = _rca_payload(incident)
    demo, demo_line = _demo(incident)
    lines = ["ForgeSRE incident report"]
    if demo:
        lines.append(demo_line)
    lines.extend(
        [
            f"Incident: {incident.number}",
            f"Title: {incident.title}",
            f"Severity: {incident.severity}",
            f"Status: {incident.status}",
            f"Started: {incident.started_at}",
        ]
    )
    if incident.ack_by:
        lines.append(f"Ack: {incident.ack_by} {incident.ack_at or ''}".rstrip())
    if incident.resolved_by:
        lines.append(
            f"Resolved/closed: {incident.resolved_by} {incident.resolved_at or incident.ended_at or ''}".rstrip()
        )
    if asset:
        lines.extend(
            [
                f"Asset: {asset.hostname} ({asset.asset_id})",
                f"Type: {asset.type or '—'}  IP: {asset.ip or '—'}",
                f"Owner: {asset.owner or '—'}  Contact: {asset.contact_name or '—'}",
                f"Email: {asset.owner_email or '—'}  Phone: {asset.owner_phone or '—'}",
            ]
        )
    if incident.summary:
        lines.extend(["", "Alert summary:", incident.summary])
    if incident.playbook:
        lines.append(f"Playbook: {incident.playbook.name} (guidance only — not executed)")
    if investigation:
        lines.extend(
            [
                "",
                f"## ForgeRCA ({investigation.engine or 'forgerca'} {investigation.engine_version or ''} · {investigation.provider or ''})".strip(),
                f"Summary: {investigation.summary or '—'}",
                f"Likely cause: {investigation.likely_cause or '—'}",
                f"Confidence: {int(investigation.confidence or 0)}% (ForgeSRE score, not a validated model)",
                f"What should I do: {investigation.recommended_action or '—'}",
            ]
        )
        facts = rca.get("facts") or []
        if facts:
            lines.append("Facts:")
            for fact in facts:
                text = fact.get("text") if isinstance(fact, dict) else fact
                lines.append(f"- {text}")
        anomalies = rca.get("anomalies") or []
        if anomalies:
            lines.append("Anomalies:")
            for item in anomalies:
                text = item.get("summary") if isinstance(item, dict) else item
                lines.append(f"- {text}")
        hyps = rca.get("hypotheses") or []
        if hyps:
            lines.append("Candidate causes:")
            for hyp in hyps:
                text = hyp.get("summary") if isinstance(hyp, dict) else hyp
                lines.append(f"- {text}")
        limits = rca.get("limitations") or []
        if limits:
            lines.append("Limitations:")
            for line in limits:
                lines.append(f"- {line}")
    else:
        lines.extend(["", "ForgeRCA has not been run yet."])
    notes = list(incident.operator_notes or [])
    if notes:
        lines.append("")
        lines.append("Operator notes:")
        for note in notes:
            lines.append(f"- {note.at} {note.actor}: {note.body}")
    lines.append("")
    lines.append(SNAPSHOT_FOOTER)
    return "\n".join(lines) + "\n"


def build_incident_report_html(db: Session | None, incident: Incident) -> str:
    """HTML alternative for the same facts as build_incident_report."""
    asset = _attach_asset(db, incident)
    investigation, rca = _rca_payload(incident)
    demo, demo_line = _demo(incident)
    meta: list[tuple[str, str]] = [
        ("Incident", esc(incident.number)),
        ("Title", esc(incident.title)),
        ("Severity", esc(incident.severity)),
        ("Status", esc(incident.status)),
        ("Started", esc(incident.started_at)),
    ]
    if incident.ack_by:
        meta.append(("Ack", esc(f"{incident.ack_by} {incident.ack_at or ''}".rstrip())))
    if incident.resolved_by:
        when = incident.resolved_at or incident.ended_at or ""
        meta.append(("Resolved/closed", esc(f"{incident.resolved_by} {when}".rstrip())))
    if asset:
        meta.extend(
            [
                ("Asset", esc(f"{asset.hostname} ({asset.asset_id})")),
                ("Type", esc(asset.type or DASH)),
                ("IP", esc(asset.ip or DASH)),
                ("Owner", esc(asset.owner or DASH)),
                ("Contact", esc(asset.contact_name or DASH)),
                ("Email", esc(asset.owner_email or DASH)),
                ("Phone", esc(asset.owner_phone or DASH)),
            ]
        )
    if incident.playbook:
        meta.append(("Playbook", esc(f"{incident.playbook.name} (guidance only — not executed)")))

    sections: list[tuple[str, str, bool]] = []
    if incident.summary:
        sections.append(("Alert summary", prose_to_html(incident.summary), True))
    if investigation:
        engine = f"{investigation.engine or 'forgerca'} {investigation.engine_version or ''} · {investigation.provider or ''}".strip()
        sections.append((f"ForgeRCA · {engine}", prose_to_html(investigation.summary or DASH), False))
        sections.append(("Likely cause", prose_to_html(investigation.likely_cause or DASH), False))
        sections.append(
            (
                "Confidence",
                prose_to_html(
                    f"{int(investigation.confidence or 0)}% (ForgeSRE score, not a validated model)"
                ),
                False,
            )
        )
        sections.append(("Recommendation", prose_to_html(investigation.recommended_action or DASH), False))
        facts = _list_texts(rca.get("facts") or [], "text")
        if facts:
            sections.append(("Facts", html_list(facts), False))
        anomalies = _list_texts(rca.get("anomalies") or [], "summary", "text")
        if anomalies:
            sections.append(("Anomalies", html_list(anomalies), False))
        hyps = _list_texts(rca.get("hypotheses") or [], "summary", "text")
        if hyps:
            sections.append(("Candidate causes", html_list(hyps), False))
        limits = _list_texts(rca.get("limitations") or [], "text")
        if limits:
            sections.append(("Limitations", html_list(limits), False))
    else:
        sections.append(("ForgeRCA", prose_to_html("ForgeRCA has not been run yet."), False))
    notes = list(incident.operator_notes or [])
    if notes:
        note_lines = [f"{note.at} {note.actor}: {note.body}" for note in notes]
        sections.append(("Operator notes", html_list(note_lines), False))

    return render_email(
        kicker="Incident report",
        heading=str(incident.number or "Incident"),
        severity=str(incident.severity or ""),
        status=str(incident.status or ""),
        is_demo=demo,
        demo_line=demo_line,
        meta_rows=meta,
        sections=sections,
        footer=SNAPSHOT_FOOTER,
        kind="incident-report",
    )


def incident_report_html(incident: Incident, db: Session | None = None) -> str:
    """HTML twin used by SMTP send and tests (db optional when asset is loaded)."""
    return build_incident_report_html(db, incident)
