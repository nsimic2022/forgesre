"""ForgeAI status line, model test, streamed answers with thinking, live chat progress, star ratings."""

from __future__ import annotations

import json

import httpx
import pytest

from app import forgeai
from app.db import Base, SessionLocal, engine
from app.models import ForgeAiCall, ForgeAiChatMessage, Job
from app.seed import seed

from test_forgeai import FakeLLM, _client, _run_only


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    seed(session)
    yield session
    session.close()


def _on(monkeypatch, **llm):
    from app.settings import settings

    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "ai": {"enabled": True, "llm": {"url": "http://127.0.0.1:8088/v1", **llm}}})


def _sse(*chunks: dict) -> str:
    lines = [f"data: {json.dumps({'choices': [{'delta': c}]})}" for c in chunks]
    return "\n\n".join([*lines, "data: [DONE]"]) + "\n\n"


def _mock(monkeypatch, handler):
    real = httpx.AsyncClient

    def fake(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("rca.llm.httpx.AsyncClient", fake)


def test_stream_shows_thinking_then_answer_and_sends_the_switch(monkeypatch):
    from rca.llm import OpenAICompatibleLLM

    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3-4b.gguf"}]})
        bodies.append(json.loads(request.content))
        text = _sse({"reasoning_content": "The disk is full "}, {"reasoning_content": "because of logs."},
                    {"content": '{"answer": '}, {"content": '"Purge the logs."}'})
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    _mock(monkeypatch, handler)
    seen = []
    llm = OpenAICompatibleLLM("http://127.0.0.1:8088/v1", thinking=True)
    out = llm.complete_json_stream("sys", "user", 64, lambda phase, thinking, answer: seen.append(phase))
    assert out == {"answer": "Purge the logs."}
    assert llm.last_thinking == "The disk is full because of logs." and llm.last_raw.startswith('{"answer"')
    assert seen[0] == "waiting" and seen[-1] == "checking"
    assert bodies[0]["stream"] is True and bodies[0]["chat_template_kwargs"] == {"enable_thinking": True}


def test_stream_splits_inline_think_and_explains_an_empty_answer(monkeypatch):
    from rca.llm import OpenAICompatibleLLM

    answers = iter([_sse({"content": "<think>check the mount</think>"}, {"content": '{"ok": true}'}),
                    _sse({"reasoning_content": "still thinking and thinking"})])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, text=next(answers), headers={"content-type": "text/event-stream"})

    _mock(monkeypatch, handler)
    llm = OpenAICompatibleLLM("http://127.0.0.1:8088/v1")
    assert llm.complete_json_stream("s", "u") == {"ok": True}
    assert llm.last_thinking == "check the mount"
    assert llm.complete_json_stream("s", "u") is None
    assert llm.last_error == "the model ran out of tokens while thinking"


def test_stream_falls_back_to_a_plain_body(monkeypatch):
    from rca.llm import OpenAICompatibleLLM

    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        bodies.append(body)
        if "chat_template_kwargs" in body:
            return httpx.Response(400, json={"error": "unknown field"})
        return httpx.Response(200, text=_sse({"content": '{"ok": true}'}), headers={"content-type": "text/event-stream"})

    _mock(monkeypatch, handler)
    assert OpenAICompatibleLLM("http://127.0.0.1:8088/v1").complete_json_stream("s", "u") == {"ok": True}
    assert len(bodies) == 2 and "chat_template_kwargs" not in bodies[1]


class StreamLLM(FakeLLM):
    """Streams: reports progress (checked from another session) and returns reasoning."""

    def __init__(self, answer, message_id=None):
        super().__init__(answer)
        self.message_id = message_id
        self.phase_seen = ""
        self.last_thinking = ""

    def complete_json_stream(self, system, user, max_tokens=512, on_progress=None):
        on_progress("thinking", "Looking at the facts", "")
        if self.message_id:
            s = SessionLocal()
            try:
                self.phase_seen = s.get(ForgeAiChatMessage, self.message_id).phase
            finally:
                s.close()
        self.last_thinking = "Looking at the facts, then the logs."
        return self.complete_json(system, user, max_tokens)


def test_chat_answer_shows_phase_while_running_and_keeps_the_reasoning(db, monkeypatch):
    _on(monkeypatch, thinking=True)
    analyst = _client("analyst")
    chat = analyst.post("/api/v1/forgeai/chats", json={}).json()
    sent = analyst.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": "What is a BGP hold timer?"}).json()
    assert sent["answer"]["phase"] == "queued"
    waiting = analyst.get(f"/api/v1/forgeai/chats/{chat['id']}").json()["messages"][-1]
    assert waiting["queue"]["timeout_s"] == 90 and waiting["queue"]["ahead"] >= 0
    fake = StreamLLM({"answer": "How long a peer waits for keepalives."}, sent["answer"]["id"])
    monkeypatch.setattr(forgeai, "_provider", lambda: fake)
    job = db.query(Job).filter_by(kind="forgeai_chat", object_id=str(sent["answer"]["id"])).one()
    _run_only(db, job.id)
    assert fake.phase_seen == "thinking"
    assert fake.calls[0][2] == forgeai.TASKS["chat"]["max_tokens"] + forgeai.THINK_BUDGET
    done = analyst.get(f"/api/v1/forgeai/chats/{chat['id']}").json()["messages"][-1]
    assert done["status"] == "done" and done["phase"] == "" and done["thinking"] == "Looking at the facts, then the logs."
    db.expire_all()
    assert db.get(ForgeAiCall, done["call_id"]).raw_response.startswith("<think>\nLooking at the facts")


def test_pulse_and_test_run(db, monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "ai": {"enabled": False}})
    analyst, viewer = _client("analyst"), _client("viewer")
    off = viewer.get("/api/v1/forgeai/pulse").json()
    assert off["local"]["on"] is False and off["can_test"] is False
    assert analyst.post("/api/v1/forgeai/ping").status_code == 409

    _on(monkeypatch)
    monkeypatch.setattr(httpx, "get", lambda url, timeout=0: httpx.Response(200, json={"data": [{"id": "qwen3-4b.gguf"}]},
                                                                              request=httpx.Request("GET", url)))
    assert viewer.post("/api/v1/forgeai/ping").status_code == 403
    queued = analyst.post("/api/v1/forgeai/ping").json()
    assert analyst.post("/api/v1/forgeai/ping").json() == {"job": queued["job"], "already": True}
    pulse = analyst.get("/api/v1/forgeai/pulse").json()
    assert pulse["local"] == {"on": True, "model": "qwen3-4b.gguf", "reachable": True, "why": ""}
    assert pulse["test"]["status"] == "pending" and pulse["lane"]["pending"] >= 1
    monkeypatch.setattr(forgeai, "_provider", lambda: StreamLLM({"ok": True}))
    _run_only(db, queued["job"])
    test = analyst.get("/api/v1/forgeai/pulse").json()["test"]
    assert test["status"] == "ok" and test["model"] == "qwen3-4b" and test["error"] == ""

    monkeypatch.setattr(forgeai, "_provider", lambda: FakeLLM(None))
    again = analyst.post("/api/v1/forgeai/ping").json()
    _run_only(db, again["job"])
    test = analyst.get("/api/v1/forgeai/pulse").json()["test"]
    assert test["status"] == "error" and test["error"] == "timed out"


def test_star_ratings_and_old_thumbs(db, monkeypatch):
    _on(monkeypatch)
    call = ForgeAiCall(task="chat", skill_slug="chat-generic", status="trial", model="m")
    db.add(call)
    db.commit()
    analyst = _client("analyst")
    assert analyst.post(f"/api/v1/forgeai/calls/{call.id}/rate", json={"stars": 4}).json() == {"id": call.id, "rating": 1, "stars": 4}
    assert analyst.post(f"/api/v1/forgeai/calls/{call.id}/rate", json={"stars": 2}).json()["rating"] == -1
    assert analyst.post(f"/api/v1/forgeai/calls/{call.id}/rate", json={"value": 1}).json()["stars"] == 5
    assert analyst.post(f"/api/v1/forgeai/calls/{call.id}/rate", json={"stars": 6}).status_code == 422
    assert analyst.post(f"/api/v1/forgeai/calls/{call.id}/rate", json={}).status_code == 400
    week = analyst.get("/api/v1/forgeai/status").json()["week"]
    assert week["stars_n"] >= 1 and 1 <= week["stars_avg"] <= 5
    assert _client("viewer").post(f"/api/v1/forgeai/calls/{call.id}/rate", json={"stars": 3}).status_code == 403


def test_cli_prints_the_status_line():
    from app.cli_ops import format_pulse

    text = format_pulse({"local": {"on": True, "model": "qwen3", "reachable": True}, "test": {"status": "ok", "ms": 4200, "first_ms": 800},
                         "lane": {"pending": 2, "running": {"kind": "forgeai_chat", "seconds": 30}}, "worker_age_s": 4,
                         "timeout_s": 90, "thinking": False})
    assert "connection ok · test ok, first token 0.8 s, total 4.2 s" in text
    assert "2 waiting, running forgeai_chat for 30 s · worker last seen 4 s ago · timeout 90 s · thinking off" in text
