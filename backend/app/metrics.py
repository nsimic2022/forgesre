from __future__ import annotations

import asyncio
import inspect
import logging
import shutil
import threading
import time
from typing import Any, Callable

import httpx
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from prometheus_client import CONTENT_TYPE_LATEST, ProcessCollector, PlatformCollector

from app.settings import settings

log = logging.getLogger("forgesre.metrics")

# Instant + range PromQL. A hung Prometheus must not freeze the asset page.
PROMETHEUS_TIMEOUT = 5.0
PROMETHEUS_CONNECT_TIMEOUT = 2.0

InstantQuery = Callable[..., dict[str, Any]]

registry = CollectorRegistry()
ProcessCollector(registry=registry)
PlatformCollector(registry=registry)

demo_cpu = Gauge(
    "forgesre_demo_cpu_percent",
    "Demo CPU percent for ForgeSRE vertical slice (forge-demo-01)",
    registry=registry,
)
demo_disk = Gauge(
    "forgesre_demo_disk_percent",
    "Demo filesystem percent for ForgeSRE RCA slice (forge-demo-01)",
    registry=registry,
)
disk_used = Gauge(
    "forgesre_disk_used_percent",
    "Disk used percent for the ForgeSRE data volume",
    registry=registry,
)
up = Gauge("forgesre_up", "ForgeSRE core availability", registry=registry)

demo_cpu.set(12)
demo_disk.set(35)
up.set(1)


def set_demo_cpu(value: float) -> None:
    demo_cpu.set(value)


def set_demo_disk(value: float) -> None:
    demo_disk.set(value)


def reset_demo_gauges() -> None:
    demo_cpu.set(12)
    demo_disk.set(35)


def refresh_runtime_metrics() -> None:
    try:
        usage = shutil.disk_usage("/")
        percent = (usage.used / usage.total) * 100 if usage.total else 0
        disk_used.set(round(percent, 2))
    except OSError:
        pass
    up.set(1)


def gauge_value(metric: Gauge) -> float:
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == metric._name:
                return float(sample.value)
    return 0.0


def demo_metric_values() -> dict[str, float]:
    return {
        "forgesre_demo_cpu_percent": gauge_value(demo_cpu),
        "forgesre_demo_disk_percent": gauge_value(demo_disk),
    }


def metrics_response() -> tuple[bytes, str]:
    refresh_runtime_metrics()
    return generate_latest(registry), CONTENT_TYPE_LATEST


def prometheus_timeout(timeout: float | None = None) -> httpx.Timeout:
    """Hard cap 5s; connect stays short so a SYN-drop fails fast."""
    total = min(PROMETHEUS_TIMEOUT, max(0.1, float(timeout if timeout is not None else PROMETHEUS_TIMEOUT)))
    connect = min(PROMETHEUS_CONNECT_TIMEOUT, total)
    return httpx.Timeout(total, connect=connect)


def range_step(span_seconds: float) -> str:
    """1-minute steps for host charts through two days; coarser only on longer windows."""
    span = max(0.0, float(span_seconds))
    if span <= 48 * 3600:
        return "1m"
    if span <= 7 * 24 * 3600:
        return "5m"
    return "15m"


def range_timeout(span_seconds: float) -> float:
    hours = max(0.0, float(span_seconds)) / 3600.0
    return min(PROMETHEUS_TIMEOUT, max(2.0, 2.0 + hours * 0.15))


def run_sync(awaitable: Any) -> Any:
    """Run a coroutine from pytest / sync FastAPI routes. A live loop uses a worker thread."""
    if not inspect.isawaitable(awaitable):
        return awaitable
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(awaitable)
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=_worker, name="metrics-prom-sync")
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _prometheus_url(path: str) -> str:
    return f"{(settings.prometheus_url or 'http://127.0.0.1:9090').rstrip('/')}{path}"


async def prometheus_request(
    path: str,
    params: dict[str, Any],
    timeout: float = PROMETHEUS_TIMEOUT,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET Prometheus /api/v1/query or /api/v1/query_range. Raises on HTTP/network failure."""
    url = _prometheus_url(path)
    bound = prometheus_timeout(timeout)

    async def _do(owned: httpx.AsyncClient) -> httpx.Response:
        return await owned.get(url, params=params)

    if client is None:
        async with httpx.AsyncClient(timeout=bound) as owned:
            response = await _do(owned)
    else:
        response = await _do(client)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


async def fetch_instant_metric(
    promql_query: str,
    timeout: float = PROMETHEUS_TIMEOUT,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Instant PromQL. Empty list on timeout, HTTP error, or a non-success body."""
    try:
        data = await prometheus_request(
            "/api/v1/query",
            {"query": promql_query},
            timeout=timeout,
            client=client,
        )
    except Exception as exc:
        log.error("prometheus instant failed: %s", exc)
        return []
    if data.get("status") and data.get("status") != "success":
        log.warning("prometheus instant status=%s query=%s", data.get("status"), promql_query)
        return []
    result = (data.get("data") or {}).get("result") or []
    return [row for row in result if isinstance(row, dict)]


async def fetch_range_metric(
    promql_query: str,
    start: float,
    end: float,
    step: str = "1m",
    timeout: float = PROMETHEUS_TIMEOUT,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Range PromQL. Empty list on timeout or error — never a fake sine / repeated gauge."""
    try:
        data = await prometheus_request(
            "/api/v1/query_range",
            {"query": promql_query, "start": start, "end": end, "step": step},
            timeout=timeout,
            client=client,
        )
    except Exception as exc:
        log.error("prometheus range failed: %s", exc)
        return []
    if data.get("status") and data.get("status") != "success":
        log.warning("prometheus range status=%s query=%s", data.get("status"), promql_query)
        return []
    result = (data.get("data") or {}).get("result") or []
    return [row for row in result if isinstance(row, dict)]


def _range_samples(series: dict[str, Any]) -> tuple[list[float], list[float]]:
    times: list[float] = []
    points: list[float] = []
    for pair in series.get("values") or []:
        try:
            ts = float(pair[0])
            value = float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if value != value or value in {float("inf"), float("-inf")}:
            continue
        if ts != ts or ts in {float("inf"), float("-inf")}:
            continue
        times.append(ts)
        points.append(value)
    return times, points


async def _invoke_query(query_fn: InstantQuery, expr: str, timeout: float) -> dict[str, Any]:
    try:
        result = query_fn(expr, timeout=timeout)
    except TypeError:
        result = query_fn(expr)
    if inspect.isawaitable(result):
        return await result
    return result


async def query_prometheus_expr(
    expr: str,
    timeout: float = PROMETHEUS_TIMEOUT,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Instant PromQL. Demo gauges are not short-circuited here — get_asset_load overlays them."""
    try:
        data = await prometheus_request("/api/v1/query", {"query": expr}, timeout=timeout, client=client)
        result = (data.get("data") or {}).get("result") or []
        if result:
            return {"value": float(result[0]["value"][1]), "query": expr}
        return {"value": None, "query": expr}
    except Exception as exc:
        return {"error": str(exc), "query": expr}


async def query_prometheus_range(
    expr: str,
    hours: float = 1.0,
    step: str = "5m",
    timeout: float = PROMETHEUS_TIMEOUT,
    start: float | None = None,
    end: float | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Samples from ``start`` to ``end`` (unix seconds). Default is the last ``hours``.

    Empty or error → no line (never a repeated gauge or a sine).
    When several series match, keep the one that actually moves, with its timestamps.
    """
    end_ts = time.time() if end is None else float(end)
    start_ts = end_ts - max(0.1, float(hours)) * 3600.0 if start is None else float(start)
    if end_ts <= start_ts:
        end_ts = start_ts + 60.0
    wait = min(PROMETHEUS_TIMEOUT, max(0.1, float(timeout if timeout is not None else PROMETHEUS_TIMEOUT)))
    try:
        data = await prometheus_request(
            "/api/v1/query_range",
            {"query": expr, "start": start_ts, "end": end_ts, "step": step},
            timeout=wait,
            client=client,
        )
        result = [row for row in ((data.get("data") or {}).get("result") or []) if isinstance(row, dict)]
        best: list[float] = []
        best_times: list[float] = []
        best_key = (-1.0, -1)
        series_out: list[dict[str, Any]] = []
        for series in result:
            times, points = _range_samples(series)
            if not points:
                continue
            labels = series.get("metric") if isinstance(series.get("metric"), dict) else {}
            series_out.append({"labels": dict(labels or {}), "values": points, "times": times})
            span = (max(points) - min(points)) if len(points) > 1 else 0.0
            key = (span, len(points))
            if key > best_key:
                best = points
                best_times = times
                best_key = key
        return {
            "values": best,
            "times": best_times,
            "series": series_out,
            "query": expr,
            "start": start_ts,
            "end": end_ts,
        }
    except Exception as exc:
        return {"error": str(exc), "query": expr, "start": start_ts, "end": end_ts}


async def query_prometheus_vector(
    expr: str,
    timeout: float = PROMETHEUS_TIMEOUT,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """All series for one instant query. Error or empty is not a fake sample."""
    try:
        data = await prometheus_request("/api/v1/query", {"query": expr}, timeout=timeout, client=client)
        rows = [row for row in ((data.get("data") or {}).get("result") or []) if isinstance(row, dict)]
    except Exception as exc:
        return {"error": str(exc), "series": [], "query": expr}
    series = []
    for row in rows:
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


def _asset_info(asset: Any = None, *, ip_address: str = "", asset_type: str = "") -> dict[str, Any]:
    if isinstance(asset, dict):
        info = dict(asset)
    elif asset is not None:
        info = {
            "asset_id": getattr(asset, "asset_id", "") or "",
            "hostname": getattr(asset, "hostname", "") or "",
            "ip": getattr(asset, "ip", "") or "",
            "type": getattr(asset, "type", "") or "",
            "monitoring_profile": getattr(asset, "monitoring_profile", "") or "",
            "scrape_address": getattr(asset, "scrape_address", "") or "",
            "snmp_port": getattr(asset, "snmp_port", None),
        }
    else:
        info = {}
    if ip_address:
        info["ip"] = ip_address
    if asset_type:
        info["type"] = asset_type
    return info


async def get_asset_load(
    asset: Any = None,
    *,
    ip_address: str = "",
    asset_type: str = "",
    timeout: float = PROMETHEUS_TIMEOUT,
    query_fn: InstantQuery | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Instant load for one asset. PromQL follows the saved type (node_ / windows_ / if_mib)."""
    from rca.collector import DEMO_ASSET, promql_queries_for

    info = _asset_info(asset, ip_address=ip_address, asset_type=asset_type)
    packed = promql_queries_for(info)
    queries = {key: expr for key, (expr, _unit) in packed.items()}
    out: dict[str, Any] = {"queries": dict(queries)}
    wait = min(PROMETHEUS_TIMEOUT, max(0.1, float(timeout if timeout is not None else PROMETHEUS_TIMEOUT)))

    async def _one(key: str, expr: str) -> tuple[str, dict[str, Any]]:
        if query_fn is not None:
            sample = await _invoke_query(query_fn, expr, wait)
        else:
            sample = await query_prometheus_expr(expr, timeout=wait, client=client)
        return key, sample

    pairs = await asyncio.gather(*[_one(key, expr) for key, expr in queries.items()], return_exceptions=True)
    for item in pairs:
        if isinstance(item, BaseException):
            out["error"] = str(item)
            continue
        key, sample = item
        if sample.get("error"):
            out.setdefault("error", sample["error"])
            continue
        if "value" in sample and sample["value"] is not None:
            out[key] = sample["value"]
    if info.get("asset_id") == DEMO_ASSET:
        live = demo_metric_values()
        out["cpu_percent"] = live["forgesre_demo_cpu_percent"]
        out["disk_percent"] = live["forgesre_demo_disk_percent"]
        out.setdefault("queries", {})["cpu_percent"] = "forgesre_demo_cpu_percent"
        out.setdefault("queries", {})["disk_percent"] = "forgesre_demo_disk_percent"
    return out
