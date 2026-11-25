"""ForgeAI chat about an incident: strict / balanced / open rules; safety rules never move."""

from __future__ import annotations

from uuid import uuid4

from app import forgeai
from app.db import Base, SessionLocal, engine
from app.models import Incident, utcnow
from app.settings import settings
from test_incident_tools import _client


def test_rules_by_mode(monkeypatch):
    inc = Incident(number="INC-X", title="t")
    assert forgeai.chat_rules(None, "strict") is forgeai.CHAT_RULES
    assert forgeai.chat_rules(inc, "strict") is forgeai.BASE_RULES
    assert forgeai.chat_rules(inc, "open") is forgeai.INCIDENT_CHAT_OPEN
    assert "simple arithmetic" in forgeai.chat_rules(inc, "balanced")
    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "ai": {**(settings.yaml.get("ai") or {}), "forgeai": {"chat_mode": "strict"}}})
    assert forgeai.chat_rules(inc, "") is forgeai.BASE_RULES
    for rules in (forgeai.BASE_RULES, forgeai.INCIDENT_CHAT_BALANCED, forgeai.INCIDENT_CHAT_OPEN, forgeai.CHAT_RULES):
        assert "you never act" in rules and "restart, delete or change" in rules


def test_chat_mode_api_and_prompt():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    inc = Incident(number=f"INC-CM-{uuid4().hex[:6]}", title="Disk full", severity="HIGH", status="OPEN", fingerprint=f"cm:{uuid4().hex}", started_at=utcnow())
    db.add(inc)
    db.commit()
    try:
        client = _client("analyst")
        chat = client.post("/api/v1/forgeai/chats", json={"incident": inc.number}).json()
        assert chat["mode"] == "" and chat["mode_effective"] in ("strict", "balanced", "open")
        changed = client.post(f"/api/v1/forgeai/chats/{chat['id']}/mode", json={"mode": "open"}).json()
        assert changed["mode"] == "open" and changed["mode_effective"] == "open"
        assert client.post(f"/api/v1/forgeai/chats/{chat['id']}/mode", json={"mode": "wild"}).status_code == 422
        general = client.post("/api/v1/forgeai/chats", json={}).json()
        assert general["mode_effective"] == "general"
        forgeai.seed_skills(db)
        skill = forgeai.pick_skill(db, "chat", inc)
        system, user = forgeai.build_prompts(db, "chat", skill, inc, question="koliko je 5 plus 2", mode="balanced")
        assert "simple arithmetic" in system and "koliko je 5 plus 2" in user
        system, _ = forgeai.build_prompts(db, "chat", skill, inc, question="x", mode="strict")
        assert "Use only the facts in the user message" in system
    finally:
        db.delete(inc)
        db.commit()
        db.close()


class _PlainLLM:
    """A model that ignores the JSON wrapper, as small models do with open questions."""

    last_error, last_ms = "", 500

    def __init__(self, raw: str):
        self.last_raw = raw

    def get_name(self):
        return "openai-compatible"

    def get_model(self):
        return "opsllm-7b"

    def complete_json(self, system, user, max_tokens=512):
        self.last_error = "LLM returned text that was not JSON"
        return None


def test_chat_answer_without_json_is_still_shown(monkeypatch):
    from app.seed import seed

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    monkeypatch.setattr("app.settings.Settings.ai_enabled", True)
    monkeypatch.setattr("app.settings.Settings.llm_url", "http://llm:8080")
    inc = Incident(number=f"INC-PT-{uuid4().hex[:6]}", title="Linux load high", severity="HIGH", status="OPEN", fingerprint=f"pt:{uuid4().hex}", started_at=utcnow())
    db.add(inc)
    db.commit()
    try:
        monkeypatch.setattr(forgeai, "_provider", lambda: _PlainLLM("<think>ok</think>\nCheck top and iostat; a backup job often causes 12 load on 8 cores."))
        call = forgeai.run(db, "chat", inc, actor="t", question="why is load high?", mode="open")
        assert call.status in ("used", "trial") and call.answer["answer"].startswith("Check top and iostat")
        monkeypatch.setattr(forgeai, "_provider", lambda: _PlainLLM("<think>only thinking, no answer"))
        call = forgeai.run(db, "chat", inc, actor="t", question="why?", mode="open")
        assert call.status == "error" and "not JSON" in call.reason
        # an incident task keeps the JSON rule
        monkeypatch.setattr(forgeai, "_provider", lambda: _PlainLLM("The disk is full."))
        assert forgeai.run(db, "explain", inc, actor="t").status == "error"
    finally:
        db.delete(inc)
        db.commit()
        db.close()
