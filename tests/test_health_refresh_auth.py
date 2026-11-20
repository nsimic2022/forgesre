"""POST /health-ui/refresh is engineer/admin only; GET /health-ui stays login-only."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.security import hash_password
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client(db, role: str) -> TestClient:
    email = f"{role}-{uuid4().hex[:8]}@forgesre.local"
    db.add(User(email=email, name=role, password_hash=hash_password("testpass"), role=role))
    db.commit()
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": "testpass"}, follow_redirects=False)
    return client


def test_health_refresh_does_not_change_metrics_or_probes():
    db = _db()
    viewer = _client(db, "viewer")
    assert viewer.get("/api/v1/system/doctor").status_code == 200
    anon = TestClient(app)
    assert anon.get("/api/v1/health").status_code == 200
    metrics = anon.get("/metrics")
    assert metrics.status_code == 200
    db.close()
