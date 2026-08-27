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


LINUX_EXPORTER_PORT = 9100
WINDOWS_EXPORTER_PORT = 9182


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _kind_flags(asset: dict[str, Any], alert: dict[str, Any] | None = None) -> tuple[bool, bool]:
    kind = str(asset.get("type") or "").lower()
    profile = str(asset.get("monitoring_profile") or "").lower()
    alertname = str((alert or {}).get("alertname") or "").lower()
    scrape = str(asset.get("scrape_address") or "").lower()
    snmpish = any(
        token in kind or token in profile or token in alertname
        for token in ("network", "switch", "router", "firewall", "snmp")
    ) or "network-switch" in profile
    windowsish = "windows" in kind or "win32" in kind or "windows" in profile or scrape.endswith(":9182")
    return snmpish, windowsish


def promql_selectors_for(asset: dict[str, Any] | None, alert: dict[str, Any] | None = None) -> list[str]:
    """Label matchers. First is verify's asset=<id>, then hostname, then instance scrape."""
    asset = asset or {}
    asset_id = str(asset.get("asset_id") or "").strip()
    hostname = str(asset.get("hostname") or "").strip()
    scrape = str(asset.get("scrape_address") or "").strip()
    ip = str(asset.get("ip") or "").strip()
    snmpish, windowsish = _kind_flags(asset, alert)
    seen: list[str] = []

    def add(selector: str) -> None:
        if selector and selector not in seen:
            seen.append(selector)

    if asset_id:
        add(f'asset="{_escape(asset_id)}"')
    if hostname and hostname != asset_id:
        add(f'asset="{_escape(hostname)}"')
    if scrape:
        add(f'instance="{_escape(scrape)}"')
    if snmpish:
        if ip:
            add(f'instance="{_escape(ip)}"')
        return seen
    port = WINDOWS_EXPORTER_PORT if windowsish else LINUX_EXPORTER_PORT
    if ip:
        derived = f"{ip}:{port}"
        if derived != scrape:
            add(f'instance="{_escape(derived)}"')
    return seen


def _fill(template: str, selector: str) -> str:
    return template.replace("__SEL__", selector)


def _or_fill(template: str, selectors: list[str]) -> str:
    if not selectors:
        return _fill(template, "")
    filled = [_fill(template, selector) for selector in selectors]
    if len(filled) == 1:
        return filled[0]
    return " or ".join(f"({part})" for part in filled)


def promql_queries_for(
    asset: dict[str, Any] | None,
    alert: dict[str, Any] | None = None,
    *,
    selector: str | None = None,
    include_fallbacks: bool = True,
) -> dict[str, tuple[str, str]]:
    """PromQL for this asset. Demo gauges only for forge-demo-01.

    Default matcher is verify's ``asset="<id>"``. When scrape/hostname/IP are on
    the asset dict, also OR ``instance="<ip>:<port>"`` so tiles find series labeled
    only by the scrape address (windows_exporter ``IP:9182``).
    """
    asset = asset or {}
    alert = alert or {}
    asset_id = str(asset.get("asset_id") or "")
    if asset_id == DEMO_ASSET:
        return dict(DEFAULT_QUERIES)
    if not asset_id:
        return {"up": ("up", "")}
    selectors = promql_selectors_for(asset, alert)
    if selector:
        selectors = [selector]
    elif not include_fallbacks:
        selectors = selectors[:1]
    if not selectors:
        selectors = [f'asset="{_escape(asset_id)}"']
    snmpish, windowsish = _kind_flags(asset, alert)
    if snmpish:
        return {
            "up": (_or_fill('up{job="forgesre-snmp",__SEL__}', selectors), ""),
        }
    if windowsish:
        return {
            "cpu_percent": (
                _or_fill(
                    '100 - (avg(rate(windows_cpu_time_total{mode="idle",__SEL__}[5m])) * 100)',
                    selectors,
                ),
                "percent",
            ),
            "disk_percent": (
                _or_fill(
                    '100 * max(1 - (windows_logical_disk_free_bytes{volume!~"_Total",__SEL__} '
                    '/ windows_logical_disk_size_bytes{volume!~"_Total",__SEL__}))',
                    selectors,
                ),
                "percent",
            ),
            "memory_percent": (
                _or_fill(
                    "100 * (1 - (windows_os_physical_memory_free_bytes{__SEL__} "
                    "/ windows_cs_physical_memory_bytes{__SEL__}))",
                    selectors,
                ),
                "percent",
            ),
            "up": (_or_fill("up{__SEL__}", selectors), ""),
        }
    return {
        "cpu_percent": (
            _or_fill(
                '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle",__SEL__}[5m])))',
                selectors,
            ),
            "percent",
        ),
        "disk_percent": (
            _or_fill(
                '100 * max(1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|fuse.*|overlay",__SEL__} '
                '/ node_filesystem_size_bytes{fstype!~"tmpfs|fuse.*|overlay",__SEL__}))',
                selectors,
            ),
            "percent",
        ),
        "memory_percent": (
            _or_fill(
                "100 * (1 - (node_memory_MemAvailable_bytes{__SEL__} "
                "/ node_memory_MemTotal_bytes{__SEL__}))",
                selectors,
            ),
            "percent",
        ),
        "up": (_or_fill("up{__SEL__}", selectors), ""),
    }


def loki_query_for(asset: dict[str, Any] | None) -> str | None:
    """LogQL for this asset, or None when Alloy does not ship that host.

    Alloy labels appliance Core logs ``asset=forge-demo-01`` / ``job=forgesre``.
    Querying ``{asset="<real-id>"}`` returns empty and must not look like host logs.
    """
    asset_id = str((asset or {}).get("asset_id") or "")
    if not asset_id or asset_id == DEMO_ASSET:
        return '{job="forgesre"}'
    return None


HOST_LOGS_LIMITATION = (
    "No host logs shipped. Alloy labels appliance Core logs as asset=forge-demo-01; "
    "empty Loki is not evidence from this VM."
)
DEMO_LOGS_LIMITATION = (
    "DEMO: Loki lines are appliance/Core logs (job=forgesre), not a customer host."
)


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
    if asset_id == DEMO_ASSET:
        limitations.append(DEMO_LOGS_LIMITATION)

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

    if loki_query is None:
        limitations.append(HOST_LOGS_LIMITATION)
    elif log_fetcher is None:
        limitations.append("Logs unavailable.")
        add("LOG", "loki", {"error": "no fetcher"}, query=loki_query, extra={"unavailable": True}, confidence=0.2)
    else:
        result = log_fetcher(loki_query, started, now)
        if result.get("skipped"):
            limitations.append(str(result.get("reason") or HOST_LOGS_LIMITATION))
        elif result.get("error"):
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
            extra = {"window_minutes": window_minutes}
            if asset_id == DEMO_ASSET:
                extra["scope"] = "appliance-demo"
                extra["label"] = "DEMO"
            for line in (result.get("lines") or [])[:max_log_lines]:
                add("LOG", "loki", normalize_log(str(line), utc_now()), query=loki_query, extra=extra)

    limitations = list(dict.fromkeys(limitations))
    return items, limitations
