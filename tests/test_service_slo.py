"""SLO per business service: assets name their services; SLOs and noise filters can cover one service."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from app.db import Base, SessionLocal, engine
from app.models import Asset, Incident, NoiseFilter, utcnow
from app.noise_filters import in_scope
from app.slo import business_services, evaluate, save


def test_service_slo_counts_every_asset_of_the_service():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tag = "".join(chr(97 + int(c, 16) % 26) for c in uuid4().hex[:6])
    erp = f"Erp{tag}"
    db_host = Asset(asset_id=f"erpdb-{tag}", hostname=f"erpdb-{tag}", ip="", type="Oracle DB", extras={"service": erp})
    app = Asset(asset_id=f"erpapp-{tag}", hostname=f"erpapp-{tag}", ip="", type="Linux Server", extras={"service": f"Payroll{tag}, {erp}"})
    other = Asset(asset_id=f"web-{tag}", hostname=f"web-{tag}", ip="", type="Linux Server", extras={"service": f"Intranet{tag}"})
    db.add_all([db_host, app, other])
    db.flush()
    now = utcnow()
    for asset, minutes in ((db_host, 30), (app, 15), (other, 500)):
        start = now - timedelta(hours=20)
        db.add(Incident(number=f"INC-V{uuid4().hex[:8]}", title="down", severity="CRITICAL", status="RESOLVED",
                        fingerprint=f"svc:{uuid4().hex}", asset_id=asset.id, started_at=start, resolved_at=start + timedelta(minutes=minutes)))
    db.commit()
    try:
        slo = save(db, {"name": f"ERP {tag}", "scope": "service", "scope_value": erp.lower(), "target": 99.9, "window_days": 30}, "eng@dc.local")
        result = evaluate(db, slo, now)
        assert result["incidents"] == 2 and abs(result["downtime_min"] - 30) < 0.2  # union of 30 and 15, the intranet is out
        assert {erp, f"Payroll{tag}", f"Intranet{tag}"} <= set(business_services(db))
        flt = NoiseFilter(name="x", scope="service", scope_value=f"payroll{tag}")
        assert in_scope(flt, app) and not in_scope(flt, db_host)
        db.delete(slo)
    finally:
        for a in (db_host, app, other):
            db.query(Incident).filter_by(asset_id=a.id).delete()
            db.delete(a)
        db.commit()
        db.close()
