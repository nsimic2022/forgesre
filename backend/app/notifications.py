"""Escalation notification bodies (plain + HTML). Same visual language as incident reports."""

from __future__ import annotations

from app.email_html import DASH, esc, prose_to_html, render_email
from app.models import Incident

ESCALATION_FOOTER = "This is a snapshot. ForgeSRE does not execute playbooks."


def _demo(incident: Incident) -> tuple[bool, str]:
    from app.services import demo_body_line, is_demo_incident

    if is_demo_incident(incident):
        return True, demo_body_line(incident)
    return False, ""


def build_escalation_body(incident: Incident, step_key: str, policy_role: str) -> str:
    """Plain-text escalation notice. Kept as the text/plain MIME part and outbox body."""
    asset = incident.asset
    demo, demo_line = _demo(incident)
    lines = []
    if demo:
        lines.append(demo_line)
    lines.extend(
        [
            f"Incident: {incident.number}",
            f"Title: {incident.title}",
            f"Severity: {incident.severity}",
            f"Status: {incident.status}",
            f"Escalation step: {step_key} (policy role: {policy_role})",
            f"Asset: {asset.hostname if asset else 'unknown'}",
            f"Playbook: {incident.playbook.name if incident.playbook else 'n/a'}",
        ]
    )
    if asset:
        lines.extend(
            [
                f"Owner: {asset.owner or '—'}",
                f"Contact: {asset.contact_name or '—'}",
                f"Email: {asset.owner_email or '—'}",
                f"Phone: {asset.owner_phone or '—'}",
            ]
        )
        if asset.notes:
            lines.append(f"Notes: {asset.notes}")
    return "\n".join(lines) + "\n"


def build_escalation_html(incident: Incident, step_key: str, policy_role: str) -> str:
    """HTML alternative for the same facts as build_escalation_body."""
    asset = incident.asset
    demo, demo_line = _demo(incident)
    meta: list[tuple[str, str]] = [
        ("Incident", esc(incident.number)),
        ("Title", esc(incident.title)),
        ("Severity", esc(incident.severity)),
        ("Status", esc(incident.status)),
        ("Escalation step", esc(f"{step_key} (policy role: {policy_role})")),
        ("Asset", esc(asset.hostname if asset else "unknown")),
        ("Playbook", esc(incident.playbook.name if incident.playbook else "n/a")),
    ]
    sections: list[tuple[str, str, bool]] = []
    if asset:
        meta.extend(
            [
                ("Owner", esc(asset.owner or DASH)),
                ("Contact", esc(asset.contact_name or DASH)),
                ("Email", esc(asset.owner_email or DASH)),
                ("Phone", esc(asset.owner_phone or DASH)),
                ("IP", esc(asset.ip or DASH)),
            ]
        )
        if asset.notes:
            sections.append(("Notes", prose_to_html(asset.notes), True))
    if incident.summary:
        sections.append(("Alert summary", prose_to_html(incident.summary), True))
    return render_email(
        kicker="Escalation notification",
        heading=str(incident.number or "Incident"),
        severity=str(incident.severity or ""),
        status=str(incident.status or ""),
        is_demo=demo,
        demo_line=demo_line,
        meta_rows=meta,
        sections=sections,
        footer=ESCALATION_FOOTER,
        kind="escalation-notice",
    )


def notification_html(incident: Incident, step_key: str, policy_role: str) -> str:
    """HTML twin used by SMTP send and tests."""
    return build_escalation_html(incident, step_key, policy_role)
