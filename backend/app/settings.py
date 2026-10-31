from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import AliasChoices, Field, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict

log = logging.getLogger("forgesre")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _dotenv_value(path: Path, key: str) -> str:
    """Last assignment of KEY= in a dotenv file. Does not print the value."""
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            found = value.strip().strip("'").strip('"')
    return found


def _env_files() -> tuple[Path, Path]:
    """Public `.env` first; `secrets/secrets.env` (or FORGESRE_SECRETS_FILE) wins on clash."""
    root = _repo_root()
    public = root / ".env"
    secrets = Path(os.environ.get("FORGESRE_SECRETS_FILE") or (root / "secrets" / "secrets.env"))
    return public, secrets


def _secrets_path() -> Path:
    return Path(os.environ.get("FORGESRE_SECRETS_FILE") or (_repo_root() / "secrets" / "secrets.env"))


def _require_http_url(value: str, name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} must be an http(s) URL")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an http(s) URL with a host")
    return text


class Settings(BaseSettings):
    """Process env + `.env` + `secrets/secrets.env`. Frozen after load. YAML stays live."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
        populate_by_name=True,
    )

    frontend_dir: Path = Field(default_factory=lambda: _repo_root() / "frontend")
    config_path: Path = Field(
        default_factory=lambda: _repo_root() / "config" / "forgesre.yml",
        validation_alias=AliasChoices("FORGESRE_CONFIG", "config_path"),
    )
    database_url: str = "postgresql+psycopg2://forgesre:forgesre@127.0.0.1:5432/forgesre"
    secret_key: str = "forgesre-dev-secret-change-me"
    admin_email: str = Field(
        default="admin@forgesre.local",
        validation_alias=AliasChoices("FORGESRE_ADMIN_EMAIL", "admin_email"),
    )
    admin_password: str = Field(
        default="admin",
        validation_alias=AliasChoices("FORGESRE_ADMIN_PASSWORD", "admin_password"),
    )
    webhook_token: str = Field(
        default="forgesre-dev-webhook-token",
        validation_alias=AliasChoices("ALERTMANAGER_WEBHOOK_TOKEN", "webhook_token"),
    )
    prometheus_url: str = "http://127.0.0.1:9090"
    loki_url: str = "http://127.0.0.1:3100"
    alertmanager_url: str = "http://127.0.0.1:9093"
    grafana_public_url: str = "http://localhost:3000"
    log_file: str = Field(default="", validation_alias=AliasChoices("FORGESRE_LOG_FILE", "log_file"))
    smtp_username: str = ""
    smtp_password: str = ""
    # Env / dotenv only. YAML inventory.*.url still wins on the public properties.
    librenms_env_url: str = Field(default="", validation_alias=AliasChoices("LIBRENMS_URL", "librenms_env_url"))
    netbox_env_url: str = Field(
        default="http://127.0.0.1:8001",
        validation_alias=AliasChoices("NETBOX_URL", "netbox_env_url"),
    )
    _yaml: dict[str, Any] = PrivateAttr(default_factory=dict)
    # config_path is patched in discovery tests; core endpoints stay locked.
    _MUTABLE_FIELDS = frozenset({"config_path"})

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"yaml", "_yaml"}:
            stored = value if isinstance(value, dict) else {}
            if name == "_yaml" and isinstance(value, dict):
                stored = value
            try:
                private = object.__getattribute__(self, "__pydantic_private__")
            except AttributeError:
                private = None
            if isinstance(private, dict):
                private["_yaml"] = stored
            else:
                object.__setattr__(self, "_yaml", stored)
            return
        fields = type(self).model_fields
        ready = object.__getattribute__(self, "__dict__").get("__settings_ready__", False)
        if name not in fields or name in self._MUTABLE_FIELDS or not ready:
            object.__setattr__(self, name, value)
            return
        raise ValueError(f"{name} is read-only after startup")

    def __delattr__(self, name: str) -> None:
        fields = type(self).model_fields
        if name not in fields or name in self._MUTABLE_FIELDS:
            object.__delattr__(self, name)
            return
        raise ValueError(f"{name} is read-only after startup")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del dotenv_settings
        return (
            init_settings,
            env_settings,
            DotEnvSettingsSource(settings_cls, env_file=_env_files(), env_file_encoding="utf-8"),
            file_secret_settings,
        )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        text = (value or "").strip()
        if text.startswith("sqlite:") or text.startswith(
            ("postgresql://", "postgresql+psycopg2://", "postgresql+asyncpg://")
        ):
            return text
        raise ValueError(
            "DATABASE_URL must start with postgresql://, postgresql+psycopg2://, "
            "postgresql+asyncpg://, or sqlite://"
        )

    @field_validator("prometheus_url", "loki_url", "alertmanager_url", "grafana_public_url")
    @classmethod
    def validate_http_url(cls, value: str, info: Any) -> str:
        name = str(getattr(info, "field_name", None) or "URL").upper()
        return _require_http_url(value, name)

    @field_validator("librenms_env_url")
    @classmethod
    def validate_optional_http_url(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            return ""
        return _require_http_url(text, "LIBRENMS_URL")

    @field_validator("netbox_env_url")
    @classmethod
    def validate_netbox_env_url(cls, value: str) -> str:
        return _require_http_url((value or "").strip() or "http://127.0.0.1:8001", "NETBOX_URL")

    def model_post_init(self, __context: Any) -> None:
        loaded = self._load_yaml()
        private = getattr(self, "__pydantic_private__", None)
        if isinstance(private, dict):
            private["_yaml"] = loaded
        else:
            object.__setattr__(self, "_yaml", loaded)
        object.__setattr__(self, "__settings_ready__", True)

    @property
    def yaml(self) -> dict[str, Any]:
        return self._yaml

    @yaml.setter
    def yaml(self, value: dict[str, Any] | None) -> None:
        object.__setattr__(self, "_yaml", value if isinstance(value, dict) else {})

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
        """Seconds to wait for llama.cpp. Lab 4B default 90; slow 14B CPU may need more."""
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

    def set_discovery_cidrs(self, cidrs: list[str] | str | None) -> list[str]:
        """Write discovery.cidrs to live YAML. Empty OK — empty means no scan.

        In-memory YAML is always updated so this Core process can scan. A
        read-only bind mount used to raise EROFS and 500 the Discovery POST.
        """
        from discovery import normalize_cidrs

        cleaned = normalize_cidrs(cidrs)
        path = self.config_path
        data: dict[str, Any] = {}
        try:
            if path.exists():
                with path.open() as handle:
                    loaded = yaml.safe_load(handle) or {}
                data = loaded if isinstance(loaded, dict) else {}
            elif isinstance(self.yaml, dict):
                data = dict(self.yaml)
        except OSError as exc:
            log.exception("could not read %s for discovery.cidrs", path)
            if isinstance(self.yaml, dict):
                data = dict(self.yaml)
            raise OSError(f"could not read {path}: {exc}") from exc
        discovery = dict(data.get("discovery") or {}) if isinstance(data.get("discovery"), dict) else {}
        discovery["cidrs"] = cleaned
        if "enabled" not in discovery:
            discovery["enabled"] = True
        if "mode" not in discovery:
            discovery["mode"] = "semi-automatic"
        data["discovery"] = discovery
        # Keep unrelated in-memory sections (tests may use a sparse YAML file).
        merged = dict(self.yaml or {})
        merged.update(data)
        merged["discovery"] = discovery
        self.yaml = merged
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w") as handle:
                yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False, allow_unicode=True)
        except OSError as exc:
            log.exception("could not persist discovery.cidrs to %s", path)
            raise
        return cleaned

    def reload_yaml(self) -> None:
        self.yaml = self._load_yaml()

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
    def netbox_auto_sync(self) -> bool:
        """inventory.netbox.auto_sync (default true): read-only sync ~30 s after Core start, then every 6 h."""
        netbox = (self.yaml.get("inventory") or {}).get("netbox") or {}
        value = netbox.get("auto_sync", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    @property
    def netbox_url(self) -> str:
        yaml_url = str(((self.yaml.get("inventory") or {}).get("netbox") or {}).get("url") or "").strip().rstrip("/")
        if yaml_url:
            return _require_http_url(yaml_url, "NETBOX_URL").rstrip("/")
        return _require_http_url(self.netbox_env_url or "http://127.0.0.1:8001", "NETBOX_URL").rstrip("/")

    @property
    def netbox_token(self) -> str:
        """Read NETBOX_API_TOKEN from the bind-mounted secrets file at runtime.

        FORGESRE_SECRETS_FILE (compose: /host-secrets.env → secrets/secrets.env)
        is the source of truth. An empty or stale project .env must not override
        it. Fall back to the process environment only when that file is absent.
        """
        secrets_path = _secrets_path()
        if secrets_path.is_file():
            return _dotenv_value(secrets_path, "NETBOX_API_TOKEN")
        return (os.environ.get("NETBOX_API_TOKEN") or "").strip()

    def _secret(self, key: str) -> str:
        """Same source of truth as netbox_token: bind-mounted secrets file, then process env."""
        secrets_path = _secrets_path()
        if secrets_path.is_file():
            value = _dotenv_value(secrets_path, key)
            if value:
                return value
        return (os.environ.get(key) or "").strip()

    def _zabbix_yaml(self) -> dict[str, Any]:
        raw = (self.yaml.get("inventory") or {}).get("zabbix") or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def zabbix_url(self) -> str:
        """Zabbix frontend base (http://zbx.example/zabbix). YAML inventory.zabbix.url wins over ZABBIX_URL."""
        yaml_url = str(self._zabbix_yaml().get("url") or "").strip()
        return (yaml_url or self._secret("ZABBIX_URL")).rstrip("/")

    @property
    def zabbix_token(self) -> str:
        return self._secret("ZABBIX_API_TOKEN")

    @property
    def zabbix_webhook_token(self) -> str:
        """Bearer for POST /api/v1/webhooks/zabbix. Separate from ALERTMANAGER_WEBHOOK_TOKEN."""
        return self._secret("ZABBIX_WEBHOOK_TOKEN")

    @property
    def zabbix_enabled(self) -> bool:
        """On when URL and API token are both set, unless inventory.zabbix.enabled is false."""
        if self._zabbix_yaml().get("enabled") is False:
            return False
        return bool(self.zabbix_url and self.zabbix_token)

    @property
    def zabbix_auto_sync(self) -> bool:
        """inventory.zabbix.auto_sync (default false): host import with the 6 h discovery loop."""
        value = self._zabbix_yaml().get("auto_sync", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def zabbix_timeout(self) -> float:
        """Seconds per Zabbix API call, clamped to 2–5 so a dead Zabbix never stalls a page."""
        try:
            raw = float(self._zabbix_yaml().get("timeout_seconds") or 4)
        except (TypeError, ValueError):
            raw = 4.0
        return max(2.0, min(5.0, raw))

    def _glpi_yaml(self) -> dict[str, Any]:
        raw = (self.yaml.get("inventory") or {}).get("glpi") or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def glpi_url(self) -> str:
        """GLPI frontend base (https://glpi.example.local/glpi). YAML inventory.glpi.url wins over GLPI_URL."""
        yaml_url = str(self._glpi_yaml().get("url") or "").strip()
        return (yaml_url or self._secret("GLPI_URL")).rstrip("/")

    @property
    def glpi_app_token(self) -> str:
        return self._secret("GLPI_APP_TOKEN")

    @property
    def glpi_user_token(self) -> str:
        return self._secret("GLPI_USER_TOKEN")

    @property
    def glpi_enabled(self) -> bool:
        """On when URL + App-Token + User-Token are set, unless inventory.glpi.enabled is false."""
        if self._glpi_yaml().get("enabled") is False:
            return False
        return bool(self.glpi_url and self.glpi_app_token and self.glpi_user_token)

    @property
    def glpi_auto_sync(self) -> bool:
        """inventory.glpi.auto_sync (default false): import with the 6 h discovery loop."""
        value = self._glpi_yaml().get("auto_sync", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def glpi_timeout(self) -> float:
        """Seconds per GLPI API call, clamped to 2–5 so a dead GLPI never stalls a page."""
        try:
            raw = float(self._glpi_yaml().get("timeout_seconds") or 4)
        except (TypeError, ValueError):
            raw = 4.0
        return max(2.0, min(5.0, raw))

    def _librenms_yaml(self) -> dict[str, Any]:
        raw = (self.yaml.get("inventory") or {}).get("librenms") or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def librenms_url(self) -> str:
        """Bundled LibreNMS UI. YAML inventory.librenms.url wins over LIBRENMS_URL."""
        yaml_url = str(self._librenms_yaml().get("url") or "").strip()
        raw = yaml_url or self.librenms_env_url or self._secret("LIBRENMS_URL") or "http://127.0.0.1:8000"
        return _require_http_url(raw, "LIBRENMS_URL").rstrip("/")

    @property
    def librenms_token(self) -> str:
        """LIBRENMS_API_TOKEN from secrets/secrets.env. No shipped default."""
        return self._secret("LIBRENMS_API_TOKEN")

    @property
    def librenms_enabled(self) -> bool:
        """Bundled LibreNMS is on unless inventory.librenms.enabled is false."""
        if self._librenms_yaml().get("enabled") is False:
            return False
        return True

    @property
    def librenms_timeout(self) -> float:
        try:
            raw = float(self._librenms_yaml().get("timeout_seconds") or 8)
        except (TypeError, ValueError):
            raw = 8.0
        return max(2.0, min(15.0, raw))

    @property
    def cookie_secure(self) -> bool:
        raw = os.environ.get("FORGESRE_COOKIE_SECURE") or (self.yaml.get("system") or {}).get("cookie_secure")
        if raw is None:
            return False
        return str(raw).lower() in {"1", "true", "yes", "on"}


ForgeSRESettings = Settings


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
