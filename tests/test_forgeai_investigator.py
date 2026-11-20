"""ForgeAI investigator: read-only tools, a step limit, and answers that must cite existing evidence."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app import forgeai
from app.db import Base, SessionLocal, engine
from app.forgeai_investigator import MAX_STEPS, investigate, latest
from app.knowledge import similar_confirmed
from app.models import Asset, ForgeAiCall, Incident, Investigation, utcnow
from app.seed import seed


class Script:
    """Fake model: plays the given JSON turns in order, recording what it was sent."""

    def __init__(self, turns):
        self.turns, self.sent = list(turns), []
        self.last_error, self.last_raw, self.last_ms = "", "", 500

    def get_name(self):
        return "openai-compatible"

    def get_model(self):
        return "qwen3-4b"

    def complete_json(self, system, user, max_tokens=512):
        self.sent.append(json.loads(user))
        turn = self.turns.pop(0) if self.turns else {"tool": "similar", "args": {}}
        self.last_raw = json.dumps(turn)
        return turn


@pytest.fixture
def db(monkeypatch):
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    seed(session)
    monkeypatch.setattr("app.settings.Settings.ai_enabled", True)
    monkeypatch.setattr("app.settings.Settings.llm_url", "http://llm:8080")
    monkeypatch.setattr("app.services.query_prometheus_range", lambda expr, **kw: {"values": [41.0, 63.5, 97.0], "times": [1, 2, 3]})
    yield session
    session.close()


def _incident(db, title="Disk almost full", alert="FilesystemUsageHigh", cause="", status="OPEN") -> Incident:
    asset = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    number = f"INC-I{uuid4().hex[:6]}"
    row = Incident(number=number, title=title, severity="HIGH", status=status, fingerprint=f"{alert}:forge-demo-01:{number}",
                   asset_id=asset.id, started_at=utcnow() - timedelta(minutes=30), root_cause=cause)
    db.add(row)
    db.commit()
    return row


def test_tool_then_cited_answer_is_used_and_shown(db, monkeypatch):
    _incident(db, "Disk almost full on /data", cause="Nightly backups wrote into /data", status="RESOLVED")
    inc = _incident(db)
    model = Script([
        {"tool": "metric", "args": {"name": "disk_percent"}, "why": "did the disk climb"},
        {"answer": {"cause": "The disk climbed to 97.0 percent, like the earlier backup incident.", "confidence": "medium",
                    "evidence": ["E1", "E3"], "next_steps": ["Check what wrote to /data in the last hours."]}},
    ])
    monkeypatch.setattr(forgeai, "_provider", lambda: model)
    call = forgeai.run(db, "investigate", inc, actor="ana@dc.local")
    assert call.status == "used", call.reason
    evidence = model.sent[1]["evidence"]
    assert any("disk_percent" in e["text"] and "max 97.0" in e["text"] for e in evidence)
    assert any(e["source"] == "similar" and "Nightly backups" in e["text"] for e in evidence)
    assert model.sent[0]["tool_calls_left"] == MAX_STEPS
    shown = latest(db, inc)
    assert shown["answer"]["confidence"] == "medium" and [c["id"] for c in shown["cited"]] == ["E1", "E3"]
    # an investigation is not the cause-and-checks answer; it never writes an Investigation row
    assert db.query(Investigation).filter_by(incident_id=inc.id).count() == 0


@pytest.mark.parametrize("answer,why", [
    ({"cause": "Backups.", "confidence": "high", "evidence": ["E99"], "next_steps": []}, "does not exist"),
    ({"cause": "Backups.", "confidence": "high", "evidence": [], "next_steps": []}, "cites no evidence"),
    ({"cause": "Host 10.66.6.6 flooded it.", "confidence": "low", "evidence": ["E1"], "next_steps": []}, "IP address"),
    ({"cause": "Backups.", "confidence": "sure", "evidence": ["E1"], "next_steps": []}, "confidence"),
    ({"cause": "Backups.", "confidence": "low", "evidence": ["E1"], "next_steps": ["sudo rm -rf /data/old"]}, "shell"),
])
def test_bad_answers_are_rejected(db, monkeypatch, answer, why):
    inc = _incident(db)
    monkeypatch.setattr(forgeai, "_provider", lambda: Script([{"answer": answer}]))
    call = investigate(db, inc)
    assert call.status == "rejected" and why in call.reason
    assert latest(db, inc)["status"] == "rejected"


def test_tool_calls_are_limited(db, monkeypatch):
    inc = _incident(db)
    model = Script([])  # asks for a tool forever
    monkeypatch.setattr(forgeai, "_provider", lambda: model)
    call = investigate(db, inc)
    assert call.status == "rejected" and "no answer within" in call.reason
    assert len(model.sent) == MAX_STEPS + 1 and model.sent[-1]["note"] == "Answer now."
    assert db.get(ForgeAiCall, call.id).user_prompt.count("tool similar") == MAX_STEPS


def test_similar_confirmed_prefers_same_alert_and_asset(db):
    inc = _incident(db, "Disk almost full", "DiskFullXyz")
    same = _incident(db, "Disk almost full", "DiskFullXyz", cause="Log rotation stopped", status="RESOLVED")
    _incident(db, "Something else", "OtherThingXyz", cause="Fan failed", status="RESOLVED")
    rows = similar_confirmed(db, inc)
    assert rows[0]["incident"] == same.number and "same alert" in rows[0]["why"]


def test_api_queues_one_investigation_job(db):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import Job, User
    from app.security import hash_password

    inc = _incident(db)
    email = f"an-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name="a", password_hash=hash_password("pw"), role="analyst"))
    db.commit()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    job = client.post(f"/api/v1/incidents/{inc.number}/forgeai", json={"task": "investigate"}).json()["job"]
    explain = client.post(f"/api/v1/incidents/{inc.number}/forgeai", json={"task": "explain"}).json()["job"]
    assert job != explain
    row = db.get(Job, job)
    assert row.kind == "forgeai" and row.object_id == f"{inc.number}#investigate" and row.payload["task"] == "investigate"
    db.query(Job).filter(Job.id.in_([job, explain])).delete()
    db.commit()
