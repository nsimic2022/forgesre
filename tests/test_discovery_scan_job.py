"""Discovery Save & scan / Scan now enqueue a Postgres job; probe stays off the HTTP thread."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import yaml
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.jobs import DISCOVERY_SCAN_KIND, enqueue_discovery_scan, run_pending_jobs
from app.main import app
from app.migrate import migrate
from app.models import Job, JournalEntry
from app.seed import seed
from app.settings import settings

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    return db


def _login() -> TestClient:
    client = TestClient(app)
    posted = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert posted.status_code in {302, 303}
    return client


def test_web_and_api_handlers_do_not_call_run_scan_inline():
    web = (ROOT / "backend" / "app" / "web.py").read_text(encoding="utf-8")
    api = (ROOT / "backend" / "app" / "api.py").read_text(encoding="utf-8")
    web_fn = web[web.index("def discovery_scan_page") : web.index("def discovery_approve_page")]
    api_fn = api[api.index("def discovery_scan(") : api.index("def discovery_approve(")]
    assert "run_scan(" not in web_fn
    assert "enqueue_discovery_scan" in web_fn
    assert "run_scan(" not in api_fn
    assert "enqueue_discovery_scan" in api_fn
    jobs = (ROOT / "backend" / "app" / "jobs.py").read_text(encoding="utf-8")
    loop = jobs[jobs.index("def run_pending_jobs") :]
    assert "DISCOVERY_SCAN_KIND" in loop
    assert "run_scan" in loop and "merge_auto" in loop
    assert "import celery" not in jobs.lower()
    assert "from celery" not in jobs.lower()


def test_discovery_buttons_not_stacked_helper_in_tip():
    html = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert "discovery-scan-actions" in html
    assert "discovery-scan-actions" in css
    actions = html[html.index("discovery-scan-actions") :]
    assert "Save &amp; scan" in actions
    assert actions.find("Save &amp; scan") < actions.find(">Scan now")
    assert html.find("Scan now") < html.find("NetBox sync")
    assert "grid-template-columns: 1fr 1fr" in css.split(".discovery-scan-actions")[1].split("}")[0]
    assert "grid-template-columns: 1fr 1fr" in css.split(".discovery-actions")[1].split("}")[0]
    assert "form-grid discovery-scan-form" not in html
    assert "Autodetected connected nets (real prefixes):" not in html
    assert "background job" in html or "background probe" in html
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert "discovery_scan" in handbook
    assert "no Celery" in handbook or "not Celery" in handbook.lower() or "**no Celery**" in handbook
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./config/forgesre.yml:/config/forgesre.yml:ro" not in compose
    assert "./config/forgesre.yml:/config/forgesre.yml" in compose


def test_post_scan_enqueues_pending_job_and_redirects(tmp_path, monkeypatch):
    db = _db()
    db.query(Job).delete(synchronize_session=False)
    db.commit()
    db.close()
    cfg = tmp_path / "forgesre.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "discovery": {"enabled": True, "mode": "semi-automatic", "cidrs": []},
                "inventory": {"provider": "local", "netbox": {"enabled": False, "mode": "disabled"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "config_path", cfg)
    settings.yaml = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("app.inventory.run_scan", boom)
    client = _login()
    page = client.get("/discovery")
    assert page.status_code == 200
    assert "discovery-scan-actions" in page.text
    posted = client.post(
        "/discovery/scan",
        data={"confirm": "1", "cidrs": "10.55.0.0/24"},
        follow_redirects=False,
    )
    assert posted.status_code == 302
    location = posted.headers.get("location") or ""
    assert "/discovery" in location
    assert "queued" in location.lower()
    assert called["n"] == 0
    written = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert written["discovery"]["cidrs"] == ["10.55.0.0/24"]
    db = SessionLocal()
    jobs = db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).all()
    assert len(jobs) == 1
    assert jobs[0].status == "pending"
    assert jobs[0].object_id == "scan"
    db.close()

    scan_now = client.post("/discovery/scan", data={"cidrs": "10.55.0.0/24"}, follow_redirects=False)
    assert scan_now.status_code == 302
    assert "already queued" in unquote(scan_now.headers.get("location") or "").lower()
    db = SessionLocal()
    assert db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).count() == 1
    run_pending_jobs(db)
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    db.close()
    assert called["n"] == 1


def test_run_pending_jobs_executes_scan_and_swallows_probe_errors(monkeypatch):
    db = _db()
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise RuntimeError("SNMP GET exploded")

    monkeypatch.setattr("app.inventory.run_scan", boom)
    job = enqueue_discovery_scan(db, actor="admin@forgesre.local", saved=False)
    assert job is not None
    assert job.status == "pending"
    run_pending_jobs(db)
    db.refresh(job)
    assert called["n"] == 1
    assert job.status == "error"
    assert "SNMP GET exploded" in (job.error or "")
    entry = (
        db.query(JournalEntry)
        .filter_by(module="discovery", action="scan", status="error")
        .order_by(JournalEntry.id.desc())
        .first()
    )
    assert entry is not None
    assert "exploded" in (entry.detail or entry.summary or "")
    db.close()


def test_run_pending_jobs_marks_scan_done(monkeypatch):
    db = _db()
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    monkeypatch.setattr(
        "app.inventory.run_scan",
        lambda db, cidrs=None, **kwargs: {"found": 0, "skipped": 0, "cidrs": cidrs or []},
    )
    job = enqueue_discovery_scan(db, actor="t", cidrs=["10.1.0.0/28"], saved=True)
    run_pending_jobs(db)
    db.refresh(job)
    assert job.status == "done"
    assert (job.error or "") == ""
    db.close()


def test_api_scan_enqueues_without_probing(monkeypatch):
    db = _db()
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    db.close()
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise RuntimeError("should not run on request")

    monkeypatch.setattr("app.inventory.run_scan", boom)
    client = _login()
    resp = client.post("/api/v1/discovery/scan")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("queued") is True
    assert body.get("kind") == "discovery_scan"
    assert called["n"] == 0
    db = SessionLocal()
    run_pending_jobs(db)
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    db.close()
    assert called["n"] == 1


def test_post_scan_empty_cidrs_and_mocked_ip_does_not_500(monkeypatch):
    """POST /discovery/scan must never return the black Starlette 500 page."""
    db = _db()
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    db.close()

    def boom(*_args, **_kwargs):
        raise RuntimeError("ioctl failed")

    monkeypatch.setattr("discovery.detect_connected_networks", boom)
    monkeypatch.setattr("discovery._iface_inet_rows", boom)
    monkeypatch.setattr(
        "app.inventory.run_scan",
        lambda db, cidrs=None, **kwargs: {"found": 0, "skipped": 0, "cidrs": cidrs or []},
    )
    client = _login()
    page = client.get("/discovery")
    assert page.status_code == 200

    empty = client.post("/discovery/scan", data={"cidrs": ""}, follow_redirects=False)
    assert empty.status_code == 302, empty.text[:800]
    assert empty.status_code != 500

    save_empty = client.post(
        "/discovery/scan",
        data={"confirm": "1", "cidrs": ""},
        follow_redirects=False,
    )
    assert save_empty.status_code == 302, save_empty.text[:800]

    mocked = client.post(
        "/discovery/scan",
        data={"cidrs": "10.66.1.0/30"},
        follow_redirects=False,
    )
    assert mocked.status_code == 302, mocked.text[:800]
    loc = unquote(mocked.headers.get("location") or "").lower()
    # Scan now persists the operator CIDR list (multi-CIDR real prefixes), then queues.
    assert "saved discovery.cidrs" in loc
    assert "10.66.1.0/30" in loc
    db = SessionLocal()
    run_pending_jobs(db)
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_save_scan_readonly_yaml_does_not_500(monkeypatch):
    def deny(*_args, **_kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(settings, "set_discovery_cidrs", deny)
    monkeypatch.setattr(
        "app.inventory.run_scan",
        lambda db, cidrs=None, **kwargs: {"found": 0, "skipped": 0, "cidrs": cidrs or []},
    )
    client = _login()
    resp = client.post(
        "/discovery/scan",
        data={"confirm": "1", "cidrs": "10.1.0.0/28"},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text[:800]
    loc = unquote(resp.headers.get("location") or "").lower()
    assert "could not write" in loc or "queued" in loc
    db = SessionLocal()
    run_pending_jobs(db)
    db.query(Job).filter_by(kind=DISCOVERY_SCAN_KIND).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_job_worker_uses_operator_cidrs_without_remerge(monkeypatch):
    from app.db import Base, SessionLocal, engine
    from app.jobs import enqueue_discovery_scan, run_pending_jobs
    from app.migrate import migrate
    from app.seed import seed

    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    seen = []

    def fake_run_scan(db, cidrs=None, merge_auto=True, **kwargs):
        seen.append({"cidrs": list(cidrs or []), "merge_auto": merge_auto})
        return {"found": 0, "skipped": 0, "cidrs": list(cidrs or []), "warnings": []}

    monkeypatch.setattr("app.inventory.run_scan", fake_run_scan)
    enqueue_discovery_scan(db, actor="t", cidrs=["10.20.30.0/25"], saved=True)
    run_pending_jobs(db)
    assert seen and seen[-1]["merge_auto"] is False
    assert seen[-1]["cidrs"] == ["10.20.30.0/25"]
    db.close()
