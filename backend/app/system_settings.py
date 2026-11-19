"""Settings page backend: what an Admin can see and change, where each value lives.

Three kinds of values:

- ``yaml``: live keys in ``config/forgesre.yml``. Editable from the UI, API and
  ``./forgesre settings set``; written back to the file and reloaded at once.
- ``secret``: tokens and passwords in ``secrets/secrets.env``. Core mounts that file
  read-only, so the UI only shows whether a secret is set. ``./forgesre settings
  set-secret`` on the host writes it and restarts Core.
- ``env``: addresses and ports fixed by the installer in ``.env`` / docker-compose. Shown
  read-only; change them with ``./install.sh`` or by editing ``.env`` and restarting.

Every change is written to the audit log with the old and new value (secrets never).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml
from sqlalchemy.orm import Session

from app.audit import audit
from app.settings import _dotenv_value, _secrets_path, settings

log = logging.getLogger("forgesre")


@dataclass(frozen=True)
class SettingDef:
    key: str  # dotted YAML path, env var or secret name
    group: str
    label: str
    kind: str = "str"  # str | bool | int | enum | list | url
    store: str = "yaml"  # yaml | secret | env
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    help: str = ""
    test: str = ""  # component id for "Test connection" (see TESTS)
    effective: Callable[[], Any] | None = field(default=None, compare=False)


def _y(key, group, label, kind="str", **kw) -> SettingDef:
    return SettingDef(key=key, group=group, label=label, kind=kind, store="yaml", **kw)


def _s(key, group, label, **kw) -> SettingDef:
    return SettingDef(key=key, group=group, label=label, kind="str", store="secret", **kw)


def _e(key, group, label, effective=None, **kw) -> SettingDef:
    return SettingDef(key=key, group=group, label=label, kind="str", store="env", effective=effective, **kw)


GROUPS = {
    "general": "General",
    "network": "Network and services",
    "integrations": "Integrations",
    "discovery": "Discovery",
    "notifications": "Notification channels",
    "ai": "AI · ForgeRCA / ForgeAI",
}

REGISTRY: tuple[SettingDef, ...] = (
    # General
    _y("system.timezone", "general", "Time zone", help="FORGESRE_TIMEZONE in .env wins when set.", effective=lambda: settings.timezone),
    _y("system.log_level", "general", "Log level", "enum", choices=("debug", "info", "warning", "error")),
    _y("system.cookie_secure", "general", "Secure cookies (HTTPS only)", "bool", effective=lambda: settings.cookie_secure),
    # Network and services (installer-owned)
    _e("FORGESRE_HTTP_PORT", "network", "Web port"),
    _e("PROMETHEUS_URL", "network", "Prometheus", effective=lambda: settings.prometheus_url, test="prometheus"),
    _e("ALERTMANAGER_URL", "network", "Alertmanager", effective=lambda: settings.alertmanager_url, test="alertmanager"),
    _e("LOKI_URL", "network", "Loki", effective=lambda: settings.loki_url, test="loki"),
    _e("GRAFANA_PUBLIC_URL", "network", "Grafana (public link)", effective=lambda: settings.grafana_public_url, test="grafana"),
    _y("logging.loki.enabled", "network", "Use Loki logs as evidence", "bool", effective=lambda: settings.loki_enabled),
    _y("grafana.enabled", "network", "Show Grafana links", "bool", effective=lambda: settings.grafana_enabled),
    _y("monitoring.snmp.enabled", "network", "SNMP monitoring", "bool", effective=lambda: settings.snmp_enabled, test="snmp"),
    _y("monitoring.snmp.exporter_url", "network", "snmp_exporter URL", "url", effective=lambda: settings.snmp_exporter_url),
    _y("monitoring.snmp.module", "network", "Default SNMP module", effective=lambda: settings.snmp_module),
    _s("ALERTMANAGER_WEBHOOK_TOKEN", "network", "Alertmanager webhook token"),
    # Integrations
    _y("inventory.glpi.enabled", "integrations", "GLPI enabled", "bool", effective=lambda: settings.glpi_enabled, test="glpi"),
    _y("inventory.glpi.url", "integrations", "GLPI URL", "url", effective=lambda: settings.glpi_url),
    _y("inventory.glpi.auto_sync", "integrations", "GLPI sync every 6 h", "bool"),
    _y("inventory.glpi.timeout_seconds", "integrations", "GLPI timeout (s)", "int", minimum=2, maximum=5),
    _s("GLPI_APP_TOKEN", "integrations", "GLPI app token"),
    _s("GLPI_USER_TOKEN", "integrations", "GLPI user token"),
    _y("inventory.zabbix.enabled", "integrations", "Zabbix enabled", "bool", effective=lambda: settings.zabbix_enabled, test="zabbix"),
    _y("inventory.zabbix.url", "integrations", "Zabbix URL", "url", effective=lambda: settings.zabbix_url),
    _y("inventory.zabbix.auto_sync", "integrations", "Zabbix host import every 6 h", "bool"),
    _y("inventory.zabbix.timeout_seconds", "integrations", "Zabbix timeout (s)", "int", minimum=2, maximum=5),
    _s("ZABBIX_API_TOKEN", "integrations", "Zabbix API token"),
    _y("inventory.librenms.enabled", "integrations", "LibreNMS enabled", "bool", effective=lambda: settings.librenms_enabled, test="librenms"),
    _y("inventory.librenms.url", "integrations", "LibreNMS URL", "url", effective=lambda: settings.librenms_url),
    _y("inventory.librenms.timeout_seconds", "integrations", "LibreNMS timeout (s)", "int", minimum=2, maximum=15),
    _s("LIBRENMS_API_TOKEN", "integrations", "LibreNMS API token"),
    _y("inventory.netbox.enabled", "integrations", "NetBox enabled", "bool", effective=lambda: settings.netbox_enabled, test="netbox"),
    _y("inventory.netbox.url", "integrations", "NetBox URL", "url", effective=lambda: settings.netbox_url),
    _y("inventory.netbox.auto_sync", "integrations", "NetBox sync every 6 h", "bool", effective=lambda: settings.netbox_auto_sync),
    _s("NETBOX_API_TOKEN", "integrations", "NetBox API token"),
    # Discovery
    _y("discovery.enabled", "discovery", "Discovery enabled", "bool", effective=lambda: settings.discovery_enabled, test="discovery"),
    _y("discovery.mode", "discovery", "Mode", "enum", choices=("manual", "semi-automatic", "automatic"), effective=lambda: settings.discovery_mode),
    _y("discovery.cidrs", "discovery", "Ranges to scan", "list", help="Comma separated CIDRs.", effective=lambda: settings.discovery_cidrs),
    # Notifications
    _y("notifications.email.enabled", "notifications", "Email enabled", "bool", effective=lambda: settings.email_enabled),
    _y("notifications.email.host", "notifications", "SMTP server", effective=lambda: settings.smtp_host),
    _y("notifications.email.port", "notifications", "SMTP port", "int", minimum=1, maximum=65535, effective=lambda: settings.smtp_port),
    _y("notifications.email.from", "notifications", "From address", effective=lambda: settings.smtp_from),
    _y("notifications.email.tls", "notifications", "STARTTLS", "bool", effective=lambda: settings.smtp_tls),
    _s("SMTP_USERNAME", "notifications", "SMTP user"),
    _s("SMTP_PASSWORD", "notifications", "SMTP password"),
    # Chat (Slack / Teams / Discord). Messages leave the server: only the incident line is sent.
    _y("notifications.chat.slack.enabled", "notifications", "Slack enabled", "bool", test="slack",
       help="Posts when an incident opens, escalates and resolves. Set SLACK_WEBHOOK_URL first."),
    _y("notifications.chat.slack.min_severity", "notifications", "Slack minimum severity", "enum", choices=("critical", "high", "warning")),
    _s("SLACK_WEBHOOK_URL", "notifications", "Slack incoming webhook URL"),
    _y("notifications.chat.teams.enabled", "notifications", "Microsoft Teams enabled", "bool", test="teams",
       help="Use a Teams Workflows webhook (\"When a Teams webhook request is received\"). Set TEAMS_WEBHOOK_URL first."),
    _y("notifications.chat.teams.min_severity", "notifications", "Teams minimum severity", "enum", choices=("critical", "high", "warning")),
    _s("TEAMS_WEBHOOK_URL", "notifications", "Teams webhook URL"),
    _y("notifications.chat.discord.enabled", "notifications", "Discord enabled", "bool", test="discord",
       help="Channel settings › Integrations › Webhooks. Set DISCORD_WEBHOOK_URL first."),
    _y("notifications.chat.discord.min_severity", "notifications", "Discord minimum severity", "enum", choices=("critical", "high", "warning")),
    _s("DISCORD_WEBHOOK_URL", "notifications", "Discord webhook URL"),
    # AI
    _y("ai.enabled", "ai", "ForgeAI enabled", "bool", effective=lambda: settings.ai_enabled, test="llm"),
    _y("ai.llm.mode", "ai", "ForgeAI model", "enum", choices=("disabled", "bundled", "external"), help="bundled = local llama.cpp; external = an OpenAI-compatible URL."),
    _y("ai.llm.url", "ai", "Model URL", "url", effective=lambda: settings.llm_url),
    _y("ai.llm.model", "ai", "Model name", effective=lambda: settings.llm_model),
    _y("ai.llm.timeout_seconds", "ai", "Model timeout (s)", "int", minimum=10, maximum=900, effective=lambda: settings.llm_timeout),
    _y("ai.rca.window_minutes", "ai", "ForgeRCA look-back (min)", "int", minimum=5, maximum=1440, effective=lambda: settings.rca_window_minutes),
    _y("ai.rca.max_log_lines", "ai", "ForgeRCA log lines", "int", minimum=1, maximum=500, effective=lambda: settings.rca_max_log_lines),
)

BY_KEY = {item.key: item for item in REGISTRY}


# --- reading ---------------------------------------------------------------------------


def _yaml_get(data: dict, dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _secret_set(name: str) -> bool:
    path = _secrets_path()
    value = _dotenv_value(path, name) if path.is_file() else ""
    return bool(value or (os.environ.get(name) or "").strip())


def _effective(item: SettingDef) -> Any:
    if item.effective is None:
        return None
    try:
        return item.effective()
    except Exception as exc:  # a broken value must not break the whole page
        return f"invalid: {exc}"


def describe(item: SettingDef) -> dict[str, Any]:
    """One setting as the UI, API and CLI show it. Secrets carry only ``set`` true/false."""
    row: dict[str, Any] = {
        "key": item.key,
        "group": item.group,
        "label": item.label,
        "kind": item.kind,
        "store": item.store,
        "editable": item.store == "yaml",
        "help": item.help,
        "test": item.test,
    }
    if item.choices:
        row["choices"] = list(item.choices)
    if item.store == "secret":
        row["set"] = _secret_set(item.key)
        return row
    if item.store == "env":
        row["value"] = _effective(item) if item.effective else os.environ.get(item.key, "")
        return row
    row["value"] = _yaml_get(settings.yaml or {}, item.key)
    row["effective"] = _effective(item)
    return row


def list_settings(group: str | None = None) -> list[dict[str, Any]]:
    return [describe(item) for item in REGISTRY if group in (None, "", item.group)]


# --- writing ---------------------------------------------------------------------------


def parse_value(item: SettingDef, raw: Any) -> Any:
    """Turn UI / CLI input into the stored type; ValueError with a short reason when invalid."""
    if item.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ValueError("expected true or false")
    if item.kind == "int":
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise ValueError("expected a whole number") from exc
        if item.minimum is not None and value < item.minimum:
            raise ValueError(f"must be at least {item.minimum}")
        if item.maximum is not None and value > item.maximum:
            raise ValueError(f"must be at most {item.maximum}")
        return value
    if item.kind == "enum":
        value = str(raw).strip()
        if value not in item.choices:
            raise ValueError("must be one of " + ", ".join(item.choices))
        return value
    if item.kind == "list":
        items = raw if isinstance(raw, list) else str(raw).replace("\n", ",").split(",")
        return [str(part).strip() for part in items if str(part).strip()]
    value = str(raw).strip()
    if item.kind == "url" and value:
        from app.settings import _require_http_url

        _require_http_url(value, item.label)
    return value


def _write_yaml(dotted: str, value: Any) -> None:
    path = settings.config_path
    data: dict = {}
    if path.exists():
        with path.open() as handle:
            loaded = yaml.safe_load(handle) or {}
        data = loaded if isinstance(loaded, dict) else {}
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written in place, not via rename: the file is a single-file bind mount in Docker.
    with path.open("w") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False, allow_unicode=True)


def set_setting(db: Session, key: str, raw: Any, actor: str) -> dict[str, Any]:
    """Validate and store one YAML setting, reload config, audit old -> new. Returns the new row.

    Secrets and installer-owned values raise ValueError: they are not written by Core.
    Discovery CIDRs go through Settings.set_discovery_cidrs so they are normalised the same
    way as on the Discovery page.
    """
    item = BY_KEY.get(key)
    if item is None:
        raise ValueError(f"unknown setting {key}")
    if item.store == "secret":
        raise ValueError(f"{key} is a secret: set it on the host with ./forgesre settings set-secret {key}")
    if item.store == "env":
        raise ValueError(f"{key} is set by the installer in .env: change it there and restart Core")
    value = parse_value(item, raw)
    old = _yaml_get(settings.yaml or {}, key)
    if key == "discovery.cidrs":
        value = settings.set_discovery_cidrs(value)
    else:
        _write_yaml(key, value)
        settings.reload_yaml()
    audit(db, "settings.update", actor=actor, object_type="setting", object_id=key, data={"old": old, "new": value}, commit=True)
    log.info("setting %s changed by %s", key, actor)
    return describe(item)


# --- testing ---------------------------------------------------------------------------


def _chat_test(channel: str) -> dict[str, Any]:
    from app.chat import send_test

    return send_test(channel)


def test_connection(component: str) -> dict[str, Any]:
    """Run the System Health check for one component now (bypassing the cache).

    Every check has its own timeout, so a dead integration answers in seconds with an error.
    """
    from app import api

    checks: dict[str, Callable[[], dict]] = {
        "prometheus": lambda: api._http(f"{settings.prometheus_url}/-/ready", "GET"),
        "alertmanager": lambda: api._http(f"{settings.alertmanager_url}/-/ready", "GET"),
        "loki": api._loki_check,
        "grafana": api._grafana_check,
        "snmp": api._snmp_check,
        "llm": lambda: api._http((settings.llm_url or "").rstrip("/") + "/models", "GET")
        if settings.llm_url
        else {"status": "disabled", "detail": "no model URL"},
        "netbox": api._netbox_check,
        "zabbix": api._zabbix_check,
        "glpi": api._glpi_check,
        "librenms": api._librenms_check,
        "discovery": api._discovery_check,
        "slack": lambda: _chat_test("slack"),
        "teams": lambda: _chat_test("teams"),
        "discord": lambda: _chat_test("discord"),
    }
    check = checks.get(component)
    if check is None:
        raise ValueError(f"no test for {component}")
    try:
        result = check() or {}
    except Exception as exc:
        result = {"status": "error", "detail": str(exc)[:300]}
    return {"component": component, **result}
