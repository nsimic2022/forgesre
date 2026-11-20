"""ForgeAI tuning: a skill's answers next to confirmed causes, and replays as trials on the LLM lane."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app import forgeai
from app.db import Base, SessionLocal, engine
from app.forgeai_tuning import overlap, replay, tuning_report
from app.main import create_app
from app.models import Asset, ForgeAiCall, Incident, Investigation, Job, User, utcnow
from app.security import hash_password
from app.seed import seed


class FakeLLM:
    def __init__(self, cause: str):
        self.cause, self.calls = cause, []
        self.last_error, self.last_raw, self.last_ms = "", "", 900

    def get_name(self):
        return "openai-compatible"

    def get_model(self):
        return "qwen3-4b"

    def complete_json(self, system, user, max_tokens=512):
        self.calls.append(system)
        answer = {"cause": self.cause, "first_checks": ["Check the disk usage graph."], "for_shift_lead": "One disk filled up."}
        self.last_raw = json.dumps(answer)
        return answer


def _resolved(db, cause: str, rating: str) -> Incident:
    asset = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    number = f"INC-T{uuid4().hex[:6]}"
    row = Incident(number=number, title="Odd tuning alarm", severity="HIGH", status="RESOLVED", fingerprint=f"OddTune{number}:x",
                   asset_id=asset.id, started_at=utcnow() - timedelta(hours=3), resolved_at=utcnow() - timedelta(hours=1),
                   root_cause=cause, ai_rating=rating)
    db.add(row)
    db.commit()
    return row


def test_overlap_is_a_word_hint():
    assert overlap("Log rotation stopped and /var/log filled the disk", "logrotate stopped; /var/log filled the disk") >= 0.5
    assert overlap("Uplink cable unplugged", "Disk full from backups") == 0.0


def test_report_and_replay_against_confirmed_causes(monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    monkeypatch.setattr("app.settings.Settings.ai_enabled", True)
    monkeypatch.setattr("app.settings.Settings.llm_url", "http://llm:8080")
    wrong = _resolved(db, "Backups wrote to /data and filled the disk", "wrong")
    good = _resolved(db, "The uplink cable on port 14 was unplugged", "correct")
    monkeypatch.setattr(forgeai, "_provider", lambda: FakeLLM("CPU was high"))
    for row in (wrong, good):
        forgeai.run(db, "incident", row, skill_slug="incident-generic", actor="t")
    db.add(ForgeAiCall(task="incident", skill_slug="incident-generic", status="rejected", reason="the answer has a number that is not in the facts"))
    db.commit()

    report = tuning_report(db, "incident-generic")
    mine = [e for e in report["examples"] if e["incident"] in {wrong.number, good.number}]
    assert mine[0]["incident"] == wrong.number and mine[0]["rating"] == "wrong"  # worst first
    assert mine[0]["confirmed"].startswith("Backups") and mine[0]["said"] == "CPU was high"
    assert any("number that is not in the facts" in r["reason"] for r in report["reject_reasons"])
    assert report["replay_candidates"] >= 2

    used_before = db.query(Investigation).filter(Investigation.incident_id == wrong.id).count()
    fake = FakeLLM("Backups filled the /data disk")
    monkeypatch.setattr(forgeai, "_provider", lambda: fake)
    out = replay(db, "incident-generic", limit=2, instructions="Look first at what wrote to the disk in the last hours, then answer.")
    assert out["asked"] == 2 and out["unsaved_text"] is True
    row = out["rows"][0]
    assert row["status"] == "trial" and row["before"] == "CPU was high"
    assert "Look first at what wrote to the disk" in fake.calls[0]
    # trials never become the incident's answer
    assert db.query(Investigation).filter(Investigation.incident_id == wrong.id).count() == used_before
    db.close()


def test_replay_api_is_engineer_only_and_runs_on_the_llm_lane(monkeypatch):
    from app.jobs import job_is_llm

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    monkeypatch.setattr("app.settings.Settings.ai_enabled", True)
    monkeypatch.setattr("app.settings.Settings.llm_url", "http://llm:8080")
    clients = {}
    for role in ("analyst", "engineer"):
        email = f"{role}-{uuid4().hex[:6]}@dc.local"
        db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
        db.commit()
        clients[role] = TestClient(create_app())
        clients[role].post("/login", data={"email": email, "password": "pw"})
    assert clients["analyst"].get("/api/v1/forgeai/skills/incident-generic/tuning").status_code == 200
    assert clients["analyst"].post("/api/v1/forgeai/skills/incident-generic/replay", json={}).status_code == 403
    assert clients["engineer"].post("/api/v1/forgeai/skills/explain-generic/replay", json={}).status_code == 400
    job_id = clients["engineer"].post("/api/v1/forgeai/skills/incident-generic/replay", json={"limit": 3}).json()["job"]
    job = db.get(Job, job_id)
    assert job.kind == "forgeai_replay" and job_is_llm(job) and job.payload["limit"] == 3
    db.delete(job)
    db.commit()
    db.close()


def test_cli_tuning_text():
    from app.cli_ops import format_tuning

    text = format_tuning({"skill": "linux-disk", "version": 2, "days": 60, "calls": 5, "used": 3, "rejected": 1, "errors": 1,
                          "ratings": {"wrong": 1}, "reject_reasons": [{"reason": "shell command", "count": 1}],
                          "examples": [{"incident": "INC-0007", "rating": "wrong", "said": "CPU was high", "confirmed": "Backups filled /data"}]})
    assert "1 wrong" in text and "rejected 1x: shell command" in text and "confirmed: Backups filled /data" in text
