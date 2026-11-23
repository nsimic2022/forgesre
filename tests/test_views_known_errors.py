"""Saved views and team filter on the incident list; the known-errors knowledge base."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.cli_ops import format_known_errors
from app.db import SessionLocal
from app.forgeai_investigator import initial_evidence
from app.known_errors import known_errors_for, matches
from app.models import Asset, Incident, KnownError, SavedView, utcnow
from test_incident_tools import _client


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    db = SessionLocal()
    db.query(SavedView).filter(SavedView.name.like("vk-%")).delete(synchronize_session=False)
    db.query(KnownError).filter(KnownError.title.like("vk-%")).delete(synchronize_session=False)
    for inc in db.query(Incident).filter(Incident.title.like("vk-%")).all():
        db.delete(inc)
    db.query(Asset).filter(Asset.asset_id.like("vk-%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def _incident(db, title: str, asset: Asset | None = None, alertname: str = "") -> Incident:
    row = Incident(number=f"INC-VK-{uuid4().hex[:6]}", title=title, severity="CRITICAL", status="OPEN",
                   fingerprint=f"{alertname or 'vk'}:{uuid4().hex}", started_at=utcnow(), asset_id=asset.id if asset else None,
                   alert_payload={"labels": {"alertname": alertname}} if alertname else {})
    db.add(row)
    db.commit()
    return row


def test_team_filter_and_teams_list():
    db = SessionLocal()
    net = Asset(asset_id="vk-sw", hostname="vk-sw", ip="10.66.0.1", type="Switch", owner="vk-network")
    db.add(net)
    db.commit()
    mine = _incident(db, "vk-team link down", net)
    _incident(db, "vk-team other")
    page = _client("viewer").get("/api/v1/incidents-page?tab=all&team=VK-Network&q=vk-team").json()
    assert [r["number"] for r in page["rows"]] == [mine.number]
    assert "vk-network" in page["teams"]
    csv = _client("viewer").get("/api/v1/incidents-page.csv?team=vk-network&q=vk-team").text
    assert mine.number in csv and "vk-team other" not in csv
    db.close()


def test_saved_views_personal_and_shared():
    analyst, engineer, viewer = _client("analyst"), _client("engineer"), _client("viewer")
    assert viewer.post("/api/v1/views", json={"name": "vk-mine", "filters": {}}).status_code == 403
    mine = analyst.post("/api/v1/views", json={"name": "vk-mine", "filters": {"severity": "CRITICAL", "junk": "x", "q": ""}})
    assert mine.status_code == 200 and mine.json()["filters"] == {"severity": "CRITICAL"}
    assert analyst.post("/api/v1/views", json={"name": "vk-share", "team": "network"}).status_code == 403
    shared = engineer.post("/api/v1/views", json={"name": "vk-net", "team": "network", "filters": {"team": "network"}}).json()
    seen = {v["name"]: v for v in viewer.get("/api/v1/views").json()["views"]}
    assert "vk-net" in seen and "vk-mine" not in seen
    own = {v["name"]: v for v in analyst.get("/api/v1/views").json()["views"]}
    assert own["vk-mine"]["mine"] and own["vk-mine"]["can_delete"] and not own["vk-net"]["can_delete"]
    assert analyst.delete(f"/api/v1/views/{shared['id']}").status_code == 403
    assert engineer.delete(f"/api/v1/views/{shared['id']}").status_code == 200


def test_known_error_matching_rules():
    both = KnownError(title="vk", alertname="DiskLatency", pattern="raid")
    assert matches(both, "disklatency", "RAID write latency high")
    assert not matches(both, "DiskLatency", "write latency high")
    assert matches(KnownError(title="vk", pattern="ups"), "", "UPS on battery")
    assert not matches(KnownError(title="vk"), "x", "y")


def test_known_errors_api_incident_and_forgeai():
    engineer, analyst = _client("engineer"), _client("analyst")
    body = {"title": "vk-battery learn cycle", "alertname": "VkDiskLatency", "symptoms": "write latency every 90 days",
            "workaround": "wait 2 h, it clears", "fix": "firmware 8.2", "link": "https://kb.example/123"}
    assert analyst.post("/api/v1/known-errors", json=body).status_code == 403
    assert engineer.post("/api/v1/known-errors", json={"title": "vk-never"}).status_code == 400
    assert engineer.post("/api/v1/known-errors", json={**body, "link": "ftp://x"}).status_code == 400
    created = engineer.post("/api/v1/known-errors", json=body).json()
    db = SessionLocal()
    inc = _incident(db, "vk-latency on db01", alertname="VkDiskLatency")
    assert [k["id"] for k in known_errors_for(db, inc)] == [created["id"]]
    view = analyst.get(f"/api/v1/incidents/{inc.number}/view").json()
    assert view["known_errors"][0]["workaround"] == "wait 2 h, it clears" and view["alertname"] == "VkDiskLatency"
    assert view["can_known_error"] is False
    ev = initial_evidence(db, inc)
    assert any(i["source"] == "known_error" and "firmware 8.2" in i["text"] for i in ev.items)
    found = analyst.get("/api/v1/known-errors?q=battery").json()
    assert [k["id"] for k in found["items"]] == [created["id"]]
    assert "vk-battery learn cycle" in format_known_errors(found) and "wait 2 h" in format_known_errors(found)
    upd = engineer.put(f"/api/v1/known-errors/{created['id']}", json={**body, "status": "fixed"}).json()
    assert upd["status"] == "fixed"
    assert engineer.delete(f"/api/v1/known-errors/{created['id']}").status_code == 200
    db.close()
