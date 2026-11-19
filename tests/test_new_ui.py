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


def test_review_accuracy_and_recurring_causes():
    from app.models import Investigation

    db = _db()
    tag = uuid4().hex[:6]
    numbers = []
    for i, (rca, ai) in enumerate([("correct", "correct"), ("partly", "wrong"), ("wrong", "")]):
        row = _incident(db, status="RESOLVED", resolved_at=utcnow())
        row.title = f"Review-{tag}-{i}"
        db.add(Investigation(incident_id=row.id, summary="s", likely_cause=f"rules cause {i}", provider="builtin-analyst"))
        if ai:
            db.add(Investigation(incident_id=row.id, summary="ai", likely_cause="ai", provider="forgerca-llm"))
        numbers.append((row.number, rca, ai))
    db.commit()
    db.close()

    analyst = _signed_in("analyst")
    for number, rca, ai in numbers:
        saved = analyst.post(f"/api/v1/incidents/{number}/review", json={"root_cause": f"SFP failure {tag}", "rca_rating": rca, "ai_rating": ai})
        assert saved.status_code == 200, saved.text
    assert analyst.post(f"/api/v1/incidents/{numbers[0][0]}/review", json={"root_cause": " "}).status_code == 400
    assert analyst.post(f"/api/v1/incidents/{numbers[0][0]}/review", json={"root_cause": "x", "rca_rating": "maybe"}).status_code == 400
    assert analyst.get(f"/api/v1/incidents/{numbers[0][0]}/view").json()["review"]["root_cause"] == f"SFP failure {tag}"

    page = analyst.get("/api/v1/investigations-page", params={"q": tag}).json()
    assert {r["number"] for r in page["rows"]} == {n for n, _, _ in numbers}
    assert all(r["forgerca"].startswith("rules cause") for r in page["rows"])
    assert any(r["cause"] == f"SFP failure {tag}" and r["count"] == 3 for r in page["recurring"])
    ai_only = analyst.get("/api/v1/investigations-page", params={"q": tag, "only_ai": "true"}).json()
    assert len(ai_only["rows"]) == 2
    acc = page["accuracy"]["forgerca"]
    assert acc["rated"] >= 3 and 0 <= acc["score"] <= 100
    assert _signed_in("viewer").post(f"/api/v1/incidents/{numbers[0][0]}/review", json={"root_cause": "x"}).status_code == 403


def test_reports_overview_and_csv():
    db = _db()
    tag = uuid4().hex[:6]
    row = _incident(db, status="RESOLVED", resolved_at=utcnow(), ack_at=utcnow() - timedelta(minutes=2))
    row.title = "NodeExporterDown"
    # Down for ~5 days so it leads the top-10 host-down list even in a busy shared test DB.
    row.started_at = utcnow() - timedelta(days=5)
    row.asset.hostname = f"rep-{tag}"
    db.commit()
    db.close()
    client = _signed_in("viewer")
    data = client.get("/api/v1/reports-page", params={"days": 7}).json()
    assert data["current"]["incidents"] >= 1 and data["current"]["mttr_min"] is not None
    assert len(data["daily"]) >= 7 and {"day", "critical", "high", "other"} <= set(data["daily"][0])
    assert any(r["asset"] == f"rep-{tag}" for r in data["host_down"])
    assert data["top_alerts"] and all({"alert", "incidents"} <= set(r) for r in data["top_alerts"])
    assert isinstance(data["scheduled"], list) and data["can_edit"] is False
    csv_resp = client.get("/api/v1/reports/incidents.csv", params={"days": 7})
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_resp.headers["content-disposition"]
    assert csv_resp.text.splitlines()[0].startswith("number,severity,status,title,asset")
    assert f"rep-{tag}" in csv_resp.text


def test_inventory_form_and_asset_edit_roles():
    """The new UI's asset form: options for everyone, writes only for Engineer and up."""
    tag = uuid4().hex[:6]
    viewer = _signed_in("analyst")
    form = viewer.get("/api/v1/inventory-form").json()
    assert any(g["group"] == "Servers" and "Linux Server" in g["names"] for g in form["types"])
    assert {"key": "rack", "label": "Rack", "max": 64} in form["extra_fields"]
    assert "Production" in form["environments"] and form["can_edit"] is False
    assert viewer.get("/api/v1/inventory").json()["can_edit"] is False
    denied = viewer.post("/api/v1/assets", json={"hostname": f"ui-{tag}", "ip": "10.66.0.1", "type": "Linux Server"})
    assert denied.status_code == 403

    eng = _signed_in("engineer")
    assert eng.get("/api/v1/inventory-form").json()["can_edit"] is True
    made = eng.post("/api/v1/assets", json={"hostname": f"ui-{tag}", "ip": "10.66.0.1", "type": "Linux Server",
                                           "extras": {"site": f"DC-{tag}", "rack": "B02", "support": "yes"}})
    assert made.status_code == 200, made.text
    asset_id = made.json()["asset_id"]
    saved = eng.post(f"/api/v1/assets/{asset_id}", json={"owner": "Compute", "extras": {"site": f"DC-{tag}", "rack": "B03", "support": "yes"}})
    assert saved.status_code == 200, saved.text
    card = eng.get(f"/api/v1/inventory/{asset_id}").json()
    assert card["owner"] == "Compute" and card["rack"] == "B03" and card["extras"]["support"] == "yes"
    assert f"DC-{tag}" in eng.get("/api/v1/inventory-form").json()["sites"]
    assert eng.post(f"/api/v1/assets/{asset_id}/delete", json={}).status_code == 200
    assert eng.get(f"/api/v1/inventory/{asset_id}").status_code == 404


def test_automation_rule_and_playbook_editing():
    """Rules and playbooks from the new UI: Engineer+ only, unique names, removal keeps incidents."""
    tag = uuid4().hex[:6]
    analyst = _signed_in("analyst")
    assert analyst.post("/api/v1/automation/rules", json={"name": f"r-{tag}"}).status_code == 403
    assert analyst.post("/api/v1/automation/playbooks", json={"name": "x", "steps": ["a"]}).status_code == 403

    eng = _signed_in("engineer")
    assert eng.post("/api/v1/automation/playbooks", json={"name": f"Book {tag}", "steps": []}).status_code == 400
    book = eng.post("/api/v1/automation/playbooks", json={"name": f"Book {tag}", "steps": ["Check disk", " ", "Call owner"]})
    assert book.status_code == 200, book.text
    book_id = book.json()["id"]
    assert [s["title"] for s in book.json()["steps"]] == ["Check disk", "Call owner"]

    body = {"name": f"r-{tag}", "alertname": f"Alert{tag}", "metric": "disk", "operator": ">=", "value": 90,
            "severity": "high", "playbook_id": book_id}
    rule = eng.post("/api/v1/automation/rules", json=body)
    assert rule.status_code == 200, rule.text
    rule_id = rule.json()["id"]
    assert eng.post("/api/v1/automation/rules", json=body).status_code == 400  # duplicate name
    assert eng.post("/api/v1/automation/rules", json={**body, "name": f"r2-{tag}", "operator": "~"}).status_code == 400
    assert eng.post("/api/v1/automation/rules", json={**body, "name": f"r3-{tag}", "severity": "panic"}).status_code == 400

    listed = next(r for r in eng.get("/api/v1/automation").json()["rules"] if r["id"] == rule_id)
    assert listed["condition"] == {"alertname": f"Alert{tag}", "metric": "disk", "operator": ">=", "value": 90.0}
    assert listed["playbook_id"] == book_id and listed["policy_id"] and listed["severity"] == "high"

    off = eng.put(f"/api/v1/automation/rules/{rule_id}", json={**body, "enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False
    renamed = eng.put(f"/api/v1/automation/playbooks/{book_id}", json={"name": f"Book {tag} v2", "steps": ["Only step"]})
    assert renamed.json()["name"] == f"Book {tag} v2"

    db = _db()
    row = _incident(db)
    row.playrule_id = rule_id
    row.playbook_id = book_id
    db.commit()
    number = row.number
    db.close()
    assert eng.delete(f"/api/v1/automation/playbooks/{book_id}").status_code == 200
    assert eng.delete(f"/api/v1/automation/rules/{rule_id}").status_code == 200
    assert eng.delete(f"/api/v1/automation/rules/{rule_id}").status_code == 404
    db = _db()
    kept = db.query(Incident).filter_by(number=number).one()
    assert kept.playrule_id is None and kept.playbook_id is None
    db.close()


def test_escalation_policy_and_oncall_editing():
    """Escalation L1–L4 and the on-call week from the new UI: Engineer+, validated like the classic page."""
    tag = uuid4().hex[:6]
    analyst = _signed_in("analyst")
    week = analyst.get("/api/v1/oncall-week").json()
    assert week["weekdays"][0] == "Monday" and week["can_edit"] is False and week["people"] == []
    assert analyst.post("/api/v1/oncall-slots", json={"weekday": 0, "start": "08:00", "end": "16:00", "email": "a@dc.local"}).status_code == 403
    assert analyst.post("/api/v1/automation/policies", json={"name": "x"}).status_code == 403

    eng = _signed_in("engineer")
    levels = [{"level": 1, "on": True, "minutes": 0, "target": "team"}, {"level": 2, "on": True, "minutes": 20, "target": f"lead-{tag}@dc.local"},
              {"level": 3, "on": False, "minutes": 30, "target": "engineer"}]
    assert eng.post("/api/v1/automation/policies", json={"name": f"P {tag}", "levels": []}).status_code == 400
    bad = eng.post("/api/v1/automation/policies", json={"name": f"P {tag}", "levels": [{"level": 1, "on": True, "minutes": 0, "target": ""}]})
    assert bad.status_code == 400 and "no recipient" in bad.json()["detail"]
    made = eng.post("/api/v1/automation/policies", json={"name": f"P {tag}", "levels": levels})
    assert made.status_code == 200, made.text
    policy = made.json()
    assert policy["slug"] == f"p-{tag}"
    assert [(s["level"], s["after_minutes"], s["target"]) for s in policy["steps"]] == [(1, 0, "team"), (2, 20, f"lead-{tag}@dc.local")]
    assert eng.post("/api/v1/automation/policies", json={"name": f"P {tag}", "levels": levels}).status_code == 400  # same slug
    listed = next(p for p in eng.get("/api/v1/automation").json()["policies"] if p["id"] == policy["id"])
    assert [lv["on"] for lv in listed["levels"]] == [True, True, False, False]

    rule = eng.post("/api/v1/automation/rules", json={"name": f"pr-{tag}", "escalation_policy_id": policy["id"]}).json()
    removed = eng.delete(f"/api/v1/automation/policies/{policy['id']}").json()
    assert removed["rules_moved"] == 1
    moved = next(r for r in eng.get("/api/v1/automation").json()["rules"] if r["id"] == rule["id"])
    assert moved["policy"] and moved["policy_id"] != policy["id"]
    default = next(p for p in eng.get("/api/v1/automation").json()["policies"] if p["slug"] == "default-warning")
    assert eng.delete(f"/api/v1/automation/policies/{default['id']}").status_code == 400

    slot_body = {"weekday": 2, "start": "08:00", "end": "16:00", "name": "Ana", "email": f"ana-{tag}@dc.local", "phone": "+381 60 1"}
    assert eng.post("/api/v1/oncall-slots", json={**slot_body, "end": "07:00"}).status_code == 400
    slot = eng.post("/api/v1/oncall-slots", json=slot_body)
    assert slot.status_code == 200, slot.text
    sid = slot.json()["id"]
    assert eng.put(f"/api/v1/oncall-slots/{sid}", json={**slot_body, "end": "18:00"}).json()["end"] == "18:00"
    assert eng.post(f"/api/v1/oncall-slots/{sid}/toggle").json()["enabled"] is False
    week = eng.get("/api/v1/oncall-week").json()
    row = next(s for s in week["slots"] if s["id"] == sid)
    assert row["start_minute"] == 480 and row["end_minute"] == 1080 and row["enabled"] is False
    assert week["can_edit"] is True
    assert eng.delete(f"/api/v1/oncall-slots/{sid}").status_code == 200
    assert eng.delete(f"/api/v1/oncall-slots/{sid}").status_code == 404


def test_maintenance_windows_from_the_new_ui():
    """Several assets at once, all or nothing, mirrored to maintenance_windows; End now clears it."""
    from app.models import MaintenanceWindow

    tag = uuid4().hex[:6]
    db = _db()
    for n in (1, 2):
        db.add(Asset(asset_id=f"mw-{tag}-{n}", hostname=f"mw-{tag}-{n}", ip=f"10.44.0.{n}", type="Linux Server"))
    db.commit()
    db.close()
    analyst = _signed_in("analyst")
    start = (utcnow() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    end = (utcnow() + timedelta(days=1, hours=4)).strftime("%Y-%m-%dT%H:%M")
    before = (utcnow() + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M")
    window = {"assets": [f"mw-{tag}-1"], "starts_at": start, "ends_at": end}
    assert analyst.post("/api/v1/maintenance", json=window).status_code == 403
    eng = _signed_in("engineer")
    assert eng.post("/api/v1/maintenance", json={**window, "ends_at": before}).status_code == 400
    missing = eng.post("/api/v1/maintenance", json={**window, "assets": [f"mw-{tag}-1", "nope-host"]})
    assert missing.status_code == 400 and "nope-host" in missing.json()["detail"]
    db = _db()
    assert db.query(MaintenanceWindow).filter(MaintenanceWindow.asset_ref.like(f"mw-{tag}-%")).count() == 0
    db.close()
    ok = eng.post("/api/v1/maintenance", json={**window, "assets": [f"mw-{tag}-1", "10.44.0.2"]})
    assert ok.status_code == 200 and ok.json()["assets"] == [f"mw-{tag}-1", f"mw-{tag}-2"]
    db = _db()
    rows = db.query(MaintenanceWindow).filter(MaintenanceWindow.asset_ref.like(f"mw-{tag}-%")).all()
    assert len(rows) == 2 and all(r.ends_at > r.starts_at for r in rows)
    db.close()
    listed = [w for w in eng.get("/api/v1/automation").json()["maintenance"] if w["asset_id"] == f"mw-{tag}-1"]
    assert listed and eng.get("/api/v1/automation").json()["can_edit_maintenance"] is True
    assert eng.delete(f"/api/v1/maintenance/mw-{tag}-1").json()["cleared"] is True
    db = _db()
    assert db.query(MaintenanceWindow).filter_by(asset_ref=f"mw-{tag}-1").count() == 0
    assert "maint_from" not in (db.query(Asset).filter_by(asset_id=f"mw-{tag}-1").one().extras or {})
    db.close()
    assert eng.delete("/api/v1/maintenance/nope").status_code == 404


def test_users_page_and_backup_api_are_admin_only(monkeypatch, tmp_path):
    """Users list with removal guards; backup runs as a background job, never inside the request."""
    from app import backup as backup_mod
    from app.jobs import run_pending_jobs
    from app.models import Job

    eng = _signed_in("engineer")
    assert eng.get("/api/v1/users-page").status_code == 403
    assert eng.get("/api/v1/backups").status_code == 403
    assert eng.post("/api/v1/backups", json={}).status_code == 403

    admin = _signed_in("admin")
    page = admin.get("/api/v1/users-page").json()
    me = next(u for u in page["users"] if u["me"])
    assert me["delete_blocked"] == "cannot remove yourself"
    assert [r["key"] for r in page["roles"]] == ["viewer", "analyst", "engineer", "admin"]

    made = []

    class Result:
        name = "backup_20261119T100000Z"

    def fake_backup(**kw):
        made.append(kw)
        return Result()

    monkeypatch.setattr(backup_mod, "create_backup", fake_backup)
    started = admin.post("/api/v1/backups", json={"include_secrets": False})
    assert started.status_code == 200 and started.json()["status"] == "pending"
    # The request only queues the job (a background worker of another test app may pick it up at once).
    db = _db()
    # Older tests may leave pending jobs in the shared database; park them so only ours runs.
    others = db.query(Job).filter(Job.status == "pending", Job.id != started.json()["job"]).all()
    for row in others:
        row.status = "parked"
    db.commit()
    run_pending_jobs(db, limit=5)
    for row in others:
        row.status = "pending"
    db.commit()
    job = db.query(Job).filter_by(id=started.json()["job"]).one()
    assert job.status == "done" and job.payload["result"] == "backup_20261119T100000Z"
    db.close()
    assert made == [{"include_secrets": False, "include_models": False}]
    assert admin.delete("/api/v1/backups/not-a-backup").status_code == 404


def test_incident_clock_buckets_last_12_local_hours():
    """Clock: 12 buckets oldest first, current hour partial, quiet incidents left out, appliance time."""
    from datetime import datetime, timezone

    from app.noc import incident_clock

    now = datetime(2026, 11, 19, 17, 4, tzinfo=timezone.utc)  # 18:04 in Belgrade
    db = _db()
    tag = uuid4().hex[:6]
    for minutes, sev, status in [(2, "CRITICAL", "OPEN"), (30, "HIGH", "RESOLVED"), (65, "WARNING", "OPEN"),
                                 (11 * 60 - 10, "HIGH", "OPEN"), (13 * 60, "CRITICAL", "OPEN"), (5, "CRITICAL", "MAINT")]:
        db.add(Incident(number=f"INC-K{tag}{minutes}", title="clock", severity=sev, status=status,
                        fingerprint=f"clock:{tag}:{minutes}", started_at=now - timedelta(minutes=minutes)))
    db.commit()
    clock = incident_clock(db, now, tz="Europe/Belgrade")
    db.close()
    assert (clock["hour"], clock["minute"]) == (18, 4)
    hours = clock["hours"]
    assert [h["hour"] for h in hours] == [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    mine = {h["hour"]: (h["critical"], h["high"], h["warning"]) for h in hours}
    assert mine[18][0] >= 1  # 18:02 critical; the MAINT one is not counted
    assert mine[17][1] >= 1 and mine[16][2] >= 1  # 17:34 high, 16:59 warning
    assert mine[7][1] >= 1  # 07:14 local high is the oldest bucket; 05:04 local is outside the window


def test_tv_endpoint_is_for_viewers():
    viewer = _signed_in("viewer")
    data = viewer.get("/api/v1/tv").json()
    assert len(data["clock"]["hours"]) == 12 and "counts" in data and "oncall" in data
