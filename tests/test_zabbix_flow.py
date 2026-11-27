"""Zabbix flow page: the step check against a fake Zabbix, the exchange log (API calls out, webhook
calls in), notices and ForgeAI on a new break, the Flow chat, the watch and the CLI text."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import zabbix as zabbix_mod
from app.db import Base, SessionLocal, engine
from app.models import ForgeAiChat, ForgeAiChatMessage, IntegrationExchange, Job, JournalEntry
from app.settings import settings
from tests.fake_zabbix import start
from tests.test_forgeai_more import Fake
from tests.test_incident_tools import _client

HOOK = "flow-hook-token"


def _steps(result, lane):
    return {s["id"]: s for s in next(x for x in result["lanes"] if x["id"] == lane)["steps"]}


@pytest.fixture
def zbx(monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.query(IntegrationExchange).delete()
    db.query(Job).filter(Job.kind.in_(["zabbix_flow", "zabbix_flow_ai"])).delete(synchronize_session=False)
    db.commit()

    def use(**kw):
        server, url, calls = start(**kw)
        cls = type(settings)
        monkeypatch.setattr(cls, "zabbix_url", property(lambda self: url))
        monkeypatch.setattr(cls, "zabbix_token", property(lambda self: "fake-api-token"))
        monkeypatch.setattr(cls, "zabbix_webhook_token", property(lambda self: HOOK))
        zabbix_mod.reset_state()
        use.servers.append(server)
        return calls

    use.servers = []
    yield db, use
    for server in use.servers:
        server.shutdown()
    zabbix_mod.reset_state()
    db.close()


def test_flow_check_reads_both_sides_and_logs_every_call(zbx):
    from app.zabbix_flow import check_flow

    db, use = zbx
    calls = use()
    result = check_flow(db)
    out, inn = _steps(result, "out"), _steps(result, "in")
    assert [out[k]["status"] for k in ("config", "dns", "tcp", "api", "token", "rights", "problems")] == ["ok"] * 7
    assert out["tls"]["status"] == "warn" and "plain HTTP" in out["tls"]["detail"]
    assert "Zabbix 7.0" in out["api"]["detail"] and "214 host(s) in 2 group(s)" in out["rights"]["detail"]
    assert inn["media"]["status"] == "ok" and inn["action"]["status"] == "ok"
    assert inn["sent"]["status"] == "bad" and "Couldn't connect to server" in inn["sent"]["detail"]
    assert "ufw allow" in inn["sent"]["fix"] and inn["sent"]["rows"][0]["status"] == "failed"
    assert inn["network"]["status"] == "warn" and "Nothing has arrived" in inn["network"]["detail"]
    assert all(c["auth"] == "Bearer fake-api-token" for c in calls if c["method"] != "apiinfo.version")
    logged = {r.kind for r in db.query(IntegrationExchange).filter_by(integration="zabbix", direction="out")}
    assert {"apiinfo.version", "host.get", "hostgroup.get", "mediatype.get", "action.get", "alert.get"} <= logged
    assert not any("fake-api-token" in (r.summary or "") + json.dumps(r.detail) for r in db.query(IntegrationExchange))


def test_hidden_zabbix_side_says_what_to_allow(zbx):
    from app.zabbix_flow import check_flow

    db, use = zbx
    use(refuse={"mediatype.get"})
    inn = _steps(check_flow(db), "in")
    assert inn["media"]["status"] == "unknown" and "mediatype.get, action.get and alert.get" in inn["media"]["fix"]


def test_unreachable_zabbix_stops_at_tcp(zbx, monkeypatch):
    from app.zabbix_flow import check_flow

    db, _ = zbx
    monkeypatch.setattr(type(settings), "zabbix_url", property(lambda self: "http://127.0.0.1:9/zabbix"))
    monkeypatch.setattr(type(settings), "zabbix_token", property(lambda self: "t"))
    out = _steps(check_flow(db), "out")
    assert out["tcp"]["status"] == "bad" and "nc -vz" in out["tcp"]["fix"] and "api" not in out


def test_webhook_calls_are_logged_with_their_result(zbx):
    db, use = zbx
    use()
    client = TestClient(__import__("app.main", fromlist=["create_app"]).create_app())
    url = "/api/v1/webhooks/zabbix"
    ok = client.post(url, headers={"Authorization": f"Bearer {HOOK}"}, json={"event_value": "1", "trigger_name": "flow test", "host_host": "flow-host", "trigger_id": "5"})
    assert ok.status_code == 200
    assert client.post(url, headers={"Authorization": "Bearer nope"}, json={}).status_code == 401
    assert client.post(url, headers={"Authorization": f"Bearer {HOOK}"}, json={"trigger_name": "{TRIGGER.NAME}", "host_host": "{HOST.HOST}", "forge_token": "x"}).status_code == 422
    rows = db.query(IntegrationExchange).filter_by(integration="zabbix", direction="in").order_by(IntegrationExchange.id).all()
    assert [(r.status, r.http_status) for r in rows] == [("ok", 200), ("refused", 401), ("test", 422)]
    assert "flow test on flow-host (firing)" in rows[0].summary and rows[0].detail["incidents"]
    assert "URL and token are right" in rows[2].summary
    admin = _client("admin")
    assert _client("engineer").get("/api/v1/integrations/zabbix/exchanges").status_code == 403
    data = admin.get("/api/v1/integrations/zabbix/exchanges", params={"direction": "in", "status": "refused"}).json()
    assert data["total"] == 1 and data["rows"][0]["http_status"] == 401 and data["last_id"] >= rows[-1].id
    after = admin.get("/api/v1/integrations/zabbix/exchanges", params={"after": rows[-1].id}).json()
    assert after["total"] == 0


def test_new_break_is_journaled_once_and_analysed(zbx, monkeypatch):
    from app.zabbix_flow import AI_KIND, check_flow, notice_changes

    db, use = zbx
    use()
    monkeypatch.setattr(type(settings), "ai_enabled", property(lambda self: True))
    monkeypatch.setattr(type(settings), "llm_url", property(lambda self: "http://llm:8088/v1"))
    first = check_flow(db)
    assert notice_changes(db, None, first) == ["Zabbix sent"]
    entry = db.query(JournalEntry).filter(JournalEntry.summary.like("Zabbix flow broke at Zabbix sent%")).first()
    assert entry is not None and entry.status == "error" and "Flow and log" in entry.detail
    job = db.query(Job).filter_by(kind=AI_KIND).one()
    assert "Zabbix sent" in job.payload["question"]
    assert notice_changes(db, first, first) == []  # still broken: no second notice
    assert db.query(Job).filter_by(kind=AI_KIND).count() == 1
    use(failed_send=False)
    healed = check_flow(db)
    notice_changes(db, first, healed)
    assert db.query(JournalEntry).filter(JournalEntry.summary == "Zabbix flow works again at Zabbix sent").count() == 1


def test_watch_runs_every_15_minutes_from_the_escalation_loop(zbx):
    import app.main as main
    from app.zabbix_flow import watch_due

    db, use = zbx
    use()
    assert watch_due(db)
    main._queue_zabbix_flow(db)
    assert db.query(Job).filter_by(kind="zabbix_flow").count() == 1 and not watch_due(db)


def test_flow_api_and_ai_analysis(zbx, monkeypatch):
    from app import forgeai
    from app.jobs import run_pending_jobs

    db, use = zbx
    use()
    admin = _client("admin")
    assert _client("engineer").post("/api/v1/integrations/zabbix/flow").status_code == 403
    assert admin.post("/api/v1/integrations/zabbix/flow").json()["job"]
    monkeypatch.setattr(type(settings), "ai_enabled", property(lambda self: True))
    monkeypatch.setattr(type(settings), "llm_url", property(lambda self: "http://llm:8088/v1"))
    monkeypatch.setattr("app.zabbix_flow.core_log_lines", lambda limit=60: {"lines": ["core | zabbix webhook 401 from 10.0.0.9"], "error": ""})
    fake = Fake({"answer": "Zabbix sent is broken: the Zabbix server cannot reach port 8080."})
    monkeypatch.setattr(forgeai, "_provider", lambda: fake)
    # other tests may have left jobs in the queue: run until ours are done
    for _ in range(10):
        run_pending_jobs(db, limit=8)
        run_pending_jobs(db, limit=4, lane="llm")
        db.expire_all()
        if db.query(Job).filter(Job.kind.in_(["zabbix_flow", "zabbix_flow_ai"]), Job.status.in_(["pending", "running"])).count() == 0:
            break
    data = admin.get("/api/v1/integrations/zabbix/flow").json()
    assert data["status"] == "done" and data["result"]["lanes"] and data["watch"] is True
    assert "cannot reach port 8080" in data["ai"]["answer"]
    assert "connection between ForgeSRE and Zabbix" in fake.systems[-1]


def test_flow_chat_reads_only_the_flow_and_offers_flow_skills(zbx, monkeypatch):
    from app import forgeai
    from app.forgeai_chat import run_answer

    db, use = zbx
    use()
    monkeypatch.setattr(type(settings), "ai_enabled", property(lambda self: True))
    monkeypatch.setattr(type(settings), "llm_url", property(lambda self: "http://llm:8088/v1"))
    monkeypatch.setattr("app.zabbix_flow.core_log_lines", lambda limit=60: {"lines": ["zabbix line"], "error": ""})
    client = _client("analyst")
    chat = client.post("/api/v1/forgeai/chats", json={"topic": "flow:zabbix", "reuse": True}).json()
    assert chat["topic"] == "flow:zabbix" and chat["topic_label"] == "Zabbix flow"
    assert client.post("/api/v1/forgeai/chats", json={"topic": "flow:zabbix", "reuse": True}).json()["id"] == chat["id"]
    assert client.post("/api/v1/forgeai/chats", json={"topic": "flow:other"}).status_code == 422
    skills = client.get(f"/api/v1/forgeai/chats/{chat['id']}").json()["skills"]
    assert skills and {s["task"] for s in skills} == {"flow"}
    said = client.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": ""}).json()  # empty = built-in question
    assert said["answer"]["status"] == "pending"
    fake = Fake({"answer": "The webhook path is fine; check the media type."})
    monkeypatch.setattr(forgeai, "_provider", lambda: fake)
    job = db.query(Job).filter_by(kind="forgeai_chat", object_id=str(said["answer"]["id"])).one()
    run_answer(db, job)
    msg = db.get(ForgeAiChatMessage, said["answer"]["id"])
    db.refresh(msg)
    assert msg.status == "done" and "media type" in msg.text
    from app.models import ForgeAiCall

    call = db.get(ForgeAiCall, msg.call_id)
    context = json.loads(call.user_prompt)
    assert context["integration"] == "zabbix" and context["core_log_lines"] == ["zabbix line"] and "incident" not in context
    assert call.task == "flow" and call.skill_slug == "flow-where-it-breaks"
    db.query(ForgeAiChatMessage).filter_by(chat_id=chat["id"]).delete()
    db.query(ForgeAiChat).filter_by(id=chat["id"]).delete()
    db.commit()


def test_cli_text():
    from app.cli_ops import format_exchanges, format_flow

    text = format_flow({"checked_at": "2026-11-27T12:00:00+00:00", "lanes": [{"id": "in", "title": "Zabbix → ForgeSRE", "steps": [
        {"id": "sent", "label": "Zabbix sent", "status": "bad", "detail": "failed", "fix": "open the port", "ms": 12,
         "rows": [{"at": "2026-11-27T11:59:00+00:00", "status": "failed", "error": "Couldn't connect"}]}]}]})
    assert "✗ Zabbix sent" in text and "(12 ms)" in text and "Fix: open the port" in text and "Couldn't connect" in text
    rows = format_exchanges([{"at": "2026-11-27T12:01:00", "direction": "in", "kind": "webhook", "status": "refused", "http_status": 401, "ms": 2, "peer": "10.0.0.9", "summary": "Wrong token"},
                             {"at": "2026-11-27T12:00:00", "direction": "out", "kind": "host.get", "status": "ok", "http_status": 0, "ms": 9, "peer": "zbx", "summary": "214"}])
    assert rows.index("host.get") < rows.index("webhook") and "← webhook" in rows and "401" in rows
