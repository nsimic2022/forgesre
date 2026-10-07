"""Zabbix JSON-RPC client. Read-only. Never used by AI. No SQLAlchemy here.

ForgeSRE only ever calls the ``*.get`` methods in READ_METHODS (plus the
unauthenticated ``apiinfo.version``). Anything else — ``*.create``,
``*.update``, ``*.delete``, ``event.acknowledge`` — is refused before a
request is built, so a typo cannot write to Zabbix.

A Zabbix outage must not slow Core down:

- every call uses settings.zabbix_timeout (clamped to 2–5 s);
- a connect/timeout/5xx failure opens a backoff window (BACKOFF_SECONDS) in
  which every caller fails fast without touching the network — no retry storm;
- the status probe behind Discovery / Health is cached (OK_TTL / FAIL_TTL), so
  page loads never fan out into API calls.

Auth: Zabbix 6.4+ takes ``Authorization: Bearer <token>``; older servers want
the token in the JSON-RPC ``auth`` field. The server version (``apiinfo.version``)
decides. The token is never logged or returned.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("forgesre")

READ_METHODS = frozenset(
    {
        "apiinfo.version",
        "host.get",
        "problem.get",
        "item.get",
        "trend.get",
    }
)
BACKOFF_SECONDS = 120.0
OK_TTL = 60.0
FAIL_TTL = BACKOFF_SECONDS
TREND_TTL = 300.0
TREND_FAIL_TTL = 120.0
TREND_HOURS = 24

NOT_CONFIGURED_WHY = (
    "Not configured. Put ZABBIX_URL and ZABBIX_API_TOKEN in secrets/secrets.env, then ./forgesre update."
)


class ZabbixError(RuntimeError):
    """Zabbix answered, but with an error (bad token, no permission, bad params)."""


class ZabbixUnavailable(ZabbixError):
    """Zabbix did not answer (connect error, timeout, 5xx) or Core is backing off."""


_lock = threading.Lock()
_state: dict[str, Any] = {
    "down_until": 0.0,
    "down_why": "",
    "versions": {},
    "status": None,
    "status_at": 0.0,
    "status_key": "",
    "trends": {},
}
_rpc_id = 0


def reset_state() -> None:
    """Forget backoff, version, status and trend caches (tests, Run doctor)."""
    with _lock:
        _state["down_until"] = 0.0
        _state["down_why"] = ""
        _state["versions"] = {}
        _state["status"] = None
        _state["status_at"] = 0.0
        _state["status_key"] = ""
        _state["trends"] = {}


def api_endpoint(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("api_jsonrpc.php"):
        return base
    return base + "/api_jsonrpc.php"


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
    text = str(exc).strip() or type(exc).__name__
    if "For more information check:" in text:
        text = text.split("For more information check:", 1)[0].strip()
    return text[:300]


def parse_version(raw: str) -> tuple[int, int]:
    match = re.match(r"\s*(\d+)\.(\d+)", str(raw or ""))
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def _post(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
    endpoint = api_endpoint(url)
    if not endpoint:
        raise ZabbixError("ZABBIX_URL is empty")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(endpoint, json=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
        why = f"Zabbix unreachable: {_short(exc)}"
        _mark_down(why)
        raise ZabbixUnavailable(why) from exc
    if response.status_code >= 500:
        why = f"Zabbix HTTP {response.status_code} from {endpoint}"
        _mark_down(why)
        raise ZabbixUnavailable(why)
    if response.status_code >= 400:
        raise ZabbixError(f"Zabbix HTTP {response.status_code} from {endpoint} (check ZABBIX_URL)")
    try:
        data = response.json()
    except ValueError as exc:
        raise ZabbixError(f"Zabbix answered non-JSON at {endpoint} (check ZABBIX_URL)") from exc
    _mark_up()
    if isinstance(data, dict) and data.get("error"):
        err = data.get("error") or {}
        message = str(err.get("message") or "error").strip()
        detail = str(err.get("data") or "").strip()
        raise ZabbixError(f"Zabbix API: {message} {detail}".strip()[:400])
    return data.get("result") if isinstance(data, dict) else None


def server_version(url: str, timeout: float, *, force: bool = False) -> tuple[int, int]:
    cached = _state["versions"].get(url)
    if cached is not None:
        return cached
    if not force and backoff_remaining() > 0:
        raise ZabbixUnavailable(str(_state["down_why"] or "Zabbix unreachable (backing off)"))
    global _rpc_id
    _rpc_id += 1
    raw = _post(
        url,
        {"jsonrpc": "2.0", "method": "apiinfo.version", "params": {}, "id": _rpc_id},
        {"Content-Type": "application/json-rpc"},
        timeout,
    )
    version = parse_version(str(raw or ""))
    with _lock:
        _state["versions"][url] = version
    return version


def call(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    url: str,
    token: str,
    timeout: float,
    force: bool = False,
) -> Any:
    """One read-only JSON-RPC call. Refuses anything not in READ_METHODS."""
    if method not in READ_METHODS:
        raise ZabbixError(f"ForgeSRE is read-only toward Zabbix; refused {method}")
    if not (url or "").strip():
        raise ZabbixError("ZABBIX_URL is empty")
    if not (token or "").strip():
        raise ZabbixError("ZABBIX_API_TOKEN is empty")
    if not force and backoff_remaining() > 0:
        raise ZabbixUnavailable(str(_state["down_why"] or "Zabbix unreachable (backing off)"))
    version = server_version(url, timeout, force=force)
    global _rpc_id
    _rpc_id += 1
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": _rpc_id}
    headers = {"Content-Type": "application/json-rpc"}
    if version >= (6, 4):
        headers["Authorization"] = f"Bearer {token.strip()}"
    else:
        body["auth"] = token.strip()
    return _post(url, body, headers, timeout)


def _status_row(**fields: Any) -> dict[str, Any]:
    row = {
        "configured": True,
        "ok": False,
        "light": "grey",
        "label": "Unreachable",
        "why": "",
        "version": "",
        "hosts": 0,
        "down": False,
    }
    row.update(fields)
    return row


def zabbix_status(url: str, token: str, *, timeout: float, force: bool = False) -> dict[str, Any]:
    """Cached probe for Discovery / Health. apiinfo.version + host.get countOutput, never writes.

    green  Connected — API answered and the token sees ≥1 host
    yellow No hosts visible — token works, but the user has no Read on any host group
    grey   Not configured / Unreachable / API error
    """
    if not (url or "").strip() or not (token or "").strip():
        return _status_row(configured=False, label="Not configured", why=NOT_CONFIGURED_WHY)
    key = api_endpoint(url)
    now = time.monotonic()
    with _lock:
        cached = _state["status"]
        age = now - float(_state["status_at"] or 0)
        same = _state["status_key"] == key
    if not force and cached is not None and same:
        ttl = OK_TTL if cached.get("ok") else FAIL_TTL
        if age < ttl:
            return dict(cached)
    if not force and backoff_remaining() > 0:
        row = _status_row(down=True, why=str(_state["down_why"] or "Zabbix unreachable"))
    else:
        try:
            version = server_version(url, timeout, force=True)
            count = call("host.get", {"countOutput": True}, url=url, token=token, timeout=timeout, force=True)
            hosts = int(str(count or "0").strip() or 0)
            label_version = f"{version[0]}.{version[1]}" if version != (0, 0) else ""
            if hosts >= 1:
                head = f"Zabbix {label_version}" if label_version else "Zabbix"
                row = _status_row(
                    ok=True,
                    light="green",
                    label="Connected",
                    version=label_version,
                    hosts=hosts,
                    why=f"{head} API answered; {hosts} host(s) readable.",
                )
            else:
                row = _status_row(
                    ok=True,
                    light="yellow",
                    label="No hosts visible",
                    version=label_version,
                    why="Token works but sees 0 hosts. Give the forgesre-ro user group Read on the host groups to import.",
                )
        except ZabbixUnavailable as exc:
            row = _status_row(down=True, why=str(exc))
        except ZabbixError as exc:
            row = _status_row(label="API error", why=str(exc))
        except Exception as exc:  # never break a page on a malformed answer
            row = _status_row(label="API error", why=f"Zabbix probe failed: {_short(exc)}")
    with _lock:
        _state["status"] = dict(row)
        _state["status_at"] = time.monotonic()
        _state["status_key"] = key
    return row


def agent_state(host: dict[str, Any]) -> str:
    """up / down / unknown from agent interface ``available`` (1/2/0) and host ``active_available``."""
    states: list[str] = []
    for iface in host.get("interfaces") or []:
        if not isinstance(iface, dict):
            continue
        if str(iface.get("type") or "1") != "1":
            continue
        states.append(str(iface.get("available") or "0"))
    active = str(host.get("active_available") or "")
    if active:
        states.append(active)
    if "1" in states:
        return "up"
    if "2" in states:
        return "down"
    return "unknown"


def _main_ip(host: dict[str, Any]) -> tuple[str, str]:
    """(ip, dns) of the main interface; agent first, then any interface."""
    interfaces = [row for row in (host.get("interfaces") or []) if isinstance(row, dict)]
    ordered = sorted(
        interfaces,
        key=lambda row: (
            0 if str(row.get("main") or "0") == "1" else 1,
            0 if str(row.get("type") or "1") == "1" else 1,
        ),
    )
    for row in ordered:
        ip = str(row.get("ip") or "").strip()
        if ip and ip not in {"0.0.0.0", "::"}:
            return ip, str(row.get("dns") or "").strip()
    for row in ordered:
        dns = str(row.get("dns") or "").strip()
        if dns:
            return "", dns
    return "", ""


def list_hosts(url: str, token: str, *, timeout: float, force: bool = False) -> list[dict[str, Any]]:
    """host.get with interfaces and groups. Returns plain dicts for sync_zabbix."""
    version = server_version(url, timeout, force=force)
    group_key = "hostgroups" if version >= (6, 2) else "groups"
    params: dict[str, Any] = {
        "output": "extend",
        "selectInterfaces": ["type", "main", "useip", "ip", "dns", "available"],
        "sortfield": "host",
    }
    params["selectHostGroups" if group_key == "hostgroups" else "selectGroups"] = ["name"]
    rows = call("host.get", params, url=url, token=token, timeout=timeout, force=force) or []
    hosts: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        hostid = str(row.get("hostid") or "").strip()
        technical = str(row.get("host") or "").strip()
        if not hostid or not technical:
            continue
        ip, dns = _main_ip(row)
        groups = [
            str(group.get("name") or "").strip()
            for group in (row.get(group_key) or row.get("groups") or [])
            if isinstance(group, dict) and str(group.get("name") or "").strip()
        ]
        hosts.append(
            {
                "hostid": hostid,
                "host": technical,
                "name": str(row.get("name") or technical).strip(),
                "ip": ip,
                "dns": dns,
                "groups": groups,
                "agent": agent_state(row),
                "monitored": str(row.get("status") or "0") == "0",
            }
        )
    return hosts


def active_problem_triggerids(url: str, token: str, triggerids: list[str], *, timeout: float) -> set[str]:
    """Trigger ids that still have an unresolved problem (problem.get, recent=false)."""
    wanted = sorted({str(item).strip() for item in triggerids if str(item or "").strip()})
    if not wanted:
        return set()
    rows = call(
        "problem.get",
        {"output": ["eventid", "objectid"], "source": 0, "object": 0, "objectids": wanted},
        url=url,
        token=token,
        timeout=timeout,
    ) or []
    return {str(row.get("objectid") or "") for row in rows if isinstance(row, dict)}


# --- webhook payload ---------------------------------------------------------

SEVERITY_MAP = {
    "disaster": "CRITICAL",
    "high": "CRITICAL",
    "average": "WARNING",
    "warning": "WARNING",
    "information": "INFO",
    "not classified": "INFO",
    "5": "CRITICAL",
    "4": "CRITICAL",
    "3": "WARNING",
    "2": "WARNING",
    "1": "INFO",
    "0": "INFO",
}
_MACRO_RE = re.compile(r"^\{[A-Z0-9_.]+(\:[^}]*)?\}$")
_RECOVERY_WORDS = {"resolved", "ok", "recovery", "recovered", "0"}


def _norm_key(key: Any) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key or "").strip())
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _clean(value: Any) -> str:
    """Strip; an unresolved Zabbix macro (literal {HOST.IP}) counts as empty."""
    text = str(value if value is not None else "").strip()
    if _MACRO_RE.match(text) or text in {"*UNKNOWN*", "UNKNOWN"}:
        return ""
    return text


def _pick(data: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = data.get(key, "")
        if value:
            return value
    return ""


def zabbix_fingerprint(triggerid: str, host: str, alertname: str = "") -> str:
    """zabbix:{triggerid}:{host} — stable across problem/recovery, not the raw trigger name."""
    ident = triggerid or re.sub(r"\s+", "-", (alertname or "trigger").strip())[:96]
    return f"zabbix:{ident}:{host}"[:255]


def parse_webhook(payload: Any) -> dict[str, Any]:
    """Zabbix webhook JSON → one Alertmanager-shaped alert for the shared ingest.

    Keys are case/punctuation-insensitive (``event_value``, ``EVENT.VALUE`` and
    ``eventValue`` are the same field). Raises ValueError when trigger name or
    host is missing so the media type test in Zabbix shows a clear 422.
    """
    if not isinstance(payload, dict):
        raise ValueError("Zabbix webhook body must be a JSON object")
    data = {_norm_key(key): _clean(value) for key, value in payload.items() if not isinstance(value, (dict, list))}
    alertname = _pick(data, "trigger_name", "event_name", "name", "alertname", "subject")
    host = _pick(data, "host_host", "host", "hostname", "host_name")
    ip = _pick(data, "host_ip", "ip", "host_conn")
    if not alertname:
        raise ValueError("missing trigger_name ({TRIGGER.NAME}) or event_name ({EVENT.NAME})")
    if not (host or ip):
        raise ValueError("missing host ({HOST.HOST}) or host_ip ({HOST.IP})")
    host = host or ip
    value = _pick(data, "event_value", "value")
    status = _pick(data, "event_status", "status", "event_recovery_status").lower()
    recovery = value == "0" or (not value and status in _RECOVERY_WORDS)
    triggerid = _pick(data, "trigger_id", "triggerid")
    raw_severity = _pick(data, "event_severity", "severity", "trigger_severity", "event_nseverity", "trigger_nseverity")
    severity = SEVERITY_MAP.get(raw_severity.lower(), "WARNING")
    labels = {
        "alertname": alertname,
        "asset": host,
        "instance": host,
        "severity": severity,
        "source": "zabbix",
    }
    for label, keys in (
        ("ip", ("host_ip", "ip", "host_conn")),
        ("zabbix_hostid", ("host_id", "hostid")),
        ("zabbix_triggerid", ("trigger_id", "triggerid")),
        ("zabbix_eventid", ("event_id", "eventid")),
        ("zabbix_severity", ("event_severity", "severity", "trigger_severity")),
        ("zabbix_host_name", ("host_name",)),
    ):
        found = _pick(data, *keys)
        if found:
            labels[label] = found
    summary = _pick(data, "event_name", "trigger_name") or alertname
    description = _pick(data, "event_opdata", "opdata", "message", "description", "trigger_description")
    return {
        "status": "resolved" if recovery else "firing",
        "labels": labels,
        "annotations": {"summary": summary, "description": description},
        "startsAt": _pick(data, "event_time", "event_date"),
        "forge": {"fingerprint": zabbix_fingerprint(triggerid, host, alertname), "source": "zabbix"},
    }


# --- trend.get graphs ---------------------------------------------------------

_CPU_KEYS = ("system.cpu.util",)
_MEM_KEYS = ("vm.memory.utilization", "vm.memory.util")


def _pick_items(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """cpu_percent / memory_percent / disk_percent → best matching numeric item."""
    numeric = [row for row in items if isinstance(row, dict) and str(row.get("value_type") or "0") in {"0", "3"}]
    out: dict[str, dict[str, Any]] = {}

    def first(pred) -> dict[str, Any] | None:
        for row in numeric:
            if pred(str(row.get("key_") or "")):
                return row
        return None

    cpu = first(lambda key: key in _CPU_KEYS) or first(lambda key: key.startswith("system.cpu.util[") and "idle" not in key)
    if cpu:
        out["cpu_percent"] = cpu
    mem = first(lambda key: key in _MEM_KEYS) or first(lambda key: key.startswith("vm.memory.size[pused"))
    if mem:
        out["memory_percent"] = mem
    disk = (
        first(lambda key: "vfs.fs" in key and "pused" in key and ("[/," in key or "[C:," in key.upper()))
        or first(lambda key: "vfs.fs" in key and "pused" in key)
    )
    if disk:
        out["disk_percent"] = disk
    return out


def _finite(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in {float("inf"), float("-inf")}:
        return None
    return value


def host_trends(
    url: str,
    token: str,
    hostid: str,
    *,
    timeout: float,
    hours: int = TREND_HOURS,
    now: float | None = None,
) -> dict[str, Any]:
    """Lazy CPU / memory / disk for one host: item.get, then trend.get (hourly avg, last ``hours``).

    Cached per hostid (TREND_TTL; failures FAIL_TTL) so the 30 s Dashboard refresh
    does not hit Zabbix every time. Never iterates all hosts.
    """
    hostid = str(hostid or "").strip()
    if not hostid:
        return {"ok": False, "error": "no zabbix_hostid", "tiles": {}}
    mono = time.monotonic()
    cached = _state["trends"].get(hostid)
    if cached is not None:
        at, result = cached
        ttl = TREND_TTL if result.get("ok") else TREND_FAIL_TTL
        if mono - at < ttl:
            return result
    try:
        items = call(
            "item.get",
            {
                "output": ["itemid", "key_", "name", "lastvalue", "lastclock", "value_type", "units"],
                "hostids": [hostid],
                "search": {"key_": ["system.cpu.util", "vm.memory.util", "vm.memory.size[pused", "pused"]},
                "searchByAny": True,
                "monitored": True,
                "sortfield": "key_",
            },
            url=url,
            token=token,
            timeout=timeout,
        ) or []
        picked = _pick_items(items)
        tiles: dict[str, dict[str, Any]] = {}
        if picked:
            clock = int(now if now is not None else time.time())
            ids = [str(row.get("itemid")) for row in picked.values()]
            trends = call(
                "trend.get",
                {
                    "output": ["itemid", "clock", "value_avg"],
                    "itemids": ids,
                    "time_from": clock - int(hours) * 3600,
                    "limit": 2000,
                },
                url=url,
                token=token,
                timeout=timeout,
            ) or []
            by_item: dict[str, list[tuple[int, float]]] = {}
            for row in trends:
                if not isinstance(row, dict):
                    continue
                value = _finite(row.get("value_avg"))
                if value is None:
                    continue
                by_item.setdefault(str(row.get("itemid") or ""), []).append((int(row.get("clock") or 0), value))
            for tile, item in picked.items():
                points = [value for _clock, value in sorted(by_item.get(str(item.get("itemid")), []))]
                last = _finite(item.get("lastvalue")) if str(item.get("lastclock") or "0") != "0" else None
                if last is None and points:
                    last = points[-1]
                tiles[tile] = {"value": last, "series": points, "key": str(item.get("key_") or "")}
        result: dict[str, Any] = {"ok": True, "error": "", "tiles": tiles, "hours": hours}
    except ZabbixError as exc:
        result = {"ok": False, "error": str(exc), "tiles": {}}
    except Exception as exc:
        result = {"ok": False, "error": f"Zabbix trends failed: {_short(exc)}", "tiles": {}}
    with _lock:
        _state["trends"][hostid] = (time.monotonic(), result)
    return result
