"""Dashboard squares, recent-incident columns, Escalation labels, asset Notes, form row order."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _ip() -> str:
    raw = uuid4().int
    return f"10.{180 + raw % 50}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


def test_templates_and_handbook_say_escalation_not_ladder():
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "Ladder" not in text, path.name
        assert "default ladder" not in text.lower(), path.name
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert "ladder" not in handbook.lower()
    assert "L1" in handbook and "L4" in handbook


