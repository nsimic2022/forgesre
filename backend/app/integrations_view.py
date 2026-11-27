"""Settings › Integrations: one card per outside system and per part of the monitoring stack.

Each card joins what is spread over the platform: the doctor status and reason, the link to the
service's own web UI (loopback-only services get the SSH tunnel hint instead), the last sync from
the journal, which settings and secrets belong to it, and whether it can be tested or synced now.
Reading this calls nothing outside: the status comes from the cached doctor run.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import JournalEntry

# Outside systems ForgeSRE reads from. ``prefix`` picks the settings rows, ``sync`` the job target.
SOURCES: list[dict[str, Any]] = [
    {"id": "zabbix", "label": "Zabbix", "role": "Reads hosts into the inventory and Zabbix problems as alarms. Read-only.",
     "prefix": "inventory.zabbix.", "secrets": ["ZABBIX_API_TOKEN"], "sync": "zabbix", "journal": ("zabbix", "sync")},
    {"id": "glpi", "label": "GLPI", "role": "Reads computers and network equipment into the inventory. Read-only.",
     "prefix": "inventory.glpi.", "secrets": ["GLPI_APP_TOKEN", "GLPI_USER_TOKEN"], "sync": "glpi", "journal": ("glpi", "sync")},
    {"id": "librenms", "label": "LibreNMS", "role": "Identifies SNMP devices (vendor, model, ports) and gives LLDP/CDP links for the dependency map.",
     "prefix": "inventory.librenms.", "secrets": ["LIBRENMS_API_TOKEN"], "sync": "librenms", "journal": ("inventory", "topology.sync")},
    {"id": "netbox", "label": "NetBox", "role": "Reads devices, sites and racks into the inventory. ForgeSRE never writes to it.",
     "prefix": "inventory.netbox.", "secrets": ["NETBOX_API_TOKEN"], "sync": "netbox", "journal": ("netbox", "sync")},
    {"id": "kubernetes", "label": "Kubernetes / OpenShift", "role": "Reads nodes and workloads every 5 minutes with a read-only token.",
     "prefix": "kubernetes.", "secrets": [], "sync": "", "journal": ("kubernetes", "sync")},
    {"id": "mcp", "label": "MCP server", "role": "Lets an AI client read incidents and assets. Off by default; data goes to that client's model.",
     "prefix": "mcp.", "secrets": ["MCP_TOKEN"], "sync": "", "journal": None},
]

# Parts of the bundled monitoring stack: status and a link to their own UI.
STACK: list[dict[str, str]] = [
    {"id": "prometheus", "label": "Prometheus", "role": "Scrapes exporters and SNMP, evaluates the alert rules."},
    {"id": "alertmanager", "label": "Alertmanager", "role": "Groups firing alerts and posts them to ForgeSRE."},
    {"id": "loki", "label": "Loki", "role": "Stores device syslog and logs used as incident evidence."},
    {"id": "alloy", "label": "Alloy", "role": "Receives syslog on 514 and ships it to Loki."},
    {"id": "snmp", "label": "SNMP exporter", "role": "Walks network devices for Prometheus."},
    {"id": "grafana", "label": "Grafana", "role": "Graphs for every asset and the appliance."},
    {"id": "llm", "label": "ForgeAI model (llama.cpp)", "role": "Local language model; optional."},
]

SYNC_KINDS = {"zabbix", "glpi", "netbox", "librenms"}
# Components system_settings.test_connection can check now.
TESTABLE = {"prometheus", "alertmanager", "loki", "grafana", "snmp", "llm", "netbox", "zabbix", "glpi", "librenms"}



# Stack parts whose path is shown on another integration's Flow page.
FLOW_FOR = {"alertmanager": "prometheus", "alloy": "syslog", "loki": "syslog"}


def _flow_names() -> set[str]:
    """Integrations that have a Flow page (app.flows registry)."""
    from app.flows import flows

    return set(flows())

def _last(db: Session, journal: tuple[str, str] | None) -> dict[str, Any] | None:
    if not journal:
        return None
    row = (
        db.query(JournalEntry)
        .filter(JournalEntry.module == journal[0], JournalEntry.action == journal[1])
        .order_by(JournalEntry.at.desc())
        .first()
    )
    if row is None:
        return None
    return {"at": row.at.isoformat() if row.at else None, "status": row.status, "summary": row.summary}


def _links(row: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for key, label in (("gui", "gui_label"), ("extra", "extra_label"), ("metrics", "metrics_label")):
        url = str(row.get(key) or "")
        if url:
            out.append({"label": str(row.get(label) or "Open"), "url": url})
    return out


def integrations(db: Session, host_header: str) -> dict[str, Any]:
    """Cards for Settings › Integrations: sources first, then the monitoring stack."""
    from app.api import doctor_payload
    from app.stack import enrich_components
    from app.system_settings import list_settings

    components = (doctor_payload() or {}).get("components") or {}
    rows = {r["id"]: r for r in enrich_components(components, host_header)}
    settings_rows = list_settings(None)

    def card(spec: dict[str, Any], kind: str) -> dict[str, Any]:
        row = rows.get(spec["id"]) or {"status": "disabled", "why": "Not checked by doctor."}
        keys = [s["key"] for s in settings_rows if spec.get("prefix") and s["key"].startswith(spec["prefix"])]
        keys += [k for k in spec.get("secrets") or [] if any(s["key"] == k for s in settings_rows)]
        return {
            "id": spec["id"], "label": spec["label"], "role": spec["role"], "kind": kind,
            "status": row.get("status") or "disabled", "why": row.get("why") or "",
            "links": _links(row), "local_only": bool(row.get("local_only")), "local_hint": row.get("local_hint") or "",
            "settings": keys, "test": spec["id"] if spec["id"] in TESTABLE else "",
            "sync": spec.get("sync") or "", "last_sync": _last(db, spec.get("journal")),
            "flow": f"/settings/integrations/{FLOW_FOR.get(spec['id'], spec['id'])}" if FLOW_FOR.get(spec["id"], spec["id"]) in _flow_names() else "",
        }

    return {
        "sources": [card(s, "source") for s in SOURCES],
        "stack": [card(s, "stack") for s in STACK],
    }


def run_sync(db: Session, target: str) -> dict[str, Any]:
    """Jobs worker: one integration sync now. Each client has its own timeout; errors go to the journal."""
    if target == "zabbix":
        from app.zabbix_sync import sync_zabbix

        return sync_zabbix(db, force=True) or {}
    if target == "glpi":
        from app.glpi_sync import sync_glpi

        return sync_glpi(db, force=True) or {}
    if target == "netbox":
        from app.inventory import sync_netbox

        return sync_netbox(db) or {}
    if target == "librenms":
        from app.topology import sync_librenms

        return sync_librenms(db) or {}
    raise ValueError(f"unknown sync target {target}")
