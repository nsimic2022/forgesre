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


def test_inventory_library_filters_and_card():
    from app.models import Asset

    db = _db()
    tag = uuid4().hex[:6]
    down = Asset(asset_id=f"INV-{tag}-1", hostname=f"inv-{tag}-esx", ip="10.77.0.1", type="ESXi host", ping_status="red",
                 extras={"site": f"DC-{tag}", "rack": "A07", "contract": "ProSupport"}, glpi_id="1182", scrape_address="10.77.0.1:9100")
    bare = Asset(asset_id=f"INV-{tag}-2", hostname=f"inv-{tag}-sw", ip="10.77.0.2", type="Linux Server", scrape_address="",
                 extras={"site": f"DC-{tag}", "rack": "A08"})
    db.add_all([down, bare])
    db.commit()
    db.close()
    client = _signed_in("viewer")
    page = client.get("/api/v1/inventory", params={"site": f"DC-{tag}"}).json()
    assert {r["hostname"] for r in page["rows"]} == {f"inv-{tag}-esx", f"inv-{tag}-sw"}
    site = next(s for s in page["library"]["sites"] if s["name"] == f"DC-{tag}")
    assert site["count"] == 2 and {r["name"] for r in site["racks"]} == {"A07", "A08"}
    rack = client.get("/api/v1/inventory", params={"site": f"DC-{tag}", "rack": "A07"}).json()
    assert [r["hostname"] for r in rack["rows"]] == [f"inv-{tag}-esx"]
    row = rack["rows"][0]
    assert row["state"] == "down" and row["sources"] == ["Prometheus", "GLPI"]
    nosrc = client.get("/api/v1/inventory", params={"view": "no_source", "q": tag}).json()
    assert [r["hostname"] for r in nosrc["rows"]] == [f"inv-{tag}-sw"]
    card = client.get(f"/api/v1/inventory/INV-{tag}-1").json()
    assert {"key": "contract", "label": "License / contract / SLA", "value": "ProSupport"} in card["details"]
    assert card["can_edit"] is False
    assert client.get("/api/v1/inventory/nope").status_code == 404


def test_automation_overview_and_simulator():
    from datetime import timedelta as _td

    from app.models import Asset, MaintenanceWindow

    db = _db()
    tag = uuid4().hex[:6]
    asset = Asset(asset_id=f"AUT-{tag}", hostname=f"aut-{tag}", ip="10.88.0.1", type="Linux Server")
    db.add(asset)
    db.add(MaintenanceWindow(asset_ref=f"AUT-{tag}", summary="Backup window", starts_at=utcnow() + _td(hours=1), ends_at=utcnow() + _td(hours=3)))
    db.commit()
    db.close()

    client = _signed_in("analyst")
    data = client.get("/api/v1/automation").json()
    assert any(r["name"] == "high-cpu" and r["playbook"] for r in data["rules"])
    assert any(p["steps"] for p in data["policies"])
    assert any(w["asset"] == f"aut-{tag}" and w["active"] is False for w in data["maintenance"])
    assert data["can_edit"] is False

    hit = client.post("/api/v1/automation/simulate", json={"alertname": "HighCPU", "asset": f"aut-{tag}", "value": "95"}).json()
    assert hit["outcome"] == "incident" and hit["rule"] == "high-cpu" and hit["steps"]
    below = client.post("/api/v1/automation/simulate", json={"alertname": "HighCPU", "asset": f"aut-{tag}", "value": "10"}).json()
    assert below["rule"] is None
    later = (utcnow() + _td(hours=2)).isoformat()
    muted = client.post("/api/v1/automation/simulate", json={"alertname": "HighCPU", "asset": f"aut-{tag}", "value": "95", "at": later}).json()
    assert muted["outcome"] == "muted" and "Backup window" in muted["trace"][-1]
    assert client.post("/api/v1/automation/simulate", json={"alertname": " "}).status_code == 400
    assert client.post("/api/v1/automation/simulate", json={"alertname": "X", "at": "tomorrow"}).status_code == 400
    assert _signed_in("viewer").get("/api/v1/automation").status_code == 403
