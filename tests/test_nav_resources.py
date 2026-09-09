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


def test_system_resources_requires_login_and_returns_this_appliance():
    anon = TestClient(app)
    assert anon.get("/api/v1/system/resources").status_code == 401
    client = _login()
    body = client.get("/api/v1/system/resources")
    assert body.status_code == 200
    data = body.json()
    assert data["ok"] is True
    assert data["source"] in {"proc", "node_exporter", "mixed"}
    assert data["ram_total_bytes"] > 1_000_000
    assert data["hdd_total_bytes"] > 1_000_000
    assert data["cpu_percent"] is None or 0 <= data["cpu_percent"] <= 100
    assert "asset_id" not in data
    home = client.get("/")
    assert home.status_code == 200
    assert 'data-nav-clock' in home.text
    assert 'data-nav-resources' in home.text
    assert 'data-nav-cpu' in home.text
    assert 'class="nav-logout"' in home.text
    assert ">Logout<" in home.text
    assert "bindNavClock" in (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "bindNavResources" in (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".nav-clock" in css
    assert "font-size: 1.7rem" in css
    assert "form.nav-logout" in css
    assert "margin-left: auto" in css
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=scan-job-2" in base
    assert "app.js?v=disc-1" in base
    assert "bindInfoTips" in (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "ops-report-actions" in css
