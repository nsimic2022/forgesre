"""Per-asset bundled alarm enable + threshold, plus async firing/resolve state."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import object_session

from app.audit import audit
from app.models import Asset, Incident, IncidentEvent, IncidentNote, utcnow

ALARM_KEYS = ("up", "cpu_percent", "memory_percent", "disk_percent")

BUNDLED_ALERT_TILE = {
    "HighCPU": "cpu_percent",
    "FilesystemUsageHigh": "disk_percent",
    "NodeCPUHigh": "cpu_percent",
    "NodeFilesystemUsageHigh": "disk_percent",
    "NodeMemoryHigh": "memory_percent",
    "NodeExporterDown": "up",
    "WindowsCPUHigh": "cpu_percent",
    "WindowsFilesystemUsageHigh": "disk_percent",
    "WindowsMemoryHigh": "memory_percent",
    "WindowsExporterDown": "up",
    "SnmpDeviceUnreachable": "up",
}

BUNDLED_ALERT_CLASS = {
    "HighCPU": "demo",
    "FilesystemUsageHigh": "demo",
    "NodeCPUHigh": "linux",
    "NodeFilesystemUsageHigh": "linux",
    "NodeMemoryHigh": "linux",
    "NodeExporterDown": "linux",
    "WindowsCPUHigh": "windows",
    "WindowsFilesystemUsageHigh": "windows",
    "WindowsMemoryHigh": "windows",
    "WindowsExporterDown": "windows",
    "SnmpDeviceUnreachable": "network",
}

_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_IS_NUMBER = re.compile(r"\bis\s+(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def clamp_pct(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return max(1.0, min(100.0, number))


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def default_alarms(klass: str = "") -> dict[str, dict[str, Any]]:
    from app.asset_metrics import bundled_thresholds

    key = klass if klass in {"linux", "windows", "network", "demo"} else "linux"
    bundled = bundled_thresholds().get(key) or {}
    out: dict[str, dict[str, Any]] = {
        "up": {"enabled": True, "threshold": None},
        "cpu_percent": {"enabled": True, "threshold": bundled.get("cpu_percent")},
        "memory_percent": {"enabled": True, "threshold": bundled.get("memory_percent")},
        "disk_percent": {"enabled": True, "threshold": bundled.get("disk_percent")},
    }
    if klass == "network":
        for tile in ("cpu_percent", "memory_percent", "disk_percent"):
            out[tile]["enabled"] = False
    return out


def normalize_alarms(raw: Any, klass: str = "") -> dict[str, dict[str, Any]]:
    merged = default_alarms(klass)
    if not isinstance(raw, dict):
        return merged
    for key in ALARM_KEYS:
        spec = raw.get(key)
        if spec is None and key == "cpu_percent":
            spec = raw.get("cpu")
        if spec is None and key == "memory_percent":
            spec = raw.get("memory")
        if spec is None and key == "disk_percent":
            spec = raw.get("disk")
        if not isinstance(spec, dict):
            continue
        enabled = spec.get("enabled")
        if enabled is not None:
            merged[key]["enabled"] = bool(enabled)
        if "threshold" in spec:
            merged[key]["threshold"] = clamp_pct(spec.get("threshold"))
    return merged


def _truthy(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "on", "true", "yes"}


def alarms_from_form(
    *,
    present: str = "",
    up_enabled: str = "",
    cpu_enabled: str = "",
    cpu_threshold: str = "",
    memory_enabled: str = "",
    memory_threshold: str = "",
    disk_enabled: str = "",
    disk_threshold: str = "",
    klass: str = "",
) -> dict[str, dict[str, Any]] | None:
    """None when the HTML form did not post the checklist (API/tests)."""
    if not str(present or "").strip():
        return None
    base = default_alarms(klass)
    base["up"]["enabled"] = _truthy(up_enabled)
    base["cpu_percent"]["enabled"] = _truthy(cpu_enabled)
    base["cpu_percent"]["threshold"] = clamp_pct(cpu_threshold) or base["cpu_percent"]["threshold"]
    base["memory_percent"]["enabled"] = _truthy(memory_enabled)
    base["memory_percent"]["threshold"] = clamp_pct(memory_threshold) or base["memory_percent"]["threshold"]
    base["disk_percent"]["enabled"] = _truthy(disk_enabled)
    base["disk_percent"]["threshold"] = clamp_pct(disk_threshold) or base["disk_percent"]["threshold"]
    return base


def tile_threshold(alarms: dict[str, dict[str, Any]] | None, key: str, bundled: float | None) -> float | None:
    spec = (alarms or {}).get(key) or {}
    custom = spec.get("threshold")
    if custom is not None:
        return clamp_pct(custom)
    return bundled


def tile_enabled(alarms: dict[str, dict[str, Any]] | None, key: str) -> bool:
    spec = (alarms or {}).get(key) or {}
    if "enabled" not in spec:
        return True
    return bool(spec.get("enabled"))


def saved_alarm_hostnames(assets: Any) -> list[str]:
    """Hostnames that already have a non-empty assets.alarms JSON."""
    names: list[str] = []
    for asset in assets or []:
        raw = asset.get("alarms") if isinstance(asset, dict) else getattr(asset, "alarms", None)
        if not isinstance(raw, dict) or not raw:
            continue
        if isinstance(asset, dict):
            label = str(asset.get("hostname") or asset.get("asset_id") or "")
        else:
            label = str(getattr(asset, "hostname", "") or getattr(asset, "asset_id", "") or "")
        if label:
            names.append(label)
    return names


def alert_sample_value(alert: dict[str, Any] | None) -> float | None:
    """Best-effort numeric sample from an Alertmanager webhook. Missing → None."""
    if not isinstance(alert, dict):
        return None
    for key in ("value", "Value"):
        if key in alert:
            parsed = _finite_number(alert.get(key))
            if parsed is not None:
                return parsed
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    for blob in (labels, annotations):
        for key in ("value", "Value"):
            parsed = _finite_number(blob.get(key))
            if parsed is not None:
                return parsed
    values = alert.get("values")
    if isinstance(values, dict):
        for raw in values.values():
            parsed = _finite_number(raw)
            if parsed is not None:
                return parsed
    for text in (
        str(annotations.get("description") or ""),
        str(annotations.get("summary") or ""),
        str(alert.get("generatorURL") or ""),
    ):
        match = _PERCENT.search(text) or _IS_NUMBER.search(text)
        if match:
            parsed = _finite_number(match.group(1))
            if parsed is not None:
                return parsed
    return None


def bundled_alert_skip_reason(
    asset: Any,
    alertname: str,
    alert: dict[str, Any] | None = None,
) -> str:
    """Why ForgeSRE should not open an incident for this bundled alert. Empty = ingest."""
    if asset is None:
        return ""
    tile = BUNDLED_ALERT_TILE.get(str(alertname or "").strip())
    if not tile:
        return ""
    from app.asset_metrics import metric_class_for

    klass = metric_class_for(asset)
    expected = BUNDLED_ALERT_CLASS.get(str(alertname or "").strip())
    if expected and klass != expected:
        return ""
    raw = asset.get("alarms") if isinstance(asset, dict) else getattr(asset, "alarms", None)
    if not isinstance(raw, dict) or not raw:
        return ""
    alarms = normalize_alarms(raw, klass)
    spec = alarms.get(tile) or {}
    if spec.get("enabled") is False:
        return f"{alertname} alarm disabled on this asset"
    sample = alert_sample_value(alert or {})
    if tile == "up":
        return ""
    threshold = tile_threshold(alarms, tile, spec.get("threshold"))
    if sample is None or threshold is None:
        return ""
    if sample < threshold:
        return f"{alertname} value {sample:g} is below asset threshold {threshold:g}%"
    return ""


# Sync helpers above stay for forms and ingest filters. Operator webhooks use
# process_incoming_alarm_state: fetch asset, mark DOWN/UP, resolve by fingerprint.

log = logging.getLogger("forgesre.asset_alarms")

ACTIVE_ALARM_STATUSES = ("OPEN", "INVESTIGATING", "ESCALATED")
RESOLVE_NOTE = "Monitoring reports the problem is gone. Automatic close."


def canonical_asset_status(value: str) -> str:
    """DOWN/UP from agentless webhooks map onto Asset.status (healthy/warning/critical)."""
    text = (value or "").strip().lower()
    if text in {"down", "critical"}:
        return "critical"
    if text in {"up", "ok", "healthy"}:
        return "healthy"
    if text == "warning":
        return "warning"
    return text or "healthy"


def _parse_alert(alert_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(alert_data, dict):
        return None
    labels = alert_data.get("labels") if isinstance(alert_data.get("labels"), dict) else {}
    alertname = str(labels.get("alertname") or "").strip()
    status = str(alert_data.get("status") or "firing").strip().lower()
    from app.asset_verify import alert_instance_ip

    ip = str(labels.get("ip") or "").strip() or alert_instance_ip(labels)
    name = str(labels.get("asset") or labels.get("hostname") or "").strip()
    if not name:
        instance = str(labels.get("instance") or "").strip()
        if instance and not ip:
            name = instance.split(":")[0] if instance.count(":") == 1 else instance
        elif instance:
            name = instance
    if not alertname and not ip and not name:
        return None
    from app.services import alert_fingerprint

    return {
        "alert": alert_data,
        "labels": labels,
        "alertname": alertname or "Alert",
        "status": status,
        "ip": ip,
        "name": name,
        "severity": str(labels.get("severity") or "warning").upper(),
        "fingerprint": alert_fingerprint(alert_data),
    }


def _downtime_seconds(started_at: datetime | None, ended_at: datetime | None) -> float:
    if started_at is None or ended_at is None:
        return 0.0
    start = started_at if started_at.tzinfo else started_at.replace(tzinfo=ended_at.tzinfo)
    end = ended_at if ended_at.tzinfo else ended_at.replace(tzinfo=started_at.tzinfo)
    return max(0.0, (end - start).total_seconds())


def apply_resolved_incident(
    incident: Incident,
    asset: Asset | None,
    *,
    alertname: str,
    resolver: str,
    labels: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> float:
    """Mark one active incident RESOLVED, audit, and record downtime. Sync or async session."""
    from app.asset_metrics import record_outage
    from app.history import apply_status_fields
    from app.services import append_timeline

    when = now or utcnow()
    apply_status_fields(incident, "RESOLVED", resolver)
    incident.status = "RESOLVED"
    incident.ended_at = when
    seconds = _downtime_seconds(incident.started_at, when)
    append_timeline(incident, "alert", "ALERT", f"{alertname} resolved by {resolver}")
    session = object_session(incident)
    if session is not None:
        session.add(IncidentEvent(incident_id=incident.id, kind="resolved", data=dict(labels or {})))
        session.add(
            IncidentNote(
                incident_id=incident.id,
                actor=resolver or "system",
                body=RESOLVE_NOTE,
            )
        )
        audit(
            session,
            "incident.resolve",
            actor=resolver or "system",
            object_type="incident",
            object_id=incident.number,
            data={"alertname": alertname, "downtime_seconds": seconds, "fingerprint": incident.fingerprint},
        )
    if asset is not None:
        asset.extras = record_outage(
            asset.extras if isinstance(asset.extras, dict) else {},
            started_at=incident.started_at,
            ended_at=when,
            seconds=seconds,
            fingerprint=incident.fingerprint or "",
        )
    return seconds


def resolve_active_incident(
    db,
    incident: Incident,
    asset: Asset | None,
    *,
    alertname: str,
    resolver: str,
    labels: dict[str, Any] | None = None,
) -> float:
    """Sync path used by ingest_alertmanager. Refreshes Asset.status after resolve."""
    from app.services import refresh_asset_status

    seconds = apply_resolved_incident(
        incident, asset, alertname=alertname, resolver=resolver, labels=labels
    )
    refresh_asset_status(db, asset)
    return seconds


async def _update_asset_status(session: AsyncSession, asset_id: int, new_status: str) -> None:
    """Core UPDATE — no full-row load. DOWN→critical, UP→healthy."""
    status = canonical_asset_status(new_status)
    await session.execute(update(Asset).where(Asset.id == asset_id).values(status=status))


async def _refresh_asset_status(session: AsyncSession, asset: Asset) -> None:
    rows = list(
        await session.scalars(
            select(Incident).where(
                Incident.asset_id == asset.id,
                Incident.status.in_(ACTIVE_ALARM_STATUSES),
            )
        )
    )
    if any(str(item.severity or "").upper() in {"CRITICAL", "HIGH"} for item in rows):
        asset.status = "critical"
    elif rows:
        asset.status = "warning"
    else:
        asset.status = "healthy"


async def fetch_asset_for_alert(session: AsyncSession, parsed: dict[str, Any]) -> Asset | None:
    from app.demo_ids import is_lab_inventory_row
    from app.inventory import find_asset_by_ip

    ip = str(parsed.get("ip") or "").strip()
    name = str(parsed.get("name") or "").strip()
    if name:
        found = await session.scalar(
            select(Asset).where((Asset.asset_id == name) | (Asset.hostname == name))
        )
        if found is not None:
            return found
    if ip:
        found = await find_asset_by_ip(session, ip)
        if found is not None and not is_lab_inventory_row(found):
            return found
    return None


async def _resolve_active_incident(
    session: AsyncSession,
    asset: Asset | None,
    fingerprint: str,
    *,
    alertname: str = "Alert",
    resolver: str = "Alertmanager",
    labels: dict[str, Any] | None = None,
) -> Incident | None:
    """Look up the open incident by fingerprint, mark RESOLVED, audit, record downtime."""
    incident = await session.scalar(
        select(Incident)
        .where(
            Incident.fingerprint == fingerprint,
            Incident.status.in_(ACTIVE_ALARM_STATUSES),
        )
        .order_by(Incident.id.desc())
    )
    if incident is None:
        log.debug("resolved signal for %s with no active incident", fingerprint)
        if asset is not None:
            await _refresh_asset_status(session, asset)
        return None
    apply_resolved_incident(
        incident, asset, alertname=alertname, resolver=resolver, labels=labels
    )
    await session.flush()
    if asset is not None:
        await _refresh_asset_status(session, asset)
    elif incident.asset_id:
        await _update_asset_status(session, incident.asset_id, "UP")
    log.info(
        "auto-resolved %s for %s downtime=%.1fs",
        incident.number,
        getattr(asset, "hostname", "") or fingerprint,
        _downtime_seconds(incident.started_at, incident.ended_at),
    )
    return incident


async def process_incoming_alarm_state(session: AsyncSession, alert_data: dict[str, Any]) -> dict[str, Any] | None:
    """One firing/resolved alert: fetch asset, mark DOWN, or auto-resolve by fingerprint."""
    parsed = _parse_alert(alert_data)
    if parsed is None:
        log.error("invalid alarm payload: %s", alert_data)
        return None
    asset = await fetch_asset_for_alert(session, parsed)
    if asset is None:
        log.warning(
            "alarm %s for %s is not in inventory",
            parsed["alertname"],
            parsed["ip"] or parsed["name"] or "?",
        )
        return {"status": "unknown-asset", "fingerprint": parsed["fingerprint"]}
    if parsed["status"] == "resolved":
        incident = await _resolve_active_incident(
            session,
            asset,
            parsed["fingerprint"],
            alertname=parsed["alertname"],
            labels=parsed["labels"],
        )
        return {
            "status": "resolved" if incident is not None else "resolved-miss",
            "fingerprint": parsed["fingerprint"],
            "asset_id": asset.asset_id,
        }
    await _update_asset_status(session, asset.id, "DOWN")
    asset.status = "critical"
    log.info("asset %s marked DOWN for %s", asset.hostname, parsed["alertname"])
    return {"status": "firing", "fingerprint": parsed["fingerprint"], "asset_id": asset.asset_id}


async def process_incoming_alarm_states(
    session: AsyncSession,
    alerts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Batch: one asset query + one incident query, then apply each state (no per-alert loop of SELECTs)."""
    parsed_rows = [_parse_alert(item) for item in alerts or []]
    parsed_rows = [row for row in parsed_rows if row is not None]
    if not parsed_rows:
        return []
    ips = {row["ip"] for row in parsed_rows if row["ip"]}
    names = {row["name"] for row in parsed_rows if row["name"]}
    fingerprints = {row["fingerprint"] for row in parsed_rows if row["fingerprint"]}
    assets: list[Asset] = []
    if ips or names:
        clauses = []
        if ips:
            clauses.append(Asset.ip.in_(list(ips)))
        if names:
            clauses.append((Asset.asset_id.in_(list(names))) | (Asset.hostname.in_(list(names))))
        query = select(Asset)
        if len(clauses) == 1:
            query = query.where(clauses[0])
        else:
            query = query.where(clauses[0] | clauses[1])
        assets = list(await session.scalars(query))
    by_ip = {row.ip: row for row in assets if row.ip}
    by_name = {row.asset_id: row for row in assets}
    by_name.update({row.hostname: row for row in assets if row.hostname})
    incidents: dict[str, Incident] = {}
    if fingerprints:
        rows = list(
            await session.scalars(
                select(Incident)
                .where(
                    Incident.fingerprint.in_(list(fingerprints)),
                    Incident.status.in_(ACTIVE_ALARM_STATUSES),
                )
                .order_by(Incident.id.desc())
            )
        )
        for item in rows:
            incidents.setdefault(item.fingerprint, item)
    out: list[dict[str, Any]] = []
    for row in parsed_rows:
        asset = by_name.get(row["name"]) or by_ip.get(row["ip"])
        if asset is None:
            out.append({"status": "unknown-asset", "fingerprint": row["fingerprint"]})
            continue
        if row["status"] == "resolved":
            incident = incidents.get(row["fingerprint"])
            if incident is None:
                await _refresh_asset_status(session, asset)
                out.append(
                    {
                        "status": "resolved-miss",
                        "fingerprint": row["fingerprint"],
                        "asset_id": asset.asset_id,
                    }
                )
                continue
            apply_resolved_incident(
                incident,
                asset,
                alertname=row["alertname"],
                resolver="Alertmanager",
                labels=row["labels"],
            )
            await session.flush()
            await _refresh_asset_status(session, asset)
            out.append(
                {
                    "status": "resolved",
                    "fingerprint": row["fingerprint"],
                    "asset_id": asset.asset_id,
                }
            )
            continue
        await _update_asset_status(session, asset.id, "DOWN")
        asset.status = "critical"
        out.append({"status": "firing", "fingerprint": row["fingerprint"], "asset_id": asset.asset_id})
    return out
