"""N's ordered V0.8 GUI/platform simplifications."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.security import hash_password
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(email: str, password: str = "testpass") -> TestClient:
    client = TestClient(app)
    posted = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert posted.status_code in {302, 303}
    return client


def _add_user(db, email: str, role: str) -> None:
    if db.query(User).filter_by(email=email).first() is None:
        db.add(
            User(
                email=email,
                name=role,
                password_hash=hash_password("testpass"),
                role=role,
            )
        )
        db.commit()


def test_readme_does_not_claim_dashboard_doctor_or_incidents_200():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "doctor lights" not in text
    assert "recent 200" not in text
    assert "10 per page" in text
    assert "System Health" in text


