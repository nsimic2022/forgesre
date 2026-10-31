"""Pydantic v2 settings: dual dotenv, typed URLs, frozen singleton, no token defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.settings import ForgeSRESettings, Settings, _dotenv_value, settings

ROOT = Path(__file__).resolve().parents[1]


def test_settings_is_frozen_pydantic_singleton():
    assert isinstance(settings, Settings)
    assert Settings is ForgeSRESettings
    assert settings.model_config.get("frozen") is True
    with pytest.raises(ValueError, match="read-only"):
        settings.database_url = "sqlite:///blocked.db"
    original = dict(settings.yaml)
    settings.yaml = {"discovery": {"cidrs": ["10.9.9.0/24"]}}
    assert settings.discovery_cidrs == ["10.9.9.0/24"]
    settings.yaml = original


def test_database_url_accepts_sqlite_and_rejects_mysql(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/forgesre-settings.db")
    ok = Settings()
    assert ok.database_url.startswith("sqlite:")
    monkeypatch.setenv("DATABASE_URL", "mysql://root@localhost/forgesre")
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings()


def test_prometheus_url_fail_fast_on_bad_scheme(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "ftp://metrics.example")
    with pytest.raises(ValidationError, match="PROMETHEUS"):
        Settings()
    monkeypatch.setenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
    assert Settings().prometheus_url.startswith("http://")


def test_librenms_token_has_no_hardcoded_fallback(monkeypatch, tmp_path):
    src = (ROOT / "backend" / "app" / "settings.py").read_text(encoding="utf-8")
    assert "PROMENI_ME" not in src
    assert "SUPER_TAJNI" not in src
    missing = tmp_path / "no-secrets.env"
    monkeypatch.setenv("FORGESRE_SECRETS_FILE", str(missing))
    monkeypatch.delenv("LIBRENMS_API_TOKEN", raising=False)
    blank = Settings()
    assert blank.librenms_token == ""
    secrets = tmp_path / "secrets.env"
    secrets.write_text("LIBRENMS_API_TOKEN=live-token-from-secrets\n", encoding="utf-8")
    monkeypatch.setenv("FORGESRE_SECRETS_FILE", str(secrets))
    monkeypatch.setenv("LIBRENMS_API_TOKEN", "from-process-env")
    loaded = Settings()
    assert loaded.librenms_token == "live-token-from-secrets"
    assert _dotenv_value(secrets, "LIBRENMS_API_TOKEN") == "live-token-from-secrets"


def test_dotenv_files_merge_secrets_win(monkeypatch, tmp_path):
    public = tmp_path / ".env"
    secrets = tmp_path / "secrets.env"
    public.write_text(
        "PROMETHEUS_URL=http://from-dot-env:9090\nLIBRENMS_URL=http://from-dot-env:8000\n",
        encoding="utf-8",
    )
    secrets.write_text(
        "PROMETHEUS_URL=http://from-secrets:9090\nLIBRENMS_URL=http://from-secrets:8000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.settings._env_files", lambda: (public, secrets))
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("LIBRENMS_URL", raising=False)
    loaded = Settings()
    assert loaded.prometheus_url == "http://from-secrets:9090"
    assert loaded.librenms_env_url == "http://from-secrets:8000"


def test_librenms_and_linux_promql_urls_stay_strings():
    assert isinstance(settings.prometheus_url, str)
    assert isinstance(settings.librenms_url, str)
    assert isinstance(settings.database_url, str)
    assert settings.database_url.startswith("sqlite:")
