"""NetBox client. Read-only by default. Never used by AI.

Bundled instance is http://127.0.0.1:8001 unless inventory.netbox.url points
at an external NetBox (--netbox-url).

Core authenticates with NETBOX_API_TOKEN (Authorization: Token …). NetBox's
REST API returns HTTP 403 (not 401) when the token is missing, unknown, or
not allowed to read DCIM. The bundled container upserts that token on every
start as write_enabled=False.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

_MDN_HELP = "For more information check:"


def format_client_error(exc: BaseException) -> str:
    """One-line operator text. Never include httpx's MDN status-code dump."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = int(exc.response.status_code)
        url = str(exc.request.url) if exc.request is not None else ""
        path = url.split("?", 1)[0]
        if code == 403:
            return (
                "HTTP 403 Forbidden on NetBox devices API "
                "(token missing, not created in NetBox, or not allowed to read). "
                f"{path}"
            ).strip()
        return f"HTTP {code} from NetBox {path}".strip()
    text = str(exc).strip()
    if _MDN_HELP in text:
        text = text.split(_MDN_HELP, 1)[0].strip()
    text = text.replace("https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403", "")
    return text.strip()[:400]


def netbox_status(url: str, token: str, timeout: float = 5.0) -> dict[str, Any]:
    if not url:
        return {"ok": False, "why": "NetBox URL missing"}
    base = url.rstrip("/") + "/"
    try:
        headers = _headers(token) if token else {"Accept": "text/html,application/json"}
        with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
            api_code = 0
            if token:
                response = client.get(urljoin(base, "api/status/"))
                api_code = int(response.status_code)
                if response.status_code >= 400:
                    response = client.get(urljoin(base, "api/dcim/devices/?limit=1"))
                    api_code = int(response.status_code)
                if response.status_code < 400:
                    return {"ok": True}
            login = client.get(urljoin(base, "login/"))
            if login.status_code < 400:
                if not token:
                    return {
                        "ok": False,
                        "degraded": True,
                        "why": "NetBox UI answers but NETBOX_API_TOKEN is empty",
                    }
                if api_code == 403:
                    return {
                        "ok": False,
                        "degraded": True,
                        "why": (
                            "NetBox UI up; API HTTP 403 (token missing, not created "
                            "in NetBox, or not allowed to read devices)"
                        ),
                    }
                return {
                    "ok": False,
                    "degraded": True,
                    "why": f"NetBox UI up; API returned HTTP {api_code}",
                }
            return {"ok": False, "why": f"NetBox HTTP {login.status_code}"}
    except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
        return {
            "ok": False,
            "starting": True,
            "why": (
                "NetBox is not answering yet (first boot runs database migrations; "
                f"wait a few minutes): {exc}"
            ),
        }
    except Exception as exc:
        return {"ok": False, "why": format_client_error(exc)}


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
                raise RuntimeError(format_client_error(exc)) from exc
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


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
