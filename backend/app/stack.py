"""Bundled stack: doctor rows plus Open links for Grafana / Prometheus / …"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse, urlunparse

from app.settings import settings


def request_hostname(host_header: str) -> str:
    return (host_header or "localhost").split(":")[0] or "localhost"


def rewrite_host(url: str, hostname: str) -> str:
    """Show 127.0.0.1 services on the same host the operator used to open Core."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return url
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return url
    netloc = f"{hostname}:{parsed.port}" if parsed.port else hostname
    return urlunparse(parsed._replace(netloc=netloc))


def runtime_state(item: dict[str, Any] | None) -> tuple[str, str]:
    """running (green), starting/paused (yellow), down (red)."""
    item = item or {}
    status = str(item.get("status") or "error").lower()
    why = str(item.get("why") or "").lower()
    if status in {"ok", "healthy"}:
        return "running", "ok"
    if status in {"disabled", "warn", "warning"}:
        return "paused", "warn"
    if "timeout" in why or "timed out" in why:
        return "starting", "warn"
    return "down", "crit"


def _port_url(hostname: str, port: int, path: str = "/") -> str:
    path = path if path.startswith("/") else f"/{path}"
    return f"http://{hostname}:{port}{path}"


def enrich_components(components: dict[str, Any], host_header: str) -> list[dict[str, Any]]:
    """Doctor components in stack order, each with Open GUI / metrics links."""
    hostname = request_hostname(host_header)
    grafana = rewrite_host(settings.grafana_public_url, hostname)
    prometheus = rewrite_host((settings.prometheus_url or "http://127.0.0.1:9090").rstrip("/"), hostname)
    alertmanager = rewrite_host((settings.alertmanager_url or "http://127.0.0.1:9093").rstrip("/"), hostname)
    loki = rewrite_host((settings.loki_url or "http://127.0.0.1:3100").rstrip("/"), hostname)
    snmp = rewrite_host((settings.snmp_exporter_url or "http://127.0.0.1:9116").rstrip("/"), hostname)
    llm = rewrite_host((settings.llm_url or "http://127.0.0.1:8088/v1").rstrip("/"), hostname)
    netbox = (settings.netbox_url or "").rstrip("/")
    catalog = [
        {
            "id": "core",
            "gui": "/",
            "gui_label": "UI",
            "metrics": "/metrics",
            "metrics_label": "Metrics",
        },
        {"id": "postgres", "gui": "", "metrics": ""},
        {
            "id": "prometheus",
            "gui": prometheus + "/graph",
            "gui_label": "GUI",
            "metrics": prometheus + "/metrics",
            "metrics_label": "Metrics",
        },
        {
            "id": "alertmanager",
            "gui": alertmanager + "/#/alerts",
            "gui_label": "GUI",
            "metrics": alertmanager + "/metrics",
            "metrics_label": "Metrics",
        },
        {
            "id": "snmp",
            "gui": snmp + "/metrics",
            "gui_label": "Metrics",
            "metrics": "",
        },
        {
            "id": "loki",
            "gui": loki + "/ready",
            "gui_label": "Ready",
            "metrics": loki + "/metrics",
            "metrics_label": "Metrics",
        },
        {
            "id": "alloy",
            "gui": _port_url(hostname, 12345, "/metrics"),
            "gui_label": "Metrics",
            "metrics": "",
        },
        {
            "id": "grafana",
            "gui": grafana,
            "gui_label": "GUI",
            "metrics": "",
        },
        {
            "id": "llm",
            "gui": llm + "/models" if not llm.endswith("/models") else llm,
            "gui_label": "API",
            "metrics": "",
        },
        {
            "id": "netbox",
            "gui": netbox,
            "gui_label": "GUI",
            "metrics": "",
        },
        {
            "id": "discovery",
            "gui": "/discovery",
            "gui_label": "UI",
            "metrics": "",
        },
    ]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in catalog:
        cid = spec["id"]
        seen.add(cid)
        item = dict(components.get(cid) or {"status": "disabled", "why": "Not in this doctor run."})
        state, css = runtime_state(item)
        gui = spec.get("gui") or ""
        metrics = spec.get("metrics") or ""
        if cid == "netbox" and not gui:
            gui = ""
        why = str(item.get("why") or "")
        if not why and str(item.get("status") or "") == "disabled":
            why = "Disabled in config or not bundled."
        rows.append(
            {
                "id": cid,
                "status": item.get("status") or "disabled",
                "state": state,
                "css": css,
                "why": why,
                "test": item.get("test") or "",
                "fix": item.get("fix") or "",
                "gui": gui,
                "gui_label": spec.get("gui_label") or "Open",
                "metrics": metrics,
                "metrics_label": spec.get("metrics_label") or "Metrics",
            }
        )
    for cid, item in components.items():
        if cid in seen:
            continue
        packed = dict(item or {})
        state, css = runtime_state(packed)
        rows.append(
            {
                "id": cid,
                "status": packed.get("status") or "error",
                "state": state,
                "css": css,
                "why": packed.get("why") or "",
                "test": packed.get("test") or "",
                "fix": packed.get("fix") or "",
                "gui": "",
                "gui_label": "Open",
                "metrics": "",
                "metrics_label": "Metrics",
            }
        )
    return rows
