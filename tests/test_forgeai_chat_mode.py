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
