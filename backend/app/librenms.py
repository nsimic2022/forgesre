"""LibreNMS REST client for Discovery SNMP identify / inventory.

Forge uses LibreNMS to discover vendor, model, ports and sensors. snmp_exporter
still collects Prometheus series — LibreNMS polling is disabled after identify
so the two do not walk the same device. Credentials are never logged.

HTTP uses ``httpx.AsyncClient`` with a short connect timeout so a hung DC hop
does not freeze the FastAPI event loop. Sync callers use ``run_sync``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
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
CONNECT_TIMEOUT = 3.0
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


def http_timeout() -> httpx.Timeout:
    """Read/write bound by YAML timeout_seconds; connect is always short."""
    total = float(settings.librenms_timeout)
    connect = min(CONNECT_TIMEOUT, total)
    return httpx.Timeout(total, connect=connect)


def run_sync(awaitable: Any) -> Any:
    """Run a coroutine from sync doctor/CLI. If a loop is already running, use a worker thread."""
    if not inspect.isawaitable(awaitable):
        return awaitable
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(awaitable)
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=_worker, name="librenms-sync")
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _maybe(value: Any) -> Any:
    return await maybe_await(value)


def _timeout() -> httpx.Timeout:
    return http_timeout()


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if not configured():
        raise LibreNmsError(NOT_CONFIGURED_WHY)
    remaining = backoff_remaining()
    if remaining > 0:
        raise LibreNmsUnavailable(f"LibreNMS backoff {_state.get('down_why') or ''} ({int(remaining)}s)".strip())
    url = api_root(settings.librenms_url) + path

    async def _do(owned: httpx.AsyncClient) -> httpx.Response:
        try:
            return await owned.request(method, url, headers=_headers(), json=json_body)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
            why = _short(exc)
            _mark_down(why)
            raise LibreNmsUnavailable(why) from exc

    if client is None:
        async with httpx.AsyncClient(timeout=http_timeout(), follow_redirects=True) as owned:
            response = await _do(owned)
    else:
        response = await _do(client)
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


async def _call(method: str, path: str, *, json_body: dict | None = None, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    try:
        result = _request(method, path, json_body=json_body, client=client)
    except TypeError:
        result = _request(method, path, json_body=json_body)
    return await _maybe(result)


async def list_devices(*, disabled: str = "", client: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    path = "/devices"
    if disabled in {"0", "1"}:
        path += f"?type=disabled&query={disabled}"
    payload = await _call("GET", path, client=client)
    rows = payload.get("devices") or []
    return [row for row in rows if isinstance(row, dict)]


def _host_path(hostname: str) -> str:
    """Keep IPs and FQDNs readable; encode spaces and slashes only."""
    host = str(hostname or "").strip()
    if not host:
        raise LibreNmsError("Need a LibreNMS hostname")
    return quote(host, safe=".-_:")


async def get_device(hostname: str, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    host = _host_path(hostname)
    payload = await _call("GET", f"/devices/{host}", client=client)
    rows = payload.get("devices") or []
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    if isinstance(payload.get("devices"), dict):
        return payload["devices"]
    raise LibreNmsError("LibreNMS returned no device")


async def get_ports(hostname: str, *, client: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    host = _host_path(hostname)
    payload = await _call(
        "GET",
        f"/devices/{host}/ports?columns=ifIndex,ifName,ifDescr,ifOperStatus,ifAdminStatus,ifSpeed",
        client=client,
    )
    rows = payload.get("ports") or []
    return [row for row in rows if isinstance(row, dict)]


async def get_health(hostname: str, kind: str, *, client: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    host = _host_path(hostname)
    safe_kind = quote(str(kind or "").strip(), safe=".-_")
    if not safe_kind:
        return []
    try:
        payload = await _call("GET", f"/devices/{host}/health/{safe_kind}", client=client)
    except LibreNmsError:
        return []
    graphs = payload.get("graphs") or payload.get("health") or payload.get(kind) or []
    if isinstance(graphs, dict):
        graphs = [graphs]
    return [row for row in graphs if isinstance(row, dict)]


async def add_device(body: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    return await _call("POST", "/devices", json_body=body, client=client)


async def set_disabled(hostname: str, disabled: bool, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    host = _host_path(hostname)
    return await _call(
        "PATCH",
        f"/devices/{host}",
        json_body={"field": ["disabled"], "data": ["1" if disabled else "0"]},
        client=client,
    )


async def delete_device(hostname: str, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    host = _host_path(hostname)
    return await _call("DELETE", f"/devices/{host}", client=client)


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


def _as_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return [value]
    return []


async def discover_device(
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
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Add (or fetch) a device, read ports/health in parallel, then disable polling."""
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

    async def _run(owned: httpx.AsyncClient) -> dict[str, Any]:
        added = False
        try:
            payload = await _maybe(add_device(body, client=owned))
            device = _first_device(payload)
            if not device:
                device = await _maybe(get_device(host, client=owned))
            added = True
        except LibreNmsError as exc:
            text = str(exc).lower()
            if "already" in text or "duplicate" in text or "exist" in text:
                device = await _maybe(get_device(host, client=owned))
            else:
                raise
        hostname = str(device.get("hostname") or host)
        ports_r, proc_r, mem_r, stor_r, temp_r = await asyncio.gather(
            _maybe(get_ports(hostname, client=owned)),
            _maybe(get_health(hostname, "processor", client=owned)),
            _maybe(get_health(hostname, "mempool", client=owned)),
            _maybe(get_health(hostname, "storage", client=owned)),
            _maybe(get_health(hostname, "temperature", client=owned)),
            return_exceptions=True,
        )
        ports = _as_rows(ports_r) if not isinstance(ports_r, BaseException) else []
        processors = _as_rows(proc_r) if not isinstance(proc_r, BaseException) else []
        mempools = _as_rows(mem_r) if not isinstance(mem_r, BaseException) else []
        storage = _as_rows(stor_r) if not isinstance(stor_r, BaseException) else []
        temperatures = _as_rows(temp_r) if not isinstance(temp_r, BaseException) else []
        if disable_polling:
            try:
                await _maybe(set_disabled(hostname, True, client=owned))
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

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient(timeout=http_timeout(), follow_redirects=True) as owned:
        return await _run(owned)


async def get_devices_many(hostnames: list[str], *, client: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    """Parallel GET /devices/{host}. A down host is a row with ``error``, never a raised hang."""
    hosts = [str(name or "").strip() for name in hostnames if str(name or "").strip()]

    async def _one(owned: httpx.AsyncClient, host: str) -> dict[str, Any]:
        try:
            return await _maybe(get_device(host, client=owned))
        except LibreNmsError as exc:
            return {"hostname": host, "error": str(exc)[:300]}

    async def _all(owned: httpx.AsyncClient) -> list[dict[str, Any]]:
        if not hosts:
            return []
        return list(await asyncio.gather(*[_one(owned, host) for host in hosts]))

    if client is not None:
        return await _all(client)
    async with httpx.AsyncClient(timeout=http_timeout(), follow_redirects=True) as owned:
        return await _all(owned)


async def discover_devices(items: list[dict[str, Any]], *, disable_polling: bool = True) -> list[dict[str, Any]]:
    """Identify several hosts at once (one AsyncClient, gather)."""

    async def _one(owned: httpx.AsyncClient, item: dict[str, Any]) -> dict[str, Any]:
        host = str(item.get("ip") or item.get("hostname") or "").strip()
        try:
            return await discover_device(
                host,
                version=str(item.get("version") or "v2c"),
                community=str(item.get("community") or ""),
                port=int(item.get("port") or 161),
                v3_user=str(item.get("v3_user") or ""),
                v3_level=str(item.get("v3_level") or ""),
                v3_auth_proto=str(item.get("v3_auth_proto") or ""),
                v3_auth_pass=str(item.get("v3_auth_pass") or ""),
                v3_priv_proto=str(item.get("v3_priv_proto") or ""),
                v3_priv_pass=str(item.get("v3_priv_pass") or ""),
                disable_polling=disable_polling,
                client=owned,
            )
        except LibreNmsError as exc:
            return {"ok": False, "hostname": host, "error": str(exc)[:300]}

    async with httpx.AsyncClient(timeout=http_timeout(), follow_redirects=True) as owned:
        if not items:
            return []
        return list(await asyncio.gather(*[_one(owned, item) for item in items]))


async def status_async() -> dict[str, Any]:
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
    row = await _probe_status(url, token)
    with _lock:
        _state["status"] = row
        _state["status_at"] = now
        _state["status_key"] = key
    return dict(row)


def status() -> dict[str, Any]:
    return run_sync(status_async())


async def _probe_status(url: str, token: str) -> dict[str, Any]:
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(min(float(settings.librenms_timeout), 4.0), connect=CONNECT_TIMEOUT), follow_redirects=True) as client:
            probe = await client.get(login)
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
        devices = await list_devices()
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


# --- webhook payload ---------------------------------------------------------

_RECOVERY_WORDS = {"resolved", "ok", "recovery", "recovered", "0"}
_SEVERITY = {
    "critical": "CRITICAL",
    "crit": "CRITICAL",
    "error": "CRITICAL",
    "major": "CRITICAL",
    "warning": "WARNING",
    "warn": "WARNING",
    "minor": "WARNING",
    "info": "INFO",
    "ok": "INFO",
    "1": "CRITICAL",
    "2": "CRITICAL",
    "3": "WARNING",
    "4": "WARNING",
    "5": "INFO",
}


def _norm_key(key: Any) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key or "").strip())
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _clean(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if text in {"*UNKNOWN*", "UNKNOWN", "{{ $hostname }}"}:
        return ""
    return text


def _pick(data: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = data.get(key, "")
        if value:
            return value
    return ""


def librenms_fingerprint(alert_id: str, host: str, alertname: str = "") -> str:
    ident = alert_id or re.sub(r"\s+", "-", (alertname or "alert").strip())[:96]
    return f"librenms:{ident}:{host}"[:255]


def parse_webhook(payload: Any) -> dict[str, Any]:
    """LibreNMS API/alert transport JSON → one Alertmanager-shaped alert.

    Accepts the usual LibreNMS keys (hostname, ip, title, rule, state, severity).
    state 0 / recovered → resolved; anything else stays firing.
    """
    if not isinstance(payload, dict):
        raise ValueError("LibreNMS webhook body must be a JSON object")
    data = {_norm_key(key): _clean(value) for key, value in payload.items() if not isinstance(value, (dict, list))}
    alertname = _pick(data, "title", "rule", "name", "alertname", "msg")
    host = _pick(data, "hostname", "sys_name", "sysname", "display", "host")
    ip = _pick(data, "ip", "ipv4", "address")
    if not alertname:
        raise ValueError("missing title / rule (LibreNMS $TITLE or $RULE)")
    if not (host or ip):
        raise ValueError("missing hostname or ip (LibreNMS $HOSTNAME / $IP)")
    host = host or ip
    state = _pick(data, "state", "status", "alerted").lower()
    recovery = state in _RECOVERY_WORDS
    raw_severity = _pick(data, "severity", "alert_severity")
    severity = _SEVERITY.get(raw_severity.lower(), "WARNING")
    labels = {
        "alertname": alertname,
        "asset": host,
        "instance": ip or host,
        "severity": severity,
        "source": "librenms",
        "monitor": "librenms",
    }
    if ip:
        labels["ip"] = ip
    device_id = _pick(data, "device_id", "deviceid")
    if device_id:
        labels["librenms_device_id"] = device_id
    alert_id = _pick(data, "id", "uid", "alert_id", "alertid")
    if alert_id:
        labels["librenms_alert_id"] = alert_id
    summary = _pick(data, "title", "name") or alertname
    description = _pick(data, "msg", "message", "description", "faults", "rule")
    starts = _pick(data, "timestamp", "time", "date", "started")
    return {
        "status": "resolved" if recovery else "firing",
        "labels": labels,
        "annotations": {"summary": summary, "description": description},
        **({"startsAt": starts} if starts else {}),
        "forge": {"fingerprint": librenms_fingerprint(alert_id, host, alertname), "source": "librenms"},
    }
