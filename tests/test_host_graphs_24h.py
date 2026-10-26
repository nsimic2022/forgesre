"""Host charts: 24 h / 1 m, no Collecting tile, multi-disk, NET %, filled area."""

from __future__ import annotations

from pathlib import Path

from app.asset_metrics import GRAPH_WINDOW_SECONDS, asset_metric_panel
from app.services import range_step

ROOT = Path(__file__).resolve().parents[1]


def _no_spark(_expr: str) -> dict:
    return {"values": []}


def test_default_window_is_a_day_at_one_minute():
    assert GRAPH_WINDOW_SECONDS == 24 * 3600
    assert range_step(24 * 3600) == "1m"
    assert range_step(48 * 3600) == "1m"
    assert range_step(49 * 3600) == "5m"
    panel = asset_metric_panel(
        {"asset_id": "app-24h", "type": "Linux Server"},
        query_fn=lambda expr: {"value": 1.0, "query": expr} if "up{" in expr else {"value": None, "query": expr},
        range_fn=_no_spark,
    )
    assert abs((panel["window"]["end"] - panel["window"]["start"]) - GRAPH_WINDOW_SECONDS) < 2
    assert [tile["key"] for tile in panel["tiles"]] == ["cpu_percent", "memory_percent", "disk_percent", "network"]


def test_ram_title_uses_total_bytes_when_real():
    def query(expr: str) -> dict:
        if "up{" in expr:
            return {"value": 1.0, "query": expr}
        if "node_memory_MemTotal_bytes" in expr:
            return {"value": 16 * 1024 ** 3, "query": expr}
        if "node_memory_MemAvailable" in expr:
            return {"value": 40.0, "query": expr}
        return {"value": None, "query": expr}

    panel = asset_metric_panel(
        {"asset_id": "app-ram", "type": "Linux Server", "monitoring_profile": "linux-standard"},
        query_fn=query,
        range_fn=_no_spark,
    )
    mem = next(tile for tile in panel["tiles"] if tile["key"] == "memory_percent")
    assert mem["name"] == "RAM · 16 GB"


def test_network_becomes_percent_when_link_speed_is_known():
    speed = 125_000_000.0  # 1 Gbit in bytes/s

    def query(expr: str) -> dict:
        if "up{" in expr:
            return {"value": 1.0, "query": expr}
        if "node_network_speed_bytes" in expr:
            return {"value": speed, "query": expr}
        if "node_network_receive" in expr:
            return {"value": speed * 0.10, "query": expr}
        if "node_network_transmit" in expr:
            return {"value": speed * 0.02, "query": expr}
        return {"value": None, "query": expr}

    def ranged(expr: str) -> dict:
        if "node_network_receive" in expr:
            return {"values": [speed * 0.10, speed * 0.20], "times": [1, 2]}
        if "node_network_transmit" in expr:
            return {"values": [speed * 0.02, speed * 0.04], "times": [1, 2]}
        return {"values": []}

    panel = asset_metric_panel(
        {"asset_id": "app-net", "type": "Linux Server", "monitoring_profile": "linux-standard"},
        query_fn=query,
        range_fn=ranged,
    )
    net = next(tile for tile in panel["tiles"] if tile["key"] == "network")
    assert net["kind"] == "percent"
    assert net["unit"] == "%"
    assert net["lines"][0]["series"] == [10.0, 20.0]
    assert net["lines"][1]["series"] == [2.0, 4.0]
    assert "RX 10%" in net["display"] and "TX 2%" in net["display"]


def test_disk_tile_keeps_four_mounts_as_colored_lines():
    def query(expr: str) -> dict:
        if "up{" in expr:
            return {"value": 1.0, "query": expr}
        if "node_filesystem" in expr:
            return {"value": 70.0, "query": expr}
        return {"value": None, "query": expr}

    def ranged(expr: str) -> dict:
        if "max(" in expr or "node_filesystem" not in expr:
            return {"values": []}
        return {
            "series": [
                {"labels": {"mountpoint": "/"}, "values": [40.0, 42.0], "times": [1, 2]},
                {"labels": {"mountpoint": "/var"}, "values": [70.0, 80.0], "times": [1, 2]},
                {"labels": {"mountpoint": "/home"}, "values": [10.0, 11.0], "times": [1, 2]},
                {"labels": {"mountpoint": "/run/lock"}, "values": [90.0, 91.0], "times": [1, 2]},
            ]
        }

    panel = asset_metric_panel(
        {"asset_id": "app-disks", "type": "Linux Server", "monitoring_profile": "linux-standard"},
        query_fn=query,
        range_fn=ranged,
    )
    disk = next(tile for tile in panel["tiles"] if tile["key"] == "disk_percent")
    names = [line["name"] for line in disk["lines"]]
    assert names[0] == "/"
    assert "/var" in names and "/home" in names
    assert "/run/lock" not in names
    assert [line["className"] for line in disk["lines"]] == ["disk-0", "disk-1", "disk-2"]


def test_markup_has_fill_grid_online_and_no_default_escalation_link():
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    form = (ROOT / "frontend" / "templates" / "_asset_form.html").read_text(encoding="utf-8")
    assert "dash-chart-fill" in js and "dash-chart-grid-minor" in js
    assert "paintOnline" in js
    assert 'tile.key !== "up"' in js
    assert 'tile.key !== "network"' not in js.split("function graphPane(", 1)[1].split("\n}\n", 1)[0]
    assert ".dash-chart-fill" in css and ".dash-chart-fill.disk-0" in css
    assert "data-asset-online" not in form
    assert 'href="/escalation">Default escalation</a>' not in form
    assert "ladder-default" not in form
