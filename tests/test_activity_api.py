"""Incident list day filter and CSV, the outbox and the audit log."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import AuditLog, Incident, Notification, User, utcnow
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


def test_day_filter_and_csv_with_bom():
    client = _client("viewer")
    db = SessionLocal()
    tag = uuid4().hex[:6]
    old = Incident(number=f"INC-D{tag}a", title=f"old {tag}", severity="HIGH", status="RESOLVED", fingerprint=f"d:{uuid4().hex}", started_at=utcnow() - timedelta(days=40))
    new = Incident(number=f"INC-D{tag}b", title=f"new {tag} čćž", severity="HIGH", status="OPEN", fingerprint=f"d:{uuid4().hex}", started_at=utcnow())
    db.add_all([old, new])
    db.commit()
    db.close()
    day = (utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
    page = client.get(f"/api/v1/incidents-page?tab=all&q={tag}&date_from={day}").json()
    assert [r["number"] for r in page["rows"]] == [f"INC-D{tag}b"]
    assert client.get("/api/v1/incidents-page?date_from=yesterday").status_code == 400
    csv = client.get(f"/api/v1/incidents-page.csv?q={tag}")
    assert csv.headers["content-type"].startswith("text/csv") and csv.content.startswith("﻿".encode())
    assert f"new {tag} čćž" in csv.text and f"old {tag}" in csv.text


def test_outbox_shows_failures_and_audit_is_admin_only():
    db = SessionLocal()
    Base.metadata.create_all(bind=engine)
    seed(db)
    tag = uuid4().hex[:6]
    db.add(Notification(channel="email", target=f"x{tag}@dc.local", subject=f"Report {tag}", body="", status="failed", error="SMTP 550", step_key="report"))
    db.add(AuditLog(actor=f"ana{tag}@dc.local", action="rule.update", object_type="rule", object_id="4"))
    db.commit()
    db.close()
    analyst = _client("analyst")
    rows = analyst.get(f"/api/v1/outbox?status=failed&q={tag}").json()["rows"]
    assert rows[0]["error"] == "SMTP 550" and rows[0]["incident"] == ""
    assert _client("viewer").get("/api/v1/outbox").status_code == 403
    assert analyst.get("/api/v1/audit").status_code == 403
    audit = _client("admin").get(f"/api/v1/audit?q=ana{tag}").json()
    assert audit["rows"][0]["action"] == "rule.update" and audit["rows"][0]["object"] == "rule:4"
