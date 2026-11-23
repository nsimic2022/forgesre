"""Power and environment: UPS / PDU / sensor asset types, SNMP power job, alert rules, panel."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from app.asset_types import ASSET_TYPE_CHOICES, PINNED_TYPES, snmp_family, snmp_port_for
from app.cli_ops import format_power
from app.db import Base, SessionLocal, engine
from app.inventory import asset_type_abbrev, sd_snmp_host_targets, sd_snmp_power_targets
from app.main import app
from app.models import Asset
from app.power import POWER_JOB, overview, power_metrics_on, sensor_reading, ups_state
from app.seed import seed
from test_incident_tools import _client

ROOT = Path(__file__).resolve().parents[1]
TOKEN = {"Authorization": "Bearer forgesre-dev-webhook-token"}


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _ip() -> str:
    raw = uuid4().int
    return f"10.{200 + raw % 20}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    db = SessionLocal()
    for asset in db.query(Asset).filter(Asset.asset_id.like("pwr-%")).all():
        db.delete(asset)
    db.commit()
    db.close()


def test_power_types_are_listed_pinned_and_on_snmp():
    for name in ("UPS", "PDU", "Environment sensor"):
        assert name in ASSET_TYPE_CHOICES and name in PINNED_TYPES
        assert snmp_family(name) is True
        assert snmp_port_for({"type": name}) == 161
        assert power_metrics_on({"type": name}) is True
    assert asset_type_abbrev("UPS") == "ups" and asset_type_abbrev("Environment sensor") == "env"
    # "backups" must not look like a UPS
    assert snmp_family("Backups server") is False
    assert power_metrics_on({"type": "Switch"}) is False
    assert power_metrics_on({"type": "Switch", "extras": {"snmp_power": "on"}}) is True


def test_power_sd_lists_ups_and_opted_in_switch_only():
    db = _db()
    ups_ip, sw_ip, plain_ip = _ip(), _ip(), _ip()
    db.add_all([
        Asset(asset_id="pwr-ups-1", hostname="pwr-ups-1", ip=ups_ip, type="UPS"),
        Asset(asset_id="pwr-sw-1", hostname="pwr-sw-1", ip=sw_ip, type="Switch", extras={"snmp_power": "1"}),
        Asset(asset_id="pwr-sw-2", hostname="pwr-sw-2", ip=plain_ip, type="Switch"),
    ])
    db.commit()
    rows = {r["labels"]["asset"]: r for r in sd_snmp_power_targets(db)}
    assert rows["pwr-ups-1"]["targets"] == [ups_ip]
    assert rows["pwr-ups-1"]["labels"]["snmp_module"] == "power"
    assert "pwr-sw-1" in rows and "pwr-sw-2" not in rows
    assert "pwr-ups-1" not in {r["labels"]["asset"] for r in sd_snmp_host_targets(db)}
    client = TestClient(app)
    assert client.get("/api/v1/sd/snmp-power").status_code == 401
    body = client.get("/api/v1/sd/snmp-power", headers=TOKEN).json()
    assert any(item["targets"] == [ups_ip] for item in body)
    db.close()


def test_sensor_reading_uses_scale_and_precision():
    assert sensor_reading(235, 9, 1) == 23.5
    assert sensor_reading(4200, 8, 0) == 4.2
    assert sensor_reading(3, 10, 0) == 3000.0
    assert sensor_reading(41, None, None) == 41.0


def test_ups_state_battery_is_critical_and_load_is_warning():
    assert ups_state({"source": "battery"}, temp_warn=32, load_warn=80) == "critical"
    assert ups_state({"source": "normal", "battery": "low"}, temp_warn=32, load_warn=80) == "critical"
    assert ups_state({"source": "normal", "battery": "normal", "load": 85}, temp_warn=32, load_warn=80) == "warning"
    assert ups_state({"source": "normal", "battery": "normal", "load": 30}, temp_warn=32, load_warn=80) == "ok"


def _fake_prom(expr: str) -> dict:
    ups = {"asset": "pwr-ups-1", "job": POWER_JOB}
    sensor = {"asset": "pwr-env-1", "entPhysicalIndex": "7", "entPhysicalName": "Rack A inlet", "job": POWER_JOB}
    table = {
        "upsBatteryStatus": [(ups, 2)],
        "upsSecondsOnBattery": [(ups, 120)],
        "upsEstimatedMinutesRemaining": [(ups, 18)],
        "upsEstimatedChargeRemaining": [(ups, 92)],
        "upsBatteryTemperature": [(ups, 27)],
        "upsOutputSource": [(ups, 5)],
        "upsAlarmsPresent": [(ups, 1)],
        "upsOutputPercentLoad": [(ups, 41)],
        "upsInputVoltage": [(ups, 0)],
        "entPhySensorValue": [(sensor, 345)],
        "entPhySensorType": [(sensor, 8)],
        "entPhySensorScale": [(sensor, 9)],
        "entPhySensorPrecision": [(sensor, 1)],
        "entPhySensorOperStatus": [(sensor, 1)],
        "up": [(ups, 1), (sensor, 1)],
    }
    for name, rows in table.items():
        if f"{name}{{" in expr:
            return {"series": [{"labels": labels, "value": float(v)} for labels, v in rows]}
    return {"series": []}


def test_overview_reads_ups_and_sensor_and_lists_silent_assets():
    db = _db()
    db.add_all([
        Asset(asset_id="pwr-ups-1", hostname="pwr-ups-1", ip=_ip(), type="UPS"),
        Asset(asset_id="pwr-env-1", hostname="pwr-env-1", ip=_ip(), type="Environment sensor"),
        Asset(asset_id="pwr-pdu-1", hostname="pwr-pdu-1", ip=_ip(), type="PDU"),
    ])
    db.commit()
    data = overview(db, vector_fn=_fake_prom)
    assert data["error"] == ""
    ups = next(r for r in data["ups"] if r["asset"] == "pwr-ups-1")
    assert ups["source"] == "battery" and ups["state"] == "critical" and ups["minutes_left"] == 18
    sensor = next(s for s in data["sensors"] if s["asset"] == "pwr-env-1")
    assert sensor["value"] == 34.5 and sensor["unit"] == "°C" and sensor["state"] == "warning"
    assert "pwr-pdu-1" in data["silent"]
    text = format_power(data)
    assert "pwr-ups-1" in text and "battery" in text and "Rack A inlet" in text and "pwr-pdu-1" in text
    db.close()


def test_overview_reports_prometheus_error_without_failing():
    db = _db()
    db.add(Asset(asset_id="pwr-ups-2", hostname="pwr-ups-2", ip=_ip(), type="UPS"))
    db.commit()
    data = overview(db, vector_fn=lambda expr: {"error": "timeout", "series": []})
    assert data["error"] == "timeout" and "pwr-ups-2" in data["silent"]
    assert "Prometheus did not answer" in format_power(data)
    db.close()


def test_power_api_needs_login_and_answers_viewer():
    assert TestClient(app).get("/api/v1/power").status_code in {401, 403}
    body = _client("viewer").get("/api/v1/power").json()
    assert set(body) >= {"ups", "sensors", "silent", "error"}


def test_snmp_power_module_job_and_rules_are_bundled():
    for name in ("snmp.yml", "snmp.yml.tpl"):
        text = (ROOT / "monitoring" / name).read_text(encoding="utf-8")
        if name == "snmp.yml":
            module = yaml.safe_load(text)["modules"]["power"]
            metrics = {m["name"] for m in module["metrics"]}
            assert {"upsOutputSource", "upsEstimatedMinutesRemaining", "entPhySensorValue"} <= metrics
        assert "  power:\n" in text
    for name in ("prometheus.yml", "prometheus.yml.tpl"):
        text = (ROOT / "monitoring" / name).read_text(encoding="utf-8")
        assert "job_name: forgesre-snmp-power" in text and "/api/v1/sd/snmp-power" in text and "module: [power]" in text
    groups = {g["name"]: g for g in yaml.safe_load((ROOT / "monitoring" / "alerts.yml").read_text(encoding="utf-8"))["groups"]}
    rules = {r["alert"]: r for r in groups["forgesre-power"]["rules"]}
    assert set(rules) == {"UpsOnBattery", "UpsBatteryLow", "UpsLoadHigh", "PowerTemperatureHigh", "PowerSnmpUnreachable"}
    assert rules["UpsOnBattery"]["labels"]["severity"] == "critical"
    assert all("for" in r and POWER_JOB in r["expr"] for r in rules.values())


def test_doctor_has_power_row():
    from app.api import _power_check

    assert _power_check()["status"] in {"disabled", "ok", "warn"}
