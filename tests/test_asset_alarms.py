"""Per-asset alarm checklist + ForgeSRE-side incident filter."""


from fastapi.testclient import TestClient

from app.asset_alarms import alert_sample_value, bundled_alert_skip_reason, normalize_alarms
from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.main import app
from app.models import Incident, User
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
        db.add(User(email=email, name="Test", password_hash=hash_password(password), role="engineer"))
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


def test_skip_reason_disabled_and_below_threshold():
    asset = {
        "asset_id": "blachole",
        "type": "Windows Server",
        "alarms": {
            "cpu_percent": {"enabled": False, "threshold": 90},
            "disk_percent": {"enabled": True, "threshold": 92},
            "memory_percent": {"enabled": True, "threshold": 85},
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
    assert "below asset threshold" in bundled_alert_skip_reason(
        asset,
        "WindowsMemoryHigh",
        {"annotations": {"description": "Memory usage is 70% on blachole."}},
    )
    assert bundled_alert_skip_reason(
        asset,
        "WindowsMemoryHigh",
        {"annotations": {"description": "Memory usage is 91% on blachole."}},
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


def test_ingest_honors_asset_memory_threshold_and_disable():
    db = _db()
    host = create_manual_asset(
        db,
        hostname="lnx-quiet-mem",
        ip="10.66.21.84",
        type="Linux Server",
        actor="tester",
        alarms={
            "memory_percent": {"enabled": True, "threshold": 92},
        },
    )
    quiet_win = create_manual_asset(
        db,
        hostname="win-quiet-mem",
        ip="10.66.21.85",
        type="Windows Server",
        actor="tester",
        alarms={
            "memory_percent": {"enabled": False, "threshold": 90},
        },
    )
    noisy = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "NodeMemoryHigh", "severity": "warning", "asset": host.asset_id},
                    "annotations": {"summary": "Memory high", "description": "Memory usage is 70% on lnx-quiet-mem."},
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
                    "labels": {"alertname": "WindowsMemoryHigh", "severity": "warning", "asset": quiet_win.asset_id},
                    "annotations": {"summary": "Windows memory high", "description": "Memory usage is 99% on win-quiet-mem."},
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
                    "labels": {"alertname": "NodeMemoryHigh", "severity": "warning", "asset": host.asset_id},
                    "annotations": {"summary": "Memory high", "description": "Memory usage is 93% on lnx-quiet-mem."},
                }
            ],
        },
    )
    assert created
    assert created[0].playrule_id is not None
    assert db.query(Incident).filter_by(asset_id=host.id).count() == 1
    assert db.query(Incident).filter_by(asset_id=quiet_win.id).count() == 0
    db.close()


def test_normalize_alarms_defaults_windows_cpu_90():
    values = normalize_alarms(None, "windows")
    assert values["cpu_percent"]["enabled"] is True
    assert values["cpu_percent"]["threshold"] == 90
    assert values["disk_percent"]["threshold"] == 90
    assert values["memory_percent"]["enabled"] is True
    assert values["memory_percent"]["threshold"] == 90
    linux = normalize_alarms(None, "linux")
    assert linux["memory_percent"]["threshold"] == 90


