"""Bundled NetBox is a default compose service. No live Docker required."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.api import doctor_payload
from app.netbox import netbox_status
from app.stack import doctor_soft_status, enrich_components, rewrite_host

ROOT = Path(__file__).resolve().parents[1]


def test_compose_netbox_is_default_service():
    data = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name in ("netbox", "netbox-redis", "netbox-db-init"):
        svc = data["services"][name]
        assert "profiles" not in svc, name
        assert svc.get("network_mode") == "host"
    netbox = data["services"]["netbox"]
    assert "netboxcommunity/netbox" in netbox["image"]
    assert netbox["depends_on"]["netbox-db-init"]["condition"] == "service_completed_successfully"
    env = data["services"]["core"]["environment"]
    assert "NETBOX_URL" in env
    assert "8001" in str(env["NETBOX_URL"])
    init = (ROOT / "scripts" / "netbox-db-init.sh").read_text(encoding="utf-8")
    assert "CREATE DATABASE netbox" in init
    assert "forgesre" in init
    assert "DROP DATABASE" not in init.upper()
    launch = (ROOT / "scripts" / "netbox-launch.sh").read_text(encoding="utf-8")
    assert "8001" in launch
    assert "granian" in launch
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "mailpit" not in compose.lower()
    assert "profiles:" not in compose[compose.index("  netbox:"):compose.index("  llm:")]


def test_netbox_status_marks_connect_as_starting():
    result = netbox_status("http://127.0.0.1:9", token="t", timeout=0.2)
    assert result["ok"] is False
    assert result.get("starting") is True
    assert "migration" in result["why"].lower() or "not answering" in result["why"].lower()


def test_doctor_netbox_warn_when_starting(monkeypatch):
    monkeypatch.setattr("app.settings.Settings.netbox_enabled", True)
    monkeypatch.setattr("app.settings.Settings.netbox_url", "http://127.0.0.1:8001")
    monkeypatch.setattr("app.settings.Settings.netbox_token", "token")

    def _status(url, token, timeout=5.0):
        return {"ok": False, "starting": True, "why": "NetBox is not answering yet (first boot runs database migrations)"}

    monkeypatch.setattr("app.netbox.netbox_status", _status)
    monkeypatch.setattr("app.api._http", lambda url, method: {"status": "ok"})
    payload = doctor_payload(force=True)
    item = payload["components"]["netbox"]
    assert item["status"] == "warn"
    assert "migration" in (item.get("why") or "").lower()
    assert "netbox" not in payload["failed"]
    assert doctor_soft_status("warn") is True
    rows = enrich_components(payload["components"], "lab.local:8080")
    row = next(item for item in rows if item["id"] == "netbox")
    assert row["css"] == "warn"
    assert "8001" in row["gui"]


def test_doctor_netbox_ok_when_api_answers(monkeypatch):
    monkeypatch.setattr("app.settings.Settings.netbox_enabled", True)
    monkeypatch.setattr("app.settings.Settings.netbox_url", "http://127.0.0.1:8001")
    monkeypatch.setattr("app.settings.Settings.netbox_token", "token")
    monkeypatch.setattr("app.netbox.netbox_status", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("app.api._http", lambda url, method: {"status": "ok"})
    payload = doctor_payload(force=True)
    assert payload["components"]["netbox"]["status"] == "ok"
    assert "netbox" not in payload["failed"]


def test_doctor_netbox_disabled_in_pytest_config():
    payload = doctor_payload(force=True)
    assert payload["components"]["netbox"]["status"] == "disabled"


def test_settings_yaml_url_overrides_env(monkeypatch):
    from app.settings import Settings

    monkeypatch.setenv("NETBOX_URL", "http://127.0.0.1:8001")
    s = Settings()
    s.yaml = {"inventory": {"netbox": {"enabled": True, "mode": "external", "url": "https://netbox.example.local"}}}
    assert s.netbox_enabled is True
    assert s.netbox_url == "https://netbox.example.local"
    s.yaml = {"inventory": {"netbox": {"enabled": False, "mode": "disabled", "url": ""}}}
    assert s.netbox_enabled is False


def test_rewrite_host_netbox_port():
    assert rewrite_host("http://127.0.0.1:8001", "10.1.2.3") == "http://10.1.2.3:8001"


def test_install_and_update_bundle_netbox_default_on():
    install = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert 'ENABLE_NETBOX="yes"' in install
    assert "--netbox-url" in install
    assert "NETBOX_MODE=" in install
    assert "admin@forgesre.local" in install
    assert "never bundles NetBox" not in install
    update = (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    assert "ensure-netbox-secrets" in update
    assert "up -d snmp-exporter netbox-redis netbox" in update
    assert "first boot can take several minutes" in update.lower() or "migrations" in update.lower()
    assert "yellow" in update.lower()
    help_txt = (ROOT / "scripts" / "forgesre").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8001" in help_txt
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "NETBOX_PORT=8001" in env
    assert "NETBOX_SUPERUSER_EMAIL=admin@forgesre.local" in env
    example = (ROOT / "config" / "forgesre.example.yml").read_text(encoding="utf-8")
    assert "mode: bundled" in example
    assert "http://127.0.0.1:8001" in example
    disc = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    assert "does not bundle NetBox" not in disc
    assert "8001" in disc
    appliance = (ROOT / "scripts" / "appliance_test.py").read_text(encoding="utf-8")
    assert "http.netbox" in appliance
    assert "do not fake green" in appliance.lower() or "migrations" in appliance.lower()


def test_docs_say_bundled_netbox_default_on():
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    install = (ROOT / "docs" / "install-config.md").read_text(encoding="utf-8")
    cont = (ROOT / "docs" / "continuation.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "8001" in handbook and "bundled" in handbook.lower()
    assert "does not bundle NetBox" not in handbook
    assert "8001" in install
    assert "--netbox-url" in install
    assert "./forgesre update" in cont
    assert "NetBox" in cont
    assert "A bundled NetBox or a cloud LLM" not in readme
    completion = (ROOT / "scripts" / "forgesre-completion.bash").read_text(encoding="utf-8")
    assert "netbox-redis" in completion
