"""Escalation page create / edit / remove + policy step parser (email only)."""

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


def _create(client: TestClient, steps: str, *, name: str = "") -> str:
    slug = f"esc-{uuid4().hex[:8]}"
    posted = client.post(
        "/escalation",
        data={"name": name or slug, "slug": slug, "steps": steps},
        follow_redirects=False,
    )
    assert posted.status_code == 302, posted.text
    return slug


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


def test_create_with_webhook_shows_message_and_saves_nothing():
    db = _db()
    client = _client()
    slug = f"esc-hook-{uuid4().hex[:6]}"
    posted = client.post(
        "/escalation",
        data={"name": "Hook ladder", "slug": slug, "steps": "0 team\n10 engineer webhook"},
        follow_redirects=False,
    )
    assert posted.status_code == 400
    assert 'class="error" role="alert"' in posted.text
    assert "webhook is not supported" in posted.text
    assert "Escalation sends email only" in posted.text
    assert "10 engineer webhook</textarea>" in posted.text
    db.expire_all()
    assert db.query(EscalationPolicy).filter_by(slug=slug).first() is None
    db.close()


def test_edit_policy_prefills_form_and_saves_new_steps():
    db = _db()
    client = _client()
    slug = _create(client, "0 team\n20 engineer", name="Edit me")
    row = db.query(EscalationPolicy).filter_by(slug=slug).one()
    listing = client.get("/escalation")
    assert f'href="/escalation?edit={row.id}#escalation-form">Edit</a>' in listing.text
    form = client.get(f"/escalation?edit={row.id}")
    assert form.status_code == 200
    assert "Edit escalation policy" in form.text
    assert f'action="/escalation/{row.id}/update"' in form.text
    assert "0 team\n20 engineer</textarea>" in form.text
    assert f'value="{slug}" readonly' in form.text
    saved = client.post(
        f"/escalation/{row.id}/update",
        data={"name": "Edited ladder", "steps": "0 noc@dc.local\n5 team-lead"},
        follow_redirects=False,
    )
    assert saved.status_code == 302
    db.expire_all()
    row = db.query(EscalationPolicy).filter_by(slug=slug).one()
    assert row.name == "Edited ladder"
    assert row.steps == [
        {"after_minutes": 0, "target": "noc@dc.local", "channel": "email"},
        {"after_minutes": 5, "target": "team-lead", "channel": "email"},
    ]
    refused = client.post(
        f"/escalation/{row.id}/update",
        data={"name": "Edited ladder", "steps": "0 noc@dc.local webhook"},
        follow_redirects=False,
    )
    assert refused.status_code == 400
    assert "webhook is not supported" in refused.text
    db.expire_all()
    assert db.query(EscalationPolicy).filter_by(slug=slug).one().steps[0]["target"] == "noc@dc.local"
    db.close()


def test_remove_policy_moves_playrules_to_default_and_default_cannot_be_removed():
    db = _db()
    client = _client()
    slug = _create(client, "0 team")
    row = db.query(EscalationPolicy).filter_by(slug=slug).one()
    rule = Playrule(name=f"esc-rule-{uuid4().hex[:6]}", enabled=True, severity="warning", condition={"alertname": "EscRemove"}, escalation_policy_id=row.id)
    db.add(rule)
    db.commit()
    listing = client.get("/escalation")
    assert f'action="/escalation/{row.id}/delete"' in listing.text
    assert "1 playrule(s) using it move to Default warning" in listing.text
    default = db.query(EscalationPolicy).filter_by(slug="default-warning").one()
    assert f'action="/escalation/{default.id}/delete"' not in listing.text
    removed = client.post(f"/escalation/{row.id}/delete", follow_redirects=False)
    assert removed.status_code == 302
    db.expire_all()
    assert db.query(EscalationPolicy).filter_by(slug=slug).first() is None
    assert db.get(Playrule, rule.id).escalation_policy_id == default.id
    blocked = client.post(f"/escalation/{default.id}/delete", follow_redirects=False)
    assert blocked.status_code == 400
    assert db.query(EscalationPolicy).filter_by(slug="default-warning").first() is not None
    db.close()


def test_ingest_uses_the_edited_policy(monkeypatch):
    db = _db()
    sent: list[str] = []
    monkeypatch.setattr("app.settings.Settings.email_enabled", True)
    monkeypatch.setattr("app.settings.Settings.smtp_host", "smtp.example.test")
    monkeypatch.setattr("app.services._send_smtp", lambda target, *_a, **_k: sent.append(target))
    client = _client()
    slug = _create(client, "0 before@dc.local")
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
    saved = client.post(
        f"/escalation/{policy.id}/update",
        data={"name": "After edit", "steps": "0 after@dc.local\n15 team-lead"},
        follow_redirects=False,
    )
    assert saved.status_code == 302
    edited = fire()
    assert edited.target == "after@dc.local"
    assert edited.channel == "email"
    assert sent == ["before@dc.local", "after@dc.local"]
    db.query(Job).filter(Job.id > start_job, Job.status == "pending").delete(synchronize_session=False)
    db.commit()
    db.close()


def test_legacy_webhook_step_is_labelled_and_mailed_as_email():
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
    page = _client().get("/escalation?per_page=100")
    assert "webhook: sent as email" in page.text
    form = _client().get(f"/escalation?edit={policy.id}")
    assert "0 team</textarea>" in form.text
    db.close()
