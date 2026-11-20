"""The summary at the top of a scheduled report mail: the period at a glance, incidents per day,
what is still open, the most frequent alerts and the noisiest assets.

Read from the database only (reports_overview), so it is quick and the same numbers as the Reports
page. Email-safe HTML: tables and inline styles, no images, no scripts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.email_html import ACCENT, CRITICAL, LINE, MUTED, TEXT, WARNING, esc
from app.models import Incident, utcnow

OPEN = ("OPEN", "ACK", "ACKNOWLEDGED", "INVESTIGATING", "ESCALATED")
HIGH = "#ea580c"


def _minutes(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{round(value)} min" if value < 120 else f"{value / 60:.1f} h"


def _delta(now: int, before: int) -> str:
    if before == now:
        return "same as the period before"
    sign = "+" if now > before else "−"
    return f"{sign}{abs(now - before)} vs the period before"


def summary(db: Session, days: int, now: datetime | None = None) -> dict[str, Any]:
    """Numbers for the summary: the Reports overview for ``days`` plus the incidents still open."""
    from app.reports_view import reports_overview

    data = reports_overview(db, days=days, now=now)
    open_rows = (
        db.query(Incident).options(joinedload(Incident.asset))
        .filter(Incident.status.in_(OPEN)).order_by(Incident.started_at.desc()).limit(10).all()
    )
    data["open"] = open_rows
    return data


def _tile(label: str, value: str, note: str = "", color: str = TEXT) -> str:
    return (
        f'<td width="25%" style="padding:10px 8px;border:1px solid {LINE};vertical-align:top;">'
        f'<div style="color:{MUTED};font-size:11px;text-transform:uppercase;letter-spacing:0.05em;">{esc(label)}</div>'
        f'<div style="color:{color};font-size:22px;font-weight:700;margin-top:4px;">{esc(value)}</div>'
        f'<div style="color:{MUTED};font-size:11px;margin-top:2px;">{esc(note)}</div></td>'
    )


def _bars(daily: list[dict[str, Any]]) -> str:
    peak = max([1] + [d["critical"] + d["high"] + d["other"] for d in daily])
    rows = []
    for d in daily:
        total = d["critical"] + d["high"] + d["other"]
        cells = ""
        for count, color in ((d["critical"], CRITICAL), (d["high"], HIGH), (d["other"], ACCENT)):
            if count:
                cells += f'<td bgcolor="{color}" width="{max(1, round(count / peak * 100))}%" style="background:{color};height:12px;font-size:0;line-height:0;">&nbsp;</td>'
        cells += '<td style="font-size:0;">&nbsp;</td>'
        day = datetime.fromisoformat(d["day"]).strftime("%a %d.%m.")
        rows.append(
            f'<tr><td style="width:90px;padding:3px 8px 3px 0;color:{MUTED};font-size:12px;white-space:nowrap;">{esc(day)}</td>'
            f'<td style="padding:3px 0;"><table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="border-collapse:collapse;"><tr>{cells}</tr></table></td>'
            f'<td style="width:36px;padding:3px 0 3px 8px;text-align:right;color:{TEXT};font-size:12px;font-weight:600;">{total}</td></tr>'
        )
    legend = (
        f'<p style="margin:8px 0 0;color:{MUTED};font-size:11px;">'
        f'<span style="color:{CRITICAL};">■</span> critical &nbsp; <span style="color:{HIGH};">■</span> high &nbsp; '
        f'<span style="color:{ACCENT};">■</span> other</p>'
    )
    return f'<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="border-collapse:collapse;">{"".join(rows)}</table>{legend}'


def _list(rows: list[tuple[str, str]], empty: str) -> str:
    if not rows:
        return f'<span style="color:{MUTED};">{esc(empty)}</span>'
    body = "".join(
        f'<tr><td style="padding:5px 0;border-bottom:1px solid {LINE};color:{TEXT};font-size:13px;">{left}</td>'
        f'<td style="padding:5px 0;border-bottom:1px solid {LINE};color:{MUTED};font-size:13px;text-align:right;white-space:nowrap;">{right}</td></tr>'
        for left, right in rows
    )
    return f'<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="border-collapse:collapse;">{body}</table>'


def _age(started: datetime | None, now: datetime) -> str:
    if started is None:
        return ""
    if started.tzinfo is None:
        from datetime import timezone

        started = started.replace(tzinfo=timezone.utc)
    minutes = max(0, int((now - started).total_seconds() // 60))
    return f"{minutes} min" if minutes < 120 else (f"{minutes // 60} h" if minutes < 2880 else f"{minutes // 1440} days")


def summary_sections(data: dict[str, Any], now: datetime | None = None) -> list[tuple[str, str, bool]]:
    """(heading, html, card) sections for render_email, in reading order."""
    now = now or utcnow()
    cur, prev = data["current"], data["previous"]
    tiles = (
        "<tr>"
        + _tile("Incidents", str(cur["incidents"]), _delta(cur["incidents"], prev["incidents"]))
        + _tile("Critical", str(cur["critical"]), f"{cur['high']} high · {cur['warning']} warning", CRITICAL if cur["critical"] else TEXT)
        + _tile("Time to acknowledge", _minutes(cur["mtta_min"]), f"before: {_minutes(prev['mtta_min'])}")
        + _tile("Time to resolve", _minutes(cur["mttr_min"]), f"before: {_minutes(prev['mttr_min'])}")
        + "</tr>"
    )
    glance = f'<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="border-collapse:collapse;">{tiles}</table>'
    sev_color = {"CRITICAL": CRITICAL, "HIGH": HIGH, "WARNING": WARNING}
    open_rows = [
        (
            f'<span style="color:{sev_color.get((i.severity or "").upper(), MUTED)};font-weight:700;">{esc((i.severity or "").title())}</span> '
            f'{esc(i.asset.hostname + " · " if i.asset else "")}{esc(i.title)} <span style="color:{MUTED};">{esc(i.number.split("_")[0])}</span>',
            esc(("acked · " if i.ack_at else "not acked · ") + _age(i.started_at, now)),
        )
        for i in data.get("open") or []
    ]
    return [
        (f"The last {data['days']} days at a glance", glance, False),
        ("Incidents per day", _bars(data["daily"]), False),
        ("Still open", _list(open_rows, "Nothing is open."), False),
        ("Most frequent alerts", _list([(esc(r["alert"]), str(r["incidents"])) for r in data["top_alerts"][:5]], "No alerts in this period."), False),
        ("Noisiest assets", _list([(esc(r["asset"]), str(r["incidents"])) for r in data["noisy_assets"][:5]], "No incidents on assets in this period."), False),
    ]


def summary_lines(data: dict[str, Any]) -> list[str]:
    """The same summary as plain text, for the text/plain part."""
    cur, prev = data["current"], data["previous"]
    lines = [
        f"Last {data['days']} days: {cur['incidents']} incidents ({_delta(cur['incidents'], prev['incidents'])}), "
        f"{cur['critical']} critical, {cur['high']} high.",
        f"Time to acknowledge {_minutes(cur['mtta_min'])}, to resolve {_minutes(cur['mttr_min'])}.",
        "",
        "Still open:",
    ]
    lines += [f"- {i.severity} {i.asset.hostname if i.asset else ''} {i.title} {i.number.split('_')[0]}" for i in data.get("open") or []] or ["- nothing"]
    lines += ["", "Most frequent alerts:"] + [f"- {r['alert']}: {r['incidents']}" for r in data["top_alerts"][:5]]
    return lines + [""]
