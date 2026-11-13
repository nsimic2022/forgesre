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


def test_viewer_gets_403_on_health_refresh_but_can_open_health_ui():
    db = _db()
    client = _client(db, "viewer")
    page = client.get("/health-ui")
    assert page.status_code == 200
    assert "Run doctor" not in page.text
    posted = client.post("/health-ui/refresh", follow_redirects=False)
    assert posted.status_code == 403
    db.close()


def test_analyst_cannot_force_health_refresh():
    db = _db()
    client = _client(db, "analyst")
    assert client.get("/health-ui").status_code == 200
    assert client.post("/health-ui/refresh", follow_redirects=False).status_code == 403
    db.close()


def test_engineer_and_admin_can_force_health_refresh():
    db = _db()
    for role in ("engineer", "admin"):
        client = _client(db, role)
        page = client.get("/health-ui")
        assert page.status_code == 200
        assert "Run doctor" in page.text
        posted = client.post("/health-ui/refresh", follow_redirects=False)
        assert posted.status_code == 303
        assert posted.headers["location"] == "/health-ui"
    db.close()


def test_health_refresh_does_not_change_metrics_or_probes():
    db = _db()
    viewer = _client(db, "viewer")
    assert viewer.get("/api/v1/system/doctor").status_code == 200
    anon = TestClient(app)
    assert anon.get("/api/v1/health").status_code == 200
    metrics = anon.get("/metrics")
    assert metrics.status_code == 200
    db.close()
