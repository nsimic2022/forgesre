"""Appliance checks: TLS expiry, ForgeSRE backup age, heartbeats → alerts through ingest."""

from __future__ import annotations

import ssl
from datetime import timedelta
from uuid import uuid4

from app import health_checks as hc
from app.db import Base, SessionLocal, engine
from app.models import CheckResult, Heartbeat, Incident, utcnow
from app.settings import settings
from tests.test_report_cron import _signed_in


def _with_checks(**cfg):
    old = settings.yaml
    settings.yaml = {**(old or {}), "checks": cfg}
    return old


def test_tls_targets_parse_and_severity_thresholds():
    tag = uuid4().hex[:6]
    old = _with_checks(tls_targets=f"ok-{tag}.dc, https://warn-{tag}.dc:8443, crit-{tag}.dc:443; bad-{tag}.dc", tls_warn_days=14, tls_critical_days=3)
    try:
        assert hc.tls_targets()[1] == (f"warn-{tag}.dc", 8443)
        days = {f"ok-{tag}.dc": 90.0, f"warn-{tag}.dc": 10.0, f"crit-{tag}.dc": 1.5}

        def probe(host, port):
            if host.startswith("bad"):
                raise ssl.SSLError("handshake")
            return days[host]

        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        alerts = hc.check_tls(db, probe)
        db.commit()
        by = {a["labels"]["target"]: a for a in alerts}
        assert by[f"ok-{tag}.dc:443"]["status"] == "resolved"
        assert by[f"warn-{tag}.dc:8443"]["labels"]["severity"] == "warning"
        assert by[f"crit-{tag}.dc:443"]["labels"]["severity"] == "critical"
        assert by[f"bad-{tag}.dc:443"]["labels"]["severity"] == "high"
        row = db.query(CheckResult).filter_by(kind="tls", target=f"warn-{tag}.dc:8443").one()
        assert row.status == "warning" and "10.0 days" in row.detail
        db.close()
    finally:
        settings.yaml = old


def test_backup_age_and_heartbeats_open_and_resolve_incidents():
    tag = uuid4().hex[:6]
    old = _with_checks(tls_targets="", backup_max_age_hours=36)
    try:
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        stale = [{"name": "b1", "mtime": (utcnow() - timedelta(hours=50)).isoformat()}]
        hc.ping(db, f"veeam-{tag}", every_hours=24, asset=f"nas-{tag}")
        hb = db.query(Heartbeat).filter_by(name=f"veeam-{tag}").one()
        hb.last_at = utcnow() - timedelta(hours=30)
        db.commit()
        result = hc.run_checks(db, archives=stale)
        assert result["failing"] >= 2
        backup = db.query(CheckResult).filter_by(kind="backup", target="forgesre").one()
        assert backup.status == "critical" and "50.0 h old" in backup.detail
        inc = db.query(Incident).filter(Incident.title.like("%HeartbeatMissing%"), Incident.status != "RESOLVED").all()
        assert any(f"veeam-{tag}" in (i.title or "") or f"nas-{tag}" in (i.fingerprint or "") for i in inc)
        hc.ping(db, f"veeam-{tag}")
        fresh = [{"name": "b2", "mtime": utcnow().isoformat()}]
        hc.run_checks(db, archives=fresh)
        assert db.query(CheckResult).filter_by(kind="heartbeat", target=f"veeam-{tag}").one().status == "ok"
        assert db.query(CheckResult).filter_by(kind="backup", target="forgesre").one().status == "ok"
        open_left = db.query(Incident).filter(Incident.fingerprint.like(f"%nas-{tag}%"), Incident.status.notin_(["RESOLVED", "CLOSED"])).count()
        assert open_left == 0
        db.close()
    finally:
        settings.yaml = old


def test_heartbeat_api_cli_and_doctor(monkeypatch):
    from app.cli_ops import format_checks
    from app.settings import Settings

    monkeypatch.setattr(Settings, "changes_webhook_token", property(lambda self: "beat-token"))
    tag = uuid4().hex[:6]
    from fastapi.testclient import TestClient

    from app.main import create_app

    anon = TestClient(create_app())
    assert anon.post(f"/api/v1/heartbeats/rsync-{tag}").status_code == 401
    ok = anon.post(f"/api/v1/heartbeats/rsync-{tag}?every_hours=12&asset=nas-01", headers={"Authorization": "Bearer beat-token"})
    assert ok.status_code == 200 and ok.json()["every_hours"] == 12
    assert anon.post("/api/v1/heartbeats/%20?every_hours=1", headers={"Authorization": "Bearer beat-token"}).status_code == 400
    assert anon.post(f"/api/v1/heartbeats/x-{tag}?every_hours=0", headers={"Authorization": "Bearer beat-token"}).status_code == 400
    viewer = _signed_in("viewer")
    data = viewer.get("/api/v1/checks").json()
    assert any(h["name"] == f"rsync-{tag}" for h in data["heartbeats"]) and data["can_edit"] is False
    assert viewer.post("/api/v1/checks/run").status_code == 403
    eng = _signed_in("engineer")
    assert eng.delete(f"/api/v1/heartbeats/rsync-{tag}").status_code == 200
    assert eng.delete(f"/api/v1/heartbeats/rsync-{tag}").status_code == 404
    text = format_checks({"results": [{"kind": "tls", "target": "a:443", "status": "ok", "detail": "fine"},
                                      {"kind": "backup", "target": "forgesre", "status": "critical", "detail": "old"}], "heartbeats": []})
    assert text.splitlines()[1].startswith("CRITICAL  backup")
    db = SessionLocal()
    assert hc.doctor_check(db)["status"] in ("ok", "warn", "disabled")
    db.close()


def test_real_probe_fails_fast_on_a_closed_port():
    import pytest

    with pytest.raises(OSError):
        hc.cert_days_left("127.0.0.1", 9, timeout=1)
