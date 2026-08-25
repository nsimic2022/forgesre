"""Per-asset bundled alarm enable + threshold. Not Prometheus rule rewrites."""

from __future__ import annotations

import re
from typing import Any

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
    "WindowsExporterDown": "up",
    "SnmpDeviceUnreachable": "up",
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
    raw = asset.get("alarms") if isinstance(asset, dict) else getattr(asset, "alarms", None)
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
