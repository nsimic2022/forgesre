"""LibreNMS, NetBox, Kubernetes, Oxidized, ForgeAI model and MCP flows against one fake HTTP server,
and the call logs they rely on (LibreNMS webhook and API, MCP guard). No tokens are ever kept."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from app.db import Base, SessionLocal, engine
from app.models import IntegrationExchange
from app.settings import settings
from tests.test_incident_tools import _client

TOKEN = "flow-more-secret-token"


class Fake:
    def __init__(self) -> None:
        self.k8s_nodes_code = 200

    def answer(self, path: str, headers: dict[str, str]) -> tuple[int, object]:
        if path in {"/login", "/login/"}:
            return 200, "login"
        if path == "/api/v0/devices":
            return (200, {"status": "ok", "devices": [{"hostname": "sw-01"}, {"hostname": "sw-02"}]}) if TOKEN in headers.get("x-auth-token", "") else (401, {"message": "Unauthenticated."})
        if path == "/api/dcim/devices/":
            return 200, {"count": 7, "results": [{"id": 1}]}
        if path == "/version":
            return 200, {"gitVersion": "v1.31.2"}
        if path == "/api/v1/nodes":
            return self.k8s_nodes_code, {"items": [{"metadata": {"name": "n1"}}, {"metadata": {"name": "n2"}}]}
        if path == "/api/v1/pods":
            return 200, {"items": [{"metadata": {"name": "p"}}]}
        if path == "/nodes.json":
            return 200, [{"name": "sw-01", "ip": "10.0.0.1", "status": "success"}, {"name": "fw-01", "ip": "10.0.0.2", "status": "no_connection"}]
        if path == "/v1/models":
            return 200, {"data": [{"id": "qwen3-4b"}]}
        return 404, {"message": "no"}


@pytest.fixture
def fake(monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(IntegrationExchange).filter(IntegrationExchange.integration.in_(["librenms", "mcp"])).delete(synchronize_session=False)
    db.commit()
    state = Fake()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            code, body = state.answer(urlsplit(self.path).path, {k.lower(): v for k, v in self.headers.items()})
            data = (json.dumps(body) if not isinstance(body, str) else body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json" if not isinstance(body, str) else "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    cls = type(settings)
    for name, value in {"librenms_url": url, "librenms_token": TOKEN, "librenms_enabled": True, "netbox_url": url, "netbox_token": TOKEN,
                        "ai_enabled": True, "llm_url": url + "/v1"}.items():
        monkeypatch.setattr(cls, name, property(lambda s, v=value: v))
    from app import librenms

    librenms.reset_state()
    yield db, state, url
    librenms.reset_state()
    server.shutdown()
    db.close()


def _steps(result, lane):
    return {s["id"]: s for s in next(x for x in result["lanes"] if x["id"] == lane)["steps"]}


def test_librenms_both_directions_and_its_logs(fake):
    from fastapi.testclient import TestClient

    from app.flow_librenms import check
    from app.main import create_app

    db, _, _ = fake
    out = _steps(check(db), "out")
    assert out["cfg"]["status"] == "ok" and out["ui"]["status"] == "ok"
    assert out["api"]["status"] == "ok" and "2 device(s)" in out["api"]["detail"]
    client = TestClient(create_app())
    good = {"Authorization": f"Bearer {settings.webhook_token}"}
    assert client.post("/api/v1/webhooks/librenms", headers={"Authorization": "Bearer nope"}, json={}).status_code == 401
    assert client.post("/api/v1/webhooks/librenms", headers=good, json={"rule_name": "Port down", "hostname": "sw-01", "state": 1}).status_code == 202
    inn = _steps(check(db), "in")
    assert inn["network"]["status"] == "ok" and inn["webhook"]["status"] == "ok"
    rows = db.query(IntegrationExchange).filter_by(integration="librenms").all()
    assert {r.direction for r in rows} == {"in", "out"} and any(r.kind == "GET /devices" for r in rows)
    dumped = json.dumps([[r.summary, r.endpoint, r.peer] for r in rows])
    assert TOKEN not in dumped and settings.webhook_token not in dumped


def test_netbox_oxidized_and_forgeai(fake, monkeypatch):
    from app import oxidized
    from app.flow_forgeai import check as forgeai
    from app.flow_netbox import check as netbox
    from app.flow_oxidized import check as oxi

    db, _, url = fake
    nb = _steps(netbox(db), "out")
    assert nb["cfg"]["status"] == "ok" and nb["api"]["status"] == "ok"
    assert _steps(oxi(db), "out")["cfg"]["status"] == "off"
    monkeypatch.setattr(oxidized, "_cfg", lambda: {"enabled": True, "url": url})
    ox = _steps(oxi(db), "out")
    assert ox["nodes"]["status"] == "warn" and "1 with a failed last backup" in ox["nodes"]["detail"]
    assert ox["nodes"]["rows"][0]["subject"].startswith("fw-01")
    ai = forgeai(db)
    local = _steps(ai, "local")
    assert local["model"]["status"] == "ok" and local["model"]["sub"] == "qwen3-4b"
    assert _steps(ai, "cloud")["cloud"]["status"] in {"off", "ok", "bad"}


def test_kubernetes_lane_per_cluster(fake, monkeypatch):
    from app import kubernetes
    from app.flow_kubernetes import check

    db, state, url = fake
    monkeypatch.setattr(kubernetes, "clusters", lambda: [{"name": "lab", "url": url, "ca_file": "", "ca_pem": "", "insecure": False}])
    real = type(settings)._secret
    monkeypatch.setattr(type(settings), "_secret", lambda s, key: TOKEN if key.startswith("K8S_TOKEN_") else real(s, key))
    lane = _steps(check(db), "c0")
    assert lane["version"]["status"] == "ok" and "v1.31.2" in lane["version"]["detail"]
    assert lane["nodes"]["status"] == "ok" and "2 node(s)" in lane["nodes"]["detail"]
    state.k8s_nodes_code = 403
    lane = _steps(check(db), "c0")
    assert lane["nodes"]["status"] == "bad" and "forgesre-reader" in lane["nodes"]["fix"]
    assert TOKEN not in json.dumps(lane)


def test_mcp_flow_and_guard_log(fake, monkeypatch):
    from fastapi.testclient import TestClient

    from app import mcp_server
    from app.flow_mcp import check
    from app.main import create_app

    db, _, _ = fake
    assert _steps(check(db), "in")["cfg"]["status"] == "off"
    monkeypatch.setattr(mcp_server, "enabled", lambda: True)
    monkeypatch.setattr(mcp_server, "token", lambda: TOKEN)
    client = TestClient(create_app())
    assert client.post("/mcp/", headers={"Authorization": "Bearer wrong"}, json={}).status_code == 401
    steps = _steps(check(db), "in")
    assert steps["cfg"]["status"] == "ok" and steps["calls"]["status"] == "bad" and "Wrong or missing MCP_TOKEN" in steps["calls"]["detail"]
    assert steps["data"]["status"] == "warn"


def test_cards_and_registry():
    from app.flows import flows

    names = list(flows())
    assert {"librenms", "netbox", "kubernetes", "oxidized", "forgeai", "mcp"} <= set(names)
    admin = _client("admin")
    cards = admin.get("/api/v1/integrations").json()
    links = {c["id"]: c.get("flow") for group in cards.values() for c in group}
    assert links["llm"] == "/settings/integrations/forgeai" and links["librenms"] == "/settings/integrations/librenms"
    assert links["mcp"] == "/settings/integrations/mcp" and links["kubernetes"] == "/settings/integrations/kubernetes"
