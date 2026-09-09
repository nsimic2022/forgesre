"""N's small GUI fixes: dashboard tiles, incidents default all, discovery CRUD, playbooks."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import upsert_candidate
from app.main import app
from app.models import DiscoveryCandidate, Playbook, User
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(client: TestClient, email: str = "admin@forgesre.local", password: str = "testpass") -> None:
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_css_cache_bust_is_current():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert "app.css?v=disc-500" in base
    assert ".banner-short" in css
    assert ".playbook-grid" in css
    assert ".ack-dot" in css
    assert ".list-filters" in css
    assert "grid-template-columns: 1fr 1fr" in css


def test_dashboard_tiles_are_shortcuts_and_journal_bar_is_short():
    db = _db()
    client = TestClient(app)
    _login(client)
    home = client.get("/")
    assert home.status_code == 200
    assert 'href="/assets"' in home.text
    assert 'href="/assets?status=healthy"' in home.text
    assert 'href="/incidents"' in home.text
    assert 'class="stat' in home.text
    assert "<a class=\"stat" in home.text or "<a class='stat" in home.text
    assert "Asset inventory counts" in home.text
    assert "Incident counts" in home.text
    dash = (ROOT / "frontend" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="journal-error-banner"' in dash
    assert "banner-short" in dash
    assert "recent error report" not in dash
    db.close()


def test_assets_verify_copy_lives_in_title_tip_not_always_on_sentence():
    html = (ROOT / "frontend" / "templates" / "assets.html").read_text(encoding="utf-8")
    assert "reach-legend" not in html
    assert "Verify (this inventory path)" in html
    assert "./forgesre doctor" in html
    assert "./forgesre test" in html
    assert ">Verify<" in html
    assert "/assets/verify" in html
    db = _db()
    client = TestClient(app)
    _login(client)
    page = client.get("/assets")
    assert page.status_code == 200
    assert "Verify all" in page.text
    assert 'class="muted reach-legend"' not in page.text
    db.close()


def test_incidents_default_is_all_and_filter_is_spaced():
    db = _db()
    client = TestClient(app)
    _login(client)
    listed = client.get("/incidents")
    assert listed.status_code == 200
    assert 'class="list-filters"' in listed.text
    assert 'value="all"' in listed.text
    assert "Open/firing" not in listed.text
    form = listed.text.split('action="/incidents"', 1)[1].split("</form>", 1)[0]
    assert "All" in form
    assert "Open" in form
    assert "Closed" in form
    closed = client.get("/incidents?status=closed")
    assert closed.status_code == 200
    assert "CLOSED" in closed.text or "RESOLVED" in closed.text
    opened = client.get("/incidents?status=open")
    assert opened.status_code == 200
    assert "CLOSED" not in opened.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    db.close()


def test_history_ack_column_is_a_status_circle():
    db = _db()
    client = TestClient(app)
    _login(client)
    page = client.get("/history")
    assert page.status_code == 200
    assert "ack-dot" in page.text
    assert 'title="Acknowledged"' in page.text or 'title="Not acknowledged"' in page.text
    html = (ROOT / "frontend" / "templates" / "history.html").read_text(encoding="utf-8")
    assert "{% if item.ack_by %}" not in html
    db.close()


def test_discovery_candidate_edit_clone_remove():
    db = _db()
    row = upsert_candidate(db, "10.66.1.9", "Possible Linux server", [22, 9100], source="scan")
    db.commit()
    db.refresh(row)
    client = TestClient(app)
    _login(client)
    listed = client.get("/discovery")
    assert listed.status_code == 200
    assert f"/discovery/{row.id}/delete" in listed.text
    assert f"/discovery?edit={row.id}" in listed.text
    assert f"/discovery?clone={row.id}" in listed.text
    assert ">Ignore<" in listed.text
    assert 'class="pill demo"' in listed.text
    edited = client.get(f"/discovery?edit={row.id}")
    assert edited.status_code == 200
    assert "Edit candidate" in edited.text
    saved = client.post(
        f"/discovery/{row.id}/update",
        data={"ip": "10.66.1.9", "hostname": "lab-box", "proposed_role": "Possible Windows server"},
        follow_redirects=False,
    )
    assert saved.status_code == 302
    db.refresh(row)
    assert row.hostname == "lab-box"
    assert row.proposed_role == "Possible Windows server"
    cloned = client.post(
        f"/discovery/{row.id}/clone",
        data={"ip": "10.66.1.10", "hostname": "lab-box-copy", "proposed_role": "Possible Windows server"},
        follow_redirects=False,
    )
    assert cloned.status_code == 302
    copy = db.query(DiscoveryCandidate).filter_by(ip="10.66.1.10").one()
    assert copy.hostname == "lab-box-copy"
    assert copy.status == "new"
    removed = client.post(f"/discovery/{copy.id}/delete", follow_redirects=False)
    assert removed.status_code == 302
    assert db.query(DiscoveryCandidate).filter_by(ip="10.66.1.10").first() is None
    assert db.get(DiscoveryCandidate, row.id) is not None
    db.close()


def test_playbooks_two_per_row_and_row_actions():
    html = (ROOT / "frontend" / "templates" / "playbooks.html").read_text(encoding="utf-8")
    assert "playbook-grid" in html
    assert '<p class="muted">V0.1 playbooks are instructions and workflow. They do not execute commands.</p>' not in html
    db = _db()
    client = TestClient(app)
    _login(client)
    page = client.get("/playbooks")
    assert page.status_code == 200
    assert "playbook-grid" in page.text
    assert ">Edit<" in page.text
    assert ">Clone<" in page.text
    assert ">Remove<" in page.text
    assert 'href="/playbooks">Cancel</a>' in page.text
    disk = db.query(Playbook).filter_by(slug="disk-full").one()
    clone_page = client.get(f"/playbooks?clone={disk.id}")
    assert clone_page.status_code == 200
    assert "Clone playbook" in clone_page.text
    posted = client.post(
        "/playbooks",
        data={"name": "DISK-FULL (copy)", "slug": "disk-full-copy", "steps": "One\nTwo"},
        follow_redirects=False,
    )
    assert posted.status_code == 302
    copy = db.query(Playbook).filter_by(slug="disk-full-copy").one()
    gone = client.post(f"/playbooks/{copy.id}/delete", follow_redirects=False)
    assert gone.status_code == 302
    assert db.query(Playbook).filter_by(slug="disk-full-copy").first() is None
    db.close()


def test_admin_users_have_edit_clone_remove():
    db = _db()
    client = TestClient(app)
    _login(client)
    client.post(
        "/admin/users",
        data={"email": "ops-clone@dc.local", "name": "Ops", "password": "ops-pass", "role": "analyst"},
        follow_redirects=False,
    )
    row = db.query(User).filter_by(email="ops-clone@dc.local").one()
    page = client.get(f"/admin?selected={row.id}")
    assert page.status_code == 200
    assert f"/admin?clone={row.id}" in page.text
    assert ">Edit<" in page.text
    assert ">Clone<" in page.text
    assert ">Remove<" in page.text
    clone_page = client.get(f"/admin?clone={row.id}")
    assert clone_page.status_code == 200
    assert "Clone user" in clone_page.text
    assert 'name="password"' in clone_page.text
    assert "ops-pass" not in clone_page.text
    db.close()
