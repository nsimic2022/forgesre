"""Glanceable class-based metric tiles for the asset detail page.

Queries Prometheus via the existing RCA/collector helpers. Does not scrape hosts
and does not invent SNMP walks. Missing series stay yellow — never a fake 0%.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from app.demo_ids import is_demo_asset_id
from app.inventory import asset_kind
from rca.catalog import PLAYRULE_PRESETS
from rca.collector import DEMO_ASSET, promql_queries_for

QueryFn = Callable[[str], dict[str, Any]]
RangeFn = Callable[[str], dict[str, Any]]

# Alertname → tile class. Demo gauges are forge-demo-01 only.
_ALERT_CLASS = {
    "HighCPU": "demo",
    "FilesystemUsageHigh": "demo",
    "NodeCPUHigh": "linux",
    "NodeFilesystemUsageHigh": "linux",
    "NodeMemoryHigh": "linux",
    "NodeExporterDown": "linux",
    "WindowsCPUHigh": "windows",
    "WindowsFilesystemUsageHigh": "windows",
    "WindowsExporterDown": "windows",
    "SnmpDeviceUnreachable": "network",
}

_ALERT_TILE = {
    "HighCPU": "cpu_percent",
    "FilesystemUsageHigh": "disk_percent",
    "NodeCPUHigh": "cpu_percent",
    "NodeFilesystemUsageHigh": "disk_percent",
    "NodeMemoryHigh": "memory_percent",
    "WindowsCPUHigh": "cpu_percent",
    "WindowsFilesystemUsageHigh": "disk_percent",
}

_PLAYRULE_METRIC = {
    "cpu_usage": "cpu_percent",
    "cpu_percent": "cpu_percent",
    "filesystem_usage": "disk_percent",
    "disk_percent": "disk_percent",
    "memory_usage": "memory_percent",
    "memory_percent": "memory_percent",
}

_TILE_META = {
    "up": {"name": "Collecting", "kind": "up"},
    "cpu_percent": {"name": "CPU", "kind": "percent"},
    "memory_percent": {"name": "Memory", "kind": "percent"},
    "disk_percent": {"name": "Disk", "kind": "percent"},
}

_CLASS_TILES = {
    "demo": ("up", "cpu_percent", "memory_percent", "disk_percent"),
    "linux": ("up", "cpu_percent", "memory_percent", "disk_percent"),
    "windows": ("up", "cpu_percent", "memory_percent", "disk_percent"),
    "network": ("up",),
    "unknown": ("up",),
}


def _alerts_path() -> Path:
    return Path(__file__).resolve().parents[2] / "monitoring" / "alerts.yml"


def _playrule_thresholds() -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for group in PLAYRULE_PRESETS:
        for rule in group.get("rules") or []:
            klass = _ALERT_CLASS.get(str(rule.get("alertname") or ""))
            tile = _PLAYRULE_METRIC.get(str(rule.get("metric") or ""))
            if not klass or not tile:
                continue
            try:
                out.setdefault(klass, {})[tile] = float(rule.get("value"))
            except (TypeError, ValueError):
                continue
    return out


def _alerts_yml_thresholds() -> dict[str, dict[str, float]]:
    path = _alerts_path()
    if not path.is_file():
        return {}
    out: dict[str, dict[str, float]] = {}
    current = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        named = re.match(r"\s+- alert:\s+(\S+)", line)
        if named:
            current = named.group(1)
            continue
        if not current or "expr:" not in line:
            continue
        tile = _ALERT_TILE.get(current)
        klass = _ALERT_CLASS.get(current)
        if tile and klass:
            match = re.search(r">\s*(\d+(?:\.\d+)?)", line)
            if match:
                out.setdefault(klass, {})[tile] = float(match.group(1))
        current = ""
    return out


def bundled_thresholds() -> dict[str, dict[str, float]]:
    """Percent thresholds from playrule presets, overlaid by monitoring/alerts.yml."""
    merged = _playrule_thresholds()
    for klass, tiles in _alerts_yml_thresholds().items():
        merged.setdefault(klass, {}).update(tiles)
    return merged


def metric_class_for(asset: Any) -> str:
    if isinstance(asset, dict):
        asset_id = str(asset.get("asset_id") or "")
        kind = str(asset.get("type") or "")
        profile = str(asset.get("monitoring_profile") or "")
    else:
        asset_id = str(getattr(asset, "asset_id", "") or "")
        kind = str(getattr(asset, "type", "") or "")
        profile = str(getattr(asset, "monitoring_profile", "") or "")
    if asset_id == DEMO_ASSET:
        return "demo"
    klass = asset_kind(kind, profile)
    if klass in {"linux", "windows", "network"}:
        return klass
    return "unknown"


def _asset_dict(asset: Any) -> dict[str, Any]:
    if isinstance(asset, dict):
        return {
            "asset_id": str(asset.get("asset_id") or ""),
            "type": str(asset.get("type") or ""),
            "monitoring_profile": str(asset.get("monitoring_profile") or ""),
        }
    return {
        "asset_id": str(getattr(asset, "asset_id", "") or ""),
        "type": str(getattr(asset, "type", "") or ""),
        "monitoring_profile": str(getattr(asset, "monitoring_profile", "") or ""),
    }


def _tone_for(kind: str, value: float | None, threshold: float | None) -> str:
    if value is None:
        return "warn"
    if kind == "up":
        if value >= 1:
            return "ok"
        return "crit"
    if threshold is None:
        return "ok"
    if value >= threshold:
        return "crit"
    return "ok"


def _display(kind: str, value: float | None) -> str:
    if value is None:
        return "not collecting"
    if kind == "up":
        return "up" if value >= 1 else "down"
    return f"{value:.0f}%"


def _bar_pct(kind: str, value: float | None, tone: str) -> int | None:
    """Fill 0–100 for a known percent/up value. None = empty track (never a fake 0%)."""
    if value is None or tone == "warn":
        return None
    if kind == "up":
        return 100 if value >= 1 else 0
    return int(max(0, min(100, round(value))))


def _spark_points(values: list[float]) -> str:
    if len(values) < 2:
        return ""
    width, height = 80.0, 22.0
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    coords: list[str] = []
    last = len(values) - 1
    for index, raw in enumerate(values):
        x = 0.0 if last == 0 else index * (width / last)
        y = height - 1.0 - ((raw - lo) / span) * (height - 2.0)
        coords.append(f"{x:.1f},{y:.1f}")
    return " ".join(coords)


def _default_query(expr: str) -> dict[str, Any]:
    from app.services import query_prometheus_expr

    return query_prometheus_expr(expr, timeout=2.0)


def _default_range(expr: str) -> dict[str, Any]:
    from app.services import query_prometheus_range

    return query_prometheus_range(expr, hours=1.0, step="5m", timeout=2.0)


def _tile(
    key: str,
    value: float | None,
    *,
    threshold: float | None,
    spark: str = "",
    query: str = "",
) -> dict[str, Any]:
    meta = _TILE_META[key]
    kind = str(meta["kind"])
    tone = _tone_for(kind, value, threshold)
    bar = _bar_pct(kind, value, tone)
    return {
        "key": key,
        "name": meta["name"],
        "kind": kind,
        "value": None if value is None else round(float(value), 2),
        "display": _display(kind, value),
        "tone": tone,
        "threshold": threshold,
        "bar_pct": bar,
        "spark": spark,
        "query": query,
    }


def asset_metric_panel(
    asset: Any,
    *,
    query_fn: QueryFn | None = None,
    range_fn: RangeFn | None = None,
) -> dict[str, Any]:
    """JSON for GET /api/v1/assets/{id}/metrics and the detail-page first paint."""
    info = _asset_dict(asset)
    asset_id = info["asset_id"]
    klass = metric_class_for(asset)
    demo = is_demo_asset_id(asset_id)
    thresholds = bundled_thresholds().get("demo" if klass == "demo" else klass, {})
    keys = _CLASS_TILES.get(klass, _CLASS_TILES["unknown"])
    packed = promql_queries_for(info) if klass != "unknown" else {}
    fetch = query_fn or _default_query
    spark_fetch = range_fn
    samples: dict[str, float | None] = {}
    queries: dict[str, str] = {}
    sparks: dict[str, str] = {}
    prom_error = ""
    prom_down = False

    if klass == "unknown":
        tiles = [
            _tile("up", None, threshold=None, query=""),
        ]
        return {
            "asset_id": asset_id,
            "class": klass,
            "demo": demo,
            "demo_label": "DEMO" if demo else "",
            "collecting": False,
            "collecting_line": "Prometheus is not collecting this target.",
            "error": "",
            "tiles": tiles,
        }

    for key in keys:
        expr = packed.get(key, ("", ""))[0] if key in packed else ""
        queries[key] = expr
        if not expr:
            samples[key] = None
            continue
        if prom_down:
            samples[key] = None
            continue
        result = fetch(expr)
        if result.get("error"):
            prom_down = True
            prom_error = str(result.get("error") or "prometheus unreachable")
            samples[key] = None
            continue
        raw = result.get("value")
        if raw is None:
            samples[key] = None
            continue
        try:
            samples[key] = float(raw)
        except (TypeError, ValueError):
            samples[key] = None

    if klass == "demo":
        from app.metrics import demo_metric_values

        live = demo_metric_values()
        samples["cpu_percent"] = float(live["forgesre_demo_cpu_percent"])
        samples["disk_percent"] = float(live["forgesre_demo_disk_percent"])
        queries["cpu_percent"] = "forgesre_demo_cpu_percent"
        queries["disk_percent"] = "forgesre_demo_disk_percent"
        # Demo Core has no memory gauge — leave missing (yellow), do not fake 0%.

    if spark_fetch is None and not prom_down:
        spark_fetch = _default_range
    if spark_fetch and not prom_down:
        for key in keys:
            expr = queries.get(key) or ""
            if not expr or samples.get(key) is None:
                continue
            ranged = spark_fetch(expr)
            if ranged.get("error"):
                break
            sparks[key] = _spark_points([float(v) for v in (ranged.get("values") or []) if v is not None])

    up_value = samples.get("up")
    collecting: bool | None
    if prom_down:
        collecting = None
        collecting_line = "Prometheus is unreachable — not collecting."
    elif up_value is None:
        collecting = False
        collecting_line = "Prometheus is not collecting this target."
    elif up_value >= 1:
        collecting = True
        collecting_line = "Prometheus sees this target (up=1)."
    else:
        collecting = False
        collecting_line = "Prometheus is not collecting this target (up=0)."

    tiles = [
        _tile(
            key,
            samples.get(key),
            threshold=thresholds.get(key) if key != "up" else None,
            spark=sparks.get(key) or "",
            query=queries.get(key) or "",
        )
        for key in keys
    ]
    return {
        "asset_id": asset_id,
        "class": klass,
        "demo": demo,
        "demo_label": "DEMO" if demo else "",
        "collecting": collecting,
        "collecting_line": collecting_line,
        "error": prom_error,
        "tiles": tiles,
    }


def safe_asset_metric_panel(asset: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return asset_metric_panel(asset, **kwargs)
    except Exception as exc:
        info = _asset_dict(asset)
        demo = is_demo_asset_id(info["asset_id"])
        return {
            "asset_id": info["asset_id"],
            "class": "unknown",
            "demo": demo,
            "demo_label": "DEMO" if demo else "",
            "collecting": False,
            "collecting_line": "Prometheus is unreachable — not collecting.",
            "error": str(exc),
            "tiles": [_tile("up", None, threshold=None)],
        }
