"""ForgeAI page: setup, ask, digest filters, jobs, reports, digest cron."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.jobs import run_pending_jobs
from app.main import app
from app.models import Incident, Job, Notification, ScheduledReport, User, utcnow
from app.seed import seed
from app.security import hash_password
from app.services import next_incident_number, process_scheduled_reports

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client(email: str = "admin@forgesre.local") -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": "testpass"}, follow_redirects=False)
    return client


def test_forgeai_page_has_five_cubes_and_cron():
    db = _db()
    db.close()
    client = _client()
    page = client.get("/investigations")
    assert page.status_code == 200
    html = page.text
    for marker in ('id="activity"', 'id="summary"', 'id="ai-more"'):
        assert marker in html, marker
    assert "Investigations" in html
    assert 'id="summary"' in html
    assert "Recent activity" not in html
    assert "Ask ForgeAI" not in html
    assert "Save digest schedule" not in html
    assert 'id="setup"' not in html
    assert 'id="cron"' not in html
    assert client.get("/forgeai").status_code == 200
    home = client.get("/").text
    assert 'href="/investigations"' in home
    assert "Open Investigations" not in home
    nav = home.split('<aside class="nav"', 1)[1].split("</aside>", 1)[0]
    assert ">Investigations<" in nav
    assert 'href="/investigations"' in nav
    assert 'data-nav-invest-toggle' not in nav
    assert ">ForgeAI<" not in nav
    assert "Coming later" not in nav
    assert nav.index(">Dashboard<") < nav.index('href="/investigations"') < nav.index(">Assets<")


def test_viewer_forbidden_on_forgeai():
    db = _db()
    email = f"view-{uuid4().hex[:6]}@forgesre.local"
    db.add(User(email=email, name="View", password_hash=hash_password("testpass"), role="viewer"))
    db.commit()
    db.close()
    client = _client(email)
    assert client.get("/investigations").status_code == 403
    assert client.get("/forgeai").status_code == 403
    home = client.get("/").text
    nav = home.split('<aside class="nav"', 1)[1].split("</aside>", 1)[0]
    assert 'href="/investigations"' not in nav
    assert client.get("/investigations/menu").status_code == 404


def test_digest_filter_chips():
    db = _db()
    from app.models import Asset

    asset = db.query(Asset).filter(Asset.asset_id == "forge-lab-01").first() or db.query(Asset).first()
    assert asset is not None
    extras = dict(asset.extras or {})
    extras["site"] = "Lab-East"
    asset.extras = extras
    db.add(
        Incident(
            number=next_incident_number(db),
            title=f"crit-filter-{uuid4().hex[:6]}",
            severity="CRITICAL",
            status="OPEN",
            fingerprint=f"forgeai-crit:{uuid4().hex}",
            asset_id=asset.id,
        )
    )
    db.commit()
    db.close()
    client = _client()
    page = client.get("/investigations?severity=CRITICAL&site=Lab-East")
    assert page.status_code == 200
    assert 'id="digest"' not in page.text
    assert "filter-chips" not in page.text
    assert "Investigations" in page.text


def test_forgeai_cron_stays_off_ops_and_mails():
    db = _db()
    db.close()
    client = _client()
    created = client.post(
        "/forgeai/cron",
        data={"name": "nightly-digest", "extra_emails": "digest@example.local", "schedule": "24h"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/forgeai#cron"
    page = client.get("/forgeai").text
    assert "nightly-digest" not in page
    assert 'id="cron"' not in page
    ops = client.get("/ops").text
    active = ops.split("Active scheduled reports", 1)[1]
    assert "nightly-digest" not in active
    db = _db()
    row = db.query(ScheduledReport).filter_by(name="nightly-digest").one()
    assert row.kind == "forgeai"
    row.next_run_at = utcnow() - timedelta(minutes=5)
    db.commit()
    report_id = row.id
    assert process_scheduled_reports(db) >= 1
    job = db.query(Job).filter_by(kind="forgeai_digest", object_id=f"report-{report_id}").first()
    assert job is not None
    assert job.status == "pending"
    run_pending_jobs(db)
    db.refresh(job)
    assert job.status == "done"
    mail = (
        db.query(Notification)
        .filter_by(target="digest@example.local", step_key="report")
        .order_by(Notification.id.desc())
        .first()
    )
    assert mail is not None
    assert "ForgeAI digest" in (mail.subject or "")
    db.close()


def test_ask_posts_question_and_css_cache():
    db = _db()
    db.close()
    client = _client()
    posted = client.post(
        "/insights/refresh",
        data={"question": "What is repeating?", "next": "/investigations#ask"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert posted.headers["location"] == "/investigations#ask"
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v09-61" in base
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".forgeai-grid" in css
