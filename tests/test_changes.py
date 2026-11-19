"""What changed before an incident: posted changes (token or Engineer) plus ForgeSRE's audit log."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from app.audit import audit
from app.changes import changes_before, record
from app.db import Base, SessionLocal, engine
from app.models import Asset, Incident, utcnow
from tests.test_report_cron import _signed_in


def _setup(tag):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    sw = Asset(asset_id=f"sw-{tag}", hostname=f"sw-{tag}", ip="10.88.0.1", type="Switch")
    db.add(sw)
    db.flush()
    srv = Asset(asset_id=f"srv-{tag}", hostname=f"srv-{tag}", ip="10.88.0.2", type="Linux Server", parent_id=sw.id)
    other = Asset(asset_id=f"oth-{tag}", hostname=f"oth-{tag}", ip="10.88.0.3", type="Linux Server")
    db.add_all([srv, other])
    db.flush()
    inc = Incident(number=f"INC-W{tag}", title="Disk", severity="HIGH", status="OPEN", fingerprint=f"chg:{tag}",
                   asset_id=srv.id, started_at=utcnow())
    db.add(inc)
    db.commit()
    return db, inc


def test_changes_on_asset_parent_and_global_but_not_elsewhere():
    tag = uuid4().hex[:6]
    db, inc = _setup(tag)
    now = utcnow()
    record(db, title=f"deploy api {tag}", kind="deploy", asset_ref=f"srv-{tag}", at=now - timedelta(minutes=40), url="https://ci.example/1")
    record(db, title=f"firmware {tag}", kind="config", asset_ref="10.88.0.1", at=now - timedelta(hours=3))
    record(db, title=f"global dns {tag}", kind="change", at=now - timedelta(hours=5))
    record(db, title=f"other host {tag}", asset_ref=f"oth-{tag}", at=now - timedelta(minutes=10))
    record(db, title=f"too old {tag}", asset_ref=f"srv-{tag}", at=now - timedelta(hours=30))
    unknown = record(db, title=f"unknown asset {tag}", asset_ref="nowhere-01", at=now - timedelta(hours=1))
    assert unknown.asset_id is None and "asset: nowhere-01" in unknown.detail
    audit(db, "asset.update", actor="ana@dc.local", object_type="asset", object_id=f"srv-{tag}")
    audit(db, "playrule.update", actor="ana@dc.local", object_type="playrule", object_id=f"disk-{tag}")
    audit(db, "asset.update", actor="ana@dc.local", object_type="asset", object_id=f"oth-{tag}")
    db.commit()
    inc.started_at = utcnow() + timedelta(seconds=5)
    db.commit()
    rows = changes_before(db, inc, limit=50)
    titles = [r["title"] for r in rows]
    assert f"deploy api {tag}" in titles and f"firmware {tag}" in titles and f"global dns {tag}" in titles
    assert f"other host {tag}" not in titles and f"too old {tag}" not in titles
    assert f"asset update srv-{tag}" in titles and f"playrule update disk-{tag}" in titles
    assert f"asset update oth-{tag}" not in titles
    deploy = next(r for r in rows if r["title"] == f"deploy api {tag}")
    assert 39 <= deploy["before_min"] <= 41 and deploy["asset"] == f"srv-{tag}"
    db.close()


def test_post_changes_with_token_or_engineer(monkeypatch):
    from app.settings import Settings

    monkeypatch.setattr(Settings, "changes_webhook_token", property(lambda self: "chg-token"))
    tag = uuid4().hex[:6]
    from fastapi.testclient import TestClient

    from app.main import create_app

    anon = TestClient(create_app())
    body = {"title": f"deploy {tag}", "kind": "deploy", "source": "gitlab"}
    assert anon.post("/api/v1/changes", json=body).status_code == 401
    assert anon.post("/api/v1/changes", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = anon.post("/api/v1/changes", json=body, headers={"Authorization": "Bearer chg-token"})
    assert ok.status_code == 200 and ok.json()["asset_matched"] is False
    assert anon.post("/api/v1/changes", json={**body, "url": "ftp://x"}, headers={"Authorization": "Bearer chg-token"}).status_code == 400
    assert _signed_in("analyst").post("/api/v1/changes", json=body).status_code == 401
    eng = _signed_in("engineer")
    assert eng.post("/api/v1/changes", json={**body, "title": f"manual {tag}"}).status_code == 200
    listed = eng.get("/api/v1/changes").json()["changes"]
    mine = [c for c in listed if tag in c["title"]]
    assert {c["source"] for c in mine} == {"gitlab", "manual"}
