from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import is_snmp_asset, sd_snmp_targets, sd_targets
from app.main import app
from app.models import Asset, Playbook, Playrule
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def test_snmp_sd_includes_network_device_excludes_linux():
    db = _db()
    linux = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    assert linux.scrape_address == "" or "9100" in (linux.scrape_address or "") or linux.type == "Linux Server"
    assert is_snmp_asset(linux) is False

    switch = Asset(
        asset_id="sw-lab-01",
        hostname="sw-lab-01",
        ip="10.30.1.1",
        type="Network device",
        environment="Production",
        status="healthy",
        monitoring_profile="network-switch",
        source="manual",
        scrape_address="",
    )
    web = Asset(
        asset_id="web-lab-01",
        hostname="web-lab-01",
        ip="10.30.1.80",
        type="Web/appliance",
        environment="Production",
        status="healthy",
        monitoring_profile="web-standard",
        source="manual",
        scrape_address="",
    )
    no_ip = Asset(
        asset_id="sw-noip",
        hostname="sw-noip",
        ip="",
        type="Network device",
        environment="Production",
        status="healthy",
        monitoring_profile="network-switch",
        source="manual",
        scrape_address="",
    )
    db.add_all([switch, web, no_ip])
    db.commit()

    assert is_snmp_asset(switch) is True
    assert is_snmp_asset(web) is False
    assert is_snmp_asset(no_ip) is False

    snmp = sd_snmp_targets(db)
    ips = [item["targets"][0] for item in snmp]
    assert "10.30.1.1" in ips
    assert "10.30.1.80" not in ips
    assert all(item["labels"]["snmp_module"] == "if_mib" for item in snmp)
    linux_sd = sd_targets(db)
    assert all("10.30.1.1" not in item["targets"][0] for item in linux_sd)
    db.close()


def test_snmp_http_sd_requires_token_and_returns_targets():
    db = _db()
    db.add(
        Asset(
            asset_id="core-sw-01",
            hostname="core-sw-01",
            ip="10.40.1.2",
            type="Network device",
            monitoring_profile="network-switch",
            scrape_address="",
        )
    )
    db.commit()
    client = TestClient(app)
    denied = client.get("/api/v1/sd/snmp")
    assert denied.status_code == 401
    ok = client.get("/api/v1/sd/snmp", headers={"Authorization": "Bearer forgesre-dev-webhook-token"})
    assert ok.status_code == 200
    body = ok.json()
    assert any(item["targets"] == ["10.40.1.2"] for item in body)
    db.close()


def test_snmp_playrule_and_playbook_seeded():
    db = _db()
    book = db.query(Playbook).filter_by(slug="network-unreachable").one()
    assert book.name == "NETWORK-UNREACHABLE"
    rule = db.query(Playrule).filter_by(name="snmp-down").one()
    assert rule.condition.get("alertname") == "SnmpDeviceUnreachable"
    assert rule.playbook_id == book.id
    db.close()


def test_asset_page_and_api_mark_snmp_target():
    db = _db()
    db.add(
        Asset(
            asset_id="edge-sw-01",
            hostname="edge-sw-01",
            ip="10.50.1.1",
            type="Network device",
            monitoring_profile="network-switch",
            scrape_address="",
        )
    )
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}
    page = client.get("/assets/edge-sw-01")
    assert page.status_code == 200
    assert b"snmp_exporter" in page.content
    data = client.get("/api/v1/assets/edge-sw-01").json()
    assert data["snmp"] is True
    demo = client.get("/api/v1/assets/forge-demo-01").json()
    assert demo["snmp"] is False
    doctor = client.get("/api/v1/system/doctor", headers={"Authorization": "Bearer forgesre-dev-webhook-token"}).json()
    assert "snmp" in doctor["components"]
    db.close()


def test_cli_help_documents_snmp_and_assets():
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    overview = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help"], text=True)
    assert "snmp" in overview
    assert "render-monitoring" in overview
    assert "assets" in overview
    assert "jobs" in overview
    assert "demo-reset" in overview
    assert "secrets-check" in overview
    assert "incidents" in overview
    assert "history" in overview
    assert "login" in overview
    assert "HTTP SD" in overview
    snmp = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "snmp"], text=True)
    assert "UDP/161" in snmp
    assert "SNMP_COMMUNITY" in snmp
    render = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "render-monitoring"], text=True)
    assert "install.sh" in render
    assets = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "assets"], text=True)
    assert "Network device" in assets
    backup = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "backup"], text=True)
    assert "--no-secrets" in backup
    version = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "version"], text=True)
    assert "0.7" in version
    assert "Interactive prompt" in overview
    assert "./f" in overview
    shell = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "shell"], text=True)
    assert "journal" in shell
    assert "quit" in shell
    short = subprocess.check_output(["bash", str(root / "f"), "help"], text=True)
    assert "journal" in short
    nested = subprocess.check_output(
        ["bash", str(root / "scripts/forgesre"), "shell"],
        input="help\nquit\n",
        text=True,
    )
    assert "ForgeSRE shell" in nested
    assert "Commands:" in nested
    unknown = subprocess.run(
        ["bash", str(root / "scripts/forgesre"), "j"],
        capture_output=True,
        text=True,
    )
    assert unknown.returncode != 0
    assert "Unknown command: j" in unknown.stdout
    incidents = subprocess.check_output(
        ["bash", str(root / "scripts/forgesre"), "help", "incidents"],
        text=True,
    )
    assert "Red" in incidents
    assert "INC-" in incidents
    login = subprocess.check_output(["bash", str(root / "scripts/forgesre"), "help", "login"], text=True)
    assert "engineer" in login
    assert "SSH" in login
