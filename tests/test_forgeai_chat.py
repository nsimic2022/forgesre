"""ForgeAI chat: private threads, general and incident questions, skills, history and limits."""

from __future__ import annotations

import json

import pytest

from app import forgeai
from app.db import Base, SessionLocal, engine
from app.models import ForgeAiChatMessage, Investigation, Job
from app.seed import seed

from test_forgeai import GOOD, FakeLLM, _client, _incident, _run_only


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    seed(session)
    yield session
    session.close()


def _on(monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "ai": {"enabled": True, "llm": {"url": "http://127.0.0.1:8088/v1"}}})


def _answer_job(db, answer_id: int) -> int:
    return db.query(Job).filter_by(kind="forgeai_chat", object_id=str(answer_id)).one().id


def test_general_chat_answers_with_history_and_keeps_line_breaks(db, monkeypatch):
    _on(monkeypatch)
    analyst = _client("analyst")
    chat = analyst.post("/api/v1/forgeai/chats", json={}).json()
    assert chat["title"] == "New chat" and chat["incident"] is None
    sent = analyst.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": "What is BGP flapping?"}).json()
    assert sent["answer"]["status"] == "pending" and sent["title"] == "What is BGP flapping?"
    fake = FakeLLM({"answer": "A BGP session that goes up and down.\nCheck the peer at 192.0.2.1 and hold timers."})
    monkeypatch.setattr(forgeai, "_provider", lambda: fake)
    _run_only(db, _answer_job(db, sent["answer"]["id"]))
    detail = analyst.get(f"/api/v1/forgeai/chats/{chat['id']}").json()
    reply = detail["messages"][-1]
    # a general chat may name addresses and numbers from general knowledge; lines stay separate
    assert reply["status"] == "done" and "\n" in reply["text"] and reply["call_id"]
    assert "general knowledge" in fake.calls[0][0]
    assert {s["task"] for s in detail["skills"]} == {"chat"}

    second = analyst.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": "And how do I see it?"}).json()
    _run_only(db, _answer_job(db, second["answer"]["id"]))
    context = json.loads(fake.calls[1][1])
    assert context["operator_question"] == "And how do I see it?"
    assert [t["who"] for t in context["earlier_in_this_chat"]] == ["user", "assistant"]


def test_chats_are_private_and_viewers_have_none(db, monkeypatch):
    _on(monkeypatch)
    mine, other, viewer = _client("analyst"), _client("analyst"), _client("viewer")
    chat = mine.post("/api/v1/forgeai/chats", json={"title": "Night notes"}).json()
    assert other.get(f"/api/v1/forgeai/chats/{chat['id']}").status_code == 404
    assert other.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": "hi"}).status_code == 404
    assert all(c["id"] != chat["id"] for c in other.get("/api/v1/forgeai/chats").json()["chats"])
    assert viewer.get("/api/v1/forgeai/chats").status_code == 403
    assert mine.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": "   "}).status_code == 400
    assert mine.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": "x", "cloud": True}).status_code == 403
    assert other.delete(f"/api/v1/forgeai/chats/{chat['id']}").status_code == 404
    assert mine.delete(f"/api/v1/forgeai/chats/{chat['id']}").json() == {"deleted": chat["id"]}
    assert mine.get(f"/api/v1/forgeai/chats/{chat['id']}").status_code == 404


def test_incident_chat_uses_facts_checks_and_skills(db, monkeypatch):
    _on(monkeypatch)
    row = _incident(db)
    analyst = _client("analyst")
    chat = analyst.post("/api/v1/forgeai/chats", json={"incident": row.number, "reuse": True}).json()
    again = analyst.post("/api/v1/forgeai/chats", json={"incident": row.number, "reuse": True}).json()
    assert again["id"] == chat["id"] and chat["incident"] == row.number
    skills = {s["slug"]: s["task"] for s in analyst.get(f"/api/v1/forgeai/chats/{chat['id']}").json()["skills"]}
    assert skills["linux-disk"] == "incident" and "ask-generic" not in skills

    # an invented address is rejected in a chat about an incident, like any incident answer
    sent = analyst.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": "Which peer?"}).json()
    monkeypatch.setattr(forgeai, "_provider", lambda: FakeLLM({"answer": "The peer 203.0.113.9 dropped."}))
    _run_only(db, _answer_job(db, sent["answer"]["id"]))
    db.expire_all()
    failed = db.get(ForgeAiChatMessage, sent["answer"]["id"])
    assert failed.status == "error" and "IP address" in failed.text

    # a skill re-runs "cause and first checks" as a trial and shows it as text
    sent = analyst.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"skill": "network-link"}).json()
    assert sent["question"]["text"] == "/network-link"
    monkeypatch.setattr(forgeai, "_provider", lambda: FakeLLM(GOOD))
    _run_only(db, _answer_job(db, sent["answer"]["id"]))
    db.expire_all()
    reply = db.get(ForgeAiChatMessage, sent["answer"]["id"])
    assert reply.status == "done" and reply.text.startswith("Cause: The uplink port") and "First checks:\n1." in reply.text
    assert not db.query(Investigation).filter_by(incident_id=row.id, provider="forgerca-llm").count()


def test_incident_skill_needs_an_incident_and_lane_limit(db, monkeypatch):
    _on(monkeypatch)
    analyst = _client("analyst")
    chat = analyst.post("/api/v1/forgeai/chats", json={}).json()
    assert analyst.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"skill": "linux-disk"}).status_code == 400
    codes = [analyst.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": f"q{i}"}).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]


def test_failed_job_finishes_the_waiting_answer(db, monkeypatch):
    _on(monkeypatch)
    analyst = _client("analyst")
    chat = analyst.post("/api/v1/forgeai/chats", json={}).json()
    sent = analyst.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": "hello"}).json()

    def boom():
        raise RuntimeError("model crashed")

    monkeypatch.setattr(forgeai, "_provider", boom)
    _run_only(db, _answer_job(db, sent["answer"]["id"]))
    db.expire_all()
    msg = db.get(ForgeAiChatMessage, sent["answer"]["id"])
    assert msg.status == "error" and "model crashed" in msg.text


def test_forgeai_off_says_so(db, monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "ai": {"enabled": False}})
    analyst = _client("analyst")
    chat = analyst.post("/api/v1/forgeai/chats", json={}).json()
    response = analyst.post(f"/api/v1/forgeai/chats/{chat['id']}/messages", json={"text": "hi"})
    assert response.status_code == 409 and "RCA / AI" in response.json()["detail"]
