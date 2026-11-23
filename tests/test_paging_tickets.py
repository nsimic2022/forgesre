"""SMS paging to the person on call, GLPI tickets and the outbound ticket webhook."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from app import sms, tickets
from app.models import Incident, Notification, utcnow
from app.settings import settings

from test_dc_noc_parent import _db
from test_incident_tools import _client


@pytest.fixture()
def cfg(monkeypatch):
    def use(**tree):
        monkeypatch.setattr(settings, "yaml", {**settings.yaml, **tree})

    monkeypatch.setattr(type(settings), "_secret", lambda self, key: {"SMS_GATEWAY_TOKEN": "tok", "TICKET_WEBHOOK_TOKEN": "hook"}.get(key, ""))
    return use


def _incident(db, severity="CRITICAL") -> Incident:
    row = Incident(number=f"INC-Q{uuid4().hex[:8]}", title="Core switch down", severity=severity, status="OPEN",
                   fingerprint=f"paging:{uuid4().hex}", started_at=utcnow(), source="prometheus")
    db.add(row)
    db.commit()
    return row


def test_generic_sms_gateway_body_and_bearer(cfg, monkeypatch):
    cfg(notifications={"sms": {"enabled": True, "url": "https://sms.example/send", "body": '{"msisdn": "{to}", "message": "{text}"}'}})
    seen = {}

    def post(url, json=None, headers=None, timeout=None, **kw):
        seen.update(url=url, json=json, headers=headers)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(sms.httpx, "post", post)
    ok, detail = sms.send("+38160111222", 'Disk "full"')
    assert ok and seen["json"] == {"msisdn": "+38160111222", "message": 'Disk "full"'} and seen["headers"]["Authorization"] == "Bearer tok"


def test_twilio_and_readiness(cfg, monkeypatch):
    cfg(notifications={"sms": {"enabled": True, "provider": "twilio"}})
    assert "Account SID" in sms.not_ready()
    cfg(notifications={"sms": {"enabled": True, "provider": "twilio", "account": "AC1", "from": "+100"}})
    seen = {}
    monkeypatch.setattr(sms.httpx, "post", lambda url, data=None, auth=None, timeout=None, **kw: (seen.update(url=url, data=data, auth=auth),
                                                                                                    httpx.Response(201, request=httpx.Request("POST", url)))[1])
    assert sms.send("+381", "hi")[0]
    assert seen["url"].endswith("/Accounts/AC1/Messages.json") and seen["data"]["From"] == "+100" and seen["auth"] == ("AC1", "tok")
    cfg(notifications={"sms": {"enabled": False}})
    assert sms.check()["status"] == "disabled"


def test_sms_goes_to_the_person_on_call_for_critical_only(cfg, monkeypatch):
    cfg(notifications={"sms": {"enabled": True, "url": "https://sms.example/send", "min_severity": "critical"}})
    monkeypatch.setattr("app.dc_noc.current_oncall", lambda db, at=None: {"phone": "+38160999", "name": "Ana"})
    db = _db()
    crit, warn = _incident(db), _incident(db, "WARNING")
    assert sms.queue_step(db, crit, "immediate", 0) == 1
    assert sms.queue_step(db, crit, "immediate", 0) == 0  # once per step
    assert sms.queue_step(db, warn, "immediate", 0) == 0
    row = db.query(Notification).filter_by(incident_id=crit.id, channel="sms").one()
    assert row.target == "+38160999" and row.body.startswith("ForgeSRE NEW [CRITICAL] Core switch down")
    monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: httpx.Response(500, request=httpx.Request("POST", "https://sms.example/send")))
    assert sms.deliver(db, row.id) is False
    db.refresh(row)
    assert row.status == "failed" and "HTTP 500" in row.error


def test_glpi_ticket_created_synced_and_followed_up(cfg, monkeypatch):
    cfg(tickets={"glpi": {"enabled": True}}, system={**(settings.yaml.get("system") or {}), "public_url": "https://forgesre.dc"})
    monkeypatch.setattr(type(settings), "glpi_configured", property(lambda self: True), raising=False)
    monkeypatch.setattr(type(settings), "glpi_url", property(lambda self: "https://glpi.dc/glpi"), raising=False)
    calls = []
    status = {"value": 2}

    def fake(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST" and path == "Ticket":
            return {"id": 77}
        if method == "GET":
            return {"status": status["value"]}
        return {}

    monkeypatch.setattr(tickets, "_glpi_call", fake)
    db = _db()
    row = _incident(db)
    analyst = _client("analyst")
    job = analyst.post(f"/api/v1/incidents/{row.number}/ticket").json()["job"]
    assert job
    tickets.run_job(db, {"incident": row.number}, "ana@dc")
    db.refresh(row)
    assert row.ticket_ref == "glpi:77" and row.ticket_url == "https://glpi.dc/glpi/front/ticket.form.php?id=77"
    body = calls[0][2]["input"]
    assert body["urgency"] == 5 and "https://forgesre.dc/app/incidents/" in body["content"]
    assert analyst.post(f"/api/v1/incidents/{row.number}/ticket").status_code == 409
    assert analyst.get(f"/api/v1/incidents/{row.number}/view").json()["ticket"]["ref"] == "glpi:77"
    status["value"] = 5
    assert tickets.sync_glpi_tickets(db)["changed"] >= 1
    db.refresh(row)
    assert row.ticket_status == "solved" and any("GLPI ticket solved" in t.get("detail", "") for t in row.timeline)
    row.status, row.resolved_at, row.resolved_by = "RESOLVED", utcnow(), "ana"
    db.commit()
    assert tickets.run_job(db, {"incident": row.number, "followup": True}, "system") == {"status": "followup"}
    assert calls[-1][1] == "Ticket/77/ITILFollowup"


def test_ticket_webhook_events(cfg, monkeypatch):
    cfg(tickets={"webhook": {"enabled": True, "url": "https://n8n.dc/hook"}})
    sent = []
    monkeypatch.setattr(tickets.httpx, "post", lambda url, json=None, headers=None, timeout=None: (sent.append((url, json, headers)),
                                                                                                 httpx.Response(200, request=httpx.Request("POST", url)))[1])
    db = _db()
    row = _incident(db)
    assert tickets.queue_step(db, row, "immediate", 0) == 1
    note = db.query(Notification).filter_by(incident_id=row.id, channel="webhook").one()
    assert tickets.deliver_webhook(db, note.id)
    url, payload, headers = sent[0]
    assert payload["event"] == "opened" and payload["incident"]["title"] == "Core switch down" and headers["Authorization"] == "Bearer hook"
    json.dumps(payload)
    row.status, row.resolved_at = "RESOLVED", utcnow()
    db.commit()
    assert tickets.sweep_resolved(db) >= 1


def test_settings_tests_exist():
    from app.system_settings import test_connection

    for name in ("sms", "ticket-glpi", "ticket-webhook"):
        assert test_connection(name)["status"] in {"disabled", "error"}
