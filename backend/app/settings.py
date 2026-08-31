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
    def llm_timeout(self) -> float:
        """Seconds to wait for llama.cpp. Default 90 for the 1.5B Q4 rewrite; do not block the worker for minutes."""
        try:
            raw = ((self.yaml.get("ai") or {}).get("llm") or {}).get("timeout_seconds")
            if raw is None:
                return 90.0
            return max(30.0, float(raw))
        except (TypeError, ValueError):
            return 90.0

    @property
    def rca_engine(self) -> str:
        return "forgerca"

    @property
    def rca_window_minutes(self) -> int:
        try:
            return int(((self.yaml.get("ai") or {}).get("rca") or {}).get("window_minutes") or 30)
        except (TypeError, ValueError):
            return 30

    @property
    def rca_max_log_lines(self) -> int:
        try:
            return int(((self.yaml.get("ai") or {}).get("rca") or {}).get("max_log_lines") or 20)
        except (TypeError, ValueError):
            return 20

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

    @property
    def snmp_enabled(self) -> bool:
        snmp = (self.yaml.get("monitoring") or {}).get("snmp") or {}
        return bool(snmp.get("enabled", True))

    @property
    def snmp_exporter_url(self) -> str:
        snmp = (self.yaml.get("monitoring") or {}).get("snmp") or {}
        return str(snmp.get("exporter_url") or "http://127.0.0.1:9116").rstrip("/")

    @property
    def snmp_module(self) -> str:
        snmp = (self.yaml.get("monitoring") or {}).get("snmp") or {}
        return str(snmp.get("module") or "if_mib")

    @property
    def discovery_enabled(self) -> bool:
        return bool((self.yaml.get("discovery") or {}).get("enabled", True))

    @property
    def discovery_mode(self) -> str:
        return str((self.yaml.get("discovery") or {}).get("mode") or "semi-automatic")

    @property
    def discovery_cidrs(self) -> list[str]:
        raw = (self.yaml.get("discovery") or {}).get("cidrs") or []
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",") if part.strip()]
        return [str(item).strip() for item in raw if str(item).strip()]

    @property
    def netbox_enabled(self) -> bool:
        inventory = self.yaml.get("inventory") or {}
        netbox = inventory.get("netbox") or {}
        mode = str(netbox.get("mode") or "").strip().lower()
        if mode == "disabled":
            return False
        yaml_url = str(netbox.get("url") or "").strip()
        if netbox.get("enabled") is False and yaml_url:
            return False
        if netbox.get("enabled") or inventory.get("provider") == "netbox":
            return True
        if os.environ.get("FORGESRE_DEV", "").lower() in {"1", "true", "yes", "on"}:
            return bool(netbox.get("enabled") or inventory.get("provider") == "netbox")
        return True

    @property
    def netbox_url(self) -> str:
        yaml_url = str(((self.yaml.get("inventory") or {}).get("netbox") or {}).get("url") or "").strip().rstrip("/")
        if yaml_url:
            return yaml_url
        return str(os.environ.get("NETBOX_URL") or "http://127.0.0.1:8001").rstrip("/")

    @property
    def netbox_token(self) -> str:
        return os.environ.get("NETBOX_API_TOKEN", "")


    @property
    def cookie_secure(self) -> bool:
        raw = os.environ.get("FORGESRE_COOKIE_SECURE") or (self.yaml.get("system") or {}).get("cookie_secure")
        if raw is None:
            return False
        return str(raw).lower() in {"1", "true", "yes", "on"}


def _truthy_dev() -> bool:
    return os.environ.get("FORGESRE_DEV", "").lower() in {"1", "true", "yes", "on"}


UNSAFE_SECRET_KEYS = {"", "forgesre-dev-secret-change-me", "change-me"}
UNSAFE_WEBHOOK_TOKENS = {"", "forgesre-dev-webhook-token", "CHANGE-ME-RENDER-MONITORING"}


def assert_runtime_secrets() -> None:
    """Refuse to start with shipped defaults unless FORGESRE_DEV=1 (unit tests / explicit lab)."""
    if _truthy_dev():
        return
    key = os.environ.get("SECRET_KEY", "forgesre-dev-secret-change-me")
    token = os.environ.get("ALERTMANAGER_WEBHOOK_TOKEN", "forgesre-dev-webhook-token")
    problems = []
    if key in UNSAFE_SECRET_KEYS:
        problems.append("SECRET_KEY is the shipped default")
    if token in UNSAFE_WEBHOOK_TOKENS:
        problems.append("ALERTMANAGER_WEBHOOK_TOKEN is the shipped default")
    if problems:
        raise SystemExit(
            "ForgeSRE refusing to start: "
            + "; ".join(problems)
            + ". Put real values in secrets/secrets.env or set FORGESRE_DEV=1 for a local lab."
        )


settings = Settings()
