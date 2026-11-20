"""ForgeAI digest: database facts always, then one checked ForgeAI shift summary; sent from the LLM lane."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, Job, ScheduledReport, User, utcnow
from app.security import hash_password
from app.seed import seed


def _client(role: str) -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def _incidents(db) -> str:
    asset = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    tag = uuid4().hex[:6]
    for n, (sev, status) in enumerate((("CRITICAL", "OPEN"), ("HIGH", "RESOLVED"))):
        db.add(Incident(number=f"INC-D{tag}{n}", title=f"Digest{tag}", severity=sev, status=status,
                        fingerprint=f"Digest{tag}:{n}", asset_id=asset.id, started_at=utcnow() - timedelta(hours=2)))
    db.commit()
    return tag


def test_digest_mail_has_facts_and_the_forgeai_summary(monkeypatch):
    from app import forgeai, morning_digest

    monkeypatch.setattr(morning_digest, "digest_facts", lambda db, hours: {
        "hours": hours, "new": 2, "critical": 1, "high": 1, "other": 0, "resolved": 1, "open_count": 1,
        "open": ["INC-0007 · CRITICAL · Disk full · db-01"], "noisy": [], "latest": ["INC-0007 · CRITICAL · Disk full · db-01"]})
    monkeypatch.setattr("app.settings.Settings.ai_enabled", True)
    monkeypatch.setattr("app.settings.Settings.llm_url", "http://llm:8080")
    asked: list[int] = []

    def fake_run(db, task, incident, *, actor, hours):
        asked.append(hours)
        return SimpleNamespace(status="used", reason="", id=1, answer={"summary": "One disk filled up overnight.", "still_open": ["INC-0007 on db-01"], "watch": []})

    monkeypatch.setattr(forgeai, "run", fake_run)
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr("app.services._send_smtp", lambda to, subject, body, html=None: sent.append((to, body, html or "")))
    monkeypatch.setattr("app.settings.Settings.email_enabled", True)
    monkeypatch.setattr("app.settings.Settings.smtp_host", "smtp.example.test")
    db = SessionLocal()
    morning_digest.send_digest(db, recipients=["noc@dc.local"], actor="t", name="morning", hours=24)
    db.close()
    assert asked == [24]
    to, body, html = sent[0]
    assert "2 new incidents in the last 24 hours (1 critical, 1 high" in body
    assert "One disk filled up overnight." in body and "Still open: INC-0007 on db-01" in body
    assert "ForgeAI" in html and "Disk full" in html


def test_digest_without_forgeai_says_why_and_skips_demo(monkeypatch):
    from app import morning_digest

    monkeypatch.setattr("app.settings.Settings.ai_enabled", False)
    db = SessionLocal()
    seed(db)
    tag = _incidents(db)
    facts = morning_digest.digest_facts(db, 24)
    ai = morning_digest.ask_forgeai(db, 24, "t")
    db.close()
    # the seeded demo host makes these DEMO lab incidents: they are not in the digest
    assert not any(tag in line for line in facts["latest"])
    _subject, text, _html = morning_digest.digest_message("m", facts, ai)
    assert "ForgeAI is off. The facts above are complete." in text


def test_forgeai_kind_from_the_new_ui_and_send_now_uses_the_llm_lane(monkeypatch):
    from app.jobs import job_is_llm

    client = _client("analyst")
    body = {"name": f"morning-{uuid4().hex[:4]}", "recipients": ["noc@dc.local"], "schedule": "cron", "cron": "0 7 * * *", "kind": "forgeai"}
    made = client.post("/api/v1/scheduled-reports", json=body).json()
    assert made["kind"] == "forgeai"
    preview = client.get(f"/api/v1/scheduled-reports/{made['id']}/preview").json()
    assert "ForgeAI digest" in preview["subject"] and "new incidents in the last 24 hours" in preview["text"]
    assert "ForgeAI writes its summary here" in preview["text"]
    client.post(f"/api/v1/scheduled-reports/{made['id']}/send")
    db = SessionLocal()
    job = db.query(Job).filter_by(kind="forgeai_digest", object_id=f"report-{made['id']}").one()
    assert job_is_llm(job) and job.payload["hours"] == 24 and job.payload["recipients"] == ["noc@dc.local"]
    edited = client.put(f"/api/v1/scheduled-reports/{made['id']}", json={**body, "kind": "performance"}).json()
    assert edited["kind"] == "performance" and db.get(ScheduledReport, made["id"]).kind == "performance"
    db.query(Job).filter_by(id=job.id).delete()
    db.commit()
    db.close()
