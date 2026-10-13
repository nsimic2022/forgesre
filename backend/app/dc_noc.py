"""DC-NOC helpers: maintenance windows, parent suppression, interface labels,
incident syslog, on-call roster, 30-day capacity, and row/rack filters.

Local Core data only. Nothing here writes NetBox or Zabbix, runs a playbook,
or invents a metric the scrape did not return.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models import Asset, Incident, MaintenanceWindow, utcnow
from app.settings import settings

MAINT_FROM = "maint_from"
MAINT_UNTIL = "maint_until"
MAINT_KEYS = (MAINT_FROM, MAINT_UNTIL)

# Quiet incidents stay on the list and keep the fingerprint, but the mail ladder does not run.
QUIET_INCIDENT_STATUSES = ("MAINT", "SUPPRESSED")


def _zone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_when(text: str, *, naive: str = "local") -> datetime | None:
    """ISO or datetime-local. Naive form values are appliance-local. Stored values carry an offset."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if " " in raw and "T" not in raw:
        raw = raw.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if naive == "utc":
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.replace(tzinfo=_zone())
    return parsed.astimezone(timezone.utc)


def maint_pair_error(start_text: str, end_text: str) -> str:
    """Empty when the form pair is usable. Both empty = not in maintenance."""
    start_raw = str(start_text or "").strip()
    end_raw = str(end_text or "").strip()
    if not start_raw and not end_raw:
        return ""
    if not start_raw or not end_raw:
        return "Maintenance needs both from and until, or leave both empty."
    start = parse_when(start_raw, naive="local")
    end = parse_when(end_raw, naive="local")
    if start is None or end is None:
        return "Maintenance from and until must be a date and time."
    if end <= start:
        return "Maintenance until must be after from."
    return ""


def normalize_maint_fields(raw: dict) -> dict[str, str]:
    """UTC ISO pair, or nothing. A half-filled or backwards window is dropped (the form rejects it first)."""
    if not isinstance(raw, dict):
        return {}
    start = parse_when(str(raw.get(MAINT_FROM) or ""), naive="local")
    end = parse_when(str(raw.get(MAINT_UNTIL) or ""), naive="local")
    if start is None or end is None or end <= start:
        return {}
    return {MAINT_FROM: start.isoformat(), MAINT_UNTIL: end.isoformat()}


def maint_input_value(stored: str) -> str:
    """datetime-local value in the appliance timezone."""
    parsed = parse_when(stored, naive="utc")
    if parsed is None:
        return ""
    return parsed.astimezone(_zone()).strftime("%Y-%m-%dT%H:%M")


def maintenance_bounds(asset: Any) -> tuple[datetime, datetime] | None:
    extras = getattr(asset, "extras", None)
    if not isinstance(extras, dict):
        return None
    window = normalize_maint_fields(extras)
    if not window:
        return None
    start = parse_when(window[MAINT_FROM], naive="utc")
    end = parse_when(window[MAINT_UNTIL], naive="utc")
    if start is None or end is None or end <= start:
        return None
    return start, end


def in_maintenance(asset: Any, at: datetime | None = None) -> bool:
    """True when `at` (default now) sits inside the asset's from/until window. Empty window = no."""
    bounds = maintenance_bounds(asset)
    if bounds is None or asset is None:
        return False
    moment = _as_utc(at or utcnow())
    start, end = bounds
    return start <= moment <= end


def maintenance_label(asset: Any) -> str:
    bounds = maintenance_bounds(asset)
    if bounds is None:
        return ""
    start, end = bounds

    def _fmt(value: datetime) -> str:
        local = value.astimezone(_zone())
        return local.strftime("%d %b %Y, %H:%M")

    return f"{_fmt(start)} – {_fmt(end)}"


def sync_asset_maintenance(db: Session, asset: Asset) -> None:
    """Mirror the asset window into maintenance_windows so RCA overlap keeps working."""
    db.query(MaintenanceWindow).filter(MaintenanceWindow.asset_ref == asset.asset_id).delete(synchronize_session=False)
    bounds = maintenance_bounds(asset)
    if bounds is None:
        return
    start, end = bounds
    db.add(
        MaintenanceWindow(
            asset_ref=asset.asset_id,
            summary="Asset maintenance window",
            starts_at=start,
            ends_at=end,
        )
    )


def maintenance_quiet_detail(asset: Any) -> str:
    label = maintenance_label(asset)
    when = f" ({label})" if label else ""
    return f"Maintenance window{when} — incident recorded, mail not started."


# Host-down / interface-down style names. CPU, disk, and memory stay on their own ladder.
_DOWN_EXEMPT = ("cpu", "memory", "mem", "disk", "filesystem", "volume", "temperature", "temp", "inode", "load")
_DOWN_MARKERS = (
    "interfacedown",
    "exporterdown",
    "hostdown",
    "nodedown",
    "devicedown",
    "pingdown",
    "unreachable",
    "unreach",
    "snmpdown",
)


def is_down_style(alertname: str) -> bool:
    folded = "".join(ch for ch in (alertname or "").lower() if ch.isalnum())
    if not folded or any(word in folded for word in _DOWN_EXEMPT):
        return False
    if any(mark in folded for mark in _DOWN_MARKERS):
        return True
    return folded.endswith("down")


def incident_alertname(incident: Incident) -> str:
    payload = incident.alert_payload if isinstance(incident.alert_payload, dict) else {}
    labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
    name = str((labels or {}).get("alertname") or "").strip()
    return name or str(incident.title or "")


def set_asset_parent(db: Session, asset: Asset, parent_asset_id: str) -> None:
    """One parent pointer. Empty clears it. Self and short loops are rejected."""
    text = " ".join(str(parent_asset_id or "").split())
    if not text:
        asset.parent_id = None
        return
    parent = db.query(Asset).filter(Asset.asset_id == text).first()
    if parent is None:
        raise ValueError("Parent asset was not found")
    if asset.id is not None and parent.id == asset.id:
        raise ValueError("An asset cannot be its own parent")
    seen: set[int] = set()
    if asset.id is not None:
        seen.add(asset.id)
    cursor: Asset | None = parent
    for _ in range(12):
        if cursor is None:
            break
        if cursor.id in seen:
            raise ValueError("That parent would make a loop")
        seen.add(cursor.id)
        if not cursor.parent_id:
            break
        cursor = db.get(Asset, cursor.parent_id)
    asset.parent_id = parent.id


def parent_down_incident(db: Session, asset: Asset | None) -> Incident | None:
    """Active down/unreachable incident on the parent, if this asset has one parent."""
    if asset is None or not getattr(asset, "parent_id", None):
        return None
    from app.services import ACTIVE_INCIDENT_STATUSES

    rows = (
        db.query(Incident)
        .filter(Incident.asset_id == asset.parent_id, Incident.status.in_(ACTIVE_INCIDENT_STATUSES))
        .order_by(Incident.id.desc())
        .all()
    )
    for row in rows:
        if is_down_style(incident_alertname(row)):
            return row
    return None


def parent_suppress_detail(asset: Asset, parent_incident: Incident) -> str:
    parent = asset.parent
    host = parent.hostname if parent is not None else "parent"
    return (
        f"Suppressed by parent {host} incident {parent_incident.number} — "
        "parent is down, so this host-down alert did not start its own mail ladder."
    )
