"""Bundled LibreNMS is Discovery SNMP inventory only. snmp_exporter still scrapes."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.librenms import (
    _host_path,
    add_payload,
    api_root,
    discover_device,
    reset_state,
    token_presence,
)
from app.main import app
from app.models import Asset
from app.seed import seed
from app.snmp_onboard import (
    LIBRENMS_HOSTNAME_KEY,
    ONBOARD_MODULE,
    identify_from_librenms,
    onboard_config,
    recommend_from_librenms,
)
from app.asset_extras import merge_extras, normalize_extras

ROOT = Path(__file__).resolve().parents[1]
SECRET = "s3cr3t-comm-librenms"


def _client() -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.close()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _inventory() -> dict:
    return {
        "ok": True,
        "hostname": "10.8.8.8",
        "added": True,
        "disabled_polling": True,
        "device": {
            "hostname": "10.8.8.8",
            "sysDescr": "Cisco IOS Software",
            "sysName": "core-sw",
            "os": "ios",
            "hardware": "C9300",
            "vendor": "Cisco",
            "version": "17.9",
        },
        "ports": [
            {"ifIndex": 1, "ifName": "Gi1/0/1", "ifOperStatus": "up"},
            {"ifIndex": 2, "ifName": "Gi1/0/2", "ifOperStatus": "down"},
        ],
        "processors": [{"sensor_id": 1}],
        "mempools": [{"mempool_id": 1}],
        "storage": [{"storage_id": 1}],
        "temperatures": [{"sensor_id": 9}],
    }


def test_api_root_and_token_presence():
    assert api_root("http://127.0.0.1:8000") == "http://127.0.0.1:8000/api/v0"
    assert api_root("http://127.0.0.1:8000/api/v0/") == "http://127.0.0.1:8000/api/v0"
    assert token_presence("") == "no"
    assert token_presence("abc") == "yes"
    assert _host_path("10.9.9.9") == "10.9.9.9"
    assert _host_path("core-sw.lab") == "core-sw.lab"


def test_add_payload_v2c_and_v3():
    v2 = add_payload("10.1.1.1", version="v2c", community=SECRET)
    assert v2["hostname"] == "10.1.1.1"
    assert v2["snmpver"] == "v2c" and v2["version"] == "v2c"
    assert v2["community"] == SECRET
    assert v2["force_add"] is False
    v3 = add_payload(
        "10.1.1.2",
        version="v3",
        v3_user="monitor",
        v3_level="authPriv",
        v3_auth_proto="SHA",
        v3_auth_pass="auth-secret",
        v3_priv_proto="AES",
        v3_priv_pass="priv-secret",
    )
    assert v3["snmpver"] == "v3"
    assert v3["authname"] == "monitor"
    assert v3["authpass"] == "auth-secret"
    assert v3["cryptopass"] == "priv-secret"
    assert "community" not in v3


def test_identify_and_recommend_from_librenms_inventory():
    ident = identify_from_librenms(_inventory())
    assert ident["ok"] and ident["vendor"] == "Cisco" and ident["os"] == "ios"
    assert ident["hardware"] == "C9300" and ident["hostname"] == "10.8.8.8"
    assert ident["disabled_polling"] is True
    rows = {row["key"]: row for row in ident["rows"]}
    assert rows["ports"]["value"] == "2"
    rec = recommend_from_librenms(_inventory())
    by = {row["key"]: row for row in rec}
    assert by["ifNumber"]["ok"] and by["ifNumber"]["applyable"]
    assert by["ifNumber"]["value"] == "2"
    assert by["hrProcessorLoad"]["ok"]
    assert by["memAvailReal"]["ok"]
    assert by["hrStorageUsed"]["ok"]
    assert by["entPhySensorValue"]["ok"]
    assert by["ifNumber"]["profile"] == "switch"


def test_discover_device_disables_polling(monkeypatch):
    reset_state()
    calls: list[tuple[str, str]] = []

    def fake_request(method, path, json_body=None):
        calls.append((method, path))
        if method == "POST" and path == "/devices":
            return {"status": "ok", "devices": [{"hostname": "10.9.9.9", "sysDescr": "lab"}]}
        if path.startswith("/devices/10.9.9.9/ports"):
            return {"ports": [{"ifIndex": 1, "ifOperStatus": "up"}]}
        if "/health/" in path:
            return {"graphs": []}
        if method == "PATCH":
            assert json_body == {"field": ["disabled"], "data": ["1"]}
            return {"status": "ok"}
        return {"status": "ok"}

    monkeypatch.setattr("app.librenms.configured", lambda: True)
    monkeypatch.setattr("app.librenms.backoff_remaining", lambda: 0.0)
    monkeypatch.setattr("app.librenms._request", fake_request)
    out = discover_device("10.9.9.9", community=SECRET, disable_polling=True)
    assert out["hostname"] == "10.9.9.9"
    assert out["disabled_polling"] is True
    assert out["ports"]
    assert ("PATCH", "/devices/10.9.9.9") in calls
    assert SECRET not in str(calls)


def test_identify_post_uses_librenms_not_ber(monkeypatch):
    seen: dict[str, str] = {}

    def fake_discover(ip, **kwargs):
        seen["community"] = kwargs.get("community") or ""
        seen["ip"] = ip
        assert kwargs.get("disable_polling") is True
        return _inventory()

    monkeypatch.setattr("app.librenms.discover_device", fake_discover)
    client = _client()
    response = client.post(
        "/discovery/snmp",
        data={"action": "identify", "ip": "10.8.8.8", "port": "161", "version": "v2c", "community": SECRET, "timeout": "0.8"},
    )
    assert response.status_code == 200
    assert seen["community"] == SECRET
    assert "Cisco" in response.text
    assert "ifNumber" in response.text
    assert "LibreNMS" in response.text
    assert SECRET not in response.text
    assert "recommend" in response.text


def test_onboard_config_has_no_secrets():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.add(
        Asset(
            asset_id="lnms-cfg",
            hostname="lnms-cfg",
            ip="10.8.8.21",
            type="Switch",
            extras={"snmp_onboard_oids": "1.3.6.1.2.1.2.1.0", "snmp_community": SECRET},
        )
    )
    db.commit()
    payload = onboard_config(db.query(Asset).all())
    db.close()
    assert payload["module"] == ONBOARD_MODULE
    assert "1.3.6.1.2.1.2.1.0" in payload["oids"]
    assert SECRET not in payload["block"]
    client = _client()
    assert client.get("/api/v1/sd/snmp-onboard-config").status_code == 401
    body = client.get(
        "/api/v1/sd/snmp-onboard-config",
        headers={"Authorization": "Bearer forgesre-dev-webhook-token"},
    ).json()
    assert body["module"] == ONBOARD_MODULE
    blob = yaml.safe_dump(body)
    assert SECRET not in blob
    assert "snmp_community" not in blob


def test_librenms_hostname_survives_asset_save():
    stored = normalize_extras(
        {"site": "DC1", "snmp_onboard_oids": "1.3.6.1.2.1.2.1.0", LIBRENMS_HOSTNAME_KEY: "10.8.8.8"}
    )
    assert stored[LIBRENMS_HOSTNAME_KEY] == "10.8.8.8"
    merged = merge_extras(stored, {"site": "DC2"}, None)
    assert merged[LIBRENMS_HOSTNAME_KEY] == "10.8.8.8"
    assert merged["snmp_onboard_oids"] == "1.3.6.1.2.1.2.1.0"


def test_compose_librenms_is_default_service():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(compose)
    for name in ("librenms-db", "librenms-redis", "librenms", "librenms-dispatcher"):
        svc = data["services"][name]
        assert "profiles" not in svc, name
        assert svc.get("network_mode") == "host"
    assert data["services"]["librenms"]["image"] == "librenms/librenms:26.10.2"
    assert data["services"]["librenms-dispatcher"]["image"] == "librenms/librenms:26.10.2"
    db = data["services"]["librenms-db"]
    assert db["image"] == "mariadb:11.4.13"
    assert db["command"][0].startswith("--")
    assert "mysqld" not in db["command"]
    assert "secrets/secrets.env" in db["env_file"]
    assert "MYSQL_PASSWORD" not in (db.get("environment") or {})
    assert "LIBRENMS_DB_PASSWORD" not in str(db.get("environment") or {})
    assert db["healthcheck"]["test"] == ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
    assert "SIDECAR_DISPATCHER" in data["services"]["librenms-dispatcher"]["environment"]
    assert "SIDECAR_SYSLOGNG" not in compose
    assert "SIDECAR_SNMPTRAPD" not in compose
    assert "LIBRENMS_URL" in data["services"]["core"]["environment"]
    generated = [v for v in data["services"]["core"]["volumes"] if "generated" in str(v)]
    assert generated and ":ro" not in str(generated[0])
    assert "/host-generated:ro" not in compose
    assert "3307" in compose and "6380" in compose
    env = data["services"]["core"]["environment"]
    assert "LIBRENMS_API_TOKEN" not in env


def test_update_and_scripts_bundle_librenms():
    update = (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    assert "ensure-librenms-secrets" in update
    assert "librenms-upsert-token" in update
    assert "up -d snmp-exporter netbox-redis netbox librenms-db librenms-redis librenms librenms-dispatcher" in update
    assert "librenms-db-1" in update
    assert "mariadb:11.4.13" in update
    secrets = (ROOT / "scripts" / "ensure-librenms-secrets.sh").read_text(encoding="utf-8")
    assert "LIBRENMS_DB_PASSWORD" in secrets
    assert "MYSQL_ROOT_PASSWORD" in secrets
    assert "MARIADB_ROOT_PASSWORD" in secrets
    assert "LIBRENMS_APP_KEY" in secrets
    assert "openssl rand" in secrets
    assert "DROP DATABASE" not in secrets.upper()
    completion = (ROOT / "scripts" / "forgesre-completion.bash").read_text(encoding="utf-8")
    assert "librenms-dispatcher" in completion
    example = (ROOT / "config" / "forgesre.example.yml").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8000" in example
    secrets_ex = (ROOT / "secrets" / "secrets.example.env").read_text(encoding="utf-8")
    assert "LIBRENMS_API_TOKEN" in secrets_ex
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "LIBRENMS_URL=http://127.0.0.1:8000" in env
    restart = (ROOT / "scripts" / "restart.sh").read_text(encoding="utf-8")
    assert "librenms-db" in restart and "librenms-dispatcher" in restart
    render = (ROOT / "scripts" / "render-monitoring.sh").read_text(encoding="utf-8")
    assert "render_snmp_onboard" in render


def test_render_snmp_onboard_keeps_block_when_core_is_down(tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import render_snmp_onboard as r

    block = "  forgesre_onboard:\n    timeout: 5s\n"
    src = (
        "modules:\n  if_mib: {}\n"
        + r.BEGIN
        + "\n"
        + block
        + r.END
        + "\n"
    )
    target = tmp_path / "snmp.yml"
    target.write_text(src, encoding="utf-8")
    monkeypatch.setattr(r, "fetch_config", lambda *a, **k: None)
    status = r.apply(target, "8080", "tok", previous_block=block)
    assert "kept existing" in status
    assert "forgesre_onboard:" in target.read_text()
    monkeypatch.setattr(
        r,
        "fetch_config",
        lambda *a, **k: {"oids": ["1.3.6.1.2.1.2.1.0"], "block": "  forgesre_onboard:\n    get:\n      - 1.3.6.1.2.1.2.1.0\n"},
    )
    status = r.apply(target, "8080", "tok")
    assert "1 OID" in status
    assert "1.3.6.1.2.1.2.1.0" in target.read_text()


def test_doctor_librenms_disabled_in_pytest():
    from app.api import doctor_payload
    from app.stack import component_label, enrich_components

    reset_state()
    payload = doctor_payload(force=True)
    row = payload["components"]["librenms"]
    assert row["status"] == "disabled"
    assert "librenms" not in payload["failed"]
    assert component_label("librenms") == "LibreNMS"
    rows = enrich_components(payload["components"], "lab.local:8080")
    item = next(item for item in rows if item["id"] == "librenms")
    assert item["label"] == "LibreNMS"
    assert item["gui"].startswith("http://lab.local:")
    assert item["local_only"] is False
