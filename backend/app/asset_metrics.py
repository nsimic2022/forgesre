"""Glanceable class-based metric tiles for the asset detail page.

Queries Prometheus via the existing RCA/collector helpers. Does not scrape hosts
and does not invent SNMP walks. Missing series stay yellow — never a fake 0%.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.alert_rules import load_alert_rules
from app.demo_ids import is_lab_inventory_row
from app.inventory import asset_kind
from rca.catalog import PLAYRULE_PRESETS
from rca.collector import DEMO_ASSET, promql_queries_for, promql_selectors_for

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
    "WindowsMemoryHigh": "windows",
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
    "WindowsMemoryHigh": "memory_percent",
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
    "memory_percent": {"name": "RAM", "kind": "percent"},
    "disk_percent": {"name": "HDD", "kind": "percent"},
    "network": {"name": "NET", "kind": "bytes"},
    "load": {"name": "LOAD", "kind": "load"},
    "swap_percent": {"name": "SWAP", "kind": "percent"},
    "proc": {"name": "PROC", "kind": "proc"},
}

# Assets list glance: Collecting, CPU, RAM, HDD, NET left → right. Missing tiles stay —.
GLANCE_CELLS = (
    ("up", "Collecting"),
    ("cpu_percent", "CPU"),
    ("memory_percent", "RAM"),
    ("disk_percent", "HDD"),
    ("network", "NET"),
)

# Chart tiles are CPU / RAM / HDD / NET. `up` is queried for Online on Identity, not drawn.
_CLASS_TILES = {
    "demo": ("cpu_percent", "memory_percent", "disk_percent", "network"),
    "linux": ("cpu_percent", "memory_percent", "disk_percent", "network"),
    "windows": ("cpu_percent", "memory_percent", "disk_percent", "network"),
    "network": ("network",),
    "unknown": ("network",),
}

GRAPH_WINDOW_SECONDS = 24 * 3600
INCIDENT_PAD_SECONDS = 3600.0
MAX_DISK_LINES = 4
MIN_RAM_BYTES = 64 * 1024 * 1024
MIN_LINK_BYTES = 1_000_000
_SKIP_MOUNT = re.compile(r"^/(run|sys|dev|proc|boot/efi)(/|$)|docker|kubelet|containerd")

# asset_kind leaves Storage / QNAP / Printer as "other". They are still if_mib hosts.
_SNMP_FAMILY = ("network", "switch", "router", "firewall", "storage", "qnap", "nas", "printer", "snmp")


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


def _alerts_yml_thresholds(base: Path | None = None) -> dict[str, dict[str, float]]:
    """``> N`` from each rule's expr. Prometheus loads every copy of a rule, so the lowest N fires first."""
    out: dict[str, dict[str, float]] = {}
    for alertname, rules in load_alert_rules(base).items():
        tile = _ALERT_TILE.get(alertname)
        klass = _ALERT_CLASS.get(alertname)
        if not tile or not klass:
            continue
        for rule in rules:
            found = re.findall(r">=?\s*(\d+(?:\.\d+)?)", rule.get("expr") or "")
            if not found:
                continue
            value = float(found[-1])
            current = out.setdefault(klass, {}).get(tile)
            out[klass][tile] = value if current is None else min(current, value)
    return out


def bundled_thresholds(base: Path | None = None) -> dict[str, dict[str, float]]:
    """Percent thresholds from playrule presets, overlaid by alerts.yml + alerts.local.yml (FORGESRE_MONITORING_DIR)."""
    merged = _playrule_thresholds()
    for klass, tiles in _alerts_yml_thresholds(base).items():
        merged.setdefault(klass, {}).update(tiles)
    return merged


def metric_class_for(asset: Any) -> str:
    if isinstance(asset, dict):
        asset_id = str(asset.get("asset_id") or "")
        kind = str(asset.get("type") or "")
        profile = str(asset.get("monitoring_profile") or "")
        scrape = str(asset.get("scrape_address") or "")
    else:
        asset_id = str(getattr(asset, "asset_id", "") or "")
        kind = str(getattr(asset, "type", "") or "")
        profile = str(getattr(asset, "monitoring_profile", "") or "")
        scrape = str(getattr(asset, "scrape_address", "") or "")
    if asset_id == DEMO_ASSET:
        return "demo"
    klass = asset_kind(kind, profile)
    if klass in {"linux", "windows", "network"}:
        return klass
    scrape = scrape.strip().lower()
    if scrape.endswith(":9182"):
        return "windows"
    if scrape.endswith(":9100"):
        return "linux"
    blob = f"{kind} {profile}".lower()
    if any(token in blob for token in _SNMP_FAMILY):
        return "network"
    return "unknown"


def _field(asset: Any, key: str) -> str:
    if isinstance(asset, dict):
        return str(asset.get(key) or "")
    return str(getattr(asset, key, "") or "")


def _asset_dict(asset: Any) -> dict[str, Any]:
    from app.asset_types import snmp_port_for

    extras = asset.get("extras") if isinstance(asset, dict) else getattr(asset, "extras", None)
    return {
        "asset_id": _field(asset, "asset_id"),
        "hostname": _field(asset, "hostname"),
        "ip": _field(asset, "ip"),
        "type": _field(asset, "type"),
        "monitoring_profile": _field(asset, "monitoring_profile"),
        "scrape_address": _field(asset, "scrape_address"),
        "source": _field(asset, "source"),
        "zabbix_hostid": _field(asset, "zabbix_hostid"),
        "glpi_id": _field(asset, "glpi_id"),
        "snmp_port": snmp_port_for(asset),
        "alarms": asset.get("alarms") if isinstance(asset, dict) else getattr(asset, "alarms", None),
        "extras": extras if isinstance(extras, dict) else {},
    }


def _finite(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in {float("inf"), float("-inf")}:
        return None
    return value


def _tone_for(kind: str, value: float | None, threshold: float | None, *, enabled: bool = True) -> str:
    if value is None:
        return "warn"
    if kind == "up":
        if value >= 1:
            return "ok"
        return "ok" if not enabled else "crit"
    if kind == "proc":
        if value < 1:
            return "ok"
        return "ok" if not enabled else "crit"
    if kind == "load":
        return "ok"
    if not enabled:
        return "ok"
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
    if kind == "bytes":
        return "—"
    if kind == "load":
        return f"{value:.2f}"
    if kind == "proc":
        return "ok" if value < 1 else "down"
    return f"{value:.0f}%"


def _rate_scale(samples: list[float]) -> tuple[float, str]:
    """Pick one unit for RX and TX so the two lines share an axis. Never invents a point."""
    peak = 0.0
    for raw in samples:
        value = _finite(raw)
        if value is None:
            continue
        peak = max(peak, abs(value))
    if peak >= 1_000_000_000:
        return 1_000_000_000.0, "GB/s"
    if peak >= 1_000_000:
        return 1_000_000.0, "MB/s"
    if peak >= 1_000:
        return 1_000.0, "kB/s"
    return 1.0, "B/s"


def _fmt_rate(value: float | None, divisor: float, unit: str) -> str | None:
    if value is None:
        return None
    scaled = value / divisor if divisor else value
    if unit == "B/s":
        return f"{scaled:.0f} {unit}"
    text = f"{scaled:.1f}".rstrip("0").rstrip(".")
    return f"{text or '0'} {unit}"


def _network_display(rx: float | None, tx: float | None, divisor: float, unit: str) -> str:
    parts: list[str] = []
    for label, value in (("RX", rx), ("TX", tx)):
        text = _fmt_rate(value, divisor, unit)
        if text:
            parts.append(f"{label} {text}")
    return " · ".join(parts) if parts else "—"


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


def _window_range(expr: str, start: float, end: float) -> dict[str, Any]:
    from app.services import query_prometheus_range, range_step, range_timeout

    span = max(60.0, float(end) - float(start))
    return query_prometheus_range(
        expr,
        hours=span / 3600.0,
        step=range_step(span),
        timeout=range_timeout(span),
        start=start,
        end=end,
    )


def _tile(
    key: str,
    value: float | None,
    *,
    threshold: float | None,
    spark: str = "",
    series: list[float] | None = None,
    times: list[float] | None = None,
    query: str = "",
    enabled: bool = True,
    unit: str = "",
    lines: list[dict[str, Any]] | None = None,
    display: str | None = None,
    name: str | None = None,
    kind: str | None = None,
    source: str = "",
) -> dict[str, Any]:
    meta = _TILE_META[key]
    kind = str(kind or meta["kind"])
    tone = _tone_for(kind, value, threshold, enabled=enabled)
    bar = _bar_pct(kind, value, tone)
    stamps = [round(float(t), 3) for t in (times or [])]
    values = [round(float(v), 3) for v in (series or [])]
    if len(stamps) != len(values):
        stamps = []
    tile: dict[str, Any] = {
        "key": key,
        "name": name or str(meta["name"]),
        "kind": kind,
        "value": None if value is None else round(float(value), 2),
        "display": display if display is not None else _display(kind, value),
        "tone": tone,
        "threshold": threshold,
        "alarm_enabled": enabled,
        "bar_pct": bar,
        "spark": spark,
        "series": values,
        "times": stamps,
        "query": query,
        "source": source,
    }
    if unit:
        tile["unit"] = unit
    if lines is not None:
        tile["lines"] = lines
    return tile


def incident_graph_bounds(incident: Any, now: datetime | None = None) -> dict[str, Any]:
    """Unix window for an incident chart: 1h before start, through now or the resolve time.

    ``marker`` is ``started_at``. It stays inside the window so the vertical line has a place to sit.
    """
    from app.services import graph_window_label, incident_end, incident_is_live, utcnow

    now_dt = now or utcnow()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    end_dt = now_dt
    started = getattr(incident, "started_at", None)
    marker: float | None = None
    if isinstance(started, datetime):
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        marker = float(started.timestamp())
        if not incident_is_live(str(getattr(incident, "status", "") or "")):
            ended = incident_end(incident)
            if isinstance(ended, datetime):
                end_dt = ended
    end = float(end_dt.timestamp())
    start = end - GRAPH_WINDOW_SECONDS
    if marker is not None:
        start = min(marker - INCIDENT_PAD_SECONDS, start)
        if end < marker:
            end = marker
    if end <= start:
        end = start + 60.0
    return {
        "start": start,
        "end": end,
        "marker": marker,
        "label": graph_window_label(start, end),
    }


def _query_up(
    selectors: list[str],
    fetch: QueryFn,
    *,
    snmp: bool = False,
) -> tuple[str, float | None, str, str]:
    """Same first matcher as verify (asset=<id>); then hostname; then instance scrape."""
    last_expr = ""
    if not selectors:
        expr = 'up{job="forgesre-snmp"}' if snmp else "up"
        result = fetch(expr)
        if result.get("error"):
            return "", None, str(result.get("error") or "prometheus unreachable"), expr
        return "", _finite(result.get("value")), "", expr
    for selector in selectors:
        expr = f'up{{job="forgesre-snmp",{selector}}}' if snmp else f"up{{{selector}}}"
        last_expr = expr
        result = fetch(expr)
        if result.get("error"):
            return selector, None, str(result.get("error") or "prometheus unreachable"), expr
        value = _finite(result.get("value"))
        if value is not None:
            return selector, value, "", expr
    return selectors[0], None, "", last_expr


def _aligned_series(ranged: dict[str, Any]) -> tuple[list[float], list[float]]:
    raw_values = list(ranged.get("values") or [])
    raw_times = list(ranged.get("times") or [])
    values: list[float] = []
    stamps: list[float] = []
    for index, raw in enumerate(raw_values):
        value = _finite(raw)
        if value is None:
            continue
        values.append(value)
        if index < len(raw_times):
            stamp = _finite(raw_times[index])
            if stamp is not None:
                stamps.append(stamp)
    if len(stamps) != len(values):
        stamps = []
    return values, stamps


def _bytes_label(raw: float | None) -> str:
    """Human RAM size. None / tiny values stay empty so a percent sample is not shown as GB."""
    value = _finite(raw)
    if value is None or value < MIN_RAM_BYTES:
        return ""
    gib = value / (1024.0 ** 3)
    if gib >= 10:
        return f"{gib:.0f} GB"
    text = f"{gib:.1f}".rstrip("0").rstrip(".")
    return f"{text or '0'} GB"


def _keep_mount(labels: dict[str, Any]) -> bool:
    mount = str(labels.get("mountpoint") or labels.get("volume") or labels.get("device") or "")
    if not mount:
        return True
    return _SKIP_MOUNT.search(mount) is None


def _disk_line_name(labels: dict[str, Any], index: int) -> str:
    mount = str(labels.get("mountpoint") or labels.get("volume") or labels.get("device") or "").strip()
    if mount in {"/", "C:", "C"}:
        return mount
    if mount and len(mount) <= 18:
        return mount
    return f"HDD{index}"


def _disk_sort_key(row: dict[str, Any]) -> tuple[int, float]:
    labels = row.get("labels") or {}
    mount = str(labels.get("mountpoint") or labels.get("volume") or "")
    primary = 0 if mount in {"/", "C:", "C"} else 1
    latest = _finite((row.get("values") or [None])[-1]) or 0.0
    return (primary, -latest)


def _range_rows(ranged: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in ranged.get("series") or []:
        if not isinstance(raw, dict):
            continue
        values, stamps = _aligned_series({"values": raw.get("values") or [], "times": raw.get("times") or []})
        if len(values) < 2:
            continue
        rows.append({"labels": dict(raw.get("labels") or {}), "values": values, "times": stamps})
    if rows:
        return rows
    values, stamps = _aligned_series(ranged)
    if len(values) < 2:
        return []
    return [{"labels": {}, "values": values, "times": stamps}]


def _load_disk_lines(
    expr: str,
    spark_fetch: RangeFn | None,
    *,
    prom_down: bool,
) -> list[dict[str, Any]]:
    """Up to four filesystem / volume series. tmpfs and run mounts stay out."""
    if prom_down or not expr or spark_fetch is None:
        return []
    ranged = spark_fetch(expr)
    if ranged.get("error"):
        return []
    picked = [row for row in _range_rows(ranged) if _keep_mount(row.get("labels") or {})]
    picked.sort(key=_disk_sort_key)
    lines: list[dict[str, Any]] = []
    for index, row in enumerate(picked[:MAX_DISK_LINES], start=1):
        item: dict[str, Any] = {
            "name": _disk_line_name(row.get("labels") or {}, index),
            "className": f"disk-{index - 1}",
            "series": [round(float(value), 3) for value in row["values"]],
        }
        stamps = row.get("times") or []
        if len(stamps) == len(row["values"]):
            item["times"] = [round(float(stamp), 3) for stamp in stamps]
        lines.append(item)
    return lines


def _fmt_pct(value: float | None) -> str | None:
    if value is None:
        return None
    if abs(value) >= 10:
        return f"{value:.0f}%"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def _network_pct_display(rx: float | None, tx: float | None) -> str:
    parts: list[str] = []
    for label, value in (("RX", rx), ("TX", tx)):
        text = _fmt_pct(value)
        if text:
            parts.append(f"{label} {text}")
    return " · ".join(parts) if parts else "—"


def _load_network(
    packed: dict[str, tuple[str, str]],
    fetch: QueryFn,
    spark_fetch: RangeFn | None,
    *,
    prom_down: bool,
) -> dict[str, Any]:
    """RX/TX from the same range helper as CPU. Empty sides stay empty — no sine, no zero fill."""
    rx_expr = packed.get("net_rx", ("", ""))[0]
    tx_expr = packed.get("net_tx", ("", ""))[0]
    query = " ; ".join(part for part in (rx_expr, tx_expr) if part)
    rx_val: float | None = None
    tx_val: float | None = None
    rx_series: list[float] = []
    tx_series: list[float] = []
    rx_times: list[float] = []
    tx_times: list[float] = []
    if not prom_down and (rx_expr or tx_expr):

        def point(expr: str) -> float | None:
            if not expr:
                return None
            result = fetch(expr)
            if result.get("error"):
                return None
            return _finite(result.get("value"))

        def ranged(expr: str) -> tuple[list[float], list[float]]:
            if not expr or spark_fetch is None:
                return [], []
            result = spark_fetch(expr)
            if result.get("error"):
                return [], []
            return _aligned_series(result)

        rx_val = point(rx_expr)
        tx_val = point(tx_expr)
        rx_series, rx_times = ranged(rx_expr)
        tx_series, tx_times = ranged(tx_expr)
        if rx_val is None and rx_series:
            rx_val = rx_series[-1]
        if tx_val is None and tx_series:
            tx_val = tx_series[-1]
    pool = list(rx_series) + list(tx_series)
    if rx_val is not None:
        pool.append(rx_val)
    if tx_val is not None:
        pool.append(tx_val)
    speed_expr = packed.get("net_speed", ("", ""))[0]
    speed = None
    if not prom_down and speed_expr:
        speed = _finite((fetch(speed_expr) or {}).get("value"))
        if speed is not None and speed < MIN_LINK_BYTES:
            speed = None
    use_pct = speed is not None
    if use_pct:
        peak = max([abs(v) for v in pool] or [0.0])
        if speed and peak / speed * 100.0 > 250.0:
            use_pct = False
            speed = None

    if use_pct and speed:
        def scaled(values: list[float]) -> list[float]:
            return [round(value / speed * 100.0, 3) for value in values]

        primary = rx_val if rx_val is not None else tx_val
        rx_pct = None if rx_val is None else rx_val / speed * 100.0
        tx_pct = None if tx_val is None else tx_val / speed * 100.0
        unit = "%"
        display = _network_pct_display(rx_pct, tx_pct) if primary is not None else "—"
        value = None if primary is None else primary / speed * 100.0
        kind = "percent"
    else:
        divisor, unit = _rate_scale(pool)

        def scaled(values: list[float]) -> list[float]:
            return [round(value / divisor, 3) for value in values]

        primary = rx_val if rx_val is not None else tx_val
        display = _network_display(rx_val, tx_val, divisor, unit) if primary is not None else "—"
        value = None if primary is None else primary / divisor
        kind = "bytes"

    def line(name: str, class_name: str, values: list[float], stamps: list[float]) -> dict[str, Any] | None:
        if len(values) < 2:
            return None
        row: dict[str, Any] = {"name": name, "className": class_name, "series": scaled(values)}
        if len(stamps) == len(values):
            row["times"] = [round(stamp, 3) for stamp in stamps]
        return row

    lines = [row for row in (line("RX", "rx", rx_series, rx_times), line("TX", "tx", tx_series, tx_times)) if row]
    if len(rx_series) >= 2:
        primary_series, primary_times = rx_series, rx_times
    elif len(tx_series) >= 2:
        primary_series, primary_times = tx_series, tx_times
    else:
        primary_series, primary_times = [], []
    return {
        "query": query,
        "value": value,
        "series": scaled(primary_series),
        "times": [round(stamp, 3) for stamp in primary_times] if len(primary_times) == len(primary_series) else [],
        "unit": unit if value is not None or lines else "",
        "kind": kind,
        "lines": lines,
        "display": display,
    }


def asset_metric_panel(
    asset: Any,
    *,
    query_fn: QueryFn | None = None,
    range_fn: RangeFn | None = None,
    range_start: float | None = None,
    range_end: float | None = None,
    marker: float | None = None,
) -> dict[str, Any]:
    """JSON for GET /api/v1/assets/{id}/metrics and the detail-page first paint."""
    from app.asset_alarms import normalize_alarms, tile_enabled, tile_threshold

    info = _asset_dict(asset)
    asset_id = info["asset_id"]
    klass = metric_class_for(asset)
    demo = is_lab_inventory_row(asset)
    bundled = bundled_thresholds().get("demo" if klass == "demo" else klass, {})
    alarms = normalize_alarms(info.get("alarms"), "demo" if klass == "demo" else klass)
    from app.asset_types import snmp_port_for
    from app.snmp_families import (
        FAMILY_TILE,
        http_source_label,
        metrics_on,
        snmp_metrics_on,
        snmp_source_label,
        tile_source_label,
    )

    fams = metrics_on(info)
    snmp_fams = snmp_metrics_on(info)
    if fams:
        keys = tuple(
            FAMILY_TILE[key]
            for key in ("cpu", "load", "mem", "swap", "disk", "proc", "net")
            if key in fams
        )
    else:
        keys = ()
    fetch = query_fn or _default_query
    queried_end = time.time() if range_end is None else float(range_end)
    queried_start = queried_end - GRAPH_WINDOW_SECONDS if range_start is None else float(range_start)
    if queried_end <= queried_start:
        queried_end = queried_start + 60.0
    spark_fetch = range_fn
    samples: dict[str, float | None] = {}
    queries: dict[str, str] = {}
    sparks: dict[str, str] = {}
    series: dict[str, list[float]] = {}
    times: dict[str, list[float]] = {}
    prom_error = ""
    prom_down = False
    selectors = promql_selectors_for(info)
    snmp = snmp_port_for(info) > 0 or klass == "network"

    winning, up_value, up_error, up_expr = _query_up(selectors, fetch, snmp=snmp)
    queries["up"] = up_expr
    if up_error:
        prom_down = True
        prom_error = up_error
        samples["up"] = None
    else:
        samples["up"] = up_value

    packed: dict[str, tuple[str, str]] = {}
    if klass != "unknown" or snmp:
        packed = promql_queries_for(
            info,
            selector=winning or None,
            include_fallbacks=not winning,
        )
        packed["up"] = (up_expr, "")

    for key in keys:
        if key in {"up", "network"}:
            continue
        expr = packed.get(key, ("", ""))[0] if key in packed else ""
        queries[key] = expr
        if not expr or prom_down:
            samples[key] = None
            continue
        result = fetch(expr)
        if result.get("error"):
            prom_down = True
            prom_error = str(result.get("error") or "prometheus unreachable")
            samples[key] = None
            continue
        samples[key] = _finite(result.get("value"))

    if klass == "demo":
        from app.metrics import demo_metric_values

        live = demo_metric_values()
        samples["cpu_percent"] = float(live["forgesre_demo_cpu_percent"])
        samples["disk_percent"] = float(live["forgesre_demo_disk_percent"])
        queries["cpu_percent"] = "forgesre_demo_cpu_percent"
        queries["disk_percent"] = "forgesre_demo_disk_percent"

    if spark_fetch is None and not prom_down:
        def spark_fetch(expr: str, _start: float = queried_start, _end: float = queried_end) -> dict[str, Any]:
            return _window_range(expr, _start, _end)

    # The instant query only sees the last ~5 minutes. A down exporter still has
    # query_range history across the chart window — skipping it left a flat `up`.
    if spark_fetch and not prom_down:
        for key in keys:
            if key == "network":
                continue
            expr = queries.get(key) or ""
            if not expr:
                continue
            ranged = spark_fetch(expr)
            if ranged.get("error"):
                continue
            values, stamps = _aligned_series(ranged)
            if not values:
                continue
            if samples.get(key) is None:
                samples[key] = values[-1]
            series[key] = values
            times[key] = stamps
            sparks[key] = _spark_points(values)

    net = _load_network(packed, fetch, spark_fetch, prom_down=prom_down) if "network" in keys else None
    if net:
        queries["network"] = net["query"]
        samples["network"] = net["value"]
        if net["series"]:
            series["network"] = net["series"]
            times["network"] = net["times"]

    collecting: bool | None
    if prom_down:
        collecting = None
        collecting_line = "Prometheus is unreachable — not collecting."
    elif samples.get("up") is None:
        collecting = False
        collecting_line = "Prometheus is not collecting this target."
    elif (samples.get("up") or 0) >= 1:
        collecting = True
        collecting_line = (
            f"Prometheus sees {snmp_source_label(info)} (up=1)."
            if snmp
            else f"Prometheus sees {http_source_label(info)} (up=1)."
        )
    else:
        collecting = False
        collecting_line = "Prometheus is not collecting this target (up=0)."

    ram_name = None
    mem_total_expr = packed.get("memory_total", ("", ""))[0]
    if mem_total_expr and not prom_down:
        total = fetch(mem_total_expr)
        if not total.get("error"):
            label = _bytes_label(_finite(total.get("value")))
            if label:
                ram_name = f"RAM · {label}"

    disk_lines: list[dict[str, Any]] = []
    volumes_expr = packed.get("disk_volumes", ("", ""))[0]
    if volumes_expr:
        disk_lines = _load_disk_lines(volumes_expr, spark_fetch, prom_down=prom_down)
        if disk_lines:
            primary = disk_lines[0]
            series["disk_percent"] = list(primary.get("series") or [])
            times["disk_percent"] = list(primary.get("times") or [])
            if samples.get("disk_percent") is None and primary.get("series"):
                samples["disk_percent"] = primary["series"][-1]
            sparks["disk_percent"] = _spark_points(series["disk_percent"])

    tiles = []
    for key in keys:
        extra: dict[str, Any] = {}
        threshold = tile_threshold(alarms, key, bundled.get(key)) if key not in {"up", "network"} else None
        if key == "network" and net:
            extra = {
                "unit": net["unit"],
                "lines": net["lines"],
                "display": net["display"],
                "kind": net.get("kind"),
            }
        if key == "memory_percent" and ram_name:
            extra["name"] = ram_name
        if key == "disk_percent" and disk_lines:
            extra["lines"] = disk_lines
        tiles.append(
            _tile(
                key,
                samples.get(key),
                threshold=threshold,
                spark=sparks.get(key) or "",
                series=series.get(key),
                times=times.get(key),
                query=queries.get(key) or "",
                enabled=tile_enabled(alarms, key) if key != "network" else True,
                source=tile_source_label(info, bool(snmp_fams) and key in {FAMILY_TILE[fam] for fam in snmp_fams}),
                **extra,
            )
        )
    from app.services import graph_window_label

    return {
        "asset_id": asset_id,
        "hostname": info["hostname"],
        "class": klass,
        "demo": demo,
        "demo_label": "DEMO" if demo else "",
        "collecting": collecting,
        "collecting_line": collecting_line,
        "error": prom_error,
        "alarms": alarms,
        "tiles": tiles,
        "sources": {"http": http_source_label(info), "snmp": snmp_source_label(info)},
        "window": {
            "start": queried_start,
            "end": queried_end,
            "marker": None if marker is None else float(marker),
            "label": graph_window_label(queried_start, queried_end),
        },
    }


def glance_cells(panel: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Glance cells follow the Metrics cube — only tracked tiles, plus Collecting."""
    tiles = list((panel or {}).get("tiles") or [])
    collecting = None if panel is None else panel.get("collecting")
    cells: list[dict[str, Any]] = []
    if collecting is True:
        display, tone = "up", "ok"
    elif collecting is False:
        display, tone = "down", "crit"
    else:
        display, tone = "—", "warn"
    cells.append({"key": "up", "label": "Collecting", "display": display, "tone": tone})
    for tile in tiles:
        key = str(tile.get("key") or "")
        if not key or key == "up":
            continue
        cells.append(
            {
                "key": key,
                "label": tile.get("name") or dict(GLANCE_CELLS).get(key) or key,
                "display": tile.get("display") or "—",
                "tone": tile.get("tone") or "warn",
            }
        )
    return cells


def prometheus_has_samples(panel: dict[str, Any]) -> bool:
    """True when Prometheus scrapes this asset (up seen) or any metric tile has a value / series."""
    if panel.get("collecting"):
        return True
    for tile in panel.get("tiles") or []:
        if tile.get("key") == "up":
            continue
        if tile.get("value") is not None or len(tile.get("series") or []) >= 2:
            return True
    return False


ZABBIX_TILE_KEYS = ("cpu_percent", "memory_percent", "disk_percent")


def zabbix_metric_panel(asset: Any, *, trends_fn: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """CPU / memory / disk from Zabbix trend.get for one asset with zabbix_hostid. Lazy, cached in app.zabbix."""
    from app.asset_alarms import normalize_alarms, tile_enabled, tile_threshold
    from app.settings import settings

    info = _asset_dict(asset)
    hostid = str(getattr(asset, "zabbix_hostid", "") or (asset.get("zabbix_hostid") if isinstance(asset, dict) else "") or "")
    klass = metric_class_for(asset)
    threshold_class = klass if klass in {"linux", "windows"} else "linux"
    bundled = bundled_thresholds().get(threshold_class, {})
    alarms = normalize_alarms(info.get("alarms"), threshold_class)
    if trends_fn is None:
        from app.zabbix import host_trends

        def trends_fn(value: str) -> dict[str, Any]:
            return host_trends(settings.zabbix_url, settings.zabbix_token, value, timeout=settings.zabbix_timeout)

    try:
        result = trends_fn(hostid) if hostid else {"ok": False, "error": "no zabbix_hostid", "tiles": {}}
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "tiles": {}}
    found = result.get("tiles") or {}
    from app.snmp_families import FAMILY_TILE, metrics_on

    wanted = {FAMILY_TILE[key] for key in metrics_on(info)}
    tiles = []
    for key in ZABBIX_TILE_KEYS:
        if wanted and key not in wanted:
            continue
        row = found.get(key)
        if not row:
            continue
        values, stamps = _aligned_series({"values": row.get("series") or [], "times": row.get("times") or []})
        tiles.append(
            _tile(
                key,
                _finite(row.get("value")),
                threshold=tile_threshold(alarms, key, bundled.get(key)),
                spark=_spark_points(values),
                series=values,
                times=stamps,
                query=f"zabbix trend.get {row.get('key') or ''}".strip(),
                enabled=tile_enabled(alarms, key),
            )
        )
    hours = int(result.get("hours") or 24)
    if not result.get("ok"):
        line = f"Zabbix unavailable — {result.get('error') or 'no answer'}"
    elif tiles:
        line = f"Zabbix trends (hourly average, last {hours} h). No Prometheus samples for this host."
    else:
        line = "No Zabbix CPU / memory / disk items on this host."
    return {
        "asset_id": info["asset_id"],
        "hostname": info["hostname"],
        "class": klass,
        "source": "zabbix",
        "demo": False,
        "demo_label": "",
        "collecting": bool(tiles),
        "collecting_line": line,
        "error": "",
        "alarms": alarms,
        "tiles": tiles,
    }


def _zabbix_window(panel: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Zabbix trends keep their own clock. The incident marker is drawn only when it falls inside them."""
    stamps: list[float] = []
    for tile in panel.get("tiles") or []:
        for raw in tile.get("times") or []:
            stamp = _finite(raw)
            if stamp is not None:
                stamps.append(stamp)
    marker = _finite(previous.get("marker"))
    if len(stamps) >= 2:
        start, end = min(stamps), max(stamps)
        if end <= start:
            end = start + 3600.0
        if marker is None or marker < start - 1 or marker > end + 1:
            marker = None
        return {"start": start, "end": end, "marker": marker, "label": "last 24 h"}
    return {
        "start": previous.get("start"),
        "end": previous.get("end"),
        "marker": None,
        "label": "last 24 h",
    }


def metric_panel_with_zabbix(asset: Any, panel: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Prometheus first. Zabbix trends only for an asset with zabbix_hostid and no Prometheus samples."""
    panel.setdefault("source", "prometheus")
    hostid = str(getattr(asset, "zabbix_hostid", "") or "").strip()
    if not hostid or prometheus_has_samples(panel):
        return panel
    from app.settings import settings

    if not settings.zabbix_enabled and "trends_fn" not in kwargs:
        return panel
    zpanel = zabbix_metric_panel(asset, **kwargs)
    zpanel["window"] = _zabbix_window(zpanel, panel.get("window") or {})
    return zpanel


def safe_asset_metric_panel(asset: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return asset_metric_panel(asset, **kwargs)
    except Exception as exc:
        info = _asset_dict(asset)
        demo = is_lab_inventory_row(asset)
        return {
            "asset_id": info["asset_id"],
            "hostname": info["hostname"],
            "class": "unknown",
            "demo": demo,
            "demo_label": "DEMO" if demo else "",
            "collecting": False,
            "collecting_line": "Prometheus is unreachable — not collecting.",
            "error": str(exc),
            "tiles": [
                _tile(key, None, threshold=None)
                for key in _CLASS_TILES["unknown"]
            ],
        }
