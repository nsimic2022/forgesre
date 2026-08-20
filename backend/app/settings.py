from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class Settings:
    def __init__(self) -> None:
        self.frontend_dir = Path(os.environ.get("FRONTEND_DIR") or (_repo_root() / "frontend"))
        self.config_path = Path(os.environ.get("FORGESRE_CONFIG") or (_repo_root() / "config" / "forgesre.yml"))
        self.database_url = os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg2://forgesre:forgesre@127.0.0.1:5432/forgesre",
        )
        self.secret_key = os.environ.get("SECRET_KEY", "forgesre-dev-secret-change-me")
        self.admin_email = os.environ.get("FORGESRE_ADMIN_EMAIL", "admin@forgesre.local")
        self.admin_password = os.environ.get("FORGESRE_ADMIN_PASSWORD", "admin")
        self.webhook_token = os.environ.get("ALERTMANAGER_WEBHOOK_TOKEN", "forgesre-dev-webhook-token")
        self.prometheus_url = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
        self.loki_url = os.environ.get("LOKI_URL", "http://127.0.0.1:3100")
        self.alertmanager_url = os.environ.get("ALERTMANAGER_URL", "http://127.0.0.1:9093")
        self.grafana_public_url = os.environ.get("GRAFANA_PUBLIC_URL", "http://localhost:3000")
        self.log_file = os.environ.get("FORGESRE_LOG_FILE", "")
        self.yaml = self._load_yaml()

    def _load_yaml(self) -> dict[str, Any]:
        if self.config_path.exists():
            with self.config_path.open() as handle:
                return yaml.safe_load(handle) or {}
        example = _repo_root() / "config" / "forgesre.example.yml"
        if example.exists():
            with example.open() as handle:
                return yaml.safe_load(handle) or {}
        return {}

    @property
    def timezone(self) -> str:
        return (
            os.environ.get("FORGESRE_TIMEZONE")
            or self.yaml.get("system", {}).get("timezone")
            or "Europe/Belgrade"
        )

    @property
    def ai_enabled(self) -> bool:
        return bool(self.yaml.get("ai", {}).get("enabled"))

    @property
    def llm_url(self) -> str | None:
        ai = self.yaml.get("ai") or {}
        llm = ai.get("llm") or {}
        if not ai.get("enabled"):
            return None
        if llm.get("mode") == "disabled":
            return None
        url = llm.get("url") or os.environ.get("LLM_URL")
        return str(url) if url else None

    @property
    def llm_model(self) -> str:
        return str((self.yaml.get("ai") or {}).get("llm", {}).get("model") or "local")

    @property
    def email_enabled(self) -> bool:
        return bool((self.yaml.get("notifications") or {}).get("email", {}).get("enabled"))

    @property
    def smtp_host(self) -> str:
        return str((self.yaml.get("notifications") or {}).get("email", {}).get("host") or "")

    @property
    def smtp_port(self) -> int:
        return int((self.yaml.get("notifications") or {}).get("email", {}).get("port") or 587)

    @property
    def smtp_from(self) -> str:
        return str((self.yaml.get("notifications") or {}).get("email", {}).get("from") or "forgesre@local")

    @property
    def smtp_tls(self) -> bool:
        return bool((self.yaml.get("notifications") or {}).get("email", {}).get("tls", True))

    @property
    def smtp_username(self) -> str:
        return os.environ.get("SMTP_USERNAME", "")

    @property
    def smtp_password(self) -> str:
        return os.environ.get("SMTP_PASSWORD", "")

    @property
    def grafana_enabled(self) -> bool:
        return bool((self.yaml.get("grafana") or {}).get("enabled", True))

    @property
    def loki_enabled(self) -> bool:
        return bool((self.yaml.get("logging") or {}).get("loki", {}).get("enabled", True))


settings = Settings()
