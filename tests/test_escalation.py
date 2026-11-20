"""Escalation policies: create / edit / remove through the API, the step parser (email only)."""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.main import app
from app.models import EscalationPolicy, Job, Notification, Playrule
from app.seed import seed
from app.services import (
    escalation_steps,
    ingest_alertmanager,
    parse_policy_steps,
    policy_step_problems,
    policy_steps_text,
)


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _levels(*steps: tuple[int, str]) -> list[dict]:
    return [{"level": i, "on": True, "minutes": m, "target": target} for i, (m, target) in enumerate(steps, 1)]


def _create(client: TestClient, *steps: tuple[int, str], name: str = "") -> str:
    slug = f"esc-{uuid4().hex[:8]}"
    posted = client.post("/api/v1/automation/policies", json={"name": name or slug, "slug": slug, "levels": _levels(*steps)})
    assert posted.status_code == 200, posted.text
    return posted.json()["slug"]


def test_parse_policy_steps_minutes_role():
    steps = parse_policy_steps("0 team\n15 team-lead\n30m engineer")
    assert steps[0] == {"after_minutes": 0, "target": "team", "channel": "email"}
    assert steps[1]["after_minutes"] == 15
    assert steps[1]["target"] == "team-lead"
    assert steps[2]["after_minutes"] == 30
    assert steps[2]["target"] == "engineer"


def test_parse_policy_steps_empty_uses_default_warning():
    steps = parse_policy_steps("   \n")
    assert [s["after_minutes"] for s in steps] == [0, 15, 30, 60]
    assert steps[3]["target"] == "manager"


def test_policy_steps_text_round_trips_the_form_format():
    text = "0 team\n15 oncall@dc.local\n30 engineer"
    steps = parse_policy_steps(text)
    assert policy_steps_text(steps) == text
    assert parse_policy_steps(policy_steps_text(steps)) == steps


def test_webhook_and_urls_are_refused_not_silently_mailed():
    assert policy_step_problems("0 team\n15 team-lead email\n30 oncall@dc.local") == []
    problems = policy_step_problems("0 team\n15 team-lead webhook\n30 https://hooks.example/x")
    assert len(problems) == 2
    assert problems[0].startswith("Line 2 (15 team-lead webhook): webhook is not supported")
    assert "email only" in problems[0]
    assert problems[1].startswith("Line 3 (30 https://hooks.example/x): a URL is not a recipient")
    assert policy_step_problems("5 sms")[0].startswith("Line 1 (5 sms): sms is not supported")
    assert all(step["channel"] == "email" for step in parse_policy_steps("0 team webhook\n5 engineer slack"))


def test_ingest_uses_the_edited_policy(monkeypatch):
    db = _db()
    sent: list[str] = []
    monkeypatch.setattr("app.settings.Settings.email_enabled", True)
    monkeypatch.setattr("app.settings.Settings.smtp_host", "smtp.example.test")
    monkeypatch.setattr("app.services._send_smtp", lambda target, *_a, **_k: sent.append(target))
    client = _client()
    slug = _create(client, (0, "before@dc.local"))
    policy = db.query(EscalationPolicy).filter_by(slug=slug).one()
    alertname = f"EscEdit{uuid4().hex[:6]}"
    db.add(Playrule(name=slug, enabled=True, severity="warning", condition={"alertname": alertname}, escalation_policy_id=policy.id))
    db.commit()
    start_job = db.query(Job.id).order_by(Job.id.desc()).limit(1).scalar() or 0

    def fire() -> Notification:
        host = create_manual_asset(
            db,
            hostname=f"esc-{uuid4().hex[:6]}",
            ip="10.67.%d.%d" % (uuid4().int % 250 + 1, uuid4().int % 230 + 20),
            type="Linux Server",
            actor="tester",
            owner_email="owner@dc.local",
        )
        payload = {
            "status": "firing",
            "alerts": [{"status": "firing", "labels": {"alertname": alertname, "asset": host.asset_id, "severity": "warning"}, "annotations": {}}],
        }
        incident = ingest_alertmanager(db, payload)[0]
        db.expire_all()
        return db.query(Notification).filter_by(incident_id=incident.id, step_key="immediate").one()

    assert fire().target == "before@dc.local"
    saved = client.put(f"/api/v1/automation/policies/{policy.id}", json={"name": "After edit", "levels": _levels((0, "after@dc.local"), (15, "team-lead"))})
    assert saved.status_code == 200, saved.text
    edited = fire()
    assert edited.target == "after@dc.local"
    assert edited.channel == "email"
    assert sent == ["before@dc.local", "after@dc.local"]
    db.query(Job).filter(Job.id > start_job, Job.status == "pending").delete(synchronize_session=False)
    db.commit()
    db.close()


def test_seed_adds_l4_to_vanilla_three_step_default():
    db = _db()
    policy = db.query(EscalationPolicy).filter_by(slug="default-warning").one()
    policy.steps = [
        {"after_minutes": 0, "target": "team", "channel": "email"},
        {"after_minutes": 15, "target": "team-lead", "channel": "email"},
        {"after_minutes": 30, "target": "engineer", "channel": "email"},
    ]
    db.commit()
    seed(db)
    db.expire_all()
    row = db.query(EscalationPolicy).filter_by(slug="default-warning").one()
    assert [str(step["target"]) for step in row.steps] == ["team", "team-lead", "engineer", "manager"]
    assert int(row.steps[3]["after_minutes"]) == 60
    db.close()




def test_api_policy_four_levels_edit_and_remove_moves_rules_to_default():
    db = _db()
    client = _client()
    slug = _create(client, (0, "team"), (15, "team-lead"), (30, "engineer"), (60, "duty@dc.local"), name="Four rungs")
    row = db.query(EscalationPolicy).filter_by(slug=slug).one()
    assert [(int(s["after_minutes"]), s["target"], int(s["level"])) for s in row.steps] == [
        (0, "team", 1), (15, "team-lead", 2), (30, "engineer", 3), (60, "duty@dc.local", 4),
    ]
    saved = client.put(f"/api/v1/automation/policies/{row.id}", json={"name": "Edited ladder", "levels": _levels((0, "noc@dc.local"), (5, "team-lead"))})
    assert saved.status_code == 200 and saved.json()["slug"] == slug
    db.expire_all()
    row = db.query(EscalationPolicy).filter_by(slug=slug).one()
    assert row.name == "Edited ladder"
    assert [(int(s["after_minutes"]), s["target"]) for s in row.steps] == [(0, "noc@dc.local"), (5, "team-lead")]
    rule = Playrule(name=f"esc-rule-{uuid4().hex[:6]}", enabled=True, severity="warning", condition={"alertname": "EscRemove"}, escalation_policy_id=row.id)
    db.add(rule)
    db.commit()
    assert client.delete(f"/api/v1/automation/policies/{row.id}").status_code == 200
    db.expire_all()
    default = db.query(EscalationPolicy).filter_by(slug="default-warning").one()
    assert db.query(EscalationPolicy).filter_by(slug=slug).first() is None
    assert db.get(Playrule, rule.id).escalation_policy_id == default.id
    assert client.delete(f"/api/v1/automation/policies/{default.id}").status_code == 400
    db.close()


def test_legacy_webhook_step_is_mailed_as_email():
    db = _db()
    slug = f"esc-legacy-{uuid4().hex[:6]}"
    policy = EscalationPolicy(name="Legacy hook", slug=slug, steps=[{"after_minutes": 0, "target": "team", "channel": "webhook"}])
    db.add(policy)
    db.commit()
    rule = Playrule(name=slug, enabled=True, severity="warning", condition={"alertname": "Legacy"}, escalation_policy_id=policy.id)
    db.add(rule)
    db.commit()

    class _Incident:
        playrule = rule

    assert escalation_steps(_Incident())[0]["channel"] == "email"
    db.close()
