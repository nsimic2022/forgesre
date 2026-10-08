"""V0.8 Zabbix (read-only): host sync, webhook incidents, backup poll, Health, trends. Mocked JSON-RPC."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import api as api_mod
from app import zabbix as zabbix_mod
from app.asset_metrics import metric_panel_with_zabbix
from app.db import Base, SessionLocal, engine
from app.email_html import render_email
from app.exporter_detect import AUTO_ASSET_TYPE
from app.inventory import assets_matching
from app.main import app
from app.models import Asset, Incident, JournalEntry, Notification, utcnow
from app.seed import seed
from app.services import incident_source_label, ingest_alertmanager
from app.settings import settings
from app.zabbix import (
    READ_METHODS,
    ZabbixError,
    host_trends,
    parse_webhook,
    zabbix_fingerprint,
)
from app.zabbix_sync import group_extras, maybe_poll, reset_poll_state, sync_zabbix

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"
WEBHOOK_TOKEN = "zbx-webhook-test-token"
ZBX_URL = "http://zbx.test/zabbix"
ZBX_TOKEN = "zbx-api-token-test"


class FakeZabbix:
    """Minimal Zabbix JSON-RPC: only the read methods ForgeSRE may call."""

    def __init__(self) -> None:
        self.version = "7.0.3"
        self.hosts: list[dict] = []
        self.problems: list[str] = []
        self.items: list[dict] = []
        self.trends: list[dict] = []
        self.down = False
        self.attempts = 0
        self.calls: list[str] = []
        self.requests: list[tuple[str, dict, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        assert request.url.path.endswith("/api_jsonrpc.php")
        body = json.loads(request.content)
        method = body["method"]
        params = body.get("params") or {}
        self.calls.append(method)
        self.requests.append((method, dict(request.headers), body))
        if method == "apiinfo.version":
            result = self.version
        elif method == "host.get":
            if params.get("countOutput"):
                result = str(len(self.hosts))
            elif params.get("hostids"):
                result = [row for row in self.hosts if row["hostid"] in params["hostids"]]
            else:
                result = self.hosts
        elif method == "problem.get":
            wanted = set(params.get("objectids") or [])
            result = [{"eventid": f"9{t}", "objectid": t} for t in self.problems if t in wanted]
        elif method == "item.get":
            result = self.items
        elif method == "trend.get":
            result = self.trends
        else:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found."}, "id": body["id"]},
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": body["id"]})


class _HttpxShim:
    """app.zabbix's ``httpx`` with Client bound to a MockTransport; everything else is real httpx."""

    def __init__(self, transport: httpx.MockTransport) -> None:
        self._transport = transport

    def __getattr__(self, name: str):
        return getattr(httpx, name)

    def Client(self, **kwargs):  # noqa: N802 - mirrors httpx.Client
        kwargs.pop("transport", None)
        return httpx.Client(transport=self._transport, **kwargs)


def _host(hostid: str, host: str, ip: str, *, groups=(), available: str = "1", name: str = "") -> dict:
    return {
        "hostid": hostid,
        "host": host,
        "name": name or host,
        "status": "0",
        "interfaces": [{"type": "1", "main": "1", "useip": "1", "ip": ip, "dns": "", "available": available}],
        "hostgroups": [{"name": group} for group in groups],
    }


def _reset_caches() -> None:
    zabbix_mod.reset_state()
    reset_poll_state()
    api_mod._doctor_cache.update(at=0.0, payload=None)


def _set_zabbix(monkeypatch, *, url: str = ZBX_URL, token: str = ZBX_TOKEN, webhook: str = WEBHOOK_TOKEN) -> None:
    cls = type(settings)
    monkeypatch.setattr(cls, "zabbix_url", property(lambda self: url))
    monkeypatch.setattr(cls, "zabbix_token", property(lambda self: token))
    monkeypatch.setattr(cls, "zabbix_webhook_token", property(lambda self: webhook))


@pytest.fixture
def zbx(monkeypatch):
    fake = FakeZabbix()
    monkeypatch.setattr(zabbix_mod, "httpx", _HttpxShim(httpx.MockTransport(fake.handler)))
    _set_zabbix(monkeypatch)
    _reset_caches()
    yield fake
    _reset_caches()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    assets = db.query(Asset).filter(Asset.asset_id.like("zt-%")).all()
    ids = [row.id for row in assets]
    incidents = db.query(Incident).filter(Incident.fingerprint.like("zabbix:%")).all()
    if ids:
        incidents += db.query(Incident).filter(Incident.asset_id.in_(ids)).all()
    for inc in {row.id: row for row in incidents}.values():
        db.query(Notification).filter(Notification.incident_id == inc.id).delete(synchronize_session=False)
        db.delete(inc)
    db.flush()
    for asset in assets:
        db.delete(asset)
    db.query(JournalEntry).filter(JournalEntry.module == "zabbix").delete(synchronize_session=False)
    db.commit()
    db.close()
    _reset_caches()


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _hook(client: TestClient, payload, token: str | None = WEBHOOK_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post("/api/v1/webhooks/zabbix", json=payload, headers=headers)


def _problem(trigger_id: str, host: str, *, ip: str = "", value: str = "1", severity: str = "High", name: str = "") -> dict:
    return {
        "event_value": value,
        "event_status": "PROBLEM" if value == "1" else "RESOLVED",
        "event_id": f"8{trigger_id}",
        "event_name": name or "zt High CPU utilization (over 90% for 5m)",
        "event_severity": severity,
        "event_opdata": "Current utilization: 97 %",
        "trigger_id": trigger_id,
        "trigger_name": name or "zt High CPU utilization (over 90% for 5m)",
        "host_host": host,
        "host_name": host,
        "host_ip": ip or "{HOST.IP}",
        "host_id": "{HOST.ID}",
    }


def _journal(db, action: str) -> list[JournalEntry]:
    return db.query(JournalEntry).filter_by(module="zabbix", action=action).order_by(JournalEntry.id).all()


# --- A. host sync -------------------------------------------------------------


def test_sync_creates_auto_asset_with_empty_scrape(zbx):
    zbx.hosts = [_host("50001", "zt-web-01", "10.77.0.11", groups=["Linux servers", "Acme/BG-DC1"], available="1")]
    db = _db()
    try:
        result = sync_zabbix(db, force=True)
        assert result == {"synced": 1, "created": 1, "linked": 0, "skipped": 0}
        asset = db.query(Asset).filter_by(asset_id="zt-web-01").one()
        assert asset.type == AUTO_ASSET_TYPE
        assert asset.scrape_address == ""
        assert asset.monitoring_profile == ""
        assert ":9100" not in (asset.scrape_address + asset.notes)
        assert asset.source == "zabbix"
        assert asset.zabbix_hostid == "50001"
        assert asset.zabbix_agent == "up"
        assert asset.owner == ""
        assert asset.ip == "10.77.0.11"
        assert asset.extras == {"customer": "Acme", "site": "BG-DC1"}
        assert "Imported from Zabbix" in asset.notes

        again = sync_zabbix(db, force=True)
        assert again["created"] == 0 and again["linked"] == 1
        assert db.query(Asset).filter_by(zabbix_hostid="50001").count() == 1
        assert [row.status for row in _journal(db, "sync")] == ["ok", "ok"]
        assert "host.get" in zbx.calls
        assert all(method.endswith(".get") or method == "apiinfo.version" for method in zbx.calls)
    finally:
        db.close()


def test_sync_never_overwrites_contacts_and_dedups_by_ip(zbx):
    db = _db()
    try:
        db.add(
            Asset(
                asset_id="zt-db-01",
                hostname="zt-db-01",
                ip="10.77.0.12",
                type="Linux Server",
                owner="Mila",
                contact_name="Mila Ops",
                owner_email="mila@acme.local",
                owner_phone="+381-11-555-0101",
                notes="operator note — do not touch",
                source="netbox",
                netbox_id="17",
                scrape_address="10.77.0.12:9100",
                extras={"customer": "Operator Co"},
            )
        )
        db.commit()
        zbx.hosts = [_host("50002", "db01.acme", "10.77.0.12", groups=["NewCustomer/Site2"], available="2")]
        result = sync_zabbix(db, force=True)
        assert result["created"] == 0 and result["linked"] == 1
        assert db.query(Asset).filter_by(ip="10.77.0.12").count() == 1
        asset = db.query(Asset).filter_by(asset_id="zt-db-01").one()
        db.refresh(asset)
        assert asset.zabbix_hostid == "50002"
        assert asset.zabbix_agent == "down"
        assert (asset.owner, asset.contact_name, asset.owner_email, asset.owner_phone) == (
            "Mila",
            "Mila Ops",
            "mila@acme.local",
            "+381-11-555-0101",
        )
        assert asset.notes == "operator note — do not touch"
        assert asset.source == "netbox"
        assert asset.type == "Linux Server"
        assert asset.scrape_address == "10.77.0.12:9100"
        assert asset.extras["customer"] == "Operator Co"
        assert asset.extras["site"] == "Site2"
    finally:
        db.close()


def test_sync_dedups_by_hostname_and_never_clones_linked_asset(zbx):
    db = _db()
    try:
        db.add(Asset(asset_id="zt-app-02", hostname="zt-app-02", ip="", source="discovery", owner="team-a"))
        db.commit()
        zbx.hosts = [
            _host("50003", "ZT-APP-02", "10.77.0.13"),
            _host("50004", "zt-app-02-clone", "10.77.0.13"),
        ]
        result = sync_zabbix(db, force=True)
        assert result == {"synced": 1, "created": 0, "linked": 1, "skipped": 1}
        asset = db.query(Asset).filter_by(asset_id="zt-app-02").one()
        assert asset.zabbix_hostid == "50003"
        assert asset.ip == "10.77.0.13"
        assert asset.owner == "team-a"
        assert db.query(Asset).filter(Asset.hostname.like("zt-app-02%")).count() == 1
    finally:
        db.close()


def test_group_extras_skips_generic_groups():
    assert group_extras(["Linux servers", "Templates/OS", "Zabbix servers"]) == {}
    assert group_extras(["Discovered hosts", "Acme"]) == {"customer": "Acme"}
    assert group_extras(["Acme/BG-DC1/rack 14"]) == {"customer": "Acme", "site": "BG-DC1"}


def test_sync_when_zabbix_down_journals_once_and_backs_off(zbx):
    zbx.down = True
    db = _db()
    try:
        first = sync_zabbix(db)
        second = sync_zabbix(db)
        assert "unreachable" in first["error"].lower()
        assert second["error"]
        assert zbx.attempts == 1, "backoff must stop a retry storm"
        assert [row.status for row in _journal(db, "sync")] == ["error"]
        assert zabbix_mod.backoff_remaining() > 0
    finally:
        db.close()


def test_sync_disabled_without_url_or_token(monkeypatch):
    _set_zabbix(monkeypatch, url="", token="")
    _reset_caches()
    db = _db()
    try:
        assert settings.zabbix_enabled is False
        assert sync_zabbix(db, force=True) == {"synced": 0, "skipped": True}
    finally:
        db.close()


# --- read-only client -----------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    ["event.acknowledge", "host.create", "host.update", "host.delete", "problem.acknowledge", "user.login", "script.execute"],
)
def test_client_refuses_any_write(zbx, method):
    with pytest.raises(ZabbixError, match="read-only"):
        zabbix_mod.call(method, {}, url=ZBX_URL, token=ZBX_TOKEN, timeout=2)
    assert zbx.attempts == 0


def test_read_methods_are_get_only():
    assert READ_METHODS == {"apiinfo.version", "host.get", "problem.get", "item.get", "trend.get"}
    for rel in ("zabbix.py", "zabbix_sync.py"):
        source = (ROOT / "backend" / "app" / rel).read_text(encoding="utf-8")
        assert not re.search(r"""["'][a-z]+\.(create|update|delete|acknowledge|massupdate)["']""", source), rel


def test_token_in_bearer_header_on_new_zabbix_and_auth_field_on_old(zbx):
    zabbix_mod.call("host.get", {"countOutput": True}, url=ZBX_URL, token=ZBX_TOKEN, timeout=2)
    method, headers, body = zbx.requests[-1]
    assert method == "host.get"
    assert headers.get("authorization") == f"Bearer {ZBX_TOKEN}"
    assert "auth" not in body

    zabbix_mod.reset_state()
    zbx.version = "6.0.25"
    zabbix_mod.call("host.get", {"countOutput": True}, url=ZBX_URL, token=ZBX_TOKEN, timeout=2)
    method, headers, body = zbx.requests[-1]
    assert body.get("auth") == ZBX_TOKEN
    assert "authorization" not in headers
    version_call = [row for row in zbx.requests if row[0] == "apiinfo.version"][-1]
    assert "auth" not in version_call[2] and "authorization" not in version_call[1]


def test_zabbix_client_imports_without_sqlalchemy():
    import subprocess
    import sys

    script = r"""
import builtins
import sys

real = builtins.__import__


def blocked(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "sqlalchemy" or name.startswith("sqlalchemy."):
        raise ModuleNotFoundError(name)
    return real(name, globals, locals, fromlist, level)


builtins.__import__ = blocked
sys.path.insert(0, "backend")
from app import zabbix

assert zabbix.parse_webhook({"trigger_name": "x", "host_host": "h"})["labels"]["source"] == "zabbix"
assert "sqlalchemy" not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# --- B. webhook ----------------------------------------------------------------


def test_webhook_needs_its_own_token(monkeypatch, zbx):
    client = TestClient(app)
    payload = _problem("70000", "zt-nohost")
    assert _hook(client, payload, token=None).status_code == 401
    assert _hook(client, payload, token="wrong").status_code == 401
    assert _hook(client, payload, token=settings.webhook_token).status_code == 401
    assert client.post("/webhooks/zabbix", json=payload).status_code == 401
    _set_zabbix(monkeypatch, webhook="")
    response = _hook(client, payload)
    assert response.status_code == 503
    assert "ZABBIX_WEBHOOK_TOKEN" in response.json()["detail"]


def test_webhook_rejects_payload_without_trigger_or_host(zbx):
    client = TestClient(app)
    assert _hook(client, {"host_host": "zt-x"}).status_code == 422
    assert _hook(client, {"trigger_name": "zt Something"}).status_code == 422
    assert _hook(client, {"trigger_name": "{TRIGGER.NAME}", "host_host": "zt-x"}).status_code == 422


def test_webhook_opens_incident_with_source_zabbix_and_recovery_resolves(zbx):
    db = _db()
    try:
        db.add(Asset(asset_id="zt-api-01", hostname="zt-api-01", ip="10.77.0.21", owner_email="api@acme.local"))
        db.commit()
    finally:
        db.close()
    client = TestClient(app)
    opened = _hook(client, _problem("70001", "zt-api-01", severity="High"))
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["accepted"] is True and body["source"] == "zabbix" and body["status"] == ["firing"]
    assert len(body["incidents"]) == 1

    again = _hook(client, _problem("70001", "zt-api-01", severity="High"))
    assert again.json()["incidents"] == []

    db = SessionLocal()
    try:
        rows = db.query(Incident).filter_by(fingerprint="zabbix:70001:zt-api-01").all()
        assert len(rows) == 1
        incident = rows[0]
        assert incident.source == "zabbix"
        assert incident.severity == "CRITICAL"
        assert incident.status in {"OPEN", "INVESTIGATING", "ESCALATED"}
        assert incident.asset is not None and incident.asset.asset_id == "zt-api-01"
        assert incident_source_label(incident) == "Zabbix"
    finally:
        db.close()

    recovered = _hook(client, _problem("70001", "zt-api-01", value="0"))
    assert recovered.status_code == 200
    assert recovered.json()["status"] == ["resolved"]
    db = SessionLocal()
    try:
        incident = db.query(Incident).filter_by(fingerprint="zabbix:70001:zt-api-01").one()
        assert incident.status == "RESOLVED"
        assert incident.ended_at is not None
        assert "resolved" in {event.kind for event in incident.events}
    finally:
        db.close()


def test_webhook_matches_asset_by_ip_and_maps_average_to_warning(zbx):
    db = _db()
    try:
        db.add(Asset(asset_id="zt-ip-01", hostname="zt-ip-01", ip="10.77.0.31"))
        db.commit()
    finally:
        db.close()
    client = TestClient(app)
    response = _hook(
        client,
        _problem("70002", "Zabbix visible name only", ip="10.77.0.31", severity="Average", name="zt Disk space is low"),
    )
    assert response.status_code == 200, response.text
    db = SessionLocal()
    try:
        incident = db.query(Incident).filter_by(fingerprint="zabbix:70002:Zabbix visible name only").one()
        assert incident.asset is not None and incident.asset.asset_id == "zt-ip-01"
        assert incident.severity == "WARNING"
        assert incident.source == "zabbix"
    finally:
        db.close()


def test_prometheus_path_unchanged_by_zabbix_matching(zbx):
    db = _db()
    try:
        db.add(Asset(asset_id="zt-prom-01", hostname="zt-prom-01", ip="10.77.0.41"))
        db.commit()
        created = ingest_alertmanager(
            db,
            {"alerts": [{"status": "firing", "labels": {"alertname": "ztPromAlert", "asset": "zt-prom-01", "severity": "warning"}}]},
        )
        assert len(created) == 1
        incident = created[0]
        assert incident.source == "prometheus"
        assert incident.fingerprint == "ztPromAlert:zt-prom-01"
        assert incident.asset.asset_id == "zt-prom-01"
        assert incident_source_label(incident) == "Prometheus"
    finally:
        db.close()


def test_parse_webhook_mapping():
    alert = parse_webhook(
        {
            "EVENT.VALUE": "1",
            "TRIGGER.ID": "123",
            "TRIGGER.NAME": "Zabbix agent is not available",
            "EVENT.SEVERITY": "Disaster",
            "HOST.HOST": "app-01",
            "HOST.IP": "10.0.0.5",
            "HOST.ID": "10584",
        }
    )
    assert alert["status"] == "firing"
    assert alert["labels"]["alertname"] == "Zabbix agent is not available"
    assert alert["labels"]["severity"] == "CRITICAL"
    assert alert["labels"]["ip"] == "10.0.0.5"
    assert alert["labels"]["zabbix_hostid"] == "10584"
    assert alert["labels"]["source"] == "zabbix"
    assert alert["forge"]["fingerprint"] == "zabbix:123:app-01" == zabbix_fingerprint("123", "app-01")

    event_only = parse_webhook({"eventValue": "0", "eventName": "Load high", "hostHost": "app-02", "severity": "Warning"})
    assert event_only["status"] == "resolved"
    assert event_only["labels"]["alertname"] == "Load high"
    assert event_only["labels"]["severity"] == "WARNING"

    assert parse_webhook({"trigger_name": "x", "host_host": "h", "event_severity": "Information"})["labels"]["severity"] == "INFO"
    assert parse_webhook({"trigger_name": "x", "host_host": "h", "event_severity": "4"})["labels"]["severity"] == "CRITICAL"
    unresolved = parse_webhook({"trigger_name": "x", "host_host": "h", "host_ip": "{HOST.IP}"})
    assert "ip" not in unresolved["labels"]
    with pytest.raises(ValueError):
        parse_webhook({"trigger_name": "x"})
    with pytest.raises(ValueError):
        parse_webhook(["not", "an", "object"])


def test_backup_poll_resolves_incident_whose_problem_is_gone(zbx):
    db = _db()
    try:
        db.add(Asset(asset_id="zt-poll-01", hostname="zt-poll-01", ip="10.77.0.51", zabbix_hostid="50051"))
        db.commit()
    finally:
        db.close()
    client = TestClient(app)
    assert _hook(client, _problem("70051", "zt-poll-01")).status_code == 200
    assert _hook(client, _problem("70052", "zt-poll-01", name="zt Memory high")).status_code == 200
    db = SessionLocal()
    try:
        for incident in db.query(Incident).filter(Incident.fingerprint.like("zabbix:7005%")).all():
            incident.started_at = utcnow() - timedelta(minutes=10)
        db.commit()
        zbx.problems = ["70052"]
        out = maybe_poll(db, now=10_000.0)
        assert out["problems"] == 1
        gone = db.query(Incident).filter_by(fingerprint="zabbix:70051:zt-poll-01").one()
        still = db.query(Incident).filter_by(fingerprint="zabbix:70052:zt-poll-01").one()
        db.refresh(gone)
        db.refresh(still)
        assert gone.status == "RESOLVED"
        assert still.status != "RESOLVED"
        assert len(_journal(db, "poll")) == 1

        calls = len(zbx.calls)
        assert maybe_poll(db, now=10_010.0) == {"problems": None, "agents": None}
        assert len(zbx.calls) == calls, "poll is throttled, not every jobs tick"
    finally:
        db.close()


def test_no_problem_poll_without_webhook_token(monkeypatch, zbx):
    _set_zabbix(monkeypatch, webhook="")
    db = _db()
    try:
        out = maybe_poll(db, now=20_000.0)
        assert out["problems"] is None
        assert "problem.get" not in zbx.calls
    finally:
        db.close()


def test_agent_refresh_updates_pill(zbx):
    db = _db()
    try:
        db.add(Asset(asset_id="zt-agent-01", hostname="zt-agent-01", ip="10.77.0.61", zabbix_hostid="50061", zabbix_agent="up"))
        db.commit()
        zbx.hosts = [_host("50061", "zt-agent-01", "10.77.0.61", available="2")]
        out = maybe_poll(db, now=30_000.0)
        assert out["agents"] >= 1
        asset = db.query(Asset).filter_by(asset_id="zt-agent-01").one()
        db.refresh(asset)
        assert asset.zabbix_agent == "down"
    finally:
        db.close()


# --- Health / dashboard when Zabbix is down -------------------------------------


def test_zabbix_down_does_not_break_dashboard_or_health(zbx):
    zbx.down = True
    client = _client()
    assert client.get("/").status_code == 200
    assert client.get("/health-ui").status_code == 200
    api_mod._doctor_cache.update(at=0.0, payload=None)
    doctor = client.get("/api/v1/system/doctor")
    assert doctor.status_code == 200
    component = doctor.json()["components"]["zabbix"]
    assert component["status"] == "warn"
    assert "Unreachable" in component["why"]
    assert client.get("/discovery").status_code == 200
    assert client.get("/incidents").status_code == 200
    assert zbx.attempts == 1, "one probe, then cached + backoff (no retry storm)"
    db = SessionLocal()
    try:
        rows = _journal(db, "health")
        assert [row.status for row in rows] == ["warn"]
    finally:
        db.close()

    zbx.down = False
    zbx.hosts = [_host("50071", "zt-health-01", "10.77.0.71")]
    zabbix_mod.reset_state()
    api_mod._doctor_cache.update(at=0.0, payload=None)
    assert client.get("/api/v1/system/doctor").json()["components"]["zabbix"]["status"] == "ok"
    db = SessionLocal()
    try:
        assert [row.status for row in _journal(db, "health")] == ["warn", "ok"]
    finally:
        db.close()


def test_zabbix_cube_disabled_when_not_configured(monkeypatch):
    _set_zabbix(monkeypatch, url="", token="")
    _reset_caches()
    client = _client()
    component = client.get("/api/v1/system/doctor").json()["components"]["zabbix"]
    assert component["status"] == "disabled"
    page = client.get("/discovery").text
    assert "data-zabbix-block" in page
    assert "Not configured" in page
    assert 'action="/discovery/zabbix-sync"' not in page


def test_no_zabbix_iframe_anywhere():
    for path in list(TEMPLATES.rglob("*.html")) + [ROOT / "frontend" / "static" / "app.js"]:
        assert "<iframe" not in path.read_text(encoding="utf-8").lower(), path.name


# --- Discovery block, Assets filters, pills ---------------------------------------


def test_discovery_block_sync_button_and_last_error(zbx):
    zbx.hosts = [_host("50081", "zt-disc-01", "10.77.0.81")]
    client = _client()
    page = client.get("/discovery").text
    assert "data-zabbix-block" in page
    assert 'action="/discovery/zabbix-sync"' in page
    assert "Sync hosts" in page
    assert "Connected" in page
    assert ZBX_URL in page
    assert "webhook on" in page
    assert "<iframe" not in page.lower()

    response = client.post("/discovery/zabbix-sync", follow_redirects=False)
    assert response.status_code in {302, 303}
    page = client.get("/discovery").text
    assert "Last sync:" in page and "1 new" in page

    zbx.down = True
    zabbix_mod.reset_state()
    client.post("/discovery/zabbix-sync", follow_redirects=False)
    page = client.get("/discovery").text
    assert "data-zabbix-last-error" in page
    assert "unreachable" in page.lower()


def test_assets_source_and_agent_filters(zbx):
    zbx.hosts = [
        _host("50091", "zt-up-01", "10.77.0.91", available="1"),
        _host("50092", "zt-down-01", "10.77.0.92", available="2"),
    ]
    db = _db()
    try:
        db.add(Asset(asset_id="zt-manual-01", hostname="zt-manual-01", ip="10.77.0.93", source="manual"))
        db.commit()
        sync_zabbix(db, force=True)
        rows = db.query(Asset).filter(Asset.asset_id.like("zt-%")).all()
        assert {row.asset_id for row in assets_matching(rows, "", "", "", source="zabbix")} == {"zt-up-01", "zt-down-01"}
        assert {row.asset_id for row in assets_matching(rows, "", "", "", source="zabbix", agent="down")} == {"zt-down-01"}
    finally:
        db.close()
    client = _client()
    page = client.get("/assets?source=zabbix&q=zt-").text
    assert "zt-up-01" in page and "zt-down-01" in page and "zt-manual-01" not in page
    assert 'data-zabbix-filter="down"' in page
    assert 'data-zabbix-agent="up"' in page
    down = client.get("/assets?source=zabbix&agent=down&q=zt-").text
    assert "zt-down-01" in down and "zt-up-01" not in down
    api_rows = client.get("/api/v1/assets?source=zabbix&agent=down").json()
    items = api_rows["items"] if isinstance(api_rows, dict) else api_rows
    found = [row for row in items if row["asset_id"].startswith("zt-")]
    assert [row["asset_id"] for row in found] == ["zt-down-01"]
    assert found[0]["zabbix_hostid"] == "50092" and found[0]["zabbix_agent"] == "down"


def test_incident_source_pills_on_list_detail_and_mail(zbx):
    db = _db()
    try:
        db.add(Asset(asset_id="zt-pill-01", hostname="zt-pill-01", ip="10.77.0.101"))
        db.commit()
        prom = ingest_alertmanager(
            db, {"alerts": [{"status": "firing", "labels": {"alertname": "ztPromPill", "asset": "zt-pill-01"}}]}
        )[0]
        prom_number = prom.number
    finally:
        db.close()
    client = _client()
    zbx_number = _hook(client, _problem("70101", "zt-pill-01")).json()["incidents"][0]

    listing = client.get("/incidents?q=zt").text
    assert 'class="src-pill src-zabbix"' in listing
    assert 'class="src-pill src-prometheus"' in listing

    detail = client.get(f"/incidents/{zbx_number}").text
    assert 'data-source="zabbix"' in detail
    assert "data-incident-graphs" in detail
    assert 'data-source="prometheus"' in client.get(f"/incidents/{prom_number}").text

    html = render_email(kicker="Incident report", heading="x", severity="CRITICAL", status="OPEN", source="Zabbix")
    assert 'class="source-badge"' in html and "Zabbix" in html
    assert 'class="source-badge"' not in render_email(kicker="Incident report", heading="x")
    db = SessionLocal()
    try:
        from app.notifications import build_escalation_body, notification_html

        incident = db.query(Incident).filter_by(fingerprint="zabbix:70101:zt-pill-01").one()
        assert "Source: Zabbix" in build_escalation_body(incident, "L1", "owner")
        assert "source-badge" in notification_html(incident, "L1", "owner")
    finally:
        db.close()


# --- C. trends --------------------------------------------------------------------


def _trend_fixture(fake: FakeZabbix) -> None:
    fake.items = [
        {"itemid": "1", "key_": "system.cpu.util", "value_type": "0", "lastvalue": "42.5", "lastclock": "1700000000"},
        {"itemid": "2", "key_": "vm.memory.utilization", "value_type": "0", "lastvalue": "61", "lastclock": "1700000000"},
        {"itemid": "3", "key_": "vfs.fs.size[/boot,pused]", "value_type": "0", "lastvalue": "20", "lastclock": "1"},
        {"itemid": "4", "key_": "vfs.fs.size[/,pused]", "value_type": "0", "lastvalue": "77", "lastclock": "1700000000"},
        {"itemid": "5", "key_": "system.cpu.util[,idle]", "value_type": "0", "lastvalue": "57", "lastclock": "1"},
    ]
    fake.trends = [
        {"itemid": item, "clock": str(1700000000 + hour * 3600), "value_avg": str(value + hour)}
        for item, value in (("1", 30), ("2", 50), ("4", 70))
        for hour in range(4)
    ]


def test_trend_helper_picks_cpu_mem_root_disk_and_caches(zbx):
    _trend_fixture(zbx)
    result = host_trends(ZBX_URL, ZBX_TOKEN, "50111", timeout=2, now=1700020000)
    assert result["ok"] is True
    tiles = result["tiles"]
    assert set(tiles) == {"cpu_percent", "memory_percent", "disk_percent"}
    assert tiles["cpu_percent"]["value"] == 42.5
    assert tiles["cpu_percent"]["series"] == [30.0, 31.0, 32.0, 33.0]
    assert tiles["disk_percent"]["key"] == "vfs.fs.size[/,pused]"
    trend_call = [body for method, _h, body in zbx.requests if method == "trend.get"][-1]
    assert trend_call["params"]["time_from"] == 1700020000 - 24 * 3600
    assert sorted(trend_call["params"]["itemids"]) == ["1", "2", "4"]
    calls = len(zbx.calls)
    host_trends(ZBX_URL, ZBX_TOKEN, "50111", timeout=2)
    assert len(zbx.calls) == calls, "cached per host"


def test_trend_helper_failure_is_no_samples(zbx):
    zbx.down = True
    result = host_trends(ZBX_URL, ZBX_TOKEN, "50112", timeout=2)
    assert result["ok"] is False and result["tiles"] == {}
    asset = Asset(asset_id="zt-trend-x", hostname="zt-trend-x", zabbix_hostid="50112")
    panel = metric_panel_with_zabbix(asset, {"asset_id": "zt-trend-x", "collecting": False, "tiles": []})
    assert panel["source"] == "zabbix"
    assert panel["collecting"] is False
    assert all(tile.get("value") is None for tile in panel["tiles"])
    assert zbx.attempts == 1


def test_metric_panel_prefers_prometheus_and_falls_back_to_zabbix():
    calls: list[str] = []

    def trends(hostid: str) -> dict:
        calls.append(hostid)
        return {"ok": True, "tiles": {"cpu_percent": {"value": 12.0, "series": [10.0, 12.0], "key": "system.cpu.util"}}}

    scraped = {"asset_id": "zt-a", "collecting": True, "tiles": [{"key": "cpu_percent", "value": 5.0, "series": [1, 2]}]}
    asset = Asset(asset_id="zt-a", hostname="zt-a", zabbix_hostid="1")
    assert metric_panel_with_zabbix(asset, dict(scraped), trends_fn=trends)["source"] == "prometheus"
    assert calls == []

    plain = Asset(asset_id="zt-b", hostname="zt-b", zabbix_hostid="")
    empty = {"asset_id": "zt-b", "collecting": False, "tiles": [{"key": "up", "value": None}]}
    assert metric_panel_with_zabbix(plain, dict(empty), trends_fn=trends)["source"] == "prometheus"
    assert calls == []

    linked = Asset(asset_id="zt-c", hostname="zt-c", zabbix_hostid="777")
    panel = metric_panel_with_zabbix(linked, dict(empty), trends_fn=trends)
    assert calls == ["777"]
    assert panel["source"] == "zabbix" and panel["collecting"] is True
    cpu = next(tile for tile in panel["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["value"] == 12.0


def test_metrics_api_uses_zabbix_trends_for_unscraped_asset(zbx):
    _trend_fixture(zbx)
    db = _db()
    try:
        db.add(Asset(asset_id="zt-trend-01", hostname="zt-trend-01", ip="10.77.0.111", zabbix_hostid="50111", scrape_address=""))
        db.commit()
    finally:
        db.close()
    client = _client()
    response = client.get("/api/v1/assets/zt-trend-01/metrics")
    assert response.status_code == 200
    panel = response.json()
    assert panel["source"] == "zabbix"
    cpu = next(tile for tile in panel["tiles"] if tile["key"] == "cpu_percent")
    assert cpu["value"] == 42.5 and len(cpu["series"]) == 4
    assert "trend.get" in zbx.calls


def test_incident_graph_js_and_zabbix_label():
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function graphPane(" in js
    assert "function bindIncidentGraphs(" in js
    assert "Zabbix trends" in js
    assert "No samples yet" in js


def test_handbook_and_secrets_example_document_zabbix():
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert "## 18. Zabbix (read-only source)" in handbook
    for needle in ("forgesre-ro", "Sync hosts", "ZABBIX_WEBHOOK_TOKEN", "HttpRequest", "{TRIGGER.ID}", "never writes back"):
        assert needle.lower() in handbook.lower(), needle
    assert "](zabbix.md)" in handbook
    example = (ROOT / "secrets" / "secrets.example.env").read_text(encoding="utf-8")
    assert "# --- Zabbix ---" in example
    for key in ("ZABBIX_URL=", "ZABBIX_API_TOKEN=", "ZABBIX_WEBHOOK_TOKEN="):
        assert re.search(rf"^{key}$", example, re.M), key
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "operator-handbook.md#18-zabbix-read-only-source" in readme
    assert "docs/zabbix.md" in readme
    assert "](zabbix.md)" in (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "./forgesre zabbix" in (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")


def test_zabbix_guide_webhook_matches_handler():
    guide = (ROOT / "docs" / "zabbix.md").read_text(encoding="utf-8")
    for needle in (
        "/api/v1/webhooks/zabbix",
        "ZABBIX_URL",
        "ZABBIX_API_TOKEN",
        "ZABBIX_WEBHOOK_TOKEN",
        "./forgesre update",
        "forgesre-ro",
        "User role",
        "Sync hosts",
        "Owner email",
        "source=zabbix",
        "Recovery operations",
    ):
        assert needle in guide, needle
    script = guide[guide.index("var p = JSON.parse(value);") : guide.index("return 'OK';")]
    assert "req.addHeader('Authorization: Bearer ' + p.forge_token);" in script
    assert "key !== 'forge_url' && key !== 'forge_token'" in script
    rows = dict(re.findall(r"^\s*\| `(\w+)` \| `(\{[A-Z.]+\})` \|$", guide, re.M))
    assert rows["trigger_name"] == "{TRIGGER.NAME}"
    assert rows["host_host"] == "{HOST.HOST}"
    sample = {name: f"x-{name}" for name in rows}
    sample.update({"event_value": "1", "event_severity": "High", "trigger_id": "23456", "host_id": "10584"})
    alert = parse_webhook(sample)
    assert alert["status"] == "firing"
    assert alert["labels"]["alertname"] == "x-trigger_name"
    assert alert["labels"]["asset"] == "x-host_host"
    assert alert["labels"]["zabbix_hostid"] == "10584"
    assert alert["labels"]["severity"] == "CRITICAL"
    assert alert["forge"]["fingerprint"] == "zabbix:23456:x-host_host"
    assert parse_webhook({**sample, "event_value": "0"})["status"] == "resolved"
