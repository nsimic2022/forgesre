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


def _sla(*path: str):
    from app.incident_process import sla_config

    value = sla_config()
    for part in path:
        value = value[part]
    return value


def _alarm(kind: str, key: str):
    from app.alarm_noise import config

    return config(kind)[key]


GROUPS = {
    "general": "General",
    "checks": "Checks",
    "alarms": "Alarms",
    "sla": "SLA",
    "network": "Network",
    "integrations": "Integrations",
    "discovery": "Discovery",
    "notifications": "Notifications",
    "ai": "RCA / AI",
    "auth": "Sign-in",
}

REGISTRY: tuple[SettingDef, ...] = (
    # General
    _y("system.timezone", "general", "Time zone", help="FORGESRE_TIMEZONE in .env wins when set.", effective=lambda: settings.timezone),
    _y("system.log_level", "general", "Log level", "enum", choices=("debug", "info", "warning", "error")),
    _y("checks.tls_targets", "checks", "TLS endpoints to watch", help="Comma separated host, host:port or https://host. Expiry is checked every 15 min."),
    _y("checks.tls_warn_days", "checks", "Certificate warning (days)", "int"),
    _y("checks.tls_critical_days", "checks", "Certificate critical (days)", "int"),
    _y("checks.backup_max_age_hours", "checks", "ForgeSRE backup max age (hours)", "int"),
    _y("checks.lifecycle_warn_days", "checks", "End of life / licence warning (days)", "int", minimum=1, maximum=365,
       help="AssetLifecycleExpiring fires this many days before an asset's end of life or licence date. Support uses the asset's own lead days."),
    _y("inventory.rack_units", "general", "Rack height (U)", "int", minimum=10, maximum=60,
       help="Height of every rack in Inventory › Racks. Default 42."),
    # Alarms (app.alarm_noise): storms and flapping alerts page once instead of every time.
    _y("alerts.storm.enabled", "alarms", "Group alarm storms", "bool",
       effective=lambda: _alarm("storm", "enabled"),
       help="Many new incidents at once open one Alarm storm incident that pages; the rest link to it."),
    _y("alerts.storm.threshold", "alarms", "Storm: new incidents", "int", minimum=2, maximum=500,
       effective=lambda: _alarm("storm", "threshold")),
    _y("alerts.storm.window_minutes", "alarms", "Storm: within minutes", "int", minimum=1, maximum=1440,
       effective=lambda: _alarm("storm", "window_minutes")),
    _y("alerts.flapping.enabled", "alarms", "Stop paging for flapping alerts", "bool",
       effective=lambda: _alarm("flapping", "enabled"),
       help="An alert that keeps resolving and firing again stays visible with a counter but stops paging."),
    _y("alerts.flapping.threshold", "alarms", "Flapping: times fired", "int", minimum=2, maximum=500,
       effective=lambda: _alarm("flapping", "threshold")),
    _y("alerts.flapping.window_minutes", "alarms", "Flapping: within minutes", "int", minimum=1, maximum=1440,
       effective=lambda: _alarm("flapping", "window_minutes")),
    # SLA (app.incident_process): targets per severity; shown on incidents and in Reports, escalation unchanged.
    _y("sla.enabled", "sla", "Show SLA timers", "bool", effective=lambda: _sla("enabled")),
    _y("sla.response.critical", "sla", "Acknowledge within (critical, min)", "int", minimum=1, maximum=43200, effective=lambda: _sla("response", "critical")),
    _y("sla.response.high", "sla", "Acknowledge within (high, min)", "int", minimum=1, maximum=43200, effective=lambda: _sla("response", "high")),
    _y("sla.response.warning", "sla", "Acknowledge within (warning, min)", "int", minimum=1, maximum=43200, effective=lambda: _sla("response", "warning")),
    _y("sla.resolve.critical", "sla", "Resolve within (critical, min)", "int", minimum=1, maximum=43200, effective=lambda: _sla("resolve", "critical")),
    _y("sla.resolve.high", "sla", "Resolve within (high, min)", "int", minimum=1, maximum=43200, effective=lambda: _sla("resolve", "high")),
    _y("sla.resolve.warning", "sla", "Resolve within (warning, min)", "int", minimum=1, maximum=43200, effective=lambda: _sla("resolve", "warning")),
    _y("system.public_url", "general", "Public address", help="How people reach ForgeSRE, e.g. https://forgesre.dc.local. Used for links in mail and chat (incident, one-click acknowledge). Empty = no links."),
    _y("system.cookie_secure", "general", "Secure cookies (HTTPS only)", "bool", effective=lambda: settings.cookie_secure),
    # Sign-in: directory and single sign-on. Roles come from groups; the local admin always works.
    _y("auth.ldap.enabled", "auth", "LDAP / Active Directory sign-in", "bool", test="ldap"),
    _y("auth.ldap.url", "auth", "LDAP server", help="ldaps://dc1.example.local:636 (or ldap://… with STARTTLS)."),
    _y("auth.ldap.bind_dn", "auth", "Service account DN", help="Used to find the user; empty = anonymous search."),
    _s("LDAP_BIND_PASSWORD", "auth", "Service account password"),
    _y("auth.ldap.base_dn", "auth", "Search base", help="e.g. OU=Staff,DC=example,DC=local"),
    _y("auth.ldap.user_filter", "auth", "User filter", help="{login} is what the person typed. Default matches mail, UPN or sAMAccountName."),
    _y("auth.ldap.groups.admin", "auth", "LDAP group → Admin", help="Group DN; members sign in as Admin."),
    _y("auth.ldap.groups.engineer", "auth", "LDAP group → Engineer"),
    _y("auth.ldap.groups.analyst", "auth", "LDAP group → Analyst"),
    _y("auth.ldap.groups.viewer", "auth", "LDAP group → Viewer"),
    _y("auth.ldap.default_role", "auth", "LDAP role without a matching group", "enum", choices=("", "viewer"),
       help="Empty = people in none of the groups cannot sign in."),
    _y("auth.oidc.enabled", "auth", "OIDC single sign-on", "bool", test="oidc"),
    _y("auth.oidc.issuer", "auth", "OIDC issuer", "url", help="e.g. https://login.microsoftonline.com/<tenant>/v2.0 or https://keycloak/realms/dc"),
    _y("auth.oidc.client_id", "auth", "OIDC client id"),
    _s("OIDC_CLIENT_SECRET", "auth", "OIDC client secret"),
    _y("auth.oidc.groups_claim", "auth", "Groups claim", help="Claim in the ID token that lists groups (default groups)."),
    _y("auth.oidc.groups.admin", "auth", "OIDC groups → Admin", help="Comma separated group names or ids."),
    _y("auth.oidc.groups.engineer", "auth", "OIDC groups → Engineer"),
    _y("auth.oidc.groups.analyst", "auth", "OIDC groups → Analyst"),
    _y("auth.oidc.groups.viewer", "auth", "OIDC groups → Viewer"),
    _y("auth.oidc.default_role", "auth", "OIDC role without a matching group", "enum", choices=("", "viewer")),
    _y("auth.oidc.button_label", "auth", "Sign-in button text"),
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
    _y("inventory.new_from_sources", "integrations", "New devices from GLPI / Zabbix / NetBox", "enum", choices=("add", "review"),
       help="add: straight into the inventory. review: listed in Discovery until an Engineer approves them.",
       effective=lambda: settings.inventory_new_from_sources),
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
    _s("ZABBIX_WEBHOOK_TOKEN", "integrations", "Zabbix webhook token (alarms from Zabbix)"),
    _y("inventory.librenms.enabled", "integrations", "LibreNMS enabled", "bool", effective=lambda: settings.librenms_enabled, test="librenms"),
    _y("inventory.librenms.url", "integrations", "LibreNMS URL", "url", effective=lambda: settings.librenms_url),
    _y("inventory.librenms.timeout_seconds", "integrations", "LibreNMS timeout (s)", "int", minimum=2, maximum=15),
    _s("LIBRENMS_API_TOKEN", "integrations", "LibreNMS API token"),
    _y("inventory.netbox.enabled", "integrations", "NetBox enabled", "bool", effective=lambda: settings.netbox_enabled, test="netbox"),
    _y("inventory.netbox.url", "integrations", "NetBox URL", "url", effective=lambda: settings.netbox_url),
    _y("inventory.netbox.auto_sync", "integrations", "NetBox sync every 6 h", "bool", effective=lambda: settings.netbox_auto_sync),
    _s("NETBOX_API_TOKEN", "integrations", "NetBox API token"),
    # Discovery
    _y("discovery.enabled", "discovery", "Read new devices from LibreNMS", "bool", effective=lambda: settings.discovery_enabled, test="discovery",
       help="ForgeSRE does not scan networks; LibreNMS finds devices (./forgesre discovery nets CIDR …)."),
    _y("discovery.mode", "discovery", "New LibreNMS devices", "enum", choices=("manual", "semi-automatic", "automatic"), effective=lambda: settings.discovery_mode,
       help="semi-automatic: read every 6 h, list in Add devices · automatic: add to the inventory · manual: only on Read now."),
    # Notifications
    _y("notifications.email.enabled", "notifications", "Email enabled", "bool", effective=lambda: settings.email_enabled, test="smtp",
       help="Test signs in to the SMTP server and mails one test message to the From address."),
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
    _s("CHANGES_WEBHOOK_TOKEN", "notifications", "Changes webhook token (deploys, GLPI changes)"),
    _y("mcp.enabled", "integrations", "Read-only MCP server at /mcp", "bool",
       help="Lets an AI client (Claude, Cursor) read incidents, assets, SLOs. Data then goes to the model that client uses. Bearer MCP_TOKEN."),
    _s("MCP_TOKEN", "integrations", "MCP bearer token"),
    _y("notifications.chat.teams.enabled", "notifications", "Microsoft Teams enabled", "bool", test="teams",
       help="Use a Teams Workflows webhook (\"When a Teams webhook request is received\"). Set TEAMS_WEBHOOK_URL first."),
    _y("notifications.chat.teams.min_severity", "notifications", "Teams minimum severity", "enum", choices=("critical", "high", "warning")),
    _s("TEAMS_WEBHOOK_URL", "notifications", "Teams webhook URL"),
    _y("notifications.chat.discord.enabled", "notifications", "Discord enabled", "bool", test="discord",
       help="Channel settings › Integrations › Webhooks. Set DISCORD_WEBHOOK_URL first."),
    _y("notifications.chat.discord.min_severity", "notifications", "Discord minimum severity", "enum", choices=("critical", "high", "warning")),
    _s("DISCORD_WEBHOOK_URL", "notifications", "Discord webhook URL"),
    # SMS to the person on call (app.sms)
    _y("notifications.sms.enabled", "notifications", "SMS to the person on call", "bool", test="sms",
       help="Off by default. Sends to the phone of the on-call slot that covers now; Test sends one SMS."),
    _y("notifications.sms.provider", "notifications", "SMS provider", "enum", choices=("generic", "twilio")),
    _y("notifications.sms.min_severity", "notifications", "SMS minimum severity", "enum", choices=("critical", "high", "warning")),
    _y("notifications.sms.url", "notifications", "SMS gateway URL", "url", help="generic: the gateway's POST URL"),
    _y("notifications.sms.body", "notifications", "SMS JSON body", help='generic: JSON with {to} and {text}, default {"to": "{to}", "text": "{text}"}'),
    _y("notifications.sms.account", "notifications", "Twilio Account SID"),
    _y("notifications.sms.from", "notifications", "SMS sender number"),
    _s("SMS_GATEWAY_TOKEN", "notifications", "SMS gateway token (Twilio: Auth Token)"),
    # Tickets (app.tickets)
    _y("tickets.glpi.enabled", "integrations", "GLPI tickets from incidents", "bool", test="ticket-glpi",
       help="Open ticket on an incident creates a GLPI ticket; its status is read every 10 min. Uses the GLPI integration's tokens."),
    _y("tickets.webhook.enabled", "integrations", "Ticket webhook (Jira, ServiceNow…)", "bool", test="ticket-webhook",
       help="One JSON POST per incident event (opened, escalated, resolved) for another ticket system."),
    _y("tickets.webhook.url", "integrations", "Ticket webhook URL", "url"),
    _s("TICKET_WEBHOOK_TOKEN", "integrations", "Ticket webhook Bearer token"),
    # AI
    _y("ai.enabled", "ai", "ForgeAI enabled", "bool", effective=lambda: settings.ai_enabled, test="llm"),
    _y("ai.llm.mode", "ai", "Where the model runs", "enum", choices=("disabled", "bundled", "external"), help="bundled = local llama.cpp; external = an OpenAI-compatible URL."),
    _y("ai.llm.url", "ai", "Model URL", "url", effective=lambda: settings.llm_url),
    _y("ai.llm.model", "ai", "Model name", effective=lambda: settings.llm_model),
    _y("ai.llm.timeout_seconds", "ai", "Model timeout (s)", "int", minimum=10, maximum=900, effective=lambda: settings.llm_timeout),
    _y("ai.llm.thinking", "ai", "Let the model think first", "bool", effective=lambda: settings.llm_thinking,
       help="Qwen3 and similar can reason before answering; the chat shows the reasoning. Slower on CPU: raise the timeout too."),
    _y("ai.cloud.enabled", "ai", "Paid cloud model (optional)", "bool", effective=lambda: settings.cloud_enabled, test="cloud",
       help="Off by default. When on, an Engineer may send one incident's facts (secrets removed) to the provider below, by choice, per question."),
    _y("ai.cloud.provider", "ai", "Cloud provider", "enum", choices=("openai", "anthropic", "azure", "openai-compatible"), effective=lambda: settings.cloud_provider),
    _y("ai.cloud.model", "ai", "Cloud model", effective=lambda: settings.cloud_model, help="e.g. gpt-4o-mini, claude-sonnet-5-5; Azure: the deployment name."),
    _y("ai.cloud.url", "ai", "Cloud API URL", "url", effective=lambda: settings.cloud_url,
       help="Empty = the provider's own. Azure: https://NAME.openai.azure.com/openai/deployments/DEPLOYMENT/chat/completions?api-version=2024-10-21"),
    _y("ai.cloud.timeout_seconds", "ai", "Cloud timeout (s)", "int", minimum=10, maximum=600, effective=lambda: settings.cloud_timeout),
    _s("CLOUD_LLM_API_KEY", "ai", "Cloud model API key"),
    _y("ai.embed.enabled", "ai", "Document search (local embeddings)", "bool", effective=lambda: bool(settings.embed_url), test="embed",
       help="Vendor manuals uploaded in Settings › ForgeAI documents; searched with a local embedding model."),
    _y("ai.embed.url", "ai", "Embedding server URL", "url", effective=lambda: settings.embed_url or ""),
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
        "embed": api._embed_check,
        "cloud": lambda: __import__("app.cloud_llm", fromlist=["check"]).check(),
        "sms": lambda: __import__("app.sms", fromlist=["check"]).check(),
        "ticket-glpi": lambda: __import__("app.tickets", fromlist=["check_glpi"]).check_glpi(),
        "ticket-webhook": lambda: __import__("app.tickets", fromlist=["check_webhook"]).check_webhook(),
        "ldap": lambda: __import__("app.ldap_auth", fromlist=["check"]).check(),
        "oidc": lambda: __import__("app.oidc_auth", fromlist=["check"]).check(),
        "slack": lambda: _chat_test("slack"),
        "teams": lambda: _chat_test("teams"),
        "discord": lambda: _chat_test("discord"),
        "smtp": lambda: __import__("app.email_service", fromlist=["check"]).check(),
    }
    check = checks.get(component)
    if check is None:
        raise ValueError(f"no test for {component}")
    try:
        result = check() or {}
    except Exception as exc:
        result = {"status": "error", "detail": str(exc)[:300]}
    return {"component": component, **result}
