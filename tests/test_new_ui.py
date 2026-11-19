"""New UI backend: /api/v1/noc payload and /app serving of the built React app."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, Notification, utcnow
from app.noc import noc_overview
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _incident(db, severity="CRITICAL", status="OPEN", **kw) -> Incident:
    asset = Asset(asset_id=f"NOC-{uuid4().hex[:6]}", hostname=f"noc-{uuid4().hex[:6]}", ip="10.99.0.1", type="Linux Server", ping_status="red")
    db.add(asset)
    db.flush()
    row = Incident(
        number=f"INC-T{uuid4().hex[:6]}", title="HostDown", severity=severity, status=status,
        fingerprint=f"noc:{uuid4().hex}", asset_id=asset.id, started_at=utcnow() - timedelta(minutes=6), **kw,
    )
    db.add(row)
    db.flush()
    return row


def test_noc_overview_splits_tabs_and_counts():
    db = _db()
    open_row = _incident(db)
    acked = _incident(db, severity="HIGH", ack_at=utcnow(), ack_by="ana@dc.local")
    done = _incident(db, severity="WARNING", status="RESOLVED", resolved_at=utcnow())
    db.add(Notification(incident_id=open_row.id, target="team@dc.local", step_key="immediate"))
    db.commit()
    data = noc_overview(db)
    active = {r["number"]: r for r in data["incidents"]["active"]}
    assert open_row.number in active and acked.number not in active
    assert any(r["number"] == acked.number for r in data["incidents"]["acknowledged"])
    assert any(r["number"] == done.number for r in data["incidents"]["resolved_today"])
    row = active[open_row.number]
    assert row["messages"] == 1 and row["level"].startswith("L") and row["asset"]["type"] == "Linux Server"
    assert data["counts"]["critical"] >= 1 and data["counts"]["not_acked"] >= 1
    assert data["counts"]["hosts_down"] >= 2
    assert data["ai"]["forgerca"] is True
    db.close()


def test_noc_api_needs_sign_in():
    client = TestClient(create_app())
    assert client.get("/api/v1/noc").status_code == 401
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"})
    assert client.get("/api/v1/noc").status_code == 200


def test_app_serves_index_for_client_routes_and_real_assets(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setenv("WEB_DIST", str(dist))
    client = TestClient(create_app())
    assert "id=root" in client.get("/app").text
    assert "id=root" in client.get("/app/incidents/INC-1").text
    assert client.get("/app/assets/app.js").text == "console.log(1)"
    assert client.get("/app/../secrets/secrets.env").status_code in (200, 404)
    assert "SECRET" not in client.get("/app/..%2F..%2Fsecrets").text


def test_app_is_off_when_not_built(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_DIST", str(tmp_path / "missing"))
    client = TestClient(create_app())
    assert client.get("/app").status_code == 404


def _signed_in(role: str = "admin"):
    from app.models import User
    from app.security import hash_password

    db = _db()
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def test_incidents_page_tabs_search_and_paging():
    db = _db()
    open_row = _incident(db)
    _incident(db, severity="HIGH", ack_at=utcnow(), ack_by="a@dc.local")
    db.commit()
    host = open_row.asset.hostname
    db.close()
    client = _signed_in("viewer")
    page = client.get("/api/v1/incidents-page", params={"tab": "active", "q": host}).json()
    assert [r["number"] for r in page["rows"]] == [open_row.number]
    assert page["counts"]["active"] == 1 and page["counts"]["all"] == 1
    paged = client.get("/api/v1/incidents-page", params={"tab": "all", "limit": 1, "offset": 0}).json()
    assert len(paged["rows"]) == 1 and paged["total"] >= 2


def test_incident_view_checklist_and_queued_investigation():
    from app.models import Job, Playbook

    db = _db()
    book = Playbook(name="T-Book", slug=f"t-book-{uuid4().hex[:6]}", steps=[{"title": "Ping it"}, {"title": "Call owner"}])
    db.add(book)
    db.flush()
    row = _incident(db, playbook_id=book.id)
    db.commit()
    number = row.number
    db.close()

    analyst = _signed_in("analyst")
    view = analyst.get(f"/api/v1/incidents/{number}/view").json()
    assert view["playbook"] == {"name": "T-Book", "steps": ["Ping it", "Call owner"], "done": []}
    assert view["can"] == {"work": True, "investigate": True, "cloud": False}
    assert analyst.post(f"/api/v1/incidents/{number}/checklist", json={"step": 1, "done": True}).json() == {"done": [1]}
    assert analyst.post(f"/api/v1/incidents/{number}/checklist", json={"step": 9, "done": True}).status_code == 400
    assert analyst.get(f"/api/v1/incidents/{number}/view").json()["playbook"]["done"] == [1]

    # Re-running the analysis is queued, never run inside the request.
    queued = analyst.post(f"/api/v1/incidents/{number}/investigate").json()
    assert queued["queued"] is True
    db = SessionLocal()
    assert db.query(Job).filter_by(kind="investigate", object_id=number, status="pending").count() == 1
    db.query(Job).filter_by(object_id=number).delete()
    db.commit()
    db.close()

    viewer = _signed_in("viewer")
    assert viewer.post(f"/api/v1/incidents/{number}/checklist", json={"step": 0, "done": True}).status_code == 403
    assert viewer.get(f"/api/v1/incidents/{number}/view").json()["can"]["work"] is False
