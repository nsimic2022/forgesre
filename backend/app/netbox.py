"""NetBox client. Read-only by default. Never used by AI.

Bundled instance is http://127.0.0.1:8001 unless inventory.netbox.url points
at an external NetBox (--netbox-url).

Core authenticates with NETBOX_API_TOKEN. NetBox 4.6 issues **v2** tokens in
the UI (``nbt_<12-char key>.<secret>``, shown once at create). REST wants
``Authorization: Bearer nbt_…``. A legacy 40-char **v1** secret still uses
``Authorization: Token <40char>``. Do not send Bearer v2 as Token, and do
not treat a v2 secret as v1 plaintext. Core is **not** a NetBox UI login
and not a second user. A token on a user without ``dcim.view_device`` /
is_superuser is HTTP 403 even when the secret is valid.

Prefer a UI v2 token in secrets. Launch skips v1 upsert when the secret
looks like v2. A 40-char v1 value is still upserted as fallback
(write_enabled=False, superuser + view permission). v1 is deprecated in
the NetBox UI; that is OK for the fallback path.

NetBox's REST API returns HTTP 403 (not 401) when the token is missing,
unknown, or not allowed to read DCIM. User-facing 403 copy must distinguish
those: empty token → missing; non-empty token → NetBox rejected it. Never
print the secret. A working v2 GET (HTTP 200) is yellow (0 devices) or
green (≥1) — do not tell the operator to recreate a v1 token.

Discovery traffic light uses GET /api/dcim/devices/?limit=1 (never writes):
- grey: UI down, API 403, or no token
- yellow: API 200 and count == 0 (empty NetBox is normal)
- green: API 200 and count >= 1
Empty must not look like 403. Admin Sync stays clickable on yellow and green.
Grey still allows a retry click when the UI answers (403 / no token).
Discovery shows a status chip (not a button): Not connected / API 403,
No devices, Connected — never the English color names Grey/Yellow/Green.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

_MDN_HELP = "For more information check:"


def is_local_netbox_url(url: str) -> bool:
    """True when Core is talking to bundled NetBox on this VM (not --netbox-url)."""
    host = (urlparse((url or "").strip()).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


FORBIDDEN_REJECTED_WHY = (
    "NetBox rejected the API token (HTTP 403). Recreate netbox+core after changing secrets.env."
)
FORBIDDEN_REJECTED_V2_WHY = (
    "NetBox rejected the API token (HTTP 403). Put the full v2 token shown once at create "
    "(nbt_…, not the 12-character key) in NETBOX_API_TOKEN, then recreate core."
)
FORBIDDEN_MISSING_WHY = (
    "HTTP 403 Forbidden on NetBox devices API "
    "(token missing, not created in NetBox, or not allowed to read)."
)
TOKEN_EMPTY_WHY = "NetBox UI answers but NETBOX_API_TOKEN is empty"
V2_PREFIX = "nbt_"


def token_presence(token: str) -> str:
    """yes/no only. Never the secret."""
    return "yes" if (token or "").strip() else "no"


def _bare_secret(token: str) -> str:
    """Strip an optional Bearer/Token scheme. Never log the value."""
    value = (token or "").strip()
    lower = value.lower()
    if lower.startswith("bearer ") or lower.startswith("token "):
        parts = value.split(None, 1)
        return parts[1].strip() if len(parts) == 2 else value
    return value


def looks_like_v2_token(token: str) -> bool:
    """True for NetBox v2 secrets (typically nbt_<key>.<secret>)."""
    return _bare_secret(token).lower().startswith(V2_PREFIX)


def forbidden_why(token: str) -> str:
    """403 copy: rejected if Core has a token, missing if it does not."""
    if not (token or "").strip():
        return FORBIDDEN_MISSING_WHY
    if looks_like_v2_token(token):
        return FORBIDDEN_REJECTED_V2_WHY
    return FORBIDDEN_REJECTED_WHY


def format_client_error(exc: BaseException, token: str = "") -> str:
    """Operator text. Never include httpx's MDN dump or the token value."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = int(exc.response.status_code)
        url = str(exc.request.url) if exc.request is not None else ""
        path = url.split("?", 1)[0]
        if code == 403:
            return forbidden_why(token)
        return f"HTTP {code} from NetBox {path}".strip()
    text = str(exc).strip()
    if _MDN_HELP in text:
        text = text.split(_MDN_HELP, 1)[0].strip()
    text = text.replace("https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403", "")
    return text.strip()[:400]


FIRST_BOOT_WHY = (
    "NetBox is still running first-boot migrations; wait until the API answers, then refresh."
)
EMPTY_DEVICES_WHY = "No devices yet; add in NetBox UI :8001 or use Assets/Discovery."


def _cta_why(status: dict[str, Any]) -> str:
    why = str(status.get("why") or "NetBox is not ready for sync.").strip()
    if "403" in why:
        if why and not why.endswith("."):
            why += "."
        return why
    first = why.split(". ", 1)[0].strip()
    if first and not first.endswith("."):
        first += "."
    return first


def _device_count(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    raw = payload.get("count")
    if isinstance(raw, int) and raw >= 0:
        return raw
    results = payload.get("results") or []
    return len(results) if isinstance(results, list) else 0


def _grey(*, why: str, count: int = 0, **extra: Any) -> dict[str, Any]:
    row = {"ok": False, "light": "grey", "count": count, "why": why}
    row.update(extra)
    return row


def _working(count: int) -> dict[str, Any]:
    if count >= 1:
        return {"ok": True, "ui_up": True, "light": "green", "count": count, "why": ""}
    return {
        "ok": True,
        "ui_up": True,
        "light": "yellow",
        "count": 0,
        "why": EMPTY_DEVICES_WHY,
    }


def _cta_light(status: dict[str, Any]) -> str:
    light = str(status.get("light") or "")
    if light:
        return light
    if status.get("ok"):
        return "green" if int(status.get("count") or 0) >= 1 else "yellow"
    return "grey"


def status_label(light: str, why: str = "") -> str:
    """Visible chip text. Never Grey/Yellow/Green — those look like a second button."""
    if light == "green":
        return "Connected"
    if light == "yellow":
        return "No devices"
    if "403" in (why or ""):
        return "API 403"
    return "Not connected"


def _cta_row(**fields: Any) -> dict[str, Any]:
    row = dict(fields)
    row["label"] = status_label(str(row.get("light") or "grey"), str(row.get("why") or ""))
    return row


def sync_cta(url: str, token: str, enabled: bool, timeout: float = 2.0) -> dict[str, Any]:
    """Discovery Sync NetBox control for admins.

    Traffic light from GET /api/dcim/devices/?limit=1.
    Clickable on yellow and green. Grey retry click when the UI answers
    (HTTP 403 / no token) — do not disable that submit. First-boot connect
    failures stay disabled with one sentence. Last journal sync is not a gate.
    The light is a status chip (label + CSS color), not a second button.
    """
    if not enabled:
        return _cta_row(
            ready=False,
            starting=False,
            clickable=False,
            light="grey",
            count=0,
            why="Sync is off in config.",
        )
    status = netbox_status(url, token, timeout=timeout)
    light = _cta_light(status)
    count = int(status.get("count") or 0)
    ui_up = bool(status.get("ok") or status.get("degraded") or status.get("ui_up"))
    if status.get("starting") and not ui_up:
        return _cta_row(
            ready=False,
            starting=True,
            clickable=False,
            light="grey",
            count=0,
            why=FIRST_BOOT_WHY,
        )
    if light == "green":
        return _cta_row(
            ready=True,
            starting=False,
            clickable=True,
            light="green",
            count=count,
            why="",
        )
    if light == "yellow":
        why = str(status.get("why") or EMPTY_DEVICES_WHY).strip() or EMPTY_DEVICES_WHY
        return _cta_row(
            ready=True,
            starting=False,
            clickable=True,
            light="yellow",
            count=0,
            why=why,
        )
    why = _cta_why(status)
    ready = bool(ui_up)
    return _cta_row(
        ready=ready,
        starting=False,
        clickable=ready,
        light="grey",
        count=0,
        why=why,
    )


def netbox_status(url: str, token: str, timeout: float = 5.0) -> dict[str, Any]:
    if not url:
        return _grey(why="NetBox URL missing")
    base = url.rstrip("/") + "/"
    try:
        headers = _headers(token) if token else {"Accept": "text/html,application/json"}
        with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
            api_code = 0
            if token:
                devices = client.get(urljoin(base, "api/dcim/devices/?limit=1"))
                api_code = int(devices.status_code)
                if devices.status_code < 400:
                    try:
                        payload = devices.json()
                    except ValueError:
                        payload = {}
                    return _working(_device_count(payload))
            login = client.get(urljoin(base, "login/"))
            if login.status_code < 400:
                if not token:
                    return _grey(
                        degraded=True,
                        ui_up=True,
                        why=TOKEN_EMPTY_WHY,
                    )
                if api_code == 403:
                    return _grey(
                        degraded=True,
                        ui_up=True,
                        why=forbidden_why(token),
                    )
                return _grey(
                    degraded=True,
                    ui_up=True,
                    why=f"NetBox UI up; API returned HTTP {api_code}",
                )
            if api_code and api_code < 500:
                return _grey(
                    degraded=True,
                    ui_up=True,
                    why=(
                        forbidden_why(token)
                        if api_code == 403
                        else f"NetBox UI up; API returned HTTP {api_code}"
                    ),
                )
            return _grey(why=f"NetBox HTTP {login.status_code}")
    except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
        return _grey(
            starting=True,
            why=(
                "NetBox is not answering yet (first boot runs database migrations; "
                f"wait a few minutes): {exc}"
            ),
        )
    except Exception as exc:
        return _grey(why=format_client_error(exc))


def list_devices(url: str, token: str, timeout: float = 10.0) -> list[dict[str, Any]]:
    if not (token or "").strip():
        raise RuntimeError("NETBOX_API_TOKEN is empty")
    devices: list[dict[str, Any]] = []
    endpoint = urljoin(url.rstrip("/") + "/", "api/dcim/devices/?limit=200")
    with httpx.Client(timeout=timeout, headers=_headers(token)) as client:
        while endpoint:
            response = client.get(endpoint)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(format_client_error(exc, token)) from exc
            payload = response.json()
            for row in payload.get("results") or []:
                primary = (row.get("primary_ip") or {}).get("address") or ""
                ip = primary.split("/")[0]
                devices.append(
                    {
                        "netbox_id": str(row.get("id") or ""),
                        "name": row.get("name") or f"nb-{row.get('id')}",
                        "ip": ip,
                        "type": ((row.get("device_type") or {}).get("model")) or "device",
                        "status": (row.get("status") or {}).get("value") or "active",
                    }
                )
            endpoint = payload.get("next")
    return devices


def _authorization(token: str) -> str:
    """NetBox 4.6: v2 → Bearer nbt_…; v1 40-char → Token <plaintext>."""
    value = (token or "").strip()
    bare = _bare_secret(value)
    if looks_like_v2_token(value):
        return f"Bearer {bare}"
    lower = value.lower()
    if lower.startswith("bearer ") or lower.startswith("token "):
        return value
    return f"Token {bare}"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": _authorization(token),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
