"""Gmail-safe HTML for incident report and escalation mail. No extra deps."""

from __future__ import annotations

import html
from email.message import EmailMessage
from typing import Any, Iterable

from app.models import Incident

# Inline styles only — many clients strip <style> in <head>.
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_PAGE = "margin:0;padding:0;background:#f4f4f5;"
_WRAP = "width:100%;max-width:600px;background:#ffffff;border:1px solid #e5e7eb;"
_HEADER = (
    "background:#111827;color:#f9fafb;padding:16px 20px;"
    f"font-family:{_FONT};font-size:18px;font-weight:700;line-height:1.3;"
)
_BANNER = (
    "background:#fbbf24;color:#78350f;padding:10px 20px;text-align:center;"
    f"font-family:{_FONT};font-size:13px;font-weight:700;line-height:1.4;"
)
_TD_LABEL = (
    "width:140px;padding:8px 12px;border-bottom:1px solid #f3f4f6;"
    f"font-family:{_FONT};font-size:13px;color:#6b7280;vertical-align:top;"
)
_TD_VALUE = (
    "padding:8px 12px;border-bottom:1px solid #f3f4f6;"
    f"font-family:{_FONT};font-size:13px;color:#111827;vertical-align:top;"
)
_SECTION_H = (
    "padding:16px 20px 4px 20px;"
    f"font-family:{_FONT};font-size:13px;font-weight:700;color:#111827;"
)
_SECTION_B = (
    "padding:4px 20px 12px 20px;"
    f"font-family:{_FONT};font-size:14px;line-height:1.5;color:#1f2937;"
)
_FOOTER = (
    "padding:16px 20px 20px 20px;border-top:1px solid #e5e7eb;"
    f"font-family:{_FONT};font-size:12px;color:#6b7280;line-height:1.45;"
)
_UL = "margin:8px 0 0 18px;padding:0;"
_LI = "margin:0 0 6px 0;"


def compose_email_message(
    *,
    sender: str,
    to: str,
    subject: str,
    body: str,
    html_body: str | None = None,
) -> EmailMessage:
    """Plain text always; HTML alternative when provided (multipart/alternative)."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body or "")
    if html_body:
        message.add_alternative(html_body, subtype="html")
    return message


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return html.escape(text, quote=True)


def _cell(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return _esc(text) if text else "—"


def _multiline(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text.strip():
        return "—"
    return "<br>".join(_esc(part) for part in text.splitlines())


def _severity_style(severity: str) -> str:
    rank = (severity or "").strip().upper()
    if rank == "CRITICAL":
        return "color:#b91c1c;font-weight:700;"
    if rank in {"WARNING", "WARN"}:
        return "color:#b45309;font-weight:700;"
    return "color:#6b7280;font-weight:600;"


def _row(label: str, value_html: str, value_style: str = "") -> str:
    style = _TD_VALUE + value_style
    return (
        f'<tr><td style="{_TD_LABEL}">{_esc(label)}</td>'
        f'<td style="{style}">{value_html}</td></tr>'
    )


def _kv_table(rows: Iterable[tuple[str, str, str]]) -> str:
    body = "".join(_row(label, value, extra) for label, value, extra in rows)
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="border-collapse:collapse;">{body}</table>'
    )


def _ul(items: Iterable[Any]) -> str:
    lis = []
    for item in items:
        text = str(item).strip() if item is not None else ""
        if not text:
            continue
        lis.append(f'<li style="{_LI}">{_esc(text)}</li>')
    if not lis:
        return ""
    return f'<ul style="{_UL}">{"".join(lis)}</ul>'


def _item_text(item: Any, *keys: str) -> str:
    if isinstance(item, dict):
        for key in keys:
            value = item.get(key)
            if value:
                return str(value)
        return str(item)
    return str(item)


def _section(title: str, inner: str) -> str:
    return (
        f'<tr><td style="{_SECTION_H}">{_esc(title)}</td></tr>'
        f'<tr><td style="{_SECTION_B}">{inner}</td></tr>'
    )


def _demo_banner(demo: bool, demo_line: str) -> str:
    if not demo:
        return ""
    line = demo_line.strip() if demo_line else ""
    extra = f"<br>{_esc(line)}" if line else ""
    return (
        f'<tr><td bgcolor="#fbbf24" style="{_BANNER}">'
        f"[DEMO] lab only{extra}</td></tr>"
    )


def _wrap(*, demo: bool, demo_line: str, inner_rows: str) -> str:
    banner = _demo_banner(demo, demo_line)
    return (
        "<!DOCTYPE html>"
        f'<html><body style="{_PAGE}">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="background:#f4f4f5;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="600" cellspacing="0" cellpadding="0" '
        f'style="{_WRAP}">'
        f"{banner}"
        f'<tr><td bgcolor="#111827" style="{_HEADER}">ForgeSRE incident report</td></tr>'
        f"{inner_rows}"
        "</table></td></tr></table></body></html>"
    )


def _asset_rows(incident: Incident) -> list[tuple[str, str, str]]:
    asset = incident.asset
    rows: list[tuple[str, str, str]] = []
    if asset:
        host = f"{asset.hostname or '—'} ({asset.asset_id or '—'})"
        owner = (asset.owner or "").strip() or "—"
        contact = (asset.contact_name or "").strip() or "—"
        rows.extend(
            [
                ("Asset", _cell(host), ""),
                ("Type", _cell(asset.type), ""),
                ("IP", _cell(asset.ip), ""),
                ("Owner/contact", _cell(f"{owner} / {contact}"), ""),
                ("Email", _cell(asset.owner_email), ""),
                ("Phone", _cell(asset.owner_phone), ""),
            ]
        )
    else:
        rows.extend(
            [
                ("Asset", "unknown", ""),
                ("Type", "—", ""),
                ("IP", "—", ""),
                ("Owner/contact", "—", ""),
            ]
        )
    return rows


def _core_rows(incident: Incident, extra: list[tuple[str, str, str]] | None = None) -> list[tuple[str, str, str]]:
    severity = incident.severity or ""
    rows: list[tuple[str, str, str]] = [
        ("Incident", _cell(incident.number), ""),
        ("Title", _cell(incident.title), ""),
        ("Severity", _cell(severity), _severity_style(severity)),
        ("Status", _cell(incident.status), ""),
        ("Started", _cell(incident.started_at), ""),
    ]
    if extra:
        rows.extend(extra)
    rows.extend(_asset_rows(incident))
    return rows


def _footer_row() -> str:
    return (
        f'<tr><td style="{_FOOTER}">'
        "This is a snapshot. ForgeSRE does not execute playbooks."
        "</td></tr>"
    )


def _demo_flags(incident: Incident) -> tuple[bool, str]:
    from app.services import demo_body_line, is_demo_incident

    if is_demo_incident(incident):
        return True, demo_body_line(incident)
    return False, ""


def incident_report_html(incident: Incident) -> str:
    """HTML twin of build_incident_report. Same facts, tables instead of a dump."""
    demo, demo_line = _demo_flags(incident)
    extra: list[tuple[str, str, str]] = []
    if incident.ack_by:
        extra.append(("Ack", _cell(f"{incident.ack_by} {incident.ack_at or ''}".rstrip()), ""))
    if incident.resolved_by:
        extra.append(
            (
                "Resolved/closed",
                _cell(f"{incident.resolved_by} {incident.resolved_at or incident.ended_at or ''}".rstrip()),
                "",
            )
        )
    chunks = [f'<tr><td style="padding:8px 8px 0 8px;">{_kv_table(_core_rows(incident, extra))}</td></tr>']
    if incident.summary:
        chunks.append(_section("Alert summary", _multiline(incident.summary)))
    if incident.playbook:
        chunks.append(
            _section(
                "Playbook",
                _esc(f"{incident.playbook.name} (guidance only — not executed)"),
            )
        )
    investigation = incident.investigations[-1] if incident.investigations else None
    rca = (investigation.result if investigation else None) or {}
    if investigation:
        engine = f"{investigation.engine or 'forgerca'} {investigation.engine_version or ''} · {investigation.provider or ''}".strip()
        rca_bits = [
            f"<p style=\"margin:0 0 8px 0;\"><strong>Summary:</strong> {_multiline(investigation.summary)}</p>",
            f"<p style=\"margin:0 0 8px 0;\"><strong>Likely cause:</strong> {_multiline(investigation.likely_cause)}</p>",
            f"<p style=\"margin:0 0 8px 0;\"><strong>Confidence:</strong> {_cell(int(investigation.confidence or 0))}% (ForgeSRE score, not a validated model)</p>",
            f"<p style=\"margin:0 0 8px 0;\"><strong>What should I do:</strong> {_multiline(investigation.recommended_action)}</p>",
        ]
        facts = [_item_text(item, "text") for item in (rca.get("facts") or [])]
        if facts:
            rca_bits.append(f"<p style=\"margin:8px 0 0 0;\"><strong>Facts:</strong></p>{_ul(facts)}")
        anomalies = [_item_text(item, "summary") for item in (rca.get("anomalies") or [])]
        if anomalies:
            rca_bits.append(f"<p style=\"margin:8px 0 0 0;\"><strong>Anomalies:</strong></p>{_ul(anomalies)}")
        hyps = [_item_text(item, "summary") for item in (rca.get("hypotheses") or [])]
        if hyps:
            rca_bits.append(f"<p style=\"margin:8px 0 0 0;\"><strong>Candidate causes:</strong></p>{_ul(hyps)}")
        limits = [str(item) for item in (rca.get("limitations") or [])]
        if limits:
            rca_bits.append(f"<p style=\"margin:8px 0 0 0;\"><strong>Limitations:</strong></p>{_ul(limits)}")
        chunks.append(_section(f"ForgeRCA ({engine})", "".join(rca_bits)))
    else:
        chunks.append(_section("ForgeRCA", "ForgeRCA has not been run yet."))
    notes = list(incident.operator_notes or [])
    if notes:
        note_lines = [f"{note.at} {note.actor}: {note.body}" for note in notes]
        chunks.append(_section("Operator notes", _ul(note_lines)))
    chunks.append(_footer_row())
    return _wrap(demo=demo, demo_line=demo_line, inner_rows="".join(chunks))


def notification_html(incident: Incident, step_key: str, policy_role: str) -> str:
    """HTML twin of _notification_body (escalation / ensure_notification)."""
    demo, demo_line = _demo_flags(incident)
    extra = [
        ("Escalation step", _cell(f"{step_key} (policy role: {policy_role})"), ""),
    ]
    chunks = [f'<tr><td style="padding:8px 8px 0 8px;">{_kv_table(_core_rows(incident, extra))}</td></tr>']
    if incident.playbook:
        chunks.append(_section("Playbook", _cell(incident.playbook.name)))
    asset = incident.asset
    if asset and asset.notes:
        chunks.append(_section("Notes", _multiline(asset.notes)))
    chunks.append(_footer_row())
    return _wrap(demo=demo, demo_line=demo_line, inner_rows="".join(chunks))
