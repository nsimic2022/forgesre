"""DC-NOC helpers: maintenance windows, parent suppression, interface labels,
incident syslog, on-call roster, 30-day capacity, and row/rack filters.

Local Core data only. Nothing here writes NetBox or Zabbix, runs a playbook,
or invents a metric the scrape did not return.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models import Asset, Incident, MaintenanceWindow, OncallSlot, utcnow
from app.security import ROLE_LABELS, role_label

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
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


_IFACE_ALIASES = {
    "ifName": ("ifName", "ifname", "if_name"),
    "ifIndex": ("ifIndex", "ifindex", "if_index"),
    "ifAlias": ("ifAlias", "ifalias", "if_alias"),
    "ifDescr": ("ifDescr", "ifdescr", "if_descr"),
}
_OPER_STATE = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}


def interface_facts(source: Any) -> dict[str, str]:
    """ifName / ifIndex / ifAlias / ifDescr from an incident or an alert label dict."""
    labels: dict[str, Any] = {}
    if isinstance(source, dict):
        raw = source.get("labels") if isinstance(source.get("labels"), dict) else source
        labels = raw if isinstance(raw, dict) else {}
    else:
        payload = getattr(source, "alert_payload", None)
        if isinstance(payload, dict) and isinstance(payload.get("labels"), dict):
            labels = payload["labels"]
    out: dict[str, str] = {}
    for key, names in _IFACE_ALIASES.items():
        for name in names:
            text = str(labels.get(name) or "").strip()
            if text:
                out[key] = text
                break
    return out


def query_prometheus_vector(expr: str, timeout: float = 2.0) -> dict[str, Any]:
    """All series for one instant query. Error or empty is not a fake sample."""
    import httpx

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{settings.prometheus_url}/api/v1/query", params={"query": expr})
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return {"error": str(exc), "series": [], "query": expr}
    series = []
    for row in (data.get("data") or {}).get("result") or []:
        if not isinstance(row, dict):
            continue
        metric = row.get("metric") if isinstance(row.get("metric"), dict) else {}
        raw = row.get("value") or [None, None]
        try:
            value = float(raw[1])
        except (TypeError, ValueError, IndexError):
            continue
        if value != value or value in {float("inf"), float("-inf")}:
            continue
        series.append({"labels": metric, "value": value})
    return {"series": series, "query": expr}


def format_snmp_uptime(hundredths: float) -> str:
    """sysUpTime is hundredths of a second in the if_mib gauge."""
    total = max(0, int(float(hundredths) / 100.0))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def snmp_surface(asset: Any, *, vector_fn: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compact if_mib view: ifName / ifIndex / ifAlias plus sysUpTime. Empty scrape stays Not scraped."""
    from app.inventory import is_snmp_asset

    if asset is None or not is_snmp_asset(asset):
        return {"show": False, "state": "", "interfaces": [], "uptime": "", "note": ""}
    asset_id = str(getattr(asset, "asset_id", "") or "").replace("\\", "\\\\").replace('"', '\\"')
    fetch = vector_fn or query_prometheus_vector
    oper = fetch(f'ifOperStatus{{job="forgesre-snmp",asset="{asset_id}"}}')
    uptime_q = fetch(f'sysUpTime{{job="forgesre-snmp",asset="{asset_id}"}}')
    rows = []
    for series in oper.get("series") or []:
        facts = interface_facts({"labels": series.get("labels") or {}})
        try:
            code = int(float(series.get("value")))
        except (TypeError, ValueError):
            code = None
        rows.append(
            {
                "ifName": facts.get("ifName") or facts.get("ifDescr") or "",
                "ifIndex": facts.get("ifIndex") or "",
                "ifAlias": facts.get("ifAlias") or "",
                "state": _OPER_STATE.get(code, "") if code is not None else "",
            }
        )
    rows.sort(key=lambda row: (int(row["ifIndex"]) if str(row["ifIndex"]).isdigit() else 10**9, row["ifName"]))
    uptime = ""
    for series in uptime_q.get("series") or []:
        try:
            uptime = format_snmp_uptime(float(series["value"]))
        except (TypeError, ValueError, KeyError):
            continue
        if uptime:
            break
    if not rows and not uptime:
        return {"show": True, "state": "not_scraped", "interfaces": [], "uptime": "", "note": "Not scraped."}
    return {"show": True, "state": "ok", "interfaces": rows, "uptime": uptime, "note": ""}


def _clock_minutes(text: str) -> int | None:
    raw = str(text or "").strip()
    if not raw or ":" not in raw:
        return None
    hour_text, minute_text = raw.split(":", 1)
    try:
        hour = int(hour_text)
        minute = int(minute_text[:2])
    except ValueError:
        return None
    if hour == 24 and minute == 0:
        return 24 * 60
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _hhmm(minute: int) -> str:
    minute = max(0, min(int(minute), 24 * 60))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def slot_enabled(row: OncallSlot) -> bool:
    """Missing or NULL enabled is ON — that is how older rows behaved."""
    value = getattr(row, "enabled", True)
    return True if value is None else bool(value)


def oncall_person_name(name: str | None, email: str | None = "", role: str | None = None) -> str:
    """Roster Name is a person. Role phrases (Super Admin, Analyst, …) are not a name."""
    text = " ".join(str(name or "").split())
    folded = text.casefold()
    labels = {value.casefold() for value in ROLE_LABELS.values()}
    labels.update(key.casefold() for key in ROLE_LABELS)
    labels.update(key.replace("_", " ").casefold() for key in ROLE_LABELS)
    if role:
        labels.add(role_label(str(role)).casefold())
        labels.add(str(role).replace("_", " ").casefold())
    if text and folded not in labels:
        return text[:255]
    local = str(email or "").strip().split("@", 1)[0]
    pretty = " ".join(local.replace(".", " ").replace("_", " ").replace("-", " ").split())
    if pretty:
        return pretty.title()[:255]
    return text[:255]


def _oncall_fields(
    *,
    weekday: str | int,
    start: str,
    end: str,
    name: str,
    email: str,
    phone: str = "",
    phone2: str = "",
) -> dict[str, Any]:
    try:
        day = int(weekday)
    except (TypeError, ValueError):
        day = -1
    if day not in range(7):
        raise ValueError("On-call needs a weekday.")
    start_minute = _clock_minutes(start)
    end_minute = _clock_minutes(end)
    if start_minute is None or end_minute is None:
        raise ValueError("On-call from and until must be times like 08:00.")
    if end_minute <= start_minute:
        raise ValueError("On-call until must be after from, on the same weekday.")
    address = str(email or "").strip()
    local, _, domain = address.partition("@")
    if not local or "." not in domain:
        raise ValueError("On-call needs an email address.")
    person = oncall_person_name(name, address)
    if not person:
        raise ValueError("On-call needs a name.")
    return {
        "weekday": day,
        "start_minute": start_minute,
        "end_minute": end_minute,
        "name": person[:255],
        "email": address[:255],
        "phone": " ".join(str(phone or "").split())[:64],
        "phone2": " ".join(str(phone2 or "").split())[:64],
    }


def add_oncall_slot(
    db: Session,
    *,
    weekday: str | int,
    start: str,
    end: str,
    name: str,
    email: str,
    phone: str = "",
    phone2: str = "",
    enabled: bool = True,
) -> OncallSlot:
    """One weekday window. Times are appliance-local. Until is inclusive and must be after from."""
    row = OncallSlot(
        **_oncall_fields(weekday=weekday, start=start, end=end, name=name, email=email, phone=phone, phone2=phone2),
        enabled=bool(enabled),
    )
    db.add(row)
    db.flush()
    return row


def update_oncall_slot(
    db: Session,
    row: OncallSlot,
    *,
    weekday: str | int,
    start: str,
    end: str,
    name: str,
    email: str,
    phone: str = "",
    phone2: str = "",
) -> OncallSlot:
    """Rewrite one roster row. ON/OFF stays on the toggle — this does not flip enabled."""
    fields = _oncall_fields(weekday=weekday, start=start, end=end, name=name, email=email, phone=phone, phone2=phone2)
    for key, value in fields.items():
        setattr(row, key, value)
    db.flush()
    return row


def toggle_oncall_slot(row: OncallSlot) -> OncallSlot:
    row.enabled = not slot_enabled(row)
    return row


def oncall_form_values(row: OncallSlot, *, clone: bool = False) -> dict[str, Any]:
    """Prefill Add on-call. Clone is a draft until Save."""
    view = oncall_slot_view(row)
    name = view["person"] or view["name"]
    if clone:
        name = f"{name} copy".strip()
    return {
        "weekday": view["weekday"],
        "start": view["start"],
        "end": view["end"],
        "name": name,
        "email": view["email"],
        "phone": view["phone"],
        "phone2": view["phone2"],
    }


def oncall_slot_view(row: OncallSlot) -> dict[str, Any]:
    day = WEEKDAYS[row.weekday] if row.weekday in range(7) else "?"
    return {
        "id": row.id,
        "weekday": row.weekday,
        "day": day,
        "start": _hhmm(row.start_minute),
        "end": _hhmm(row.end_minute),
        "name": row.name,
        "person": oncall_person_name(row.name, row.email),
        "email": row.email,
        "phone": row.phone or "",
        "phone2": getattr(row, "phone2", "") or "",
        "enabled": slot_enabled(row),
        "window": f"{day} {_hhmm(row.start_minute)}–{_hhmm(row.end_minute)}",
    }


def list_oncall_slots(db: Session) -> list[dict[str, Any]]:
    rows = db.query(OncallSlot).order_by(OncallSlot.weekday, OncallSlot.start_minute, OncallSlot.id).all()
    return [oncall_slot_view(row) for row in rows]


def current_oncall(db: Session, at: datetime | None = None) -> dict[str, Any] | None:
    """Whoever covers `at` in the appliance timezone. Inclusive from and until. No roster → None."""
    moment = at or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(_zone())
    minute = local.hour * 60 + local.minute
    rows = (
        db.query(OncallSlot)
        .filter(OncallSlot.weekday == local.weekday())
        .order_by(OncallSlot.start_minute, OncallSlot.id)
        .all()
    )
    matches = [
        row
        for row in rows
        if slot_enabled(row) and int(row.start_minute) <= minute <= int(row.end_minute)
    ]
    if not matches:
        return None
    matches.sort(key=lambda row: (int(row.start_minute), int(row.id or 0)))
    return oncall_slot_view(matches[-1])


def take_duty(db: Session, user: Any, at: datetime | None = None) -> dict[str, Any]:
    """Cover the rest of today as this user. A later start wins over an earlier slot."""
    moment = at or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(_zone())
    start_minute = local.hour * 60 + local.minute
    end_minute = 23 * 60 + 59
    if start_minute >= end_minute:
        raise ValueError("Too late today to take the rest of the day.")
    person = current_oncall(db, moment)
    email = str(getattr(user, "email", "") or "").strip()
    if person and person.get("email", "").lower() == email.lower():
        return person
    # Close whoever still covers the rest of today so the roster does not stack.
    overlapping = (
        db.query(OncallSlot)
        .filter(OncallSlot.weekday == local.weekday())
        .all()
    )
    for row in overlapping:
        if not slot_enabled(row):
            continue
        if int(row.start_minute) <= start_minute <= int(row.end_minute):
            closed = start_minute - 1
            if closed < int(row.start_minute):
                db.delete(row)
            else:
                row.end_minute = closed
    db.flush()
    name = oncall_person_name(getattr(user, "name", ""), email, getattr(user, "role", None)) or (
        email.split("@")[0] if email else "On call"
    )
    slot = add_oncall_slot(
        db,
        weekday=local.weekday(),
        start=_hhmm(start_minute),
        end=_hhmm(end_minute),
        name=name,
        email=email,
        phone=str(getattr(user, "phone", "") or ""),
        phone2=str(getattr(user, "phone2", "") or ""),
    )
    return oncall_slot_view(slot)


SYSLOG_LIMIT = 20
SYSLOG_PAD_MINUTES = 30


def incident_syslog_lines(incident: Any, *, fetch: Callable[..., list[str]] | None = None) -> list[str] | None:
    """Last ~20 Loki syslog lines for this asset around started_at.

    Hostname, then IP, job=syslog. The demo asset may use job=forgesre.
    No asset, no query, or no lines → None (the page omits the block).
    A real host never keeps a forge-demo-01 line.
    """
    asset = getattr(incident, "asset", None)
    if asset is None:
        return None
    from rca.collector import DEMO_ASSET, asset_log_identity, is_demo_loki_query, loki_query_for

    identity = asset_log_identity(asset)
    query = loki_query_for(identity)
    if not query:
        return None
    demo = str(identity.get("asset_id") or "") == DEMO_ASSET
    if not demo and is_demo_loki_query(query):
        return None
    started = getattr(incident, "started_at", None) or utcnow()
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    pad = timedelta(minutes=SYSLOG_PAD_MINUTES)
    if fetch is None:
        fetch = _bounded_syslog
    try:
        raw = fetch(limit=SYSLOG_LIMIT, query=query, start=started - pad, end=started + pad) or []
    except Exception:
        return None
    lines: list[str] = []
    for line in raw:
        text = str(line or "")
        if not text:
            continue
        if not demo and ("forge-demo-01" in text or 'job="forgesre"' in text):
            continue
        lines.append(text)
        if len(lines) >= SYSLOG_LIMIT:
            break
    return lines or None


def _bounded_syslog(*, limit: int, query: str, start: datetime, end: datetime) -> list[str]:
    """LogQL around the incident only. A failed selector is skipped — no unbounded retry."""
    from app.services import _query_loki_selector
    from rca.collector import logql_selectors

    lines: list[str] = []
    seen: set[str] = set()
    for selector in logql_selectors(query):
        got = _query_loki_selector(selector, limit, start, end) or []
        for line in got:
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
            if len(lines) >= limit:
                return lines
    return lines


def parent_suppress_detail(asset: Asset, parent_incident: Incident) -> str:
    parent = asset.parent
    host = parent.hostname if parent is not None else "parent"
    return (
        f"Suppressed by parent {host} incident {parent_incident.number} — "
        "parent is down, so this host-down alert did not start its own mail ladder."
    )


CAPACITY_SECONDS = 30 * 24 * 3600


def forecast_days_to_full(times: list[float], values: list[float], *, full: float = 100.0) -> int | None:
    """Linear days until `full`. None when there are fewer than 2 points or the slope is not climbing."""
    pairs: list[tuple[float, float]] = []
    for raw_t, raw_v in zip(times, values):
        try:
            stamp = float(raw_t)
            value = float(raw_v)
        except (TypeError, ValueError):
            continue
        if stamp != stamp or value != value:
            continue
        pairs.append((stamp, value))
    if len(pairs) < 2:
        return None
    origin = pairs[0][0]
    xs = [(stamp - origin) / 86400.0 for stamp, _value in pairs]
    ys = [value for _stamp, value in pairs]
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    var = sum((x - mean_x) ** 2 for x in xs)
    if var <= 1e-9:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var
    if slope <= 0.05:
        return None
    last = ys[-1]
    if last >= full:
        return 0
    days = (full - last) / slope
    if days <= 0 or days > 3650:
        return None
    return max(1, int(round(days)))


def _spark(values: list[float]) -> str:
    if len(values) < 2:
        return ""
    width, height = 80.0, 22.0
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    last = len(values) - 1
    coords = []
    for index, raw in enumerate(values):
        x = 0.0 if last == 0 else index * (width / last)
        y = height - 1.0 - ((raw - lo) / span) * (height - 2.0)
        coords.append(f"{x:.1f},{y:.1f}")
    return " ".join(coords)


def _capacity_row(label: str, result: Any, *, unit: str, allow_forecast: bool, window: float) -> dict[str, Any]:
    base = {
        "label": label,
        "state": "not_scraped",
        "note": "Not scraped.",
        "spark": "",
        "forecast_days": None,
        "window_seconds": window,
        "points": 0,
    }
    if not isinstance(result, dict) or result.get("error"):
        return base
    raw_values = list(result.get("values") or [])
    raw_times = list(result.get("times") or [])
    values: list[float] = []
    times: list[float] = []
    for index, raw in enumerate(raw_values):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value != value or value in {float("inf"), float("-inf")}:
            continue
        values.append(value)
        if index < len(raw_times):
            try:
                times.append(float(raw_times[index]))
            except (TypeError, ValueError):
                pass
    if len(times) != len(values):
        times = []
    if not values:
        return base
    if len(values) < 2:
        base.update(state="thin", note="Not enough history for a forecast.", points=len(values))
        return base
    forecast = None
    if allow_forecast and unit == "percent" and len(times) >= 2:
        forecast = forecast_days_to_full(times, values)
    if forecast is None:
        note = "30-day history"
        state = "spark"
    elif forecast == 0:
        note = "Full now"
        state = "forecast"
    else:
        note = f"~{forecast} days to full"
        state = "forecast"
    return {
        "label": label,
        "state": state,
        "note": note,
        "spark": _spark(values),
        "forecast_days": forecast,
        "window_seconds": window,
        "points": len(values),
    }


def _asset_query_dict(asset: Any) -> dict[str, Any]:
    return {
        "asset_id": str(getattr(asset, "asset_id", "") or ""),
        "hostname": str(getattr(asset, "hostname", "") or ""),
        "ip": str(getattr(asset, "ip", "") or ""),
        "type": str(getattr(asset, "type", "") or ""),
        "monitoring_profile": str(getattr(asset, "monitoring_profile", "") or ""),
        "scrape_address": str(getattr(asset, "scrape_address", "") or ""),
        "snmp_port": getattr(asset, "snmp_port", None),
    }


def capacity_panel(
    asset: Any,
    *,
    range_fn: Callable[[str, float, float], dict[str, Any]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """~30 days of disk % and/or interface receive. No forecast without two climbing points."""
    from rca.collector import promql_queries_for

    end = float(time.time() if now is None else now)
    start = end - CAPACITY_SECONDS
    window = end - start
    packed = promql_queries_for(_asset_query_dict(asset))

    def live(expr: str, span_start: float, span_end: float) -> dict[str, Any]:
        from app.services import query_prometheus_range

        return query_prometheus_range(expr, start=span_start, end=span_end, step="1h", timeout=3.0)

    fetch = range_fn or live
    specs = (
        ("disk_percent", "Disk", True),
        ("net_rx", "Interface receive", False),
    )
    rows: list[dict[str, Any]] = []
    for key, label, allow_forecast in specs:
        expr, unit = packed.get(key, ("", ""))
        if not expr:
            continue
        try:
            sampled = fetch(expr, start, end)
        except Exception as exc:
            sampled = {"error": str(exc), "values": []}
        rows.append(_capacity_row(label, sampled, unit=unit, allow_forecast=allow_forecast, window=window))
    if range_fn is None:
        rows = _fill_disk_from_zabbix(asset, rows, window=window, now=end)
    if not rows:
        rows.append(
            {
                "label": "Capacity",
                "state": "not_scraped",
                "note": "Not scraped.",
                "spark": "",
                "forecast_days": None,
                "window_seconds": window,
                "points": 0,
            }
        )
    return {"rows": rows, "window_seconds": window, "start": start, "end": end}


def _fill_disk_from_zabbix(asset: Any, rows: list[dict[str, Any]], *, window: float, now: float) -> list[dict[str, Any]]:
    """Prometheus first. Zabbix disk trends (~30 days) only when that row was not scraped and Zabbix is on."""
    disk = next((row for row in rows if row.get("label") == "Disk"), None)
    hostid = str(getattr(asset, "zabbix_hostid", "") or "").strip()
    if disk is None or disk.get("state") != "not_scraped" or not hostid or not settings.zabbix_enabled:
        return rows
    try:
        from app.zabbix import host_trends

        result = host_trends(
            settings.zabbix_url,
            settings.zabbix_token,
            hostid,
            timeout=settings.zabbix_timeout,
            hours=30 * 24,
            now=now,
        )
    except Exception:
        return rows
    tile = (result.get("tiles") or {}).get("disk_percent") or {}
    series = list(tile.get("series") or [])
    times = list(tile.get("times") or [])
    if not series:
        return rows
    replacement = _capacity_row(
        "Disk",
        {"values": series, "times": times},
        unit="percent",
        allow_forecast=True,
        window=window,
    )
    return [replacement if row.get("label") == "Disk" else row for row in rows]
