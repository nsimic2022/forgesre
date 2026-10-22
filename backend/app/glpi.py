"""GLPI REST client. Read-only. Never used by AI. No SQLAlchemy here.

ForgeSRE only GETs initSession, Computer, NetworkEquipment, IPAddress, and
killSession. POST / PUT / PATCH / DELETE on inventory items are refused before
a request is built.

A GLPI outage must not slow Core:

- every call uses settings.glpi_timeout (clamped to 2–5 s);
- a connect/timeout/5xx failure opens a backoff window (BACKOFF_SECONDS);
- the status probe behind Discovery / Health is cached (OK_TTL / FAIL_TTL).

Auth: App-Token (API client) + user_token (remote access key). Session token
is requested per sync and never logged.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import urljoin

import httpx

log = logging.getLogger("forgesre")

BACKOFF_SECONDS = 120.0
OK_TTL = 60.0
FAIL_TTL = BACKOFF_SECONDS
PAGE = 200
MAX_PAGES = 10
ITEMTYPES = ("Computer", "NetworkEquipment")
ALLOWED_GET = frozenset(
    {
        "initSession",
        "killSession",
        "Computer",
        "NetworkEquipment",
        "IPAddress",
        "getFullSession",
    }
)
NOT_CONFIGURED_WHY = (
    "Not configured. Put GLPI_URL, GLPI_APP_TOKEN and GLPI_USER_TOKEN in "
    "secrets/secrets.env, then ./forgesre update."
)

_lock = threading.Lock()
_state: dict[str, Any] = {
    "down_until": 0.0,
    "down_why": "",
    "status": None,
    "status_at": 0.0,
    "status_key": "",
}


class GlpiError(RuntimeError):
    """GLPI answered, but with an error (bad token, no permission, bad params)."""


class GlpiUnavailable(GlpiError):
    """GLPI did not answer (connect error, timeout, 5xx) or Core is backing off."""


def reset_state() -> None:
    """Forget backoff and status caches (tests, Run doctor)."""
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
    if base.endswith("/apirest.php"):
        return base
    return base + "/apirest.php"


def token_presence(app_token: str, user_token: str) -> str:
    return "yes" if (app_token or "").strip() and (user_token or "").strip() else "no"


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
    return text[:300]


def _status_row(
    *,
    configured: bool = True,
    ok: bool = False,
    down: bool = False,
    light: str = "",
    label: str = "",
    why: str = "",
    hosts: int = 0,
) -> dict[str, Any]:
    if not configured:
        light = light or "grey"
        label = label or "Not configured"
        why = why or NOT_CONFIGURED_WHY
    elif down:
        light = light or "grey"
        label = label or "Unreachable"
    elif not light:
        light = "green" if ok else "grey"
        label = label or ("Connected" if ok else "API error")
    return {
        "configured": configured,
        "ok": ok,
        "down": down,
        "light": light,
        "label": label,
        "why": why,
        "hosts": hosts,
    }


def _auth_headers(app_token: str, user_token: str, session: str = "") -> dict[str, str]:
    headers = {
        "App-Token": (app_token or "").strip(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if session:
        headers["Session-Token"] = session
    else:
        headers["Authorization"] = f"user_token {(user_token or '').strip()}"
    return headers


def _get(
    path: str,
    *,
    url: str,
    app_token: str,
    user_token: str = "",
    session: str = "",
    timeout: float,
    params: dict[str, Any] | None = None,
    force: bool = False,
) -> Any:
    root = api_root(url)
    leaf = path.strip().lstrip("/")
    head = leaf.split("?", 1)[0]
    if head not in ALLOWED_GET:
        raise GlpiError(f"ForgeSRE is read-only toward GLPI (refused {leaf})")
    if not root:
        raise GlpiError("GLPI_URL is empty")
    if not force and backoff_remaining() > 0:
        raise GlpiUnavailable(str(_state["down_why"] or "GLPI unreachable"))
    target = urljoin(root.rstrip("/") + "/", leaf)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(
                target,
                headers=_auth_headers(app_token, user_token, session),
                params=params,
            )
    except httpx.TimeoutException as exc:
        why = f"GLPI timeout after {timeout:.0f}s"
        _mark_down(why)
        raise GlpiUnavailable(why) from exc
    except httpx.HTTPError as exc:
        why = f"GLPI unreachable: {_short(exc)}"
        _mark_down(why)
        raise GlpiUnavailable(why) from exc
    if response.status_code >= 500:
        why = f"GLPI HTTP {response.status_code}"
        _mark_down(why)
        raise GlpiUnavailable(why)
    if response.status_code >= 400:
        raise GlpiError(f"GLPI HTTP {response.status_code}: {_body_error(response)}")
    _mark_up()
    if not response.content:
        return []
    try:
        return response.json()
    except ValueError as exc:
        raise GlpiError("GLPI returned non-JSON") from exc


def _body_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:200]
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)[:200]
    return str(payload)[:200]


def init_session(url: str, app_token: str, user_token: str, *, timeout: float, force: bool = False) -> str:
    payload = _get(
        "initSession",
        url=url,
        app_token=app_token,
        user_token=user_token,
        timeout=timeout,
        force=force,
    )
    token = ""
    if isinstance(payload, dict):
        token = str(payload.get("session_token") or "").strip()
    if not token:
        raise GlpiError("GLPI initSession did not return a session_token")
    return token


def kill_session(url: str, app_token: str, session: str, *, timeout: float) -> None:
    if not session:
        return
    try:
        _get(
            "killSession",
            url=url,
            app_token=app_token,
            session=session,
            timeout=timeout,
            force=True,
        )
    except GlpiError:
        log.debug("GLPI killSession skipped", exc_info=True)


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if payload.get("id") is not None:
            return [payload]
    return []


def _name_of(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("completename") or value.get("name") or "").strip()
    text = str(value or "").strip()
    if text.isdigit():
        return ""
    return text


def _paginate(
    itemtype: str,
    *,
    url: str,
    app_token: str,
    session: str,
    timeout: float,
    extra: dict[str, Any] | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        start = page * PAGE
        params = {"range": f"{start}-{start + PAGE - 1}", "expand_dropdowns": "true"}
        if extra:
            params.update(extra)
        try:
            chunk = _as_rows(
                _get(
                    itemtype,
                    url=url,
                    app_token=app_token,
                    session=session,
                    timeout=timeout,
                    params=params,
                    force=force,
                )
            )
        except GlpiError:
            if itemtype == "IPAddress" and page == 0:
                return []
            raise
        rows.extend(chunk)
        if len(chunk) < PAGE:
            break
    return rows


def _ip_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for row in rows:
        ip = str(row.get("name") or row.get("ip") or "").strip()
        if not ip or ip.startswith("127.") or ip in {"::1", "0.0.0.0"}:
            continue
        itemtype = str(row.get("mainitemtype") or row.get("itemtype") or "").strip()
        item_id = str(row.get("mainitems_id") or row.get("items_id") or "").strip()
        if itemtype and item_id and (itemtype, item_id) not in out:
            out[(itemtype, item_id)] = ip
    return out


def _normalize(row: dict[str, Any], itemtype: str, ips: dict[tuple[str, str], str]) -> dict[str, Any] | None:
    item_id = str(row.get("id") or "").strip()
    name = str(row.get("name") or "").strip()
    if not item_id and not name:
        return None
    ip = ips.get((itemtype, item_id), "")
    location = _name_of(row.get("locations_id"))
    entity = _name_of(row.get("entities_id"))
    status = _name_of(row.get("states_id")).lower()
    offline = any(word in status for word in ("offline", "old", "trash", "inventory"))
    return {
        "glpi_id": f"{itemtype}:{item_id}" if item_id else "",
        "itemtype": itemtype,
        "name": name,
        "ip": ip,
        "serial": str(row.get("serial") or row.get("otherserial") or "").strip(),
        "location": location,
        "entity": entity,
        "comment": str(row.get("comment") or "").strip(),
        "offline": offline,
    }


def list_inventory(
    url: str,
    app_token: str,
    user_token: str,
    *,
    timeout: float,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Computer + NetworkEquipment, first IPv4 from IPAddress when GLPI exposes it."""
    session = init_session(url, app_token, user_token, timeout=timeout, force=force)
    try:
        ips = _ip_index(
            _paginate("IPAddress", url=url, app_token=app_token, session=session, timeout=timeout, force=force)
        )
        found: list[dict[str, Any]] = []
        for itemtype in ITEMTYPES:
            for row in _paginate(
                itemtype, url=url, app_token=app_token, session=session, timeout=timeout, force=force
            ):
                item = _normalize(row, itemtype, ips)
                if item:
                    found.append(item)
        return found
    finally:
        kill_session(url, app_token, session, timeout=timeout)


def _visible_count(url: str, app_token: str, user_token: str, *, timeout: float) -> int:
    """Cheap probe: session + one Computer and one NetworkEquipment row. Not a full sync."""
    session = init_session(url, app_token, user_token, timeout=timeout, force=True)
    try:
        found = 0
        for itemtype in ITEMTYPES:
            rows = _as_rows(
                _get(
                    itemtype,
                    url=url,
                    app_token=app_token,
                    session=session,
                    timeout=timeout,
                    params={"range": "0-0"},
                    force=True,
                )
            )
            found += len(rows)
        return found
    finally:
        kill_session(url, app_token, session, timeout=timeout)


def glpi_status(
    url: str,
    app_token: str,
    user_token: str,
    *,
    timeout: float,
    force: bool = False,
) -> dict[str, Any]:
    """Cached probe for Discovery / Health. initSession + Computer count, never writes.

    green  Connected — session opened and at least one Computer / NetworkEquipment is readable
    yellow No hosts visible — tokens work, but the account sees 0 items
    grey   Not configured / Unreachable / API error
    """
    if not (url or "").strip() or not (app_token or "").strip() or not (user_token or "").strip():
        return _status_row(configured=False)
    key = api_root(url)
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
        row = _status_row(down=True, why=str(_state["down_why"] or "GLPI unreachable"))
    else:
        try:
            count = _visible_count(url, app_token, user_token, timeout=timeout)
            if count >= 1:
                row = _status_row(
                    ok=True,
                    light="green",
                    label="Connected",
                    hosts=count,
                    why=f"GLPI API answered; {count} Computer / NetworkEquipment readable.",
                )
            else:
                row = _status_row(
                    ok=True,
                    light="yellow",
                    label="No hosts visible",
                    why="Tokens work but see 0 Computer / NetworkEquipment. Give the API user Read on those itemtypes.",
                )
        except GlpiUnavailable as exc:
            row = _status_row(down=True, why=str(exc))
        except GlpiError as exc:
            row = _status_row(label="API error", why=str(exc))
        except Exception as exc:
            row = _status_row(label="API error", why=f"GLPI probe failed: {_short(exc)}")
    with _lock:
        _state["status"] = dict(row)
        _state["status_at"] = time.monotonic()
        _state["status_key"] = key
    return row
