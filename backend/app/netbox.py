"""Optional external NetBox client. Read-only by default. Never used by AI."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx


def netbox_status(url: str, token: str, timeout: float = 5.0) -> dict[str, Any]:
    if not url or not token:
        return {"ok": False, "why": "NetBox URL or token missing"}
    try:
        with httpx.Client(timeout=timeout, headers=_headers(token)) as client:
            response = client.get(urljoin(url.rstrip("/") + "/", "api/status/"))
            if response.status_code >= 400:
                response = client.get(urljoin(url.rstrip("/") + "/", "api/dcim/devices/?limit=1"))
            response.raise_for_status()
        return {"ok": True}
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
