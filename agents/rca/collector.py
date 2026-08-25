"""Evidence collector. Queries existing systems; never scrapes hosts itself."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from rca.types import EvidenceItem, normalize_log, normalize_metric, utc_now

MetricFetcher = Callable[[str], dict[str, Any]]
LogFetcher = Callable[[str, datetime, datetime], dict[str, Any]]

DEMO_ASSET = "forge-demo-01"

DEFAULT_QUERIES = {
    "cpu_percent": ("forgesre_demo_cpu_percent", "percent"),
    "disk_percent": ("forgesre_demo_disk_percent", "percent"),
    "disk_volume_percent": ("forgesre_disk_used_percent", "percent"),
    "memory_bytes": ("process_resident_memory_bytes", "bytes"),
    "up": ("forgesre_up", ""),
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def promql_queries_for(asset: dict[str, Any] | None, alert: dict[str, Any] | None = None) -> dict[str, tuple[str, str]]:
    """PromQL for this asset. Demo gauges only for forge-demo-01."""
    asset = asset or {}
    alert = alert or {}
    asset_id = str(asset.get("asset_id") or "")
    kind = str(asset.get("type") or "").lower()
    profile = str(asset.get("monitoring_profile") or "").lower()
    alertname = str(alert.get("alertname") or "").lower()
    if asset_id == DEMO_ASSET:
        return dict(DEFAULT_QUERIES)
    if not asset_id:
        return {"up": ("up", "")}
    matcher = f'asset="{_escape(asset_id)}"'
    snmpish = any(
        token in kind or token in profile or token in alertname
        for token in ("network", "switch", "router", "firewall", "snmp")
    ) or "network-switch" in profile
    if snmpish:
        return {
            "up": (f'up{{job="forgesre-snmp",{matcher}}}', ""),
        }
    windowsish = "windows" in kind or "win32" in kind or "windows" in profile
    if windowsish:
        return {
            "cpu_percent": (
                f'100 - (avg(rate(windows_cpu_time_total{{mode="idle",{matcher}}}[5m])) * 100)',
                "percent",
            ),
            "disk_percent": (
                f'100 * max(1 - (windows_logical_disk_free_bytes{{volume!~"_Total",{matcher}}} '
                f'/ windows_logical_disk_size_bytes{{volume!~"_Total",{matcher}}}))',
                "percent",
            ),
            "memory_percent": (
                f'100 * (1 - (windows_os_physical_memory_free_bytes{{{matcher}}} '
                f'/ windows_cs_physical_memory_bytes{{{matcher}}}))',
                "percent",
            ),
            "up": (f'up{{{matcher}}}', ""),
        }
    queries = {
        "cpu_percent": (
            f'100 * (1 - avg(rate(node_cpu_seconds_total{{mode="idle",{matcher}}}[5m])))',
            "percent",
        ),
        "disk_percent": (
            f'100 * max(1 - (node_filesystem_avail_bytes{{fstype!~"tmpfs|fuse.*|overlay",{matcher}}} '
            f'/ node_filesystem_size_bytes{{fstype!~"tmpfs|fuse.*|overlay",{matcher}}}))',
            "percent",
        ),
        "memory_percent": (
            f'100 * (1 - (node_memory_MemAvailable_bytes{{{matcher}}} '
            f'/ node_memory_MemTotal_bytes{{{matcher}}}))',
            "percent",
        ),
        "up": (f'up{{{matcher}}}', ""),
    }
    return queries


def loki_query_for(asset: dict[str, Any] | None) -> str:
    asset_id = str((asset or {}).get("asset_id") or "")
    if not asset_id or asset_id == DEMO_ASSET:
        return '{job="forgesre"}'
    return '{asset="%s"}' % _escape(asset_id)


def collect_evidence_set(
    *,
    incident: dict[str, Any],
    asset: dict[str, Any],
    alert: dict[str, Any] | None,
    history: list[dict[str, Any]],
    playrules: list[dict[str, Any]],
    maintenance: list[dict[str, Any]],
    metric_fetcher: MetricFetcher | None,
    log_fetcher: LogFetcher | None,
    window_minutes: int = 30,
    max_log_lines: int = 20,
    max_evidence: int = 40,
    now: datetime | None = None,
) -> tuple[list[EvidenceItem], list[str]]:
    now = now or datetime.now(timezone.utc)
    started = now - timedelta(minutes=window_minutes)
    asset_id = str(asset.get("asset_id") or incident.get("asset") or "")
    items: list[EvidenceItem] = []
    limitations: list[str] = []
    seq = 1
    queries = promql_queries_for(asset, alert)
    loki_query = loki_query_for(asset)

    def add(kind: str, source: str, content: Any, query: str = "", confidence: float = 1.0, extra: dict[str, Any] | None = None) -> None:
        nonlocal seq
        if len(items) >= max_evidence:
            return
        item = EvidenceItem(
            evidence_id=f"EV-{seq:05d}",
            type=kind,
            source=source,
            timestamp=utc_now(),
            asset_id=asset_id,
            content=content,
            query=query,
            metadata=extra or {},
            confidence=confidence,
        )
        item.ensure_hash()
        items.append(item)
        seq += 1

    if alert:
        add("ALERT", "alertmanager", alert, extra={"alertname": alert.get("alertname")})
    if asset:
        add("INVENTORY", "forgesre", asset)
    if playrules:
        add("PLAYRULE", "forgesre", playrules[0], extra={"executed": False})
    if maintenance:
        add("MAINTENANCE", "forgesre", maintenance)
    if history:
        add("INCIDENT_HISTORY", "forgesre", history)

    if metric_fetcher is None:
        limitations.append("Metrics unavailable.")
        add("METRIC", "prometheus", {"error": "no fetcher"}, extra={"unavailable": True}, confidence=0.2)
    else:
        for name, (expr, unit) in queries.items():
            result = metric_fetcher(expr)
            if result.get("error"):
                limitations.append("Metrics unavailable.")
                add(
                    "METRIC",
                    "prometheus",
                    {"error": result["error"]},
                    query=expr,
                    extra={"unavailable": True, "window_minutes": window_minutes},
                    confidence=0.2,
                )
                break
            add(
                "METRIC",
                "prometheus",
                normalize_metric(name, result.get("value"), utc_now(), unit),
                query=expr,
                extra={"window_minutes": window_minutes, "start": started.isoformat()},
            )

    if log_fetcher is None:
        limitations.append("Logs unavailable.")
        add("LOG", "loki", {"error": "no fetcher"}, query=loki_query, extra={"unavailable": True}, confidence=0.2)
    else:
        result = log_fetcher(loki_query, started, now)
        if result.get("error"):
            limitations.append("Logs unavailable.")
            add(
                "LOG",
                "loki",
                {"error": result["error"]},
                query=loki_query,
                extra={"unavailable": True, "window_minutes": window_minutes},
                confidence=0.2,
            )
        else:
            for line in (result.get("lines") or [])[:max_log_lines]:
                add("LOG", "loki", normalize_log(str(line), utc_now()), query=loki_query)

    limitations = list(dict.fromkeys(limitations))
    return items, limitations
