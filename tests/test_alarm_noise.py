"""Alarm noise: parents up the chain, alarm storms, flapping alerts, alert quality report."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.alarm_noise import STORM_FINGERPRINT, config
from app.dc_noc import set_asset_parent
from app.models import Incident, Notification
from app.services import ingest_alertmanager

from test_dc_noc_parent import _client, _db, _fire, _host, _resolve


@pytest.fixture()
def alarms(monkeypatch):
    """Turn storm and flapping detection on with small numbers (tests/forgesre.test.yml has them off)."""
    from app.settings import settings

    def use(storm: dict | None = None, flapping: dict | None = None):
        monkeypatch.setattr(settings, "yaml", {**settings.yaml, "alerts": {
            "storm": {"enabled": bool(storm), **(storm or {})},
            "flapping": {"enabled": bool(flapping), **(flapping or {})},
        }})

    return use


def _park_recent(db):
    """Other tests' incidents opened seconds ago would count toward a storm: move them back."""
    from datetime import timedelta

    from app.models import utcnow

    old = utcnow() - timedelta(days=1)
    db.query(Incident).filter(Incident.started_at >= old).update({"started_at": old}, synchronize_session=False)
    db.commit()


def test_grandparent_down_suppresses_and_links(alarms):
    alarms()
    db = _db()
    token = uuid4().hex[:8]
    core = _host(db, token + "a", "sw", "net@dc.local")
    rack = _host(db, token + "b", "sw", "net@dc.local")
    server = _host(db, token + "c", "srv", "sys@dc.local")
    set_asset_parent(db, rack, core.asset_id)
    set_asset_parent(db, server, rack.asset_id)
    db.commit()
    down = ingest_alertmanager(db, _fire(core.asset_id, "SnmpDeviceUnreachable"))[0]
    child = ingest_alertmanager(db, _fire(server.asset_id, "NodeExporterDown"))[0]
    db.refresh(child)
    assert child.status == "SUPPRESSED" and child.caused_by_id == down.id
    assert not db.query(Notification).filter(Notification.incident_id == child.id).count()


def test_storm_opens_one_paging_incident_and_links_the_rest(alarms):
    alarms(storm={"threshold": 4, "window_minutes": 5})
    db = _db()
    _park_recent(db)
    token = uuid4().hex[:8]
    hosts = [_host(db, f"{token}{i}", "srv", "sys@dc.local") for i in range(6)]
    opened = [ingest_alertmanager(db, _fire(h.asset_id, "NodeCPUHigh"))[0] for h in hosts]
    db.expire_all()
    storm = db.query(Incident).filter(Incident.fingerprint == STORM_FINGERPRINT, Incident.status == "OPEN").one()
    assert storm.severity == "CRITICAL" and storm.source == "forgesre"
    assert db.query(Notification).filter(Notification.incident_id == storm.id).count() >= 1
    linked = [db.get(Incident, i.id) for i in opened]
    assert all(i.caused_by_id == storm.id for i in linked)
    # the ones after the storm started did not page on their own
    assert not db.query(Notification).filter(Notification.incident_id.in_([i.id for i in linked[4:]])).count()
    assert storm.title.startswith("Alarm storm: 6 incidents since")
    storm.status = "RESOLVED"
    db.commit()


def test_flapping_alert_stops_paging_and_shows_a_counter(alarms):
    alarms(flapping={"threshold": 3, "window_minutes": 60})
    db = _db()
    token = uuid4().hex[:8]
    host = _host(db, token, "srv", "sys@dc.local")
    last = None
    for _ in range(3):
        last = ingest_alertmanager(db, _fire(host.asset_id, "NodeMemoryHigh"))[0]
        ingest_alertmanager(db, _resolve(host.asset_id, "NodeMemoryHigh"))
    db.refresh(last)
    assert last.flap_count == 3
    assert not db.query(Notification).filter(Notification.incident_id == last.id).count()
    row = _client().get("/api/v1/incidents-page?tab=all&q=" + host.hostname).json()["rows"][0]
    assert row["flap_count"] == 3


def test_settings_clamp_and_defaults(alarms):
    alarms(storm={"threshold": 1, "window_minutes": 99999})
    assert config("storm") == {"enabled": True, "threshold": 2, "window_minutes": 1440}
    assert config("flapping")["enabled"] is False
    groups = _client().get("/api/v1/settings").json()["groups"]
    assert groups["alarms"] == "Alarms"


def test_alert_quality_report_and_cli_format():
    from app.alert_quality import advice
    from app.cli_ops import format_alert_quality

    data = _client().get("/api/v1/reports/alert-quality?days=30").json()
    assert data["days"] == 30 and isinstance(data["rows"], list)
    assert all({"alertname", "incidents", "acked_pct", "self_cleared_pct", "advice"} <= set(r) for r in data["rows"])
    assert advice({"incidents": 10, "self_cleared_pct": 80, "flapping": 0, "acked_pct": 10}).startswith("Mostly clears")
    assert advice({"incidents": 2, "self_cleared_pct": 80, "flapping": 0, "acked_pct": 10}) == "Too few to judge"
    text = format_alert_quality({"days": 7, "incidents": 3, "actionable_pct": 33, "rows": [
        {"alertname": "NodeCPUHigh", "incidents": 3, "acked_pct": 33, "self_cleared_pct": 67, "flapping": 1, "advice": "Too few to judge"}]})
    assert "Last 7 days: 3 incidents, 33% acknowledged" in text and "NodeCPUHigh" in text
