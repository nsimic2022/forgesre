"""Honesty fixes: demo off dashboard tiles, playrule sample match, RCA re-collect, viewer /ops."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.demo_ids import is_lab_inventory_row
from app.inventory import asset_tiles, create_manual_asset
from app.main import app
from app.models import Asset, Playbook, Playrule, User
from app.security import hash_password
from app.seed import seed
from app.services import match_playrule
from rca.analysis import detect_anomalies
from rca.collector import collect_evidence_set
from rca.llm import ground_llm_fields
from rca.types import RCAContext


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(client: TestClient, email: str = "admin@forgesre.local", password: str = "testpass") -> None:
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_asset_tiles_exclude_lab_and_list_shows_demo_pill():
    db = _db()
    live = create_manual_asset(db, hostname=f"prod-{uuid4().hex[:6]}", ip="10.55.1.8", actor="tester")
    tiles = {tile["key"]: tile for tile in asset_tiles(db.query(Asset).all())}
    assert tiles["assets"]["href"] == "/assets?lab=0"
    lab_only = [row for row in db.query(Asset).all() if is_lab_inventory_row(row)]
    assert tiles["assets"]["count"] == db.query(Asset).count() - len(lab_only)
    client = TestClient(app)
    _login(client)
    listed = client.get("/assets?per_page=100")
    assert 'class="pill demo">DEMO</span>' in listed.text
    assert "forge-demo-01" in listed.text
    production = client.get(f"/assets?lab=0&q={live.asset_id}")
    assert "forge-demo-01" not in production.text
    assert live.asset_id in production.text
    db.close()


def test_dashboard_incident_tiles_exclude_demo_href():
    client = TestClient(app)
    _login(client)
    home = client.get("/")
    alarms = home.text.split("dash-tiles-incidents", 1)[1].split("</section>", 1)[0]
    assert "demo=0" in alarms
    infra = home.text.split("dash-tiles-infra", 1)[1].split("</section>", 1)[0]
    assert "lab=0" in infra
    assert 'href="/ops"' in home.text


def test_viewer_forbidden_on_ops_and_insights_export():
    db = _db()
    email = f"view-{uuid4().hex[:6]}@forgesre.local"
    db.add(User(email=email, name="V", password_hash=hash_password("testpass"), role="viewer"))
    db.commit()
    client = TestClient(app)
    _login(client, email)
    home = client.get("/")
    assert 'href="/ops"' not in home.text
    assert client.get("/ops").status_code == 403
    assert client.post("/insights/export", data={"format": "txt"}).status_code == 403
    db.close()


def test_match_playrule_uses_firing_sample_when_threshold_set():
    db = _db()
    book = db.query(Playbook).filter_by(slug="cpu-high").one()
    token = uuid4().hex[:6]
    mild = Playrule(
        name=f"cpu-mild-{token}",
        enabled=True,
        severity="warning",
        condition={"alertname": f"CpuSplit{token}", "metric": "cpu_usage", "operator": ">", "value": 80},
        playbook_id=book.id,
    )
    hot = Playrule(
        name=f"cpu-hot-{token}",
        enabled=True,
        severity="critical",
        condition={"alertname": f"CpuSplit{token}", "metric": "cpu_usage", "operator": ">", "value": 90},
        playbook_id=book.id,
    )
    db.add_all([hot, mild])
    db.commit()
    labels = {"alertname": f"CpuSplit{token}"}
    notes = {"description": f"CPU usage is 85% on host-{token}."}
    assert match_playrule(db, f"CpuSplit{token}", labels, annotations=notes).id == mild.id
    notes94 = {"description": f"CPU usage is 94% on host-{token}."}
    # both thresholds hold; first by id wins (hot was inserted first)
    assert match_playrule(db, f"CpuSplit{token}", labels, annotations=notes94).id == hot.id
    too_low = {"description": "CPU usage is 10% on host."}
    assert match_playrule(db, f"CpuSplit{token}", labels, annotations=too_low) is None
    assert match_playrule(db, "HighCPU", {}) is None
    assert match_playrule(
        db, "HighCPU", {}, annotations={"description": "CPU usage is 94% on forge-demo-01."}
    ).name == "high-cpu"
    db.close()


def test_sudden_increase_needs_previous_sample():
    ctx = RCAContext.from_legacy(
        {
            "incident": {"number": "INC-1", "title": "High CPU"},
            "asset": {"hostname": "app-01"},
            "alert": {"alertname": "HighCPU"},
            "metrics": {"cpu_percent": 94},
        }
    )
    kinds = {item.kind for item in detect_anomalies(ctx)}
    assert "threshold_violation" in kinds
    assert "sudden_increase" not in kinds


def test_metric_collect_continues_after_one_prom_error():
    seen: list[str] = []

    def fetcher(expr: str):
        seen.append(expr)
        if "cpu" in expr:
            return {"error": "timeout"}
        return {"value": 12.0, "query": expr}

    items, limits = collect_evidence_set(
        incident={"number": "INC-1", "title": "High CPU"},
        asset={"asset_id": "app-01", "hostname": "app-01", "type": "Linux Server"},
        alert={"alertname": "NodeCPUHigh"},
        history=[],
        playrules=[],
        maintenance=[],
        metric_fetcher=fetcher,
        log_fetcher=lambda query, start, end: {"skipped": True},
    )
    assert "Metrics unavailable." in limits
    assert len(seen) > 1
    assert any(item.type == "METRIC" and item.metadata.get("unavailable") for item in items)
    assert any(item.type == "METRIC" and not item.metadata.get("unavailable") for item in items)


def test_ground_llm_drops_invented_numbers():
    draft = {
        "summary": "CPU usage increased rapidly on app-01.",
        "likely_cause": "High process activity is consuming CPU.",
        "recommended_action": "Engineer should inspect top CPU processes.",
    }
    kept, invented = ground_llm_fields(
        {"summary": "CPU is 94% on app-01.", "likely_cause": draft["likely_cause"], "recommended_action": draft["recommended_action"]},
        draft=draft,
        allowed_text="cpu_percent is 94.0%.",
    )
    assert invented is False
    assert "94" in kept["summary"]
    dropped, invented = ground_llm_fields(
        {"summary": "Memory is 12% on other-host.", "likely_cause": draft["likely_cause"], "recommended_action": draft["recommended_action"]},
        draft=draft,
        allowed_text="cpu_percent is 94.0%.",
    )
    assert invented is True
    assert dropped["summary"] == draft["summary"]


def test_collect_writes_previous_from_range():
    def fetcher(expr: str):
        return {"value": 94.0, "query": expr}

    def ranged(expr: str):
        del expr
        return {"values": [40.0, 55.0, 94.0]}

    items, _limits = collect_evidence_set(
        incident={"number": "INC-1", "title": "High CPU"},
        asset={"asset_id": "app-01", "hostname": "app-01", "type": "Linux Server"},
        alert={"alertname": "NodeCPUHigh"},
        history=[],
        playrules=[],
        maintenance=[],
        metric_fetcher=fetcher,
        log_fetcher=lambda query, start, end: {"skipped": True},
        range_fetcher=ranged,
    )
    cpu = next(
        item
        for item in items
        if item.type == "METRIC" and isinstance(item.content, dict) and item.content.get("name") == "cpu_percent"
    )
    assert cpu.content["previous"] == 40.0
    ctx = RCAContext.from_legacy({"incident": {"title": "High CPU"}, "asset": {"hostname": "app-01"}})
    ctx.evidence = [cpu]
    kinds = {item.kind for item in detect_anomalies(ctx)}
    assert "sudden_increase" in kinds


def test_score_confidence_has_no_baseline_bonus():
    from rca.analysis import score_confidence

    empty = score_confidence(anomalies=[], hypotheses=[], history=[], maintenance=[], sources_ok=True)
    assert empty == 0.10
    from rca.types import Anomaly, Hypothesis

    richer = score_confidence(
        anomalies=[Anomaly(kind="threshold_violation", summary="cpu", metric="cpu_percent")],
        hypotheses=[Hypothesis(id="high-process", summary="x", supporting_evidence=["EV-1", "EV-2"])],
        history=[{"number": "INC-1"}],
        maintenance=[],
        sources_ok=True,
    )
    assert richer == 0.45
    assert richer > empty


def test_facts_state_missing_process_and_ifoper_lists():
    from rca.analysis import facts_from

    linux = RCAContext.from_legacy(
        {
            "incident": {"number": "INC-1", "title": "High CPU"},
            "asset": {"hostname": "app-01", "type": "Linux Server"},
            "alert": {"alertname": "NodeCPUHigh"},
            "metrics": {"cpu_percent": 94},
        }
    )
    texts = [fact["text"] for fact in facts_from(linux)]
    assert any("No process list was collected" in text for text in texts)
    switch = RCAContext.from_legacy(
        {
            "incident": {"number": "INC-2", "title": "Interface down"},
            "asset": {"hostname": "core-sw-01", "type": "Switch"},
            "alert": {"alertname": "NetworkInterfaceDown"},
            "metrics": {"up": 1},
        }
    )
    texts = [fact["text"] for fact in facts_from(switch)]
    assert any("No per-interface SNMP ifOperStatus list" in text for text in texts)


def test_dashboard_recent_hides_lab_demo():
    from app.models import Incident
    from app.services import next_incident_number

    db = _db()
    demo = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    row = Incident(
        number=next_incident_number(db),
        title=f"lab-recent-{uuid4().hex[:6]}",
        severity="WARNING",
        status="OPEN",
        fingerprint=f"lab-recent:{uuid4().hex}",
        asset_id=demo.id,
    )
    db.add(row)
    db.commit()
    title = row.title
    db.close()
    client = TestClient(app)
    _login(client)
    home = client.get("/")
    recent = home.text.split("<h2>Recent incidents</h2>", 1)[1].split("dash-graphs", 1)[0]
    assert "filter-chips" not in recent
    assert "Lab DEMO" not in recent
    assert ">Production<" not in recent
    assert title not in recent
    lab = client.get("/?demo=1")
    shown = lab.text.split("<h2>Recent incidents</h2>", 1)[1].split("dash-graphs", 1)[0]
    assert title in shown
    assert "forge-demo-01" in shown
    assert "filter-chips" not in shown


def test_force_investigation_overwrites_latest_row():
    from app.models import Incident, Investigation
    from app.services import run_investigation

    db = _db()
    incident = Incident(
        number="INC-0777_20.10.2026_18:00",
        title="overwrite rca",
        severity="WARNING",
        status="OPEN",
        fingerprint=f"overwrite:{uuid4().hex[:8]}",
    )
    db.add(incident)
    db.commit()
    first = run_investigation(db, incident, actor="a", use_llm=False)
    assert db.query(Investigation).filter_by(incident_id=incident.id).count() == 1
    second = run_investigation(db, incident, actor="b", force=True, use_llm=False)
    assert db.query(Investigation).filter_by(incident_id=incident.id).count() == 1
    assert first.id == second.id
    assert second.requested_by == "b"
    db.close()


def test_query_prometheus_expr_does_not_short_circuit_demo_gauge(monkeypatch):
    from app.services import query_prometheus_expr

    class Boom:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *args, **kwargs):
            raise RuntimeError("prometheus down")

    monkeypatch.setattr("httpx.Client", lambda **kwargs: Boom())
    out = query_prometheus_expr("forgesre_demo_cpu_percent")
    assert "error" in out
    assert "value" not in out or out.get("value") is None
