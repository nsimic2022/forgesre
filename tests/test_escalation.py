"""Escalation page create form + policy step parser."""

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import EscalationPolicy
from app.seed import seed
from app.services import parse_policy_steps


def test_parse_policy_steps_minutes_role():
    steps = parse_policy_steps("0 team\n15 team-lead\n30m engineer")
    assert steps[0] == {"after_minutes": 0, "target": "team", "channel": "email"}
    assert steps[1]["after_minutes"] == 15
    assert steps[1]["target"] == "team-lead"
    assert steps[2]["after_minutes"] == 30
    assert steps[2]["target"] == "engineer"


def test_parse_policy_steps_empty_uses_default_warning():
    steps = parse_policy_steps("   \n")
    assert [s["after_minutes"] for s in steps] == [0, 15, 30]


def test_escalation_page_has_create_cancel_and_saves_policy():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    client = TestClient(app)
    login = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}
    page = client.get("/escalation")
    assert page.status_code == 200
    assert 'href="/escalation">Cancel</a>' in page.text
    assert 'id="escalation-form"' in page.text
    assert "Default warning" in page.text
    created = client.post(
        "/escalation",
        data={
            "name": "Night ladder",
            "slug": "night-ladder",
            "steps": "0 team\n45 engineer",
        },
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    db.expire_all()
    row = db.query(EscalationPolicy).filter_by(slug="night-ladder").one()
    assert row.name == "Night ladder"
    assert row.steps[0]["after_minutes"] == 0
    assert row.steps[1]["after_minutes"] == 45
    assert row.steps[1]["target"] == "engineer"
    again = client.get("/escalation")
    assert "Night ladder" in again.text
    play = client.get("/playrules")
    assert play.status_code == 200
    assert 'name="escalation_policy_id"' in play.text
    assert "Night ladder" in play.text
    db.close()
