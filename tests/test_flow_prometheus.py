"""Prometheus → Alertmanager → ForgeSRE flow against a fake Prometheus + Alertmanager, the
Alertmanager webhook log, the generic flow API and the integration list."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from app.db import Base, SessionLocal, engine
from app.models import IntegrationExchange, Job
from app.settings import settings
from tests.test_incident_tools import _client


class Stack:
    def __init__(self):
        self.failed = 0.0
        self.rule_error = ""
        self.silenced = False

    def answer(self, path: str, query: dict) -> tuple[int, str, object]:
        expr = (query.get("query") or [""])[0]
        if path == "/api/v1/query":
            value = "2" if "sd_http_failures" in expr else "41"
            return 200, "json", {"status": "success", "data": {"result": [{"value": [0, value]}]}}
        if path == "/api/v1/targets":
            return 200, "json", {"data": {"activeTargets": [
                {"health": "up", "scrapePool": "node", "labels": {"instance": "db-01"}},
                {"health": "down", "scrapePool": "snmp", "labels": {"instance": "sw-03"}, "lastError": "context deadline exceeded"}]}}
        if path == "/api/v1/rules":
            rules = [{"name": "HostDown", "health": "ok", "state": "firing"}, {"name": "DiskFull", "health": "ok", "state": "inactive"}]
            if self.rule_error:
                rules.append({"name": "Broken", "health": "err", "lastError": self.rule_error, "state": "inactive"})
            return 200, "json", {"data": {"groups": [{"rules": rules}]}}
        if path == "/-/ready":
            return 200, "text", "OK"
        if path == "/api/v2/silences":
            return 200, "json", [{"status": {"state": "active"}, "matchers": [{"name": "alertname", "value": "HostDown", "isEqual": True}],
                                  "createdBy": "ana", "comment": "maintenance", "endsAt": "2026-11-28T08:00:00Z"}] if self.silenced else []
        if path == "/api/v2/alerts":
            return 200, "json", [{"status": {"state": "suppressed" if self.silenced else "active"}}]
        if path == "/api/v2/status":
            return 200, "json", {"cluster": {"status": "disabled"}}
        if path == "/metrics":
            return 200, "text", (f'alertmanager_notifications_total{{integration="webhook"}} 12\n'
                                 f'alertmanager_notifications_failed_total{{integration="webhook",reason="other"}} {self.failed}\n'
                                 'alertmanager_notifications_total{integration="email"} 3\n')
        return 404, "text", "no"


@pytest.fixture
def stack(monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(IntegrationExchange).filter_by(integration="prometheus").delete()
    db.query(Job).filter(Job.kind.in_(["flow_check", "flow_ai"])).delete(synchronize_session=False)
    db.commit()
    fake = Stack()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            code, kind, body = fake.answer(parts.path, parse_qs(parts.query))
            data = (json.dumps(body) if kind == "json" else str(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json" if kind == "json" else "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr("app.flow_prometheus.prom_url", lambda: url)
    monkeypatch.setattr("app.flow_prometheus.am_url", lambda: url)
    yield db, fake
    server.shutdown()
    db.close()


def _steps(result, lane="in"):
    return {s["id"]: s for s in next(x for x in result["lanes"] if x["id"] == lane)["steps"]}


def test_alarm_path_blocks(stack):
    from app.flow_prometheus import check

    db, fake = stack
    inn = _steps(check(db))
    assert inn["sd"]["status"] == "warn" and "2 failed target refresh" in inn["sd"]["detail"]
    assert inn["targets"]["status"] == "warn" and "1 of 2 up" in inn["targets"]["detail"]
    assert inn["targets"]["rows"][0]["error"] == "context deadline exceeded"
    assert inn["rules"]["status"] == "ok" and "1 firing" in inn["rules"]["detail"]
    assert inn["alertmanager"]["status"] == "ok" and inn["am_sent"]["status"] == "ok" and inn["am_sent"]["counters"] == {"sent": 12.0, "failed": 0.0}
    assert {"network", "webhook", "incident"} <= set(inn)
    out = _steps(check(db), "out")
    assert out["prom_api"]["status"] == "ok" and out["am_api"]["status"] == "ok"
    fake.rule_error, fake.silenced = "bad expression", True
    inn = _steps(check(db))
    assert inn["rules"]["status"] == "bad" and inn["rules"]["rows"][0]["error"] == "bad expression"
    assert inn["alertmanager"]["status"] == "warn" and "held back" in inn["alertmanager"]["detail"]
    assert "alertname=HostDown — ana: maintenance" in inn["alertmanager"]["rows"][0]["subject"]


def test_new_webhook_failures_since_the_last_check_are_bad(stack):
    from app.flows import run_check
    from app.jobs import run_pending_jobs

    db, fake = stack
    admin = _client("admin")
    admin.post("/api/v1/integrations/prometheus/flow")
    run_pending_jobs(db, limit=8)
    fake.failed = 3
    first = run_check(db, "prometheus")
    assert _steps(first)["am_sent"]["status"] == "bad" and "3 new failure(s)" in _steps(first)["am_sent"]["detail"]


def test_alertmanager_webhook_calls_are_logged(stack):
    from fastapi.testclient import TestClient

    from app.flow_prometheus import check
    from app.main import create_app

    db, _ = stack
    client = TestClient(create_app())
    good = {"Authorization": f"Bearer {settings.webhook_token}"}
    body = {"receiver": "forgesre", "status": "firing", "alerts": [{"status": "firing", "labels": {"alertname": "FlowTest", "instance": "flow-01"}, "fingerprint": "fl1"}]}
    assert client.post("/api/v1/webhooks/alertmanager", headers=good, json=body).status_code == 202
    assert client.post("/api/v1/webhooks/alertmanager", headers={"Authorization": "Bearer no"}, json=body).status_code == 401
    rows = db.query(IntegrationExchange).filter_by(integration="prometheus", direction="in").order_by(IntegrationExchange.id).all()
    assert [(r.status, r.http_status) for r in rows] == [("ok", 202), ("refused", 401)]
    assert "FlowTest" in rows[0].summary and rows[0].detail["receiver"] == "forgesre"
    assert settings.webhook_token not in json.dumps([r.summary for r in rows])
    inn = _steps(check(db))
    assert inn["network"]["status"] == "ok" and inn["webhook"]["status"] == "bad"  # the last call was refused


def test_flow_api_lists_every_flow_and_links_cards(stack):
    db, _ = stack
    admin = _client("admin")
    names = [f["name"] for f in admin.get("/api/v1/flows").json()]
    assert names[:2] == ["prometheus", "zabbix"]
    assert admin.get("/api/v1/integrations/nope/flow").status_code == 404
    assert admin.post("/api/v1/integrations/nope/flow").status_code == 404
    assert _client("engineer").get("/api/v1/flows").status_code == 403
    data = admin.get("/api/v1/integrations/prometheus/flow").json()
    assert data["label"] == "Prometheus" and data["configured"] is True
    cards = admin.get("/api/v1/integrations").json()
    flat = [c for group in cards.values() for c in group] if isinstance(cards, dict) else cards
    links = {c["id"]: c.get("flow") for c in flat if isinstance(c, dict)}
    assert links.get("prometheus") == "/settings/integrations/prometheus" and links.get("zabbix") == "/settings/integrations/zabbix"
    assert not links.get("grafana")
