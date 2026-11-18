"""Settings module: list, change, validate, audit, test connections; CLI helpers."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import AuditLog, User
from app.security import hash_password
from app.seed import seed
from app.settings import settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    """Point Core at a copy of the test YAML so writes never touch the repo file."""
    copy = tmp_path / "forgesre.yml"
    shutil.copy(ROOT / "tests" / "forgesre.test.yml", copy)
    original = settings.config_path
    monkeypatch.setattr(settings, "config_path", copy)
    settings.reload_yaml()
    yield copy
    monkeypatch.setattr(settings, "config_path", original)
    settings.reload_yaml()


def _client(role: str = "admin") -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False)
    return client


def test_admin_lists_settings_and_secrets_never_carry_a_value(tmp_config):
    data = _client().get("/api/v1/settings").json()
    keys = {row["key"]: row for row in data["settings"]}
    assert "ai.enabled" in keys and keys["ai.enabled"]["editable"] is True
    assert keys["ZABBIX_API_TOKEN"]["store"] == "secret"
    for row in data["settings"]:
        if row["store"] == "secret":
            assert "value" not in row and isinstance(row["set"], bool)
    assert set(data["groups"]) >= {"general", "integrations", "notifications", "ai"}


def test_non_admin_cannot_read_or_change_settings(tmp_config):
    engineer = _client("engineer")
    assert engineer.get("/api/v1/settings").status_code == 403
    assert engineer.put("/api/v1/settings/ai.enabled", json={"value": True}).status_code == 403


def test_change_is_validated_written_reloaded_and_audited(tmp_config):
    client = _client()
    saved = client.put("/api/v1/settings/ai.llm.timeout_seconds", json={"value": "120"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["value"] == 120
    assert yaml.safe_load(tmp_config.read_text())["ai"]["llm"]["timeout_seconds"] == 120
    assert settings.llm_timeout == 120
    db = SessionLocal()
    row = db.query(AuditLog).filter_by(action="settings.update", object_id="ai.llm.timeout_seconds").order_by(AuditLog.id.desc()).first()
    assert row is not None and row.data["new"] == 120
    db.close()

    assert client.put("/api/v1/settings/ai.llm.timeout_seconds", json={"value": "5"}).status_code == 400
    assert client.put("/api/v1/settings/discovery.mode", json={"value": "wild"}).status_code == 400
    assert client.put("/api/v1/settings/inventory.zabbix.url", json={"value": "ftp://x"}).status_code == 400
    assert client.put("/api/v1/settings/no.such.key", json={"value": 1}).status_code == 400


def test_secrets_and_installer_values_are_not_written_by_core(tmp_config):
    client = _client()
    secret = client.put("/api/v1/settings/ZABBIX_API_TOKEN", json={"value": "x"})
    assert secret.status_code == 400 and "set-secret" in secret.json()["detail"]
    env = client.put("/api/v1/settings/PROMETHEUS_URL", json={"value": "http://x:9090"})
    assert env.status_code == 400 and ".env" in env.json()["detail"]


def test_discovery_ranges_use_the_discovery_normaliser(tmp_config):
    client = _client()
    saved = client.put("/api/v1/settings/discovery.cidrs", json={"value": "10.10.0.0/24, 10.20.0.0/24"})
    assert saved.status_code == 200, saved.text
    assert settings.discovery_cidrs == ["10.10.0.0/24", "10.20.0.0/24"]


def test_connection_test_runs_one_check(monkeypatch, tmp_config):
    from app import api

    monkeypatch.setattr(api, "_zabbix_check", lambda: {"status": "error", "why": "401 bad token"})
    client = _client("engineer")
    result = client.post("/api/v1/settings/test/zabbix").json()
    assert result == {"component": "zabbix", "status": "error", "why": "401 bad token"}
    assert client.post("/api/v1/settings/test/nothing").status_code == 404
    assert _client("analyst").post("/api/v1/settings/test/zabbix").status_code == 403


def test_cli_secret_list_matches_registry():
    from app.cli_ops import SETTABLE_SECRETS
    from app.system_settings import REGISTRY

    assert set(SETTABLE_SECRETS) == {item.key for item in REGISTRY if item.store == "secret"}


def test_cli_write_secret_keeps_other_lines_and_mode(tmp_path):
    from app.cli_ops import write_secret

    path = tmp_path / "secrets.env"
    path.write_text("# keep me\nPOSTGRES_PASSWORD=pg\nZABBIX_API_TOKEN=old\n")
    write_secret(path, "ZABBIX_API_TOKEN", "new-token\n")
    write_secret(path, "GLPI_APP_TOKEN", "app")
    assert path.read_text() == "# keep me\nPOSTGRES_PASSWORD=pg\nZABBIX_API_TOKEN=new-token\nGLPI_APP_TOKEN=app\n"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(SystemExit):
        write_secret(path, "SECRET_KEY", "x")
    with pytest.raises(SystemExit):
        write_secret(path, "SMTP_PASSWORD", "  ")


def test_cli_settings_output_hides_secrets():
    from app.cli_ops import format_settings

    text = format_settings(
        {
            "groups": {"ai": "AI", "integrations": "Integrations"},
            "settings": [
                {"key": "ai.enabled", "group": "ai", "store": "yaml", "value": None, "effective": False},
                {"key": "ZABBIX_API_TOKEN", "group": "integrations", "store": "secret", "set": True},
                {"key": "GLPI_APP_TOKEN", "group": "integrations", "store": "secret", "set": False},
            ],
        }
    )
    assert "ai.enabled" in text and "False" in text
    assert "ZABBIX_API_TOKEN" in text and "set (secret)" in text
    assert "NOT SET (secret)" in text


def test_every_settings_group_is_known():
    from app.system_settings import GROUPS, REGISTRY

    assert {item.group for item in REGISTRY} <= set(GROUPS)
