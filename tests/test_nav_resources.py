"""Left-nav clock, this-appliance resources glance, logout spacing."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.host_resources import appliance_resources, parse_node_exporter, reset_cpu_sample
from app.main import app
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]

NODE_SAMPLE = """
# HELP node_memory_MemAvailable_bytes Memory information field MemAvailable_bytes.
# TYPE node_memory_MemAvailable_bytes gauge
node_memory_MemAvailable_bytes 2.147483648e+09
node_memory_MemTotal_bytes 8.589934592e+09
node_filesystem_size_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/"} 6.442450944e+10
node_filesystem_avail_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/"} 4.294967296e+10
node_filesystem_size_bytes{device="tmpfs",fstype="tmpfs",mountpoint="/run"} 1.073741824e+09
node_filesystem_avail_bytes{device="tmpfs",fstype="tmpfs",mountpoint="/run"} 1.073741824e+09
"""


def _login() -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.close()
    client = TestClient(app)
    posted = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert posted.status_code in {302, 303}
    return client


def test_parse_node_exporter_skips_tmpfs_and_uses_root_disk():
    parsed = parse_node_exporter(NODE_SAMPLE)
    assert parsed["ram_total_bytes"] == 8589934592
    assert parsed["ram_used_bytes"] == 6442450944
    assert parsed["hdd_mount"] == "/"
    assert parsed["hdd_total_bytes"] == 64424509440
    assert parsed["hdd_used_bytes"] == 21474836480


def test_appliance_resources_from_proc_not_inventory(monkeypatch):
    reset_cpu_sample()
    monkeypatch.setattr("app.host_resources.fetch_node_exporter", lambda *a, **k: None)
    first = appliance_resources(probe_node=False)
    assert first["ok"] is True
    assert first["source"] == "proc"
    assert first["ram_total_bytes"] and first["ram_total_bytes"] > 0
    assert first["hdd_total_bytes"] and first["hdd_total_bytes"] > 0
    assert first["cpu_percent"] is None or 0 <= first["cpu_percent"] <= 100
    second = appliance_resources(node_text=NODE_SAMPLE, probe_node=False)
    assert second["source"] == "node_exporter"
    assert second["ram_total_bytes"] == 8589934592
    assert second["hdd_mount"] == "/"
    assert "forge-demo" not in str(second).lower()


def test_resource_levels_thresholds():
    from app.host_resources import resource_level

    assert resource_level(10) == "ok"
    assert resource_level(79.9) == "ok"
    assert resource_level(80) == "warn"
    assert resource_level(94.9) == "warn"
    assert resource_level(95) == "crit"
    assert resource_level(100) == "crit"
    assert resource_level(None) == "crit"
    data = appliance_resources(node_text=NODE_SAMPLE, probe_node=False)
    assert data["ram_percent"] == 75.0
    assert data["levels"]["ram"] == "ok"
    assert data["hdd_percent"] == 33.3
