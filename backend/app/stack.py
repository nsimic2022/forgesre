"""Bundled stack: doctor rows plus Open links for Grafana / Prometheus / …"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

from app.settings import settings

log = logging.getLogger("forgesre")

SOFT_STATUSES = frozenset({"ok", "disabled", "paused", "warn", "warning"})
_SNMP_ENSURE_AT = 0.0
_SNMP_ENSURE_COOLDOWN = 45.0

# Machine keys stay compose service ids (dashboard Open links, failed[], JSON).
# "core" is the stack row (dummy always-ok), not the /api/v1/health curl.
COMPONENT_LABELS = {
    "core": "Core (container)",
}


def component_label(cid: str) -> str:
    """Human name for doctor CLI and Health UI. Unknown keys print as-is."""
    return COMPONENT_LABELS.get(str(cid or ""), str(cid or ""))


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


def doctor_soft_status(status: str) -> bool:
    """Statuses that must not turn overall doctor DEGRADED / DOWN."""
    return str(status or "").lower() in SOFT_STATUSES


def runtime_state(item: dict[str, Any] | None) -> tuple[str, str]:
    """running (green), starting/paused (yellow), down (red)."""
    item = item or {}
    status = str(item.get("status") or "error").lower()
    why = str(item.get("why") or "").lower()
    if status in {"ok", "healthy"}:
        return "running", "ok"
    if status in {"disabled", "warn", "warning", "paused"}:
        return "paused", "warn"
    if "timeout" in why or "timed out" in why:
        return "starting", "warn"
    return "down", "crit"


def snmp_target_count() -> int:
    """How many inventory rows snmp_exporter would actually poll (not demo)."""
    try:
        from app.db import SessionLocal
        from app.inventory import sd_snmp_targets

        db = SessionLocal()
        try:
            return len(sd_snmp_targets(db))
        finally:
            db.close()
    except Exception:
        log.debug("snmp target count skipped", exc_info=True)
        return 0


def ensure_snmp_exporter() -> bool:
    """Start bundled snmp-exporter via compose when it should run.

    Skipped under FORGESRE_DEV (pytest) unless FORGESRE_START_SNMP=1.
    Core-in-container usually has no docker CLI; ``./forgesre doctor`` on the
    host does the same ``docker compose up -d snmp-exporter`` when SD has
    network targets. Does not require Zabbix.
    """
    global _SNMP_ENSURE_AT
    dev = os.environ.get("FORGESRE_DEV", "").strip().lower() in {"1", "true", "yes", "on"}
    force = os.environ.get("FORGESRE_START_SNMP", "").strip().lower() in {"1", "true", "yes", "on"}
    if dev and not force:
        return False
    now = time.monotonic()
    if now - _SNMP_ENSURE_AT < _SNMP_ENSURE_COOLDOWN:
        return False
    _SNMP_ENSURE_AT = now
    try:
        from app.backup import _compose_argv, _repo_root

        root = _repo_root()
        argv = [*_compose_argv(root), "up", "-d", "snmp-exporter"]
        result = subprocess.run(
            argv,
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
        )
        if result.returncode != 0:
            log.warning("snmp-exporter compose up failed: %s", (result.stderr or result.stdout or "")[:400])
            return False
        return True
    except Exception as exc:
        log.debug("snmp-exporter compose up skipped: %s", exc)
        return False


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
    netbox = rewrite_host((settings.netbox_url or "http://127.0.0.1:8001").rstrip("/"), hostname)
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
            "gui": prometheus + "/targets?search=",
            "gui_label": "Targets",
            "metrics": prometheus + "/alerts",
            "metrics_label": "Alerts",
            "extra": prometheus + "/graph",
            "extra_label": "Graph",
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
                "label": spec.get("label") or item.get("label") or component_label(cid),
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
                "extra": spec.get("extra") or "",
                "extra_label": spec.get("extra_label") or "Open",
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
                "label": packed.get("label") or component_label(cid),
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
                "extra": "",
                "extra_label": "Open",
            }
        )
    return rows
