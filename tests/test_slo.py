"""SLOs: availability from incidents (union for groups), budget, burn, quiet incidents ignored."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from app.cli_ops import format_slos
from app.db import Base, SessionLocal, engine
from app.models import Asset, Incident, Slo, utcnow
from app.slo import evaluate, save
from tests.test_report_cron import _signed_in


def test_availability_budget_and_burn_with_overlap_and_quiet_incidents():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tag = uuid4().hex[:6]
    kind = f"SloType{tag}"
    a1 = Asset(asset_id=f"slo1-{tag}", hostname=f"slo1-{tag}", ip="10.31.0.1", type=kind)
    a2 = Asset(asset_id=f"slo2-{tag}", hostname=f"slo2-{tag}", ip="10.31.0.2", type=kind)
    db.add_all([a1, a2])
    db.flush()
    now = utcnow()

    def inc(asset, start_h, minutes, sev="CRITICAL", status="RESOLVED"):
        start = now - timedelta(hours=start_h)
        db.add(Incident(number=f"INC-S{uuid4().hex[:8]}", title="down", severity=sev, status=status, fingerprint=f"slo:{uuid4().hex}",
                        asset_id=asset.id, started_at=start, resolved_at=start + timedelta(minutes=minutes)))

    inc(a1, 48, 30)            # 30 min
    inc(a2, 48, 20)            # overlaps the first: union stays 30 min
    inc(a1, 10, 10)            # 10 min within the last day
    inc(a2, 5, 999, sev="WARNING")       # not counted (severity)
    inc(a1, 3, 120, status="MAINT")      # not counted (maintenance)
    db.commit()
    slo = save(db, {"name": f"SLO {tag}", "scope": "type", "scope_value": kind, "target": 99.9, "window_days": 30}, "eng@dc.local")
    r = evaluate(db, slo, now)
    assert r["incidents"] == 3 and abs(r["downtime_min"] - 40) < 0.2
    assert abs(r["budget_min"] - 43.2) < 0.1                      # 0.1 % of 30 days
    assert r["status"] == "at_risk" and 90 < r["budget_used_pct"] < 95
    assert abs(r["burn_rate_24h"] - 10 / 1.44) < 0.05             # 10 min in a day vs 1.44 min/day even spend
    assert r["worst"][0]["asset"] == f"slo1-{tag}"
    assert 99.9 < r["availability"] < 99.92
    db.close()


def test_slo_validation_api_roles_and_cli():
    tag = uuid4().hex[:6]
    viewer = _signed_in("viewer")
    assert "slos" in viewer.get("/api/v1/slos").json()
    assert viewer.post("/api/v1/slos", json={"name": "x"}).status_code == 403
    eng = _signed_in("engineer")
    for bad in ({"name": ""}, {"name": f"b{tag}", "target": 100}, {"name": f"b{tag}", "scope": "asset"},
                {"name": f"b{tag}", "window_days": 0}, {"name": f"b{tag}", "severities": ["NOPE"]}, {"name": f"b{tag}", "scope": "rack", "scope_value": "x"}):
        assert eng.post("/api/v1/slos", json=bad).status_code == 400, bad
    made = eng.post("/api/v1/slos", json={"name": f"All {tag}", "target": 99.5})
    assert made.status_code == 200 and made.json()["status"] in ("ok", "at_risk", "breached")
    assert eng.post("/api/v1/slos", json={"name": f"All {tag}"}).status_code == 400
    sid = made.json()["id"]
    assert eng.put(f"/api/v1/slos/{sid}", json={"name": f"All {tag}", "target": 99.0}).json()["target"] == 99.0
    assert eng.delete(f"/api/v1/slos/{sid}").status_code == 200
    db = SessionLocal()
    assert db.get(Slo, sid) is None
    db.close()
    text = format_slos([{"status": "breached", "name": "ERP", "availability": 99.861, "target": 99.9, "budget_left_min": -16.8, "burn_rate_24h": 2.5}])
    assert "BREACHED  ERP" in text and "99.861%" in text and "2.5x" in text
