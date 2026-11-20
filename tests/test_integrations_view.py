"""Settings › Integrations cards and the background sync job."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Job, JournalEntry, User
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


def test_cards_join_status_links_settings_and_last_sync(monkeypatch):
    import app.api as api

    monkeypatch.setattr(api, "doctor_payload", lambda **kw: {"components": {
        "zabbix": {"status": "error", "why": "HTTP 401"}, "grafana": {"status": "ok", "why": ""}}})
    db = SessionLocal()
    db.add(JournalEntry(module="zabbix", action="sync", status="error", summary="Zabbix sync failed"))
    db.commit()
    db.close()
    data = _client("admin").get("/api/v1/integrations", headers={"host": "noc.dc.local:8080"}).json()
    zabbix = next(c for c in data["sources"] if c["id"] == "zabbix")
    assert zabbix["status"] == "error" and zabbix["why"] == "HTTP 401"
    assert zabbix["last_sync"]["summary"] == "Zabbix sync failed" and zabbix["sync"] == "zabbix" and zabbix["test"] == "zabbix"
    assert "inventory.zabbix.url" in zabbix["settings"] and "ZABBIX_API_TOKEN" in zabbix["settings"]
    grafana = next(c for c in data["stack"] if c["id"] == "grafana")
    assert grafana["links"] and grafana["links"][0]["url"].startswith("http")
    assert {c["id"] for c in data["stack"]} >= {"prometheus", "alertmanager", "loki", "alloy", "snmp", "llm"}


def test_sync_is_admin_only_and_runs_as_a_job(monkeypatch):
    import app.integrations_view as iv
    from app.jobs import run_pending_jobs

    assert _client("engineer").post("/api/v1/integrations/zabbix/sync").status_code == 403
    admin = _client("admin")
    assert admin.post("/api/v1/integrations/grafana/sync").status_code == 404
    db = SessionLocal()
    for job in db.query(Job).filter(Job.status == "pending").all():
        db.delete(job)
    db.commit()
    assert admin.post("/api/v1/integrations/zabbix/sync").json()["queued"] is True
    calls = []
    monkeypatch.setattr(iv, "run_sync", lambda db, target: calls.append(target) or {"hosts": 3})
    run_pending_jobs(db, limit=5)
    job = db.query(Job).filter_by(kind="integration_sync", object_id="zabbix").order_by(Job.id.desc()).first()
    assert calls == ["zabbix"] and job.status == "done" and job.payload["result"] == {"hosts": 3}
    db.close()
