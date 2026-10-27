"""SNMP metric families and the collecting source label (node :9100 vs SNMP).

NET stays on ``if_mib``. CPU / load / RAM / swap / disk / processes use the bundled
``host`` module (HOST-RESOURCES + UCD-SNMP). Any asset type can opt in — not a vendor SKU.
"""

from __future__ import annotations

from typing import Any

from app.asset_types import DEFAULT_SNMP_PORT, parse_snmp_port, snmp_family, snmp_poll_on, snmp_port_for

HOST_MODULE = "host"
SNMP_METRICS_KEY = "snmp_metrics"
NET_FAMILY = "net"
HOST_FAMILIES: tuple[str, ...] = ("cpu", "load", "mem", "swap", "disk", "proc")
ALL_FAMILIES: tuple[str, ...] = (NET_FAMILY,) + HOST_FAMILIES
FAMILY_LABELS = {
    "net": "NET",
    "cpu": "CPU",
    "load": "Load",
    "mem": "RAM",
    "swap": "Swap",
    "disk": "Disk",
    "proc": "Proc",
}
PROMETHEUS_FAMILIES: frozenset[str] = frozenset({"cpu", "mem", "disk", "net"})
FAMILY_TILE = {
    "net": "network",
    "cpu": "cpu_percent",
    "load": "load",
    "mem": "memory_percent",
    "swap": "swap_percent",
    "disk": "disk_percent",
    "proc": "proc",
}


def _extras(asset: Any) -> dict:
    raw = asset.get("extras") if isinstance(asset, dict) else getattr(asset, "extras", None)
    return raw if isinstance(raw, dict) else {}


def parse_snmp_metrics(raw: Any) -> frozenset[str] | None:
    """None = key absent (default depends on poll vs family SNMP). Explicit empty = NET only."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple, set, frozenset)):
        parts = [str(item).strip().lower() for item in raw]
    else:
        text = str(raw).strip().lower()
        if text == "":
            return frozenset()
        parts = [bit.strip() for bit in text.replace(" ", ",").split(",") if bit.strip()]
    return frozenset(part for part in parts if part in ALL_FAMILIES)


def normalize_snmp_metrics(raw: Any) -> dict[str, str]:
    parsed = parse_snmp_metrics(raw if not isinstance(raw, dict) else raw.get(SNMP_METRICS_KEY))
    if parsed is None:
        return {}
    ordered = [key for key in ALL_FAMILIES if key in parsed]
    return {SNMP_METRICS_KEY: ",".join(ordered)}


def snmp_metrics_on(asset: Any) -> frozenset[str]:
    """Families this asset scrapes over SNMP. Empty when SNMP is off.

    No stored key: Poll SNMP (or an explicit port on a non-family type) gets
    if_mib + host. Family SNMP (switch / router / printer / …) stays if_mib
    until the Metrics cube stores a pick. Existing ``lnx`` + poll therefore
    joins the host scrape without a form revisit.
    """
    if snmp_port_for(asset) <= 0:
        return frozenset()
    extras = _extras(asset)
    stored = parse_snmp_metrics(extras.get(SNMP_METRICS_KEY) if SNMP_METRICS_KEY in extras else None)
    if stored is None:
        kind = asset.get("type") if isinstance(asset, dict) else getattr(asset, "type", "")
        profile = (
            asset.get("monitoring_profile")
            if isinstance(asset, dict)
            else getattr(asset, "monitoring_profile", "")
        )
        if snmp_poll_on(extras) or not snmp_family(str(kind or ""), str(profile or "")):
            return frozenset(ALL_FAMILIES)
        return frozenset({NET_FAMILY})
    return frozenset(stored)


def host_metrics_on(asset: Any) -> bool:
    return bool(snmp_metrics_on(asset) & set(HOST_FAMILIES))


def families_from_form(present: str, **flags: str) -> str | None:
    """Posted Metrics-cube checkboxes. None when the cube was not on the form."""
    if not str(present or "").strip():
        return None
    picked = [
        key
        for key in ALL_FAMILIES
        if str(flags.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}
    ]
    return ",".join(picked)


def _flag_on(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def sources_from_form(present: str, prometheus: str = "", snmp: str = "") -> dict[str, bool] | None:
    """Posted Source-cube checkboxes. None when the cubes were not on the form."""
    if not str(present or "").strip():
        return None
    return {"prometheus": _flag_on(prometheus), "snmp": _flag_on(snmp)}


def http_exporter_on(asset: Any) -> bool:
    """True when this asset has a node / windows scrape address — graphs use that, not SNMP."""
    if isinstance(asset, dict):
        scrape = str(asset.get("scrape_address") or "").strip()
        kind = str(asset.get("type") or "")
    else:
        scrape = str(getattr(asset, "scrape_address", "") or "").strip()
        kind = str(getattr(asset, "type", "") or "")
    if not scrape:
        return False
    blob = kind.lower()
    lower = scrape.lower()
    return (
        blob.startswith("auto")
        or "linux" in blob
        or "windows" in blob
        or "win32" in blob
        or lower.endswith(":9100")
        or lower.endswith(":9182")
    )


def metrics_on(asset: Any) -> frozenset[str]:
    """Families drawn as graphs. Stored pick wins; HTTP scrape uses Prometheus CPU/RAM/Disk/NET."""
    extras = _extras(asset)
    stored = parse_snmp_metrics(extras.get(SNMP_METRICS_KEY) if SNMP_METRICS_KEY in extras else None)
    if stored is not None:
        return frozenset(stored)
    if http_exporter_on(asset):
        return PROMETHEUS_FAMILIES
    if snmp_port_for(asset) > 0:
        return snmp_metrics_on(asset)
    scrape = (
        str(asset.get("scrape_address") or "").strip()
        if isinstance(asset, dict)
        else str(getattr(asset, "scrape_address", "") or "").strip()
    )
    kind = (
        str(asset.get("type") or "")
        if isinstance(asset, dict)
        else str(getattr(asset, "type", "") or "")
    )
    blob = kind.lower()
    if scrape or "linux" in blob or "windows" in blob:
        return PROMETHEUS_FAMILIES
    zid = (
        str(asset.get("zabbix_hostid") or "").strip()
        if isinstance(asset, dict)
        else str(getattr(asset, "zabbix_hostid", "") or "").strip()
    )
    source = (
        str(asset.get("source") or "")
        if isinstance(asset, dict)
        else str(getattr(asset, "source", "") or "")
    )
    if zid or source == "zabbix":
        return frozenset({"cpu", "mem", "disk"})
    return frozenset({NET_FAMILY})


def metrics_on_form(asset: Any, form_mode: str = "add") -> frozenset[str]:
    """Checkbox defaults on Add: Prometheus CPU/RAM/Disk/NET until the operator saves a pick."""
    extras = _extras(asset)
    stored = parse_snmp_metrics(extras.get(SNMP_METRICS_KEY) if SNMP_METRICS_KEY in extras else None)
    if stored is not None:
        return frozenset(stored)
    if form_mode != "edit":
        if snmp_port_for(asset) > 0 or snmp_poll_on(extras):
            return snmp_metrics_on(asset)
        return PROMETHEUS_FAMILIES
    return metrics_on(asset)


def metric_source_cubes(asset: Any, form_mode: str = "add") -> list[dict[str, Any]]:
    if isinstance(asset, dict):
        scrape = str(asset.get("scrape_address") or "").strip()
        kind = str(asset.get("type") or "")
        source = str(asset.get("source") or "")
        zid = str(asset.get("zabbix_hostid") or "").strip()
        gid = str(asset.get("glpi_id") or "").strip()
    else:
        scrape = str(getattr(asset, "scrape_address", "") or "").strip()
        kind = str(getattr(asset, "type", "") or "")
        source = str(getattr(asset, "source", "") or "")
        zid = str(getattr(asset, "zabbix_hostid", "") or "").strip()
        gid = str(getattr(asset, "glpi_id", "") or "").strip()
    extras = _extras(asset)
    snmp = snmp_port_for(asset) > 0 or snmp_poll_on(extras)
    blob = kind.lower()
    prom = bool(scrape)
    if form_mode == "add" and not scrape and not snmp:
        prom = blob.startswith("auto") or "linux" in blob or "windows" in blob
    return [
        {"key": "prometheus", "label": "Prometheus", "on": prom, "toggle": True},
        {"key": "zabbix", "label": "Zabbix", "on": source == "zabbix" or bool(zid), "toggle": False},
        {"key": "snmp", "label": "SNMP", "on": snmp, "toggle": True},
        {"key": "glpi", "label": "GLPI", "on": source == "glpi" or bool(gid), "toggle": False},
    ]


def metric_family_heading(asset: Any, form_mode: str = "add") -> str:
    extras = _extras(asset)
    if http_exporter_on(asset):
        return "Prometheus metrics"
    if snmp_port_for(asset) > 0 or snmp_poll_on(extras):
        return "SNMP metrics"
    kind = (
        str(asset.get("type") or "")
        if isinstance(asset, dict)
        else str(getattr(asset, "type", "") or "")
    )
    blob = kind.lower()
    if form_mode == "add" and (blob.startswith("auto") or "linux" in blob or "windows" in blob):
        return "Prometheus metrics"
    return "Metrics"


def http_source_label(asset: Any) -> str:
    if isinstance(asset, dict):
        scrape = str(asset.get("scrape_address") or "").strip()
        kind = str(asset.get("type") or "")
    else:
        scrape = str(getattr(asset, "scrape_address", "") or "").strip()
        kind = str(getattr(asset, "type", "") or "")
    lower = scrape.lower()
    if lower.endswith(":9182"):
        return "windows :9182"
    if lower.endswith(":9100"):
        return "node :9100"
    if scrape:
        return f"HTTP {scrape}"
    blob = kind.lower()
    if "windows" in blob:
        return "windows :9182 (default scrape, not SNMP)"
    if "linux" in blob:
        return "node :9100 (default scrape, not SNMP)"
    return "HTTP off"


def snmp_source_label(asset: Any) -> str:
    port = snmp_port_for(asset) or DEFAULT_SNMP_PORT
    fams = snmp_metrics_on(asset)
    if not fams:
        return "SNMP off"
    extra = "if_mib + host" if fams & set(HOST_FAMILIES) else "if_mib"
    return f"SNMP UDP/{port} · {extra}"


def tile_source_label(asset: Any, family_on: bool) -> str:
    if family_on and snmp_port_for(asset) > 0:
        port = snmp_port_for(asset) or DEFAULT_SNMP_PORT
        return f"SNMP UDP/{port}"
    http = http_source_label(asset)
    if http != "HTTP off" and "not SNMP" not in http:
        return http
    if http.endswith("(default scrape, not SNMP)"):
        return http
    return "not collected"


def scrape_uses_snmp(asset: Any) -> bool:
    return snmp_port_for(asset) > 0 or snmp_poll_on(_extras(asset))


def parse_explicit_snmp_port(asset: Any) -> int:
    raw = asset.get("snmp_port") if isinstance(asset, dict) else getattr(asset, "snmp_port", None)
    return parse_snmp_port(raw)
