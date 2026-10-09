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
    assert set(data["levels"]) == {"cpu", "ram", "hdd", "net"}
    assert data["levels"]["net"] in {"ok", "crit"}
    assert data["net"]["level"] == data["levels"]["net"]
    assert data["thresholds"] == {"warn": 80, "crit": 95}
    home = client.get("/")
    assert home.status_code == 200
    assert "data-appliance-card" in home.text
    assert "data-appliance-resources" in home.text
    assert 'data-metric="cpu"' in home.text
    nav = home.text.split('<aside class="nav">', 1)[1].split("</aside>", 1)[0]
    assert "nav-clock" in nav and "data-clock" in nav
    assert ">CPU<" not in nav and "data-metric" not in nav
    assert 'data-nav-cube="forgesre"' in nav and ">ForgeSRE<" in nav
    assert 'data-nav-cube="forgeai"' in nav and ">ForgeAI<" in nav
    assert nav.index(">Administration<") < nav.index('class="nav-status"')
    assert nav.index('class="nav-status"') < nav.index('class="nav-glance"') < nav.index("data-clock")
    glance = nav.split('class="nav-glance"', 1)[1].split('class="who"', 1)[0]
    assert "data-nav-cube" not in glance and "data-clock" in glance
    assert "Coming later" in nav
    css_nav = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    status_css = css_nav.split(".nav-status {", 1)[1].split("}", 1)[0]
    assert "margin: 0.85rem 0" in status_css
    glance_css = css_nav.split(".nav-glance {", 1)[1].split("}", 1)[0]
    assert "border-top:" in glance_css
    assert 'class="nav-logout"' in home.text
    assert ">Logout<" in home.text
    other = client.get("/incidents")
    other_nav = other.text.split('<aside class="nav">', 1)[1].split("</aside>", 1)[0]
    assert "nav-clock" in other_nav and "data-clock" in other_nav
    assert "data-appliance-card" not in other.text
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "bindClock" in js
    assert "bindApplianceResources" in js
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".nav a.nav-clock" in css
    assert ".appliance-clock" in css
    assert "form.nav-logout" in css
    assert "margin-left: auto" in css
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v09-2" in base
    assert "app.js?v=v09-2" in base
    assert "bindInfoTips" in js
    assert "ops-report-actions" in css


def test_nav_cubes_precede_clock_separator():
    """Stacked ForgeSRE / ForgeAI cubes sit above the clock rule, then the clock."""
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    nav = base.split('<aside class="nav">', 1)[1].split("</aside>", 1)[0]
    admin = nav.index(">Administration<")
    status = nav.index('class="nav-status"')
    forgesre = nav.index('data-nav-cube="forgesre"')
    forgeai = nav.index('data-nav-cube="forgeai"')
    glance = nav.index('class="nav-glance"')
    clock = nav.index("nav-clock")
    assert admin < status < forgesre < forgeai < glance < clock
    glance_html = nav.split('class="nav-glance"', 1)[1].split('class="who"', 1)[0]
    assert "data-nav-cube" not in glance_html
    assert "data-clock" in glance_html
    assert 'class="nav-cube idle"' in nav and "Coming later" in nav
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    status_css = css.split(".nav-status {", 1)[1].split("}", 1)[0]
    assert "flex-direction: column" in status_css
    assert "margin: 0.85rem 0" in status_css
    glance_css = css.split(".nav-glance {", 1)[1].split("}", 1)[0]
    assert "border-top: 1px solid var(--shell-line)" in glance_css
    cube = css.split(".nav span.nav-cube {", 1)[1].split("}", 1)[0]
    assert "min-width: 6.72rem" in cube
    assert "height: 1.5rem" in cube
    assert "font-size: 0.82rem" in cube
    clock_css = css.split(".nav a.nav-clock {", 1)[1].split("}", 1)[0]
    assert "font-size: 2.4rem" in clock_css
    assert ".nav a.nav-cube.ok { background: var(--ok)" in css
    assert ".nav a.nav-cube.warn { background: var(--warn)" in css
    assert ".nav span.nav-cube.idle { background: #4e5358" in css
    assert 'href="/health-ui"' in nav and ">Journal<" not in nav


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
