"""Bundled NetBox is a default compose service. No live Docker required."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.api import doctor_payload
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.netbox import EMPTY_DEVICES_WHY, FIRST_BOOT_WHY, is_local_netbox_url, netbox_status, sync_cta
from app.security import hash_password
from app.seed import seed
from app.stack import doctor_soft_status, enrich_components, rewrite_host

ROOT = Path(__file__).resolve().parents[1]


def test_compose_netbox_is_default_service():
    data = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name in ("netbox", "netbox-redis", "netbox-db-init"):
        svc = data["services"][name]
        assert "profiles" not in svc, name
        assert svc.get("network_mode") == "host"
    netbox = data["services"]["netbox"]
    assert netbox["image"] == "docker.io/netboxcommunity/netbox:v4.6.9-5.0.2"
    assert "v4.4-3.2.0" not in netbox["image"]
    assert netbox["depends_on"]["netbox-db-init"]["condition"] == "service_completed_successfully"
    env = data["services"]["core"]["environment"]
    assert "NETBOX_URL" in env
    assert "8001" in str(env["NETBOX_URL"])
    assert "NETBOX_API_TOKEN" not in env
    assert "secrets/secrets.env" in data["services"]["core"]["env_file"]
    assert "FORGESRE_SECRETS_FILE" in env
    assert "NETBOX_API_TOKEN" not in netbox["environment"]
    assert "SUPERUSER_API_TOKEN" not in netbox["environment"]
    assert "secrets/secrets.env" in netbox["env_file"]
    assert netbox["environment"].get("SECRET_KEY") == "${NETBOX_SECRET_KEY}"
    assert netbox["environment"].get("API_TOKEN_PEPPER_1") == "${NETBOX_API_TOKEN_PEPPER}"
    assert netbox["environment"].get("NETBOX_API_TOKEN_PEPPER") == "${NETBOX_API_TOKEN_PEPPER}"
    vols = netbox.get("volumes") or []
    assert any("config/netbox/forgesre.py" in str(v) for v in vols)
    assert any("/etc/netbox/config/forgesre.py" in str(v) for v in vols)
    assert any("secrets/secrets.env" in str(v) and "/run/secrets/forgesre-secrets.env" in str(v) for v in vols)
    init = (ROOT / "scripts" / "netbox-db-init.sh").read_text(encoding="utf-8")
    assert "CREATE DATABASE netbox" in init
    assert "forgesre" in init
    assert "DROP DATABASE" not in init.upper()
    launch = (ROOT / "scripts" / "netbox-launch.sh").read_text(encoding="utf-8")
    assert "8001" in launch
    assert "granian" in launch
    assert "NETBOX_API_TOKEN" in launch
    assert "API_TOKEN_PEPPER" in launch
    assert "write_enabled=False" in launch
    assert "users.models import Token" in launch
    assert "TokenVersionChoices.V1" in launch
    assert "plaintext=token_key" in launch
    assert "token=token_key" in launch
    assert "ForgeSRE Core read-sync" in launch
    assert "dcim" in launch and "view" in launch
    assert "v1 token ready for GET /api/dcim/devices/" in launch
    assert "/run/secrets/forgesre-secrets.env" in launch
    assert "DROP DATABASE" not in launch.upper()
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "mailpit" not in compose.lower()
    assert "profiles:" not in compose[compose.index("  netbox:"):compose.index("  llm:")]
    extra = (ROOT / "config" / "netbox" / "forgesre.py").read_text(encoding="utf-8")
    assert "API_TOKEN_PEPPERS" in extra
    assert "API_TOKEN_PEPPER_1" in extra
    assert "NETBOX_API_TOKEN_PEPPER" in extra
    assert "kp7ht" not in extra


def test_netbox_forgesre_extra_sets_peppers_from_env(monkeypatch):
    import importlib.util

    path = ROOT / "config" / "netbox" / "forgesre.py"
    pepper = "x" * 50
    monkeypatch.delenv("API_TOKEN_PEPPER_1", raising=False)
    monkeypatch.setenv("NETBOX_API_TOKEN_PEPPER", pepper)
    spec = importlib.util.spec_from_file_location("forgesre_netbox_extra", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    assert mod.API_TOKEN_PEPPERS == {1: pepper}


def test_netbox_forgesre_extra_skips_empty_pepper(monkeypatch):
    import importlib.util

    path = ROOT / "config" / "netbox" / "forgesre.py"
    monkeypatch.delenv("API_TOKEN_PEPPER_1", raising=False)
    monkeypatch.delenv("NETBOX_API_TOKEN_PEPPER", raising=False)
    spec = importlib.util.spec_from_file_location("forgesre_netbox_extra_empty", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    assert not hasattr(mod, "API_TOKEN_PEPPERS")



def test_netbox_token_prefers_secrets_file(monkeypatch, tmp_path):
    from app.settings import Settings, _dotenv_value

    secrets = tmp_path / "secrets.env"
    file_token = "s" * 40
    env_token = "e" * 40
    secrets.write_text("NETBOX_API_TOKEN=" + file_token + "\n", encoding="utf-8")
    monkeypatch.setenv("FORGESRE_SECRETS_FILE", str(secrets))
    monkeypatch.setenv("NETBOX_API_TOKEN", env_token)
    s = Settings()
    assert s.netbox_token == file_token
    assert _dotenv_value(secrets, "NETBOX_API_TOKEN") == file_token


def test_netbox_status_ok_on_devices_200(monkeypatch):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url)
        if "/api/dcim/devices/" in path:
            return httpx.Response(200, json={"count": 0, "results": []}, request=request)
        if "/login/" in path:
            return httpx.Response(200, text="login", request=request)
        if "/api/status/" in path:
            return httpx.Response(403, json={}, request=request)
        return httpx.Response(404, request=request)

    real = httpx.Client

    def wrapped(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", wrapped)
    result = netbox_status("http://127.0.0.1:8001", "a" * 40)
    assert result["ok"] is True
    assert result.get("light") == "yellow"
    assert result.get("count") == 0
    assert "403" not in (result.get("why") or "")
    assert result.get("why") == EMPTY_DEVICES_WHY


def test_netbox_status_403_ignores_status_endpoint_200(monkeypatch):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url)
        if "/api/dcim/devices/" in path:
            return httpx.Response(403, json={}, request=request)
        if "/api/status/" in path:
            return httpx.Response(200, json={"netbox-version": "4.6.9"}, request=request)
        if "/login/" in path:
            return httpx.Response(200, text="login", request=request)
        return httpx.Response(404, request=request)

    real = httpx.Client

    def wrapped(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", wrapped)
    result = netbox_status("http://127.0.0.1:8001", "a" * 40)
    assert result["ok"] is False
    assert result.get("light") == "grey"
    assert "403" in result["why"]
    assert "No devices yet" not in result["why"]

def test_is_local_netbox_url():
    assert is_local_netbox_url("http://127.0.0.1:8001") is True
    assert is_local_netbox_url("http://localhost:8001") is True
    assert is_local_netbox_url("https://netbox.example.local") is False
    assert is_local_netbox_url("") is False


def test_netbox_status_marks_connect_as_starting():
    result = netbox_status("http://127.0.0.1:9", token="t", timeout=0.2)
    assert result["ok"] is False
    assert result.get("starting") is True
    assert "migration" in result["why"].lower() or "not answering" in result["why"].lower()


def test_sync_cta_ready_when_api_ok(monkeypatch):
    monkeypatch.setattr("app.netbox.netbox_status", lambda *a, **k: {"ok": True})
    result = sync_cta("http://127.0.0.1:8001", "token", True)
    assert result["ready"] is True
    assert result.get("clickable") is True
    assert result.get("light") == "yellow"
    assert result["why"] == EMPTY_DEVICES_WHY
    assert result["starting"] is False


def test_sync_cta_green_when_devices(monkeypatch):
    monkeypatch.setattr(
        "app.netbox.netbox_status",
        lambda *a, **k: {"ok": True, "light": "green", "count": 2, "ui_up": True},
    )
    result = sync_cta("http://127.0.0.1:8001", "token", True)
    assert result["ready"] is True
    assert result["clickable"] is True
    assert result["light"] == "green"
    assert result["why"] == ""


def test_sync_cta_grey_403_stays_clickable_retry(monkeypatch):
    monkeypatch.setattr(
        "app.netbox.netbox_status",
        lambda *a, **k: {
            "ok": False,
            "light": "grey",
            "degraded": True,
            "ui_up": True,
            "why": "NetBox UI up; API HTTP 403 (token missing, not created in NetBox, or not allowed to read devices)",
        },
    )
    result = sync_cta("http://127.0.0.1:8001", "token", True)
    assert result["ready"] is True
    assert result["clickable"] is True
    assert result["light"] == "grey"
    assert "403" in result["why"]
    assert "No devices yet" not in result["why"]


def test_sync_cta_first_boot_keeps_disabled(monkeypatch):
    monkeypatch.setattr(
        "app.netbox.netbox_status",
        lambda *a, **k: {"ok": False, "starting": True, "why": "connect refused"},
    )
    result = sync_cta("http://127.0.0.1:8001", "token", True)
    assert result["ready"] is False
    assert result["starting"] is True
    assert result["why"] == FIRST_BOOT_WHY
    assert result["why"].count(".") == 1


def test_sync_cta_off_when_disabled(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("netbox_status must not run when sync is off")

    monkeypatch.setattr("app.netbox.netbox_status", boom)
    result = sync_cta("http://127.0.0.1:8001", "token", False)
    assert result["ready"] is False
    assert "off in config" in result["why"].lower()


def _discovery_client(email: str = "admin@forgesre.local") -> tuple[TestClient, object]:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    client = TestClient(app)
    posted = client.post("/login", data={"email": email, "password": "testpass"}, follow_redirects=False)
    assert posted.status_code in {302, 303}
    return client, db


def test_discovery_sync_button_enabled_for_admin_when_api_ok(monkeypatch):
    monkeypatch.setattr("app.settings.Settings.netbox_enabled", True)
    monkeypatch.setattr("app.settings.Settings.netbox_url", "http://127.0.0.1:8001")
    monkeypatch.setattr("app.settings.Settings.netbox_token", "token")
    monkeypatch.setattr("app.netbox.netbox_status", lambda *a, **k: {"ok": True})
    client, db = _discovery_client()
    page = client.get("/discovery")
    assert page.status_code == 200
    html = page.text
    assert ">Sync NetBox<" in html
    assert "disabled>Sync NetBox<" not in html
    assert 'class="secondary">Sync NetBox' not in html
    assert 'action="/discovery/netbox-sync"' in html
    assert 'class="pill yellow"' in html
    assert "Yellow" in html
    assert EMPTY_DEVICES_WHY in html
    assert "403" not in html
    assert "Admin only." not in html
    assert "still points Core at an external instance" not in html
    assert "NETBOX_API_TOKEN" in html
    assert "do not need a second token" in html
    db.close()


def test_discovery_footer_omits_external_when_bundled(monkeypatch):
    monkeypatch.setattr("app.settings.Settings.netbox_enabled", True)
    monkeypatch.setattr("app.settings.Settings.netbox_url", "http://127.0.0.1:8001")
    monkeypatch.setattr("app.settings.Settings.netbox_token", "token")
    monkeypatch.setattr("app.netbox.netbox_status", lambda *a, **k: {"ok": True})
    client, db = _discovery_client()
    html = client.get("/discovery").text
    assert "bundled at" in html
    assert "still points Core at an external instance" not in html
    assert "NETBOX_API_TOKEN" in html
    db.close()


def test_discovery_footer_names_external_when_url_is_not_local(monkeypatch):
    monkeypatch.setattr("app.settings.Settings.netbox_enabled", True)
    monkeypatch.setattr("app.settings.Settings.netbox_url", "https://netbox.example.local")
    monkeypatch.setattr("app.settings.Settings.netbox_token", "token")
    monkeypatch.setattr("app.netbox.netbox_status", lambda *a, **k: {"ok": True})
    client, db = _discovery_client()
    html = client.get("/discovery").text
    assert "https://netbox.example.local" in html
    assert "inventory.netbox.url" in html
    assert "--netbox-url" in html
    assert "do not need a second token" not in html
    db.close()


def test_discovery_sync_button_disabled_during_first_boot(monkeypatch):
    monkeypatch.setattr("app.settings.Settings.netbox_enabled", True)
    monkeypatch.setattr("app.settings.Settings.netbox_url", "http://127.0.0.1:8001")
    monkeypatch.setattr("app.settings.Settings.netbox_token", "token")
    monkeypatch.setattr(
        "app.netbox.netbox_status",
        lambda *a, **k: {"ok": False, "starting": True, "why": "connect refused"},
    )
    client, db = _discovery_client()
    page = client.get("/discovery")
    assert page.status_code == 200
    html = page.text
    assert "disabled" in html
    assert FIRST_BOOT_WHY in html
    assert 'action="/discovery/netbox-sync"' not in html
    assert 'class="pill grey"' in html
    db.close()


def test_discovery_sync_is_admin_only_for_engineer(monkeypatch):
    monkeypatch.setattr("app.settings.Settings.netbox_enabled", True)
    monkeypatch.setattr("app.settings.Settings.netbox_url", "http://127.0.0.1:8001")
    monkeypatch.setattr("app.settings.Settings.netbox_token", "token")
    monkeypatch.setattr("app.netbox.netbox_status", lambda *a, **k: {"ok": True})
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    if db.query(User).filter_by(email="engineer@forgesre.local").first() is None:
        db.add(
            User(
                email="engineer@forgesre.local",
                name="engineer",
                password_hash=hash_password("testpass"),
                role="engineer",
            )
        )
        db.commit()
    client = TestClient(app)
    posted = client.post(
        "/login",
        data={"email": "engineer@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert posted.status_code in {302, 303}
    page = client.get("/discovery")
    assert page.status_code == 200
    assert "Admin only." in page.text
    assert 'action="/discovery/netbox-sync"' not in page.text
    blocked = client.post("/discovery/netbox-sync", follow_redirects=False)
    assert blocked.status_code == 403
    db.close()


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
    assert "NETBOX_API_TOKEN" in install
    assert "NETBOX_API_TOKEN_PEPPER" in install
    assert "API_TOKEN_PEPPER_1" in install
    update = (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    assert "ensure-netbox-secrets" in update
    assert "up -d snmp-exporter netbox-redis netbox" in update
    assert "--force-recreate netbox" in update
    assert "netbox_launch_hash" in update
    assert ".netbox-launch.stamp" in update
    assert "--force-recreate core" in update
    assert "first boot can take several minutes" in update.lower() or "migrations" in update.lower()
    assert "yellow" in update.lower()
    secrets = (ROOT / "scripts" / "ensure-netbox-secrets.sh").read_text(encoding="utf-8")
    assert "NETBOX_API_TOKEN" in secrets
    assert "SUPERUSER_API_TOKEN" in secrets
    assert "NETBOX_API_TOKEN_PEPPER" in secrets
    assert "API_TOKEN_PEPPER_1" in secrets
    assert "openssl rand -hex 32" in secrets
    assert "sed -i" in secrets
    assert "DROP DATABASE" not in secrets.upper()
    help_txt = (ROOT / "scripts" / "forgesre").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8001" in help_txt
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "NETBOX_PORT=8001" in env
    assert "NETBOX_SUPERUSER_EMAIL=admin@forgesre.local" in env
    assert "NETBOX_API_TOKEN_PEPPER" in env
    example = (ROOT / "config" / "forgesre.example.yml").read_text(encoding="utf-8")
    assert "mode: bundled" in example
    assert "http://127.0.0.1:8001" in example
    disc = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    assert "does not bundle NetBox" not in disc
    assert "8001" in disc
    assert "not nmap" in disc.lower()
    assert 'action="/discovery/scan"' in disc
    assert 'action="/discovery/netbox-sync"' in disc
    assert 'class="secondary">Sync NetBox' not in disc
    assert "Admin only." in disc
    assert "disabled" in disc
    assert disc.find("Scan now") < disc.find("NetBox sync")
    assert "still points Core at an external instance" not in disc
    assert "NETBOX_API_TOKEN" in disc
    assert "do not need a second token" in disc
    assert "netbox_url_is_local" in disc
    assert "netbox_sync_clickable" in disc
    assert "netbox_sync_light" in disc
    assert "pill {{ netbox_sync_light }}" in disc
    assert "--netbox-url" in disc
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert "/api/status/" in handbook
    assert "Admin only" in handbook
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
    assert "NETBOX_API_TOKEN" in handbook
    assert "403" in handbook
    assert "write_enabled=False" in handbook or "read-only" in handbook.lower()
    assert "plaintext" in handbook.lower()
    assert "second" in handbook.lower()
    assert "TokenVersionChoices" not in handbook
    assert "API_TOKEN_PEPPER" in handbook or "peppers" in handbook.lower()
    assert "/api/status/" in handbook
    assert "Admin only" in handbook
    assert "grey" in handbook.lower()
    assert "yellow" in handbook.lower()
    assert "No devices yet" in handbook or "no devices" in handbook.lower()
    assert "install.sh" in handbook
    assert "8001" in install
    assert "--netbox-url" in install
    assert "NETBOX_API_TOKEN_PEPPER" in install
    assert "./forgesre update" in cont
    assert "NetBox" in cont
    assert "NETBOX_API_TOKEN" in cont
    assert "403" in cont
    assert "yellow" in cont.lower()
    assert "grey" in cont.lower()
    assert "No devices yet" in cont
    assert "API_TOKEN_PEPPER" in cont or "peppers" in cont.lower()
    assert "install.sh" in cont
    cli = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    assert "8001" in cli
    assert "./forgesre update" in cli
    assert "yellow" in cli.lower()
    v07 = (ROOT / "docs" / "v0.7.md").read_text(encoding="utf-8")
    assert "not bundled NetBox" not in v07
    assert "Bundled NetBox is on this V0.7" in v07
    assert "API_TOKEN_PEPPER" in v07 or "peppers" in v07.lower()
    assert "A bundled NetBox or a cloud LLM" not in readme
    completion = (ROOT / "scripts" / "forgesre-completion.bash").read_text(encoding="utf-8")
    assert "netbox-redis" in completion


# Hub-verified 27 Aug 2026. Dead tag v4.4-3.2.0 is not in this list.
_COMPOSE_IMAGE_PINS = {
    "postgres": "postgres:16-alpine",
    "prometheus": "prom/prometheus:v2.54.1",
    "alertmanager": "prom/alertmanager:v0.27.0",
    "snmp-exporter": "prom/snmp-exporter:v0.26.0",
    "loki": "grafana/loki:3.4.2",
    "alloy": "grafana/alloy:v1.7.5",
    "grafana": "grafana/grafana:11.4.0",
    "netbox-redis": "redis:7-alpine",
    "netbox-db-init": "postgres:16-alpine",
    "netbox": "docker.io/netboxcommunity/netbox:v4.6.9-5.0.2",
    "llm": "ghcr.io/ggml-org/llama.cpp:server",
    "mailserver": "ghcr.io/docker-mailserver/docker-mailserver:15.1.0",
    "roundcube": "roundcube/roundcubemail:1.6.11-apache",
}


def test_compose_and_mailbox_image_pins():
    data = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    images = {
        name: svc["image"]
        for name, svc in data["services"].items()
        if "image" in svc
    }
    assert images == _COMPOSE_IMAGE_PINS
    assert "v4.4-3.2.0" not in images.values()
    assert "build" in data["services"]["core"]
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.splitlines()[0] == "FROM python:3.12-slim"
    assert "iputils-ping" in dockerfile
    mailbox = (ROOT / "scripts" / "mailbox.sh").read_text(encoding="utf-8")
    assert 'DMS_IMAGE="ghcr.io/docker-mailserver/docker-mailserver:15.1.0"' in mailbox
