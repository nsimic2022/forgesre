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
    {"Id": "d" * 64, "Names": ["/forgesre-netbox-db-init-1"], "Image": "postgres:16", "State": "exited", "Status": "Exited (0) 3 hours ago",
     "Labels": {"com.docker.compose.project": "forgesre", "com.docker.compose.service": "netbox-db-init"}},
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
    assert [r["service"] for r in rows] == ["librenms", "netbox-db-init", "core"]
    assert rows[1]["done"] and not rows[0]["done"]
    assert rows[2]["health"] == "healthy" and rows[0]["state"] == "exited"
    row = containers.doctor_row(_mock())
    # the finished one-shot netbox-db-init is not a problem; the exited librenms is
    assert row["status"] == "warn" and "librenms" in row["why"] and "netbox-db-init" not in row["why"] and "1 of 3" in row["why"]


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


def test_librenms_token_script_runs_as_librenms_and_stores_the_plain_token():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "librenms-upsert-token.sh").read_text(encoding="utf-8")
    assert "exec -T --user librenms librenms lnms user:add" in script
    assert "exec -T --user librenms librenms php -r" in script
    assert '$row->token_hash = $token;' in script and 'hash("sha256"' not in script
    assert "./forgesre librenms-token" in script
    cli = (root / "scripts" / "forgesre").read_text(encoding="utf-8")
    assert "librenms-token)" in cli and "librenms-upsert-token.sh" in cli


def test_glpi_dns_error_says_what_to_do(monkeypatch):
    from app import api
    from app.settings import settings

    monkeypatch.setattr(type(settings), "glpi_enabled", property(lambda self: True))
    monkeypatch.setattr(type(settings), "glpi_url", property(lambda self: "http://glpi.dc.local/apirest.php"))
    monkeypatch.setattr("app.glpi_sync.current_status", lambda: {"configured": True, "ok": False, "label": "Unreachable",
                                                                  "why": "GLPI unreachable: [Errno -2] Name or service not known"})
    row = api._glpi_check()
    assert row["status"] == "warn" and "glpi.dc.local" in row["why"] and row["test"] == "getent hosts glpi.dc.local"
    assert "inventory.glpi.url" in row["fix"]
