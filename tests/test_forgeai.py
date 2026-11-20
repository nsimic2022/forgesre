"""ForgeAI: skills, the checked answer, the call log, tasks, trials and ratings."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import forgeai
from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, Investigation, Job, User, utcnow
from app.security import hash_password
from app.seed import seed


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []
        self.last_error = ""
        self.last_raw = ""
        self.last_ms = 1234

    def get_name(self):
        return "openai-compatible"

    def get_model(self):
        return "qwen3-4b"

    def complete_json(self, system, user, max_tokens=512):
        self.calls.append((system, user, max_tokens))
        if self.answer is None:
            self.last_error = "timed out"
            return None
        self.last_raw = json.dumps(self.answer)
        return self.answer


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    seed(session)
    yield session
    session.close()


def _incident(db, title="Interface Gi1/0/14 down", atype="Switch", fp="InterfaceDown") -> Incident:
    host = f"sw-{uuid4().hex[:6]}"
    asset = Asset(asset_id=host, hostname=host, ip="10.20.0.7", type=atype)
    db.add(asset)
    db.flush()
    row = Incident(number=f"INC-A{uuid4().hex[:6]}", title=title, severity="HIGH", status="OPEN",
                   fingerprint=f"{fp}:{host}", asset_id=asset.id, started_at=utcnow() - timedelta(minutes=5))
    db.add(row)
    db.commit()
    return row


GOOD = {"cause": "The uplink port went down on the access switch.", "first_checks": ["Check the far end of the link.", "Look at port errors."],
        "for_shift_lead": "An access switch lost one link; users behind it may be offline."}


def test_skills_are_seeded_and_picked_by_alert_and_type(db):
    switch = _incident(db)
    assert forgeai.pick_skill(db, "incident", switch).slug == "network-link"
    disk = _incident(db, "Disk /u02 92%", "Oracle DB", "DiskFull")
    assert forgeai.pick_skill(db, "incident", disk).slug == "linux-disk"
    other = _incident(db, "Strange thing", "Printer", "Weird")
    assert forgeai.pick_skill(db, "incident", other).slug == "incident-generic"
    assert forgeai.pick_skill(db, "explain", other).slug == "explain-generic"


def test_a_good_answer_is_used_kept_and_shown(db, monkeypatch):
    fake = FakeLLM(GOOD)
    monkeypatch.setattr(forgeai, "_provider", lambda: fake)
    row = _incident(db)
    call = forgeai.run(db, "incident", row, actor="ana@dc.local")
    assert call.status == "used" and call.model == "qwen3-4b" and call.duration_ms == 1234
    system, user, _ = fake.calls[0]
    assert "never act" in system and "Instructions for this kind of work" in system and "LINK-DOWN" in system
    assert json.loads(user)["incident"]["title"] == "Interface Gi1/0/14 down"
    inv = db.query(Investigation).filter_by(incident_id=row.id, provider="forgerca-llm").one()
    assert inv.likely_cause == GOOD["cause"] and inv.result["skill"] == "network-link" and inv.result["call_id"] == call.id


@pytest.mark.parametrize("answer,why", [
    ({**GOOD, "cause": "CPU was at 8319.75% for an hour."}, "number"),
    ({**GOOD, "first_checks": ["Run sudo reboot on the switch."]}, "shell"),
    ({**GOOD, "for_shift_lead": "Ping 10.99.99.99 from the core."}, "IP"),
    ({"cause": "x"}, "for_shift_lead"),
])
def test_bad_answers_are_rejected_with_the_reason(db, monkeypatch, answer, why):
    monkeypatch.setattr(forgeai, "_provider", lambda: FakeLLM(answer))
    row = _incident(db)
    call = forgeai.run(db, "incident", row)
    assert call.status == "rejected" and why in call.reason
    assert db.query(Investigation).filter_by(incident_id=row.id, provider="forgerca-llm").count() == 0


def test_no_answer_and_off_are_errors(db, monkeypatch):
    monkeypatch.setattr(forgeai, "_provider", lambda: FakeLLM(None))
    assert forgeai.run(db, "incident", _incident(db)).reason == "timed out"
    from rca.llm import NullLLM

    monkeypatch.setattr(forgeai, "_provider", lambda: NullLLM())
    assert "off" in forgeai.run(db, "incident", _incident(db)).reason


def test_trial_with_unsaved_text_never_becomes_the_answer(db, monkeypatch):
    fake = FakeLLM(GOOD)
    monkeypatch.setattr(forgeai, "_provider", lambda: fake)
    row = _incident(db)
    call = forgeai.run(db, "incident", row, trial=True, instructions="Be very brief.")
    assert call.status == "trial" and "Be very brief." in fake.calls[0][0]
    assert db.query(Investigation).filter_by(incident_id=row.id, provider="forgerca-llm").count() == 0
    assert "Be very brief." not in db.query(forgeai.ForgeAiSkill).filter_by(slug="network-link").one().instructions


def test_shift_and_postmortem_tasks(db, monkeypatch):
    monkeypatch.setattr(forgeai, "_provider", lambda: FakeLLM({"summary": "Quiet night with one switch link down.", "still_open": ["Link on sw"], "watch": []}))
    row = _incident(db)
    call = forgeai.run(db, "shift")
    sent = json.loads(call.user_prompt)  # whole JSON even with many incidents in the shared test database
    assert call.status == "used" and call.incident_id is None and sent["hours"] == 12 and sent["incidents"]
    assert len(call.user_prompt) <= forgeai.CONTEXT_MAX
    monkeypatch.setattr(forgeai, "_provider", lambda: FakeLLM({"paragraph": "The access switch lost its uplink and users were offline until it was fixed."}))
    assert forgeai.run(db, "postmortem", row).status == "used"


def _client(role: str) -> TestClient:
    db = SessionLocal()
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def test_api_queues_jobs_shows_prompts_by_role_and_rates(db, monkeypatch):
    from app.jobs import run_pending_jobs
    from app.settings import settings

    row = _incident(db)
    analyst, viewer = _client("analyst"), _client("viewer")
    assert analyst.post(f"/api/v1/incidents/{row.number}/forgeai", json={"task": "explain"}).status_code == 409
    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "ai": {"enabled": True, "llm": {"url": "http://127.0.0.1:8088/v1"}}})
    for job in db.query(Job).filter(Job.status == "pending").all():
        db.delete(job)
    db.commit()
    assert viewer.post(f"/api/v1/incidents/{row.number}/forgeai", json={"task": "explain"}).status_code == 403
    queued = analyst.post(f"/api/v1/incidents/{row.number}/forgeai", json={"task": "incident", "skill": "incident-generic"}).json()
    monkeypatch.setattr(forgeai, "_provider", lambda: FakeLLM(GOOD))
    run_pending_jobs(db, limit=5)
    state = analyst.get(f"/api/v1/forgeai/jobs/{queued['job']}").json()
    assert state["status"] == "done" and state["call_id"]
    view = analyst.get(f"/api/v1/incidents/{row.number}/view").json()["forgeai"]
    assert view["answer"]["cause"] == GOOD["cause"] and view["skill"] == "Any incident"
    assert {"slug": "network-link", "name": "Network link or interface"} in view["skills"]
    calls_a = analyst.get(f"/api/v1/incidents/{row.number}/forgeai").json()["calls"]
    calls_v = viewer.get(f"/api/v1/incidents/{row.number}/forgeai").json()["calls"]
    assert "system_prompt" in calls_a[0] and "system_prompt" not in calls_v[0]
    assert analyst.post(f"/api/v1/forgeai/calls/{state['call_id']}/rate", json={"value": -1}).json()["rating"] == -1
    assert analyst.post("/api/v1/forgeai/trial", json={}).status_code == 403
    status = analyst.get("/api/v1/forgeai/status").json()
    assert status["enabled"] and status["week"]["thumbs_down"] >= 1
