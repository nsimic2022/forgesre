from __future__ import annotations

import shutil

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from prometheus_client import CONTENT_TYPE_LATEST, ProcessCollector, PlatformCollector

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
        disk_used.set(0)
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
