"""Settings › Demo: open the lab scenarios from the new UI (Admin).

Each scenario opens a DEMO-tagged incident on a seeded forge-demo-* asset, runs ForgeRCA and the
first escalation step, exactly like ``./forgesre demo``. Because that path can query Prometheus
and send mail, it runs as the ``demo_run`` job and never inside the web request. Nothing touches a
real host: the CPU and disk values are lab gauges, lowered again with Lower demo gauges.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

DEMO_KIND = "demo_run"


def _scenarios() -> dict[str, tuple[str, str, Callable[[Session], Any]]]:
    from app import services
    from app.metrics import set_demo_cpu, set_demo_disk

    def cpu(db: Session):
        set_demo_cpu(94)
        return services.run_demo(db)

    def disk(db: Session):
        set_demo_disk(94)
        return services.run_demo_rca(db)

    return {
        "highcpu": ("High CPU", "HighCPU on forge-demo-01: owner mail, similar history, ForgeRCA.", cpu),
        "disk": ("Disk filling", "FilesystemUsageHigh on forge-demo-01: the ForgeRCA disk path.", disk),
        "host": ("Host down", "NodeExporterDown on forge-demo-01; the real scrape is unchanged.", services.run_demo_host),
        "nodecpu": ("Linux NodeCPUHigh", "A second Linux alert on the same demo host.", services.run_demo_nodecpu),
        "windows": ("Windows CPU", "WindowsCPUHigh on forge-demo-win-01 (not scraped).", services.run_demo_windows),
        "network": ("Network device down", "SnmpDeviceUnreachable on forge-demo-sw-01 (no real SNMP walk).", services.run_demo_network),
    }


def scenario_list() -> list[dict[str, str]]:
    return [{"key": key, "label": label, "detail": detail} for key, (label, detail, _run) in _scenarios().items()]


def run_scenario(db: Session, key: str) -> dict[str, Any]:
    """Job body: seed the lab inventory if needed, then open the scenario's incident."""
    from app.inventory import seed_demo_candidate
    from app.seed import seed

    chosen = _scenarios().get(key)
    if chosen is None:
        raise RuntimeError(f"unknown demo scenario {key}")
    seed(db)
    seed_demo_candidate(db)
    incident = chosen[2](db)
    return {"incident": incident.number if incident is not None else None}
