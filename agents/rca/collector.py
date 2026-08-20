"""Evidence collector. Queries existing systems; never scrapes hosts itself."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from rca.types import EvidenceItem, normalize_log, normalize_metric, utc_now

MetricFetcher = Callable[[str], dict[str, Any]]
LogFetcher = Callable[[str, datetime, datetime], dict[str, Any]]

DEFAULT_QUERIES = {
    "cpu_percent": ("forgesre_demo_cpu_percent", "percent"),
    "disk_percent": ("forgesre_demo_disk_percent", "percent"),
    "disk_volume_percent": ("forgesre_disk_used_percent", "percent"),
    "memory_bytes": ("process_resident_memory_bytes", "bytes"),
    "up": ("forgesre_up", ""),
}


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
        for name, (expr, unit) in DEFAULT_QUERIES.items():
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

    loki_query = '{job="forgesre"}'
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
