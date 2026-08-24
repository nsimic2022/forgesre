"""Shared email-safe HTML shell for incident reports and escalation notices.

Inline CSS on elements (Gmail strips <style> in some clients). Table layout,
600px max width. Light background, dark text, ForgeSRE accent. No JS.
"""

from __future__ import annotations

import html
import re
from typing import Any

DASH = "—"

# ForgeSRE light UI tokens (email-safe hex, no CSS variables).
ACCENT = "#c47a2e"
TEXT = "#1a2233"
MUTED = "#5a6a82"
LINE = "#d4dbe8"
BG = "#f3f5f9"
PANEL = "#ffffff"
CRITICAL = "#c44538"
WARNING = "#c47a2e"
INFO = "#2b6cb0"
INVESTIGATING = "#0d9488"

_FONT = "Segoe UI, Helvetica, Arial, sans-serif"

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")


def esc(value: Any) -> str:
    """Escape user/incident/RCA strings for HTML text or attributes."""
    if value is None:
        return DASH
    text = str(value).strip()
    if not text:
        return DASH
    return html.escape(text, quote=True)


def severity_theme(severity: str = "", status: str = "") -> tuple[str, str]:
    """Return (css_class, hex) for the color bar. Critical wins over status."""
    sev = str(severity or "").strip().upper()
    st = str(status or "").strip().upper()
    if sev in {"CRITICAL", "CRIT", "P1", "HIGH", "FATAL"}:
        return "severity-critical", CRITICAL
    if st == "INVESTIGATING":
        return "severity-investigating", INVESTIGATING
    if sev in {"INFO", "INFORMATIONAL", "P3", "P4", "LOW", "OK"}:
        return "severity-info", INFO
    return "severity-warning", WARNING


def prose_to_html(value: Any) -> str:
    """Escape text, turn newlines into <br>, markdown-ish bullets into lists."""
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return DASH
    lines = raw.split("\n")
    parts: list[str] = []
    in_list = False
    for line in lines:
        escaped = html.escape(line, quote=True)
        heading = _HEADING_RE.match(escaped)
        bullet = _BULLET_RE.match(escaped)
        if heading:
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append(
                f'<p style="margin:12px 0 6px;font-size:14px;font-weight:700;color:{TEXT};">'
                f"{heading.group(2)}</p>"
            )
            continue
        if bullet:
            if not in_list:
                parts.append(
                    f'<ul style="margin:8px 0 8px 20px;padding:0;color:{TEXT};font-size:14px;line-height:1.45;">'
                )
                in_list = True
            parts.append(f'<li style="margin:0 0 4px;">{bullet.group(1)}</li>')
            continue
        if in_list:
            parts.append("</ul>")
            in_list = False
        if escaped.strip() == "":
            parts.append("<br>")
        else:
            parts.append(f"{escaped}<br>")
    if in_list:
        parts.append("</ul>")
    html_out = "".join(parts)
    if html_out.endswith("<br>"):
        html_out = html_out[: -len("<br>")]
    return html_out or DASH


def html_list(items: list[Any]) -> str:
    """Render a list of already-plain strings as an escaped <ul>."""
    texts = [str(item).strip() for item in items if str(item or "").strip()]
    if not texts:
        return DASH
    lis = "".join(
        f'<li style="margin:0 0 6px;">{prose_to_html(item)}</li>' for item in texts
    )
    return (
        f'<ul style="margin:8px 0 0 18px;padding:0;color:{TEXT};font-size:14px;line-height:1.45;">'
        f"{lis}</ul>"
    )


def _meta_table(rows: list[tuple[str, str]]) -> str:
    cells = []
    for label, value in rows:
        if not label:
            continue
        cells.append(
            "<tr>"
            f'<td style="padding:7px 12px 7px 0;width:140px;color:{MUTED};font-size:12px;'
            f'vertical-align:top;white-space:nowrap;">{esc(label)}</td>'
            f'<td style="padding:7px 0;color:{TEXT};font-size:14px;font-weight:600;'
            f'vertical-align:top;">{value}</td>'
            "</tr>"
        )
    if not cells:
        return ""
    return (
        f'<table class="meta" width="100%" cellpadding="0" cellspacing="0" role="presentation" '
        f'style="border-collapse:collapse;">{"".join(cells)}</table>'
    )


def _section_block(heading: str, body_html: str, *, card: bool = False) -> str:
    if card:
        inner = (
            f'<table width="100%" cellpadding="0" cellspacing="0" role="presentation" '
            f'style="border-collapse:collapse;background:{BG};border-left:4px solid {ACCENT};">'
            f"<tr><td style=\"padding:14px 16px;\">"
            f'<p style="margin:0 0 8px;color:{MUTED};font-size:11px;font-weight:700;'
            f'letter-spacing:0.06em;text-transform:uppercase;">{esc(heading)}</p>'
            f'<div style="color:{TEXT};font-size:14px;line-height:1.5;">{body_html}</div>'
            f"</td></tr></table>"
        )
    else:
        inner = (
            f'<p style="margin:0 0 8px;color:{MUTED};font-size:11px;font-weight:700;'
            f'letter-spacing:0.06em;text-transform:uppercase;">{esc(heading)}</p>'
            f'<div style="color:{TEXT};font-size:14px;line-height:1.5;">{body_html}</div>'
        )
    return f'<tr><td style="padding:16px 24px 4px;">{inner}</td></tr>'


def render_email(
    *,
    kicker: str,
    heading: str,
    severity: str = "",
    status: str = "",
    is_demo: bool = False,
    demo_line: str = "",
    meta_rows: list[tuple[str, str]] | None = None,
    sections: list[tuple[str, str, bool]] | None = None,
    footer: str = "",
    kind: str = "incident-report",
) -> str:
    """Full HTML document. section tuples are (heading, html_body, card)."""
    css_class, bar = severity_theme(severity, status)
    demo_banner = ""
    if is_demo:
        extra = ""
        if demo_line:
            extra = " · " + html.escape(str(demo_line), quote=True)
        demo_banner = (
            f'<tr><td class="demo-banner" bgcolor="#f6e9c8" style="padding:10px 24px;background:#f6e9c8;'
            f"color:#7a5a08;font-size:13px;font-weight:700;border-bottom:1px solid {LINE};\">"
            f"[DEMO] lab only{extra}"
            f"</td></tr>"
        )
    sev_badge = esc(severity) if severity else ""
    st_badge = esc(status) if status else ""
    badges = []
    if sev_badge != DASH and severity:
        badges.append(
            f'<span style="display:inline-block;padding:3px 8px;margin:0 6px 0 0;border-radius:3px;'
            f"background:{bar};color:#ffffff;font-size:11px;font-weight:700;letter-spacing:0.04em;\">"
            f"{sev_badge}</span>"
        )
    if st_badge != DASH and status:
        badges.append(
            f'<span style="display:inline-block;padding:3px 8px;margin:0 6px 0 0;border-radius:3px;'
            f"background:{BG};color:{TEXT};font-size:11px;font-weight:700;border:1px solid {LINE};\">"
            f"{st_badge}</span>"
        )
    badge_html = "".join(badges)
    section_html = ""
    for heading_s, body_html, card in sections or []:
        if not heading_s:
            continue
        section_html += _section_block(heading_s, body_html or DASH, card=card)
    footer_html = ""
    if footer:
        footer_html = (
            f'<tr><td style="padding:20px 24px 24px;color:{MUTED};font-size:12px;line-height:1.45;'
            f'border-top:1px solid {LINE};">{esc(footer)}</td></tr>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(heading)}</title>
</head>
<body class="{html.escape(kind, quote=True)}" style="margin:0;padding:0;background:{BG};color:{TEXT};font-family:{_FONT};">
<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:{BG};border-collapse:collapse;">
<tr><td align="center" style="padding:24px 12px;">
<table class="forgesre-mail" width="600" cellpadding="0" cellspacing="0" role="presentation" style="max-width:600px;width:100%;background:{PANEL};border:1px solid {LINE};border-collapse:collapse;">
<tr><td class="severity-bar {css_class}" bgcolor="{bar}" style="height:6px;line-height:6px;font-size:0;background:{bar};">&nbsp;</td></tr>
<tr><td style="padding:18px 24px 12px;border-bottom:1px solid {LINE};">
  <p style="margin:0 0 6px;color:{ACCENT};font-size:13px;font-weight:700;letter-spacing:0.08em;">ForgeSRE</p>
  <p style="margin:0 0 8px;color:{MUTED};font-size:12px;letter-spacing:0.04em;text-transform:uppercase;">{esc(kicker)}</p>
  <p style="margin:0 0 10px;color:{TEXT};font-size:20px;font-weight:700;line-height:1.3;">{esc(heading)}</p>
  <p style="margin:0;">{badge_html}</p>
</td></tr>
{demo_banner}
<tr><td style="padding:16px 24px 8px;">{_meta_table(meta_rows or [])}</td></tr>
{section_html}
{footer_html}
</table>
</td></tr>
</table>
</body>
</html>
"""
