"""Per-asset alarm checklist + ForgeSRE-side incident filter."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.asset_alarms import alert_sample_value, bundled_alert_skip_reason, normalize_alarms
from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.main import app
from app.models import Asset, Incident, User
from app.security import hash_password
from app.seed import seed
from app.services import ingest_alertmanager
from app.exporter_detect import bundled_families_from_metrics, detect_exporter


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(email: str, password: str = "testpass") -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    if db.query(User).filter_by(email=email).first() is None:
        db.add(User(email=email, name="Test", password_hash=hash_password(password), role="analyst"))
        db.commit()
    db.close()
    client = TestClient(app)
    login = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert login.status_code in {302, 303}
    return client


def test_families_from_windows_metrics_not_raw_series():
    body = (
        "# HELP windows_cpu_time_total CPU\nwindows_cpu_time_total 1\n"
        "windows_os_physical_memory_free_bytes 2\n"
        "windows_logical_disk_free_bytes 3\n"
        "windows_net_bytes_total 99\n"
    )
    families = bundled_families_from_metrics(body, exporter_up=True)
    assert families == {"up": True, "cpu": True, "memory": True, "disk": True}


def test_detect_windows_reports_cpu_family():
    def fetch(url, timeout):
        del timeout
        if ":9182" in url:
            return 200, "windows_cpu_time_total 1\nwindows_logical_disk_size_bytes 2\n", ""
        return None, "", "timeout 1.0s"

    result = detect_exporter("10.77.9.82", fetcher=fetch)
    assert result.kind == "windows"
    assert result.families["cpu"] is True
    assert result.families["disk"] is True
    assert result.families["memory"] is False
    assert result.families["up"] is True


def test_add_form_has_alarm_checklist_not_on_asset_list_columns():
    client = _login("analyst-alarms@forgesre.local")
    page = client.get("/assets")
    assert page.status_code == 200
    assert "<legend>Alarms</legend>" in page.text
    assert "Bundled alarms for this asset" not in page.text
    assert "asset-form-grid" in page.text
    assert 'name="alarm_disk_threshold"' in page.text
    assert "Auto (detect exporter)" in page.text
    host_at = page.text.find('name="hostname"')
    ip_at = page.text.find('name="ip"')
    alarm_at = page.text.find("alarm-families")
    type_at = page.text.find('name="type"')
    assert 0 < host_at < ip_at < alarm_at < type_at
    table = page.text.split("<table")[1].split("</table>")[0]
    assert "alarm_disk_threshold" not in table
    assert ">Cancel</a>" in page.text
    assert 'href="/assets">Cancel' in page.text
    css = Path("frontend/static/app.css").read_text()
    assert "asset-form-grid" in css
    assert "minmax(0, 1fr) minmax(0, 1fr) minmax(13rem, 1.05fr)" in css


def test_edit_form_cancel_returns_to_asset():
    client = _login("analyst-alarms-cancel@forgesre.local")
    created = client.post(
        "/assets",
        data={
            "hostname": "win-cancel-01",
            "ip": "10.66.21.90",
            "type": "Windows Server",
            "alarms_present": "1",
            "alarm_up_enabled": "1",
            "alarm_cpu_enabled": "1",
            "alarm_cpu_threshold": "90",
            "alarm_memory_enabled": "1",
            "alarm_memory_threshold": "90",
            "alarm_disk_enabled": "1",
            "alarm_disk_threshold": "90",
        },
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    page = client.get("/assets?edit=win-cancel-01")
    assert page.status_code == 200
    assert 'href="/assets/win-cancel-01">Cancel</a>' in page.text
    assert "<legend>Alarms</legend>" in page.text


def test_skip_reason_disabled_and_below_threshold():
    asset = {
        "asset_id": "blachole",
        "type": "Windows Server",
        "alarms": {
            "cpu_percent": {"enabled": False, "threshold": 90},
            "disk_percent": {"enabled": True, "threshold": 92},
        },
    }
    assert "disabled" in bundled_alert_skip_reason(
        asset, "WindowsCPUHigh", {"annotations": {"description": "CPU usage is 91% on blachole."}}
    )
    assert "below asset threshold" in bundled_alert_skip_reason(
        asset,
        "WindowsFilesystemUsageHigh",
        {"annotations": {"description": "Disk usage is 70% on blachole."}},
    )
    assert bundled_alert_skip_reason(
        asset,
        "WindowsFilesystemUsageHigh",
        {"annotations": {"description": "Disk usage is 93% on blachole."}},
    ) == ""
    assert bundled_alert_skip_reason(None, "WindowsCPUHigh", {"value": 99}) == ""
    assert alert_sample_value({"annotations": {"description": "Disk usage is 70.4% on host."}}) == 70.4


def test_ingest_honors_asset_disk_threshold_and_disable():
    db = _db()
    host = create_manual_asset(
        db,
        hostname="win-quiet-01",
        ip="10.66.21.82",
        type="Windows Server",
        actor="tester",
        alarms={
            "cpu_percent": {"enabled": False, "threshold": 90},
            "disk_percent": {"enabled": True, "threshold": 92},
        },
    )
    noisy = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "WindowsFilesystemUsageHigh", "severity": "warning", "asset": host.asset_id},
                    "annotations": {"summary": "Volume usage high", "description": "Disk usage is 70% on win-quiet-01."},
                }
            ],
        },
    )
    assert noisy == []
    disabled = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "WindowsCPUHigh", "severity": "warning", "asset": host.asset_id},
                    "annotations": {"summary": "Windows CPU high", "description": "CPU usage is 95% on win-quiet-01."},
                }
            ],
        },
    )
    assert disabled == []
    created = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "WindowsFilesystemUsageHigh", "severity": "warning", "asset": host.asset_id},
                    "annotations": {"summary": "Volume usage high", "description": "Disk usage is 93% on win-quiet-01."},
                }
            ],
        },
    )
    assert created
    assert db.query(Incident).filter_by(asset_id=host.id).count() == 1
    db.close()


def test_save_edit_persists_custom_disk_threshold():
    client = _login("analyst-alarms2@forgesre.local")
    created = client.post(
        "/assets",
        data={
            "hostname": "win-th-01",
            "ip": "10.66.21.83",
            "type": "Windows Server",
            "alarms_present": "1",
            "alarm_up_enabled": "1",
            "alarm_cpu_enabled": "1",
            "alarm_cpu_threshold": "90",
            "alarm_memory_enabled": "1",
            "alarm_memory_threshold": "90",
            "alarm_disk_enabled": "1",
            "alarm_disk_threshold": "70",
        },
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    body = client.get("/api/v1/assets/win-th-01")
    assert body.status_code == 200
    alarms = body.json()["alarms"]
    assert alarms["disk_percent"]["threshold"] == 70
    assert alarms["disk_percent"]["enabled"] is True
    edited = client.post(
        "/assets/win-th-01/update",
        data={
            "hostname": "win-th-01",
            "ip": "10.66.21.83",
            "type": "Windows Server",
            "scrape_address": "10.66.21.83:9182",
            "alarms_present": "1",
            "alarm_up_enabled": "1",
            "alarm_cpu_enabled": "1",
            "alarm_cpu_threshold": "90",
            "alarm_memory_enabled": "1",
            "alarm_memory_threshold": "90",
            "alarm_disk_threshold": "92",
        },
        follow_redirects=False,
    )
    assert edited.status_code in {302, 303}
    after = client.get("/api/v1/assets/win-th-01").json()["alarms"]
    assert after["disk_percent"]["enabled"] is False
    assert after["disk_percent"]["threshold"] == 92


def test_normalize_alarms_defaults_windows_cpu_90():
    values = normalize_alarms(None, "windows")
    assert values["cpu_percent"]["enabled"] is True
    assert values["cpu_percent"]["threshold"] == 90
    assert values["disk_percent"]["threshold"] == 90


def test_playrules_mentions_saved_asset_alarms():
    from app.asset_alarms import saved_alarm_hostnames

    db = _db()
    host = create_manual_asset(
        db,
        hostname="win-play-alarms",
        ip="10.66.21.91",
        type="Windows Server",
        actor="tester",
        alarms={"disk_percent": {"enabled": True, "threshold": 70}},
    )
    names = saved_alarm_hostnames(db.query(Asset).order_by(Asset.hostname).all())
    assert host.hostname in names
    db.close()
    client = _login("analyst-play-alarms@forgesre.local")
    page = client.get("/playrules")
    assert page.status_code == 200
    assert "win-play-alarms" in page.text
    assert "assets.alarms" in page.text
    assert "default warning" in page.text.lower()
    assert "not a second alerting engine" in page.text
