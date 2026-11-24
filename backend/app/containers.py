"""Containers view: state and recent logs of the appliance's containers, read only.

Core never touches the Docker socket. The ``docker-ro`` service (docker-socket-proxy) holds it
read-only and lets through GET requests on containers only (POST=0), on 127.0.0.1. Here we list
the containers of this compose project and read the tail of one container's log, each call with
a short timeout. Log lines that look like passwords or tokens are masked before they leave.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

TIMEOUT = 5.0
MAX_TAIL = 1000
_SECRET = re.compile(
    r"(?i)((?:password|passwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*(?:bearer\s+|basic\s+)?)(\"?)[^\s\"',;&]+"
)


def base_url() -> str:
    return os.environ.get("FORGESRE_DOCKER_URL", "http://127.0.0.1:2377").rstrip("/")


def mask(line: str) -> str:
    """Hide values after password=, token:, Authorization: Bearer … and similar."""
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}•••", line)


def _client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    return (client, False) if client is not None else (httpx.Client(timeout=TIMEOUT), True)


def _row(c: dict[str, Any]) -> dict[str, Any]:
    labels = c.get("Labels") or {}
    status = str(c.get("Status") or "")
    health = "healthy" if "(healthy)" in status else "unhealthy" if "(unhealthy)" in status else "starting" if "health: starting" in status else ""
    name = ((c.get("Names") or ["?"])[0]).lstrip("/")
    return {
        "id": str(c.get("Id") or "")[:12],
        "name": name,
        "service": labels.get("com.docker.compose.service", name),
        "project": labels.get("com.docker.compose.project", ""),
        "image": c.get("Image", ""),
        "state": c.get("State", ""),
        "status": status,
        "health": health,
        "created": c.get("Created"),
    }


def list_containers(client: httpx.Client | None = None, project: str | None = None) -> list[dict[str, Any]]:
    """Containers of this compose project (all of them, running or not), by service name.

    The project is FORGESRE_COMPOSE_PROJECT, else the project of the ``core`` container. Raises
    httpx.HTTPError when docker-ro does not answer.
    """
    http, own = _client(client)
    try:
        r = http.get(f"{base_url()}/containers/json", params={"all": "1"})
        r.raise_for_status()
        rows = [_row(c) for c in r.json()]
    finally:
        if own:
            http.close()
    project = project if project is not None else os.environ.get("FORGESRE_COMPOSE_PROJECT", "")
    if not project:
        cores = [x["project"] for x in rows if x["service"] == "core" and x["project"]]
        project = cores[0] if cores else ""
    if project:
        rows = [x for x in rows if x["project"] == project]
    return sorted(rows, key=lambda x: (x["state"] == "running", x["service"]))


def _demux(raw: bytes) -> list[str]:
    """Docker's log stream without a TTY: 8-byte frame headers (stream, 0, 0, 0, size)."""
    if not raw or raw[0] not in (0, 1, 2) or raw[1:4] != b"\x00\x00\x00":
        return raw.decode("utf-8", "replace").splitlines()
    out, i = [], 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + size].decode("utf-8", "replace"))
        i += 8 + size
    return "".join(out).splitlines()


def logs(service: str, tail: int = 200, client: httpx.Client | None = None) -> dict[str, Any]:
    """The last ``tail`` lines (stdout and stderr, with timestamps) of one service's container, masked.

    Raises LookupError for an unknown service and httpx.HTTPError when docker-ro does not answer.
    """
    tail = max(10, min(int(tail), MAX_TAIL))
    http, own = _client(client)
    try:
        match = [c for c in list_containers(http) if c["service"] == service or c["name"] == service]
        if not match:
            raise LookupError(f"no container for service {service!r}")
        c = match[0]
        r = http.get(f"{base_url()}/containers/{c['id']}/logs", params={"stdout": "1", "stderr": "1", "timestamps": "1", "tail": str(tail)})
        r.raise_for_status()
        lines = [mask(x) for x in _demux(r.content)]
    finally:
        if own:
            http.close()
    return {"service": c["service"], "name": c["name"], "state": c["state"], "status": c["status"], "tail": tail, "lines": lines}


def doctor_row(client: httpx.Client | None = None) -> dict[str, Any]:
    """Doctor row: every container of the project running (and healthy where it has a health check)."""
    try:
        rows = list_containers(client)
    except Exception as exc:  # noqa: BLE001 - any failure is a doctor warning, never a crash
        return {"status": "warn", "why": f"containers view (docker-ro) does not answer: {exc.__class__.__name__}",
                "test": "curl -fsS http://127.0.0.1:2377/_ping", "fix": "docker compose up -d docker-ro"}
    bad = [x["service"] for x in rows if x["state"] != "running" or x["health"] == "unhealthy"]
    if not rows:
        return {"status": "warn", "why": "no containers of this project found", "fix": "docker compose ps"}
    if bad:
        return {"status": "warn", "why": f"{len(bad)} of {len(rows)} not running or unhealthy: {', '.join(bad[:6])}",
                "test": "./forgesre status", "fix": f"./forgesre logs {bad[0]}"}
    return {"status": "ok", "detail": f"{len(rows)} running"}
