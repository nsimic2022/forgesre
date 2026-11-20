"""Notes column and Edit, report asset filters, TXT separators, per-asset HTML reports."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.history import incident_notes_snippet
from app.main import app
from app.models import Incident, IncidentNote, User, utcnow
from app.security import hash_password
from app.seed import seed
from app.services import (
    next_incident_number,
)

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client(email: str = "admin@forgesre.local", password: str = "testpass") -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return client


def _user(db, email: str, role: str) -> None:
    row = db.query(User).filter_by(email=email).first()
    if row is None:
        db.add(User(email=email, name=role, password_hash=hash_password("testpass"), role=role))
    else:
        row.role = role
        row.password_hash = hash_password("testpass")
    db.commit()


def _ip() -> str:
    number = uuid4().int
    return f"10.{number % 200 + 1}.{(number >> 8) % 200 + 1}.{(number >> 16) % 200 + 1}"


def _incident(db, title: str, asset_id: int | None = None) -> Incident:
    row = Incident(
        number=next_incident_number(db),
        title=title,
        severity="WARNING",
        status="OPEN",
        fingerprint=f"notes:{uuid4().hex}",
        started_at=utcnow(),
        summary=title,
        asset_id=asset_id,
    )
    db.add(row)
    db.flush()
    return row


def _headers(html: str) -> str:
    return html.split("<thead>", 1)[1].split("</thead>", 1)[0]


def _notes_cell(row_html: str) -> str:
    return row_html.split('class="col-notes"', 1)[1].split("</td>", 1)[0]


def test_notes_snippet_uses_latest_operator_notes():
    db = _db()
    row = _incident(db, "snippet")
    db.add(IncidentNote(incident_id=row.id, actor="a", body="older write-up"))
    db.flush()
    db.add(IncidentNote(incident_id=row.id, actor="b", body="latest write-up"))
    db.commit()
    db.refresh(row)
    text = incident_notes_snippet(row)
    assert text.startswith("latest write-up")
    assert "older write-up" in text
    empty = _incident(db, "empty")
    db.commit()
    db.refresh(empty)
    assert incident_notes_snippet(empty) == ""
    db.close()


def filled_alarm(db):
    row = db.query(Incident).filter(Incident.asset_id.isnot(None)).first()
    return row


