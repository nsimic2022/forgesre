"""Settings › Containers: list through docker-ro, log tail demuxed and masked, Admin only, doctor row."""

from __future__ import annotations

import httpx
import pytest

from app import containers
from test_incident_tools import _client

LIST = [
    {"Id": "a" * 64, "Names": ["/forgesre-core-1"], "Image": "forgesre-core", "State": "running", "Status": "Up 2 hours (healthy)",
     "Labels": {"com.docker.compose.project": "forgesre", "com.docker.compose.service": "core"}},
    {"Id": "b" * 64, "Names": ["/forgesre-librenms-1"], "Image": "librenms/librenms", "State": "exited", "Status": "Exited (1) 5 minutes ago",
     "Labels": {"com.docker.compose.project": "forgesre", "com.docker.compose.service": "librenms"}},
    {"Id": "c" * 64, "Names": ["/other-app"], "Image": "nginx", "State": "running", "Status": "Up 1 day",
     "Labels": {"com.docker.compose.project": "other", "com.docker.compose.service": "web"}},
]


def _frame(stream: int, text: str) -> bytes:
    data = text.encode()
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


def _mock(seen: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=LIST)
        if request.url.path.endswith("/logs"):
            body = _frame(1, "2026-11-24T10:00:00Z started\n") + _frame(2, "2026-11-24T10:00:01Z ERROR login failed password=hunter2 token: abc123\n")
            return httpx.Response(200, content=body)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_lists_only_this_project_and_flags_stopped_ones(monkeypatch):
    monkeypatch.delenv("FORGESRE_COMPOSE_PROJECT", raising=False)
    rows = containers.list_containers(_mock())
    assert [r["service"] for r in rows] == ["librenms", "core"]
    assert rows[1]["health"] == "healthy" and rows[0]["state"] == "exited"
    row = containers.doctor_row(_mock())
    assert row["status"] == "warn" and "librenms" in row["why"]


def test_logs_are_demuxed_masked_and_read_only():
    seen: list[str] = []
    out = containers.logs("librenms", 5000, _mock(seen))
    assert out["tail"] == containers.MAX_TAIL
    assert out["lines"][0].endswith("started")
    assert "hunter2" not in out["lines"][1] and "abc123" not in out["lines"][1] and "password=•••" in out["lines"][1]
    assert all(s.startswith("GET ") for s in seen)
    with pytest.raises(LookupError):
        containers.logs("nope", 10, _mock())


def test_mask_and_plain_logs():
    assert containers.mask("Authorization: Bearer eyJabc.def") == "Authorization: Bearer •••"
    assert containers.mask("api_key=\"s3cr3t\" ok") == "api_key=\"•••\" ok"
    assert containers._demux(b"tty line one\nline two\n") == ["tty line one", "line two"]


def test_doctor_row_when_docker_ro_is_down():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")
    row = containers.doctor_row(httpx.Client(transport=httpx.MockTransport(boom)))
    assert row["status"] == "warn" and "docker-ro" in row["fix"]


def test_api_is_admin_only_and_reports_a_missing_helper(monkeypatch):
    monkeypatch.setenv("FORGESRE_DOCKER_URL", "http://127.0.0.1:9")
    assert _client("engineer").get("/api/v1/system/containers").status_code == 403
    admin = _client("admin")
    body = admin.get("/api/v1/system/containers").json()
    assert body["containers"] == [] and "docker-ro" in body["error"]
    assert admin.get("/api/v1/system/containers/core/logs").status_code == 503
