"""Escalation notification bodies (plain + HTML) and async SMTP / webhook send."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.asset_extras import extras_rows, support_status
from app.email_html import DASH, esc, meta_table, prose_to_html, render_email
from app.email_service import send_smtp_async
from app.incident_report_mail import incident_header_rows
from app.models import Incident

log = logging.getLogger("forgesre.notifications")
WEBHOOK_TIMEOUT = 3.0


def _source_label(incident: Incident) -> str:
    from app.services import incident_source_label

    return incident_source_label(incident)

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
            f"Source: {_source_label(incident)}",
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
        lines.extend(f"{label}: {value}" for _key, label, value in extras_rows(asset))
        support = support_status(asset)
        if support["state"] != "unknown":
            lines.append(f"Support: {support['call_note']}")
        if asset.notes:
            lines.append(f"Notes: {asset.notes}")
    return "\n".join(lines) + "\n"


def build_escalation_html(incident: Incident, step_key: str, policy_role: str) -> str:
    """HTML alternative for the same facts as build_escalation_body."""
    asset = incident.asset
    demo, demo_line = _demo(incident)
    meta = incident_header_rows(incident, asset)
    meta.append(("Escalation step", esc(f"{step_key} (policy role: {policy_role})")))
    details: list[tuple[str, str]] = [
        ("Asset", esc(asset.hostname if asset else "unknown")),
        ("Playbook", esc(incident.playbook.name if incident.playbook else "n/a")),
    ]
    sections: list[tuple[str, str, bool]] = []
    if asset:
        details.extend(
            [
                ("Owner", esc(asset.owner or DASH)),
                ("Contact", esc(asset.contact_name or DASH)),
                ("Email", esc(asset.owner_email or DASH)),
                ("Phone", esc(asset.owner_phone or DASH)),
                ("IP", esc(asset.ip or DASH)),
            ]
        )
        details.extend((label, esc(value)) for key, label, value in extras_rows(asset) if key != "runbook_note")
        support = support_status(asset)
        if support["state"] != "unknown":
            details.append(("Support", esc(support["call_note"])))
    sections.append(("Details", meta_table(details), False))
    if asset:
        for key, label, value in extras_rows(asset):
            if key == "runbook_note":
                sections.append((label, prose_to_html(value), True))
        if asset.notes:
            sections.append(("Notes", prose_to_html(asset.notes), True))
    if incident.summary:
        sections.append(("Alert summary", prose_to_html(incident.summary), True))
    from app.services import incident_mail_heading

    return render_email(
        kicker="Escalation notification",
        heading=incident_mail_heading(incident),
        severity=str(incident.severity or ""),
        status=str(incident.status or ""),
        is_demo=demo,
        demo_line=demo_line,
        meta_rows=meta,
        sections=sections,
        footer="",
        kind="escalation-notice",
        source=_source_label(incident),
    )


def notification_html(incident: Incident, step_key: str, policy_role: str) -> str:
    """HTML twin used by SMTP send and tests."""
    return build_escalation_html(incident, step_key, policy_role)


async def send_email(to_email: str, subject: str, html_content: str = "", body: str = "") -> bool:
    """Async SMTP send. Escalation stays email-only; this does not invent Slack/SMS."""
    try:
        await send_smtp_async(to_email, subject, body or "", html=html_content or None)
        log.info("smtp sent to %s", to_email)
        return True
    except Exception as exc:
        log.error("smtp send failed to %s: %s", to_email, exc)
        return False


async def send_webhook(url: str, payload: dict[str, Any], timeout: float = WEBHOOK_TIMEOUT) -> bool:
    """POST JSON to an operator webhook. Not wired to escalation (email only)."""
    target = (url or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        return False
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(target, json=payload)
            return 200 <= response.status_code < 300
        except httpx.RequestError as exc:
            log.error("webhook send failed: %s", exc)
            return False
