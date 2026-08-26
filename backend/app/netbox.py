"""NetBox client. Read-only by default. Never used by AI.

Bundled instance is http://127.0.0.1:8001 unless inventory.netbox.url points
at an external NetBox (--netbox-url).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx


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
        return {"ok": False, "why": str(exc)}


def list_devices(url: str, token: str, timeout: float = 10.0) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    endpoint = urljoin(url.rstrip("/") + "/", "api/dcim/devices/?limit=200")
    with httpx.Client(timeout=timeout, headers=_headers(token)) as client:
        while endpoint:
            response = client.get(endpoint)
            response.raise_for_status()
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
