"""Terminal formatting for the host CLI incident board.

Colors: red = critical/open, yellow = in progress, green = done.
No color when stdout is not a TTY unless FORGESRE_COLOR=1.
"""

from __future__ import annotations

import os
import sys
from typing import Any

RESET = "\033[0m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
BOLD = "\033[1m"
DIM = "\033[2m"

LANE_CRITICAL = "critical"
LANE_PROGRESS = "progress"
LANE_DONE = "done"

LANE_TITLE = {
    LANE_CRITICAL: "CRITICAL / OPEN",
    LANE_PROGRESS: "IN PROGRESS",
    LANE_DONE: "DONE",
}

LANE_COLOR = {
    LANE_CRITICAL: RED,
    LANE_PROGRESS: YELLOW,
    LANE_DONE: GREEN,
}


def color_enabled(stream=None) -> bool:
    flag = os.environ.get("FORGESRE_COLOR", "")
    if flag == "0":
        return False
    if flag == "1":
        return True
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty()


def paint(text: str, code: str, enabled: bool) -> str:
    if not enabled or not code:
        return text
    return f"{code}{text}{RESET}"


def lane_for(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "").upper()
    severity = str(item.get("severity") or "").upper()
    if status in {"CLOSED", "RESOLVED"}:
        return LANE_DONE
    if status == "ESCALATED" or severity in {"CRITICAL", "ERROR", "FATAL"}:
        return LANE_CRITICAL
    if status == "INVESTIGATING":
        return LANE_PROGRESS
    if status == "OPEN" and severity in {"WARNING", "INFO", "LOW"}:
        return LANE_PROGRESS
    if status == "OPEN":
        return LANE_CRITICAL
    return LANE_PROGRESS


def asset_name(item: dict[str, Any]) -> str:
    asset = item.get("asset") or {}
    return str(asset.get("hostname") or asset.get("asset_id") or "—")


def _row(item: dict[str, Any], enabled: bool, width_title: int = 36) -> str:
    lane = lane_for(item)
    dot = paint("●", LANE_COLOR[lane], enabled)
    number = str(item.get("number") or "")
    status = str(item.get("status") or "")
    sev = str(item.get("severity") or "")[:8]
    host = asset_name(item)[:16]
    title = str(item.get("title") or "")[:width_title]
    status_c = paint(f"{status:<14}", LANE_COLOR[lane], enabled)
    sev_c = paint(f"{sev:<8}", LANE_COLOR[lane], enabled)
    return f"{dot} {number:<12} {status_c} {sev_c} {host:<16} {title}"


def format_board(rows: list[dict[str, Any]], *, color: bool | None = None, who: str = "") -> str:
    enabled = color_enabled() if color is None else color
    lines: list[str] = []
    header = "Incidents  (red=critical/open  yellow=in progress  green=done)"
    lines.append(paint(header, BOLD, enabled))
    if who:
        lines.append(paint(f"session  {who}", DIM, enabled))
    lines.append("Open one:  incidents INC-000012")
    lines.append("")
    grouped: dict[str, list[dict[str, Any]]] = {
        LANE_CRITICAL: [],
        LANE_PROGRESS: [],
        LANE_DONE: [],
    }
    for item in rows:
        grouped[lane_for(item)].append(item)
    done = grouped[LANE_DONE][:12]
    for lane in (LANE_CRITICAL, LANE_PROGRESS, LANE_DONE):
        chunk = grouped[lane] if lane != LANE_DONE else done
        title = LANE_TITLE[lane]
        count = len(chunk) if lane != LANE_DONE else len(grouped[LANE_DONE])
        extra = "" if lane != LANE_DONE else f"  (showing {len(done)} of {count})"
        lines.append(paint(f"=== {title}{extra} ===", LANE_COLOR[lane] + BOLD, enabled))
        if not chunk:
            lines.append(paint("    (none)", DIM, enabled))
        else:
            for item in chunk:
                lines.append(_row(item, enabled))
        lines.append("")
    total = len(rows)
    lines.append(f"{total} row(s). History lookback:  history --days 90")
    return "\n".join(lines) + "\n"


def format_history_rows(rows: list[dict[str, Any]], *, days: int = 90, total: int | None = None, color: bool | None = None) -> str:
    enabled = color_enabled() if color is None else color
    lines = [
        paint(f"History last {days} day(s)  (same colors as incidents)", BOLD, enabled),
        "",
    ]
    if not rows:
        lines.append(paint("(none)", DIM, enabled))
    else:
        for item in rows:
            ack = (item.get("ack_by") or "—")[:16]
            resolved = (item.get("resolved_by") or "—")[:16]
            lines.append(f"{_row(item, enabled, width_title=22)}  ack:{ack}  res:{resolved}")
    shown = total if total is not None else len(rows)
    lines.append("")
    lines.append(f"{shown} match(es). Open one:  history INC-000012   or   incidents INC-000012")
    return "\n".join(lines) + "\n"


def format_detail(data: dict[str, Any], *, color: bool | None = None) -> str:
    enabled = color_enabled() if color is None else color
    lane = lane_for(data)
    status = str(data.get("status") or "")
    lines = [
        paint(
            f"{data.get('number', '')}  {status}  {data.get('severity', '')}  {data.get('title', '')}",
            LANE_COLOR[lane] + BOLD,
            enabled,
        )
    ]
    asset = data.get("asset") or {}
    if asset:
        lines.append(f"asset     {asset.get('hostname') or asset.get('asset_id')}  {asset.get('ip') or ''}")
    lines.append(f"ack       {data.get('ack_by') or '—'}  {data.get('ack_at') or ''}")
    lines.append(f"resolved  {data.get('resolved_by') or '—'}  {data.get('resolved_at') or data.get('ended_at') or ''}")
    lines.append("")
    lines.append(paint("=== mail ===", BOLD, enabled))
    notes = data.get("notifications") or []
    if not notes:
        lines.append("(none)")
    for row in notes:
        lines.append(f"{row.get('created_at')}  {row.get('status')}  {row.get('target')}  {row.get('subject')}")
        body = (row.get("body") or "").strip()
        if body:
            lines.append(body)
            lines.append("")
    lines.append(paint("=== audit ===", BOLD, enabled))
    audit = data.get("audit") or []
    if not audit:
        lines.append("(none)")
    for row in audit:
        extra = row.get("data") or {}
        lines.append(f"{row.get('at')}  {row.get('actor')}  {row.get('action')}  {extra}")
    lines.append("")
    lines.append(paint("=== notes ===", BOLD, enabled))
    op = data.get("notes") or []
    if not op:
        lines.append("(none)")
    for row in op:
        lines.append(f"{row.get('at')}  {row.get('actor')}")
        lines.append(row.get("body") or "")
        lines.append("")
    return "\n".join(lines) + "\n"
