"""LibreNMS REST client for Discovery SNMP identify / inventory.

Forge uses LibreNMS to discover vendor, model, ports and sensors. snmp_exporter
still collects Prometheus series — LibreNMS polling is disabled after identify
so the two do not walk the same device. Credentials are never logged.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.settings import settings

log = logging.getLogger("forgesre")

BACKOFF_SECONDS = 120.0
OK_TTL = 60.0
FAIL_TTL = BACKOFF_SECONDS
NOT_CONFIGURED_WHY = (
    "LibreNMS API token missing. ./forgesre update writes LIBRENMS_API_TOKEN after "
    "the bundled container is up, or create one in LibreNMS → API Access."
)

_lock = threading.Lock()
_state: dict[str, Any] = {
    "down_until": 0.0,
    "down_why": "",
    "status": None,
    "status_at": 0.0,
    "status_key": "",
}


class LibreNmsError(RuntimeError):
    """LibreNMS answered with an error, or Core is not configured."""


class LibreNmsUnavailable(LibreNmsError):
    """Connect/timeout/5xx, or backoff window."""


def reset_state() -> None:
    with _lock:
        _state["down_until"] = 0.0
        _state["down_why"] = ""
        _state["status"] = None
        _state["status_at"] = 0.0
        _state["status_key"] = ""


def api_root(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/api/v0"):
        return base
    return base + "/api/v0"


def token_presence(token: str) -> str:
    return "yes" if (token or "").strip() else "no"


def backoff_remaining() -> float:
    return max(0.0, float(_state["down_until"]) - time.monotonic())


def _mark_down(why: str) -> None:
    with _lock:
        _state["down_until"] = time.monotonic() + BACKOFF_SECONDS
        _state["down_why"] = why


def _mark_up() -> None:
    with _lock:
        _state["down_until"] = 0.0
        _state["down_why"] = ""


def _short(exc: BaseException) -> str:
    return (str(exc).strip() or type(exc).__name__)[:300]


def configured() -> bool:
    return bool(settings.librenms_url and settings.librenms_token)


def _headers() -> dict[str, str]:
    token = settings.librenms_token
    return {
        "Authorization": f"Bearer {token}",
        "X-Auth-Token": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _timeout() -> float:
    return settings.librenms_timeout


def _request(method: str, path: str, *, json_body: dict | None = None) -> dict[str, Any]:
    if not configured():
        raise LibreNmsError(NOT_CONFIGURED_WHY)
    remaining = backoff_remaining()
    if remaining > 0:
        raise LibreNmsUnavailable(f"LibreNMS backoff {_state.get('down_why') or ''} ({int(remaining)}s)".strip())
    url = api_root(settings.librenms_url) + path
    try:
        with httpx.Client(timeout=_timeout()) as client:
            response = client.request(method, url, headers=_headers(), json=json_body)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
        why = _short(exc)
        _mark_down(why)
        raise LibreNmsUnavailable(why) from exc
    if response.status_code >= 500:
        why = f"HTTP {response.status_code}"
        _mark_down(why)
        raise LibreNmsUnavailable(why)
    _mark_up()
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"raw": payload}
    if response.status_code >= 400:
        message = str(payload.get("message") or payload.get("status") or f"HTTP {response.status_code}")[:300]
        raise LibreNmsError(message)
    if str(payload.get("status") or "").lower() == "error":
        raise LibreNmsError(str(payload.get("message") or "LibreNMS error")[:300])
    return payload


def list_devices(*, disabled: str = "") -> list[dict[str, Any]]:
    path = "/devices"
    if disabled in {"0", "1"}:
        path += f"?type=disabled&query={disabled}"
    payload = _request("GET", path)
    rows = payload.get("devices") or []
    return [row for row in rows if isinstance(row, dict)]


def _host_path(hostname: str) -> str:
    """Keep IPs and FQDNs readable; encode spaces and slashes only."""
    host = str(hostname or "").strip()
    if not host:
        raise LibreNmsError("Need a LibreNMS hostname")
    return quote(host, safe=".-_:")


def get_device(hostname: str) -> dict[str, Any]:
    host = _host_path(hostname)
    payload = _request("GET", f"/devices/{host}")
    rows = payload.get("devices") or []
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    if isinstance(payload.get("devices"), dict):
        return payload["devices"]
    raise LibreNmsError("LibreNMS returned no device")


def get_ports(hostname: str) -> list[dict[str, Any]]:
    host = _host_path(hostname)
    payload = _request("GET", f"/devices/{host}/ports?columns=ifIndex,ifName,ifDescr,ifOperStatus,ifAdminStatus,ifSpeed")
    rows = payload.get("ports") or []
    return [row for row in rows if isinstance(row, dict)]


def get_health(hostname: str, kind: str) -> list[dict[str, Any]]:
    host = _host_path(hostname)
    safe_kind = quote(str(kind or "").strip(), safe=".-_")
    if not safe_kind:
        return []
    try:
        payload = _request("GET", f"/devices/{host}/health/{safe_kind}")
    except LibreNmsError:
        return []
    graphs = payload.get("graphs") or payload.get("health") or payload.get(kind) or []
    if isinstance(graphs, dict):
        graphs = [graphs]
    return [row for row in graphs if isinstance(row, dict)]


def add_device(body: dict[str, Any]) -> dict[str, Any]:
    return _request("POST", "/devices", json_body=body)


def set_disabled(hostname: str, disabled: bool) -> dict[str, Any]:
    host = _host_path(hostname)
    return _request(
        "PATCH",
        f"/devices/{host}",
        json_body={"field": ["disabled"], "data": ["1" if disabled else "0"]},
    )


def delete_device(hostname: str) -> dict[str, Any]:
    host = _host_path(hostname)
    return _request("DELETE", f"/devices/{host}")


def add_payload(
    ip: str,
    *,
    version: str = "v2c",
    community: str = "",
    port: int = 161,
    v3_user: str = "",
    v3_level: str = "",
    v3_auth_proto: str = "",
    v3_auth_pass: str = "",
    v3_priv_proto: str = "",
    v3_priv_pass: str = "",
) -> dict[str, Any]:
    host = (ip or "").strip()
    ver = (version or "v2c").strip().lower()
    if ver not in {"v1", "v2c", "v3"}:
        ver = "v2c"
    body: dict[str, Any] = {
        "hostname": host,
        "snmpver": ver,
        "version": ver,
        "port": int(port or 161),
        "force_add": False,
    }
    if ver in {"v1", "v2c"}:
        body["community"] = (community or "public")[:128]
    else:
        body["authlevel"] = (v3_level or "authPriv")[:32]
        body["authname"] = (v3_user or "")[:64]
        body["authpass"] = (v3_auth_pass or "")[:128]
        body["authalgo"] = (v3_auth_proto or "SHA")[:16]
        if v3_priv_pass:
            body["cryptopass"] = v3_priv_pass[:128]
            body["cryptoalgo"] = (v3_priv_proto or "AES")[:16]
    return body


def _first_device(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("devices") or []
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    if isinstance(rows, dict):
        return rows
    return {}


def discover_device(
    ip: str,
    *,
    version: str = "v2c",
    community: str = "",
    port: int = 161,
    v3_user: str = "",
    v3_level: str = "",
    v3_auth_proto: str = "",
    v3_auth_pass: str = "",
    v3_priv_proto: str = "",
    v3_priv_pass: str = "",
    disable_polling: bool = True,
) -> dict[str, Any]:
    """Add (or fetch) a device, read ports/health, then disable LibreNMS SNMP polling."""
    host = (ip or "").strip()
    if not host:
        raise LibreNmsError("Need an IP")
    body = add_payload(
        host,
        version=version,
        community=community,
        port=port,
        v3_user=v3_user,
        v3_level=v3_level,
        v3_auth_proto=v3_auth_proto,
        v3_auth_pass=v3_auth_pass,
        v3_priv_proto=v3_priv_proto,
        v3_priv_pass=v3_priv_pass,
    )
    added = False
    try:
        payload = add_device(body)
        device = _first_device(payload) or get_device(host)
        added = True
    except LibreNmsError as exc:
        text = str(exc).lower()
        if "already" in text or "duplicate" in text or "exist" in text:
            device = get_device(host)
        else:
            raise
    hostname = str(device.get("hostname") or host)
    ports: list[dict[str, Any]] = []
    processors: list[dict[str, Any]] = []
    mempools: list[dict[str, Any]] = []
    storage: list[dict[str, Any]] = []
    temperatures: list[dict[str, Any]] = []
    try:
        ports = get_ports(hostname)
    except LibreNmsError:
        ports = []
    processors = get_health(hostname, "processor")
    mempools = get_health(hostname, "mempool")
    storage = get_health(hostname, "storage")
    temperatures = get_health(hostname, "temperature")
    if disable_polling:
        try:
            set_disabled(hostname, True)
            device["disabled"] = 1
        except LibreNmsError:
            pass
    return {
        "ok": True,
        "hostname": hostname,
        "device": device,
        "added": added,
        "ports": ports,
        "processors": processors,
        "mempools": mempools,
        "storage": storage,
        "temperatures": temperatures,
        "disabled_polling": disable_polling,
    }


def status() -> dict[str, Any]:
    """Cached doctor row. Never logs the API token."""
    url = settings.librenms_url or "http://127.0.0.1:8000"
    token = settings.librenms_token
    key = f"{url}|{token_presence(token)}|{int(settings.librenms_enabled)}"
    now = time.monotonic()
    with _lock:
        cached = _state["status"]
        if cached is not None and _state["status_key"] == key and now - float(_state["status_at"] or 0) < (
            OK_TTL if cached.get("ok") else FAIL_TTL
        ):
            return dict(cached)
    row = _probe_status(url, token)
    with _lock:
        _state["status"] = row
        _state["status_at"] = now
        _state["status_key"] = key
    return dict(row)


def _probe_status(url: str, token: str) -> dict[str, Any]:
    if not settings.librenms_enabled:
        return {
            "ok": True,
            "configured": False,
            "light": "grey",
            "label": "Disabled",
            "why": "inventory.librenms.enabled is false",
            "hosts": 0,
        }
    login = (url or "").rstrip("/") + "/login"
    try:
        with httpx.Client(timeout=min(_timeout(), 4.0), follow_redirects=True) as client:
            probe = client.get(login)
        ui_up = probe.status_code < 500
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
        ui_up = False
    if not ui_up:
        return {
            "ok": False,
            "configured": bool(token),
            "light": "yellow",
            "label": "Starting",
            "why": "LibreNMS UI is not answering yet (first boot migrates MariaDB).",
            "hosts": 0,
        }
    if not (token or "").strip():
        return {
            "ok": False,
            "configured": False,
            "light": "yellow",
            "label": "No API token",
            "why": NOT_CONFIGURED_WHY,
            "hosts": 0,
        }
    try:
        devices = list_devices()
    except LibreNmsUnavailable as exc:
        return {
            "ok": False,
            "configured": True,
            "light": "yellow",
            "label": "Unreachable",
            "why": str(exc)[:200],
            "hosts": 0,
        }
    except LibreNmsError as exc:
        return {
            "ok": False,
            "configured": True,
            "light": "yellow",
            "label": "API error",
            "why": str(exc)[:200],
            "hosts": 0,
        }
    return {
        "ok": True,
        "configured": True,
        "light": "green",
        "label": "Connected",
        "why": f"{len(devices)} device(s) in LibreNMS (SNMP polling off after Identify).",
        "hosts": len(devices),
    }
