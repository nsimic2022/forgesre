"""Major incidents, SLA timers, shift handover and follow-up actions."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from app.incident_process import sla_state
from app.models import Incident, utcnow

from test_dc_noc_parent import _db
from test_incident_tools import _client


def _incident(db, title: str) -> Incident:
    row = Incident(number=f"INC-P{uuid4().hex[:8]}", title=title, severity="HIGH", status="OPEN",
                   fingerprint=f"process:{uuid4().hex}", started_at=utcnow() - timedelta(minutes=5), source="prometheus")
    db.add(row)
    db.commit()
    return row


def test_major_incident_declare_update_and_stand_down():
    db = _db()
    row = _incident(db, f"MajorTest{uuid4().hex[:6]}")
    analyst, viewer = _client("analyst"), _client("viewer")
    url = f"/api/v1/incidents/{row.number}"
    assert viewer.post(f"{url}/major", json={"on": True}).status_code == 403
    assert analyst.post(f"{url}/major", json={"on": True, "bridge_url": "ftp://x"}).status_code == 400
    major = analyst.post(f"{url}/major", json={"on": True, "commander": "Ana", "bridge_url": "https://meet.example/abc", "update_minutes": 15}).json()
    assert major["on"] and major["commander"] == "Ana" and major["next_due"] and "[MAJOR INCIDENT]" in major["message"]
    assert "Bridge call: https://meet.example/abc" in major["message"]
    assert analyst.post(f"{url}/updates", json={"text": "  "}).status_code == 400
    after = analyst.post(f"{url}/updates", json={"text": "Storage array rebooting, ETA 20 min"}).json()
    assert after["note"]["body"].startswith("Status update: Storage array") and after["major"]["last_update_at"]
    view = analyst.get(f"{url}/view").json()
    assert view["major"]["on"] and any(t.get("title") == "UPDATE" for t in view["timeline"])
    noc = analyst.get("/api/v1/noc").json()
    assert any(m["number"] == row.number for m in noc["major"])
    analyst.post(f"{url}/major", json={"on": False})
    assert not analyst.get(f"{url}/view").json()["major"]["on"]


def test_sla_running_met_and_breached(monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "sla": {"enabled": True, "response": {"critical": 10}}})
    now = utcnow()
    open_inc = Incident(number="X", title="t", severity="CRITICAL", started_at=now - timedelta(minutes=4), source="prometheus")
    st = sla_state(open_inc)
    assert st["response"]["state"] == "running" and 5 <= st["response"]["minutes_left"] <= 6
    late = Incident(number="Y", title="t", severity="CRITICAL", started_at=now - timedelta(minutes=30), ack_at=now - timedelta(minutes=5), source="prometheus")
    assert sla_state(late)["response"]["state"] == "breached"
    fast = Incident(number="Z", title="t", severity="HIGH", started_at=now - timedelta(minutes=30), ack_at=now - timedelta(minutes=25),
                    resolved_at=now, source="prometheus")
    st = sla_state(fast)
    assert st["response"]["state"] == "met" and st["resolve"]["state"] == "met" and st["response"]["target_min"] == 30
    assert sla_state(Incident(number="S", title="storm", severity="CRITICAL", started_at=now, source="forgesre")) is None
    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "sla": {"enabled": False}})
    assert sla_state(open_inc) is None


def test_sla_report_endpoint():
    data = _client("viewer").get("/api/v1/sla?days=7").json()
    assert data["days"] == 7 and set(data["by_severity"]) == {"critical", "high", "warning"}
    assert data["targets"]["response"]["critical"] == 15


def test_handover_written_then_taken_by_someone_else():
    db = _db()
    _incident(db, f"HandoverTest{uuid4().hex[:6]}")
    writer, other, viewer = _client("analyst"), _client("engineer"), _client("viewer")
    assert viewer.post("/api/v1/handover", json={"notes": "x"}).status_code == 403
    made = writer.post("/api/v1/handover", json={"notes": "Watch the backup job; vendor promised a call at 9"}).json()
    assert made["open_incidents"] and not made["taken_by"]
    assert writer.post(f"/api/v1/handover/{made['id']}/take").status_code == 409
    taken = other.post(f"/api/v1/handover/{made['id']}/take").json()
    assert taken["taken_by"] and taken["taken_at"]
    assert other.post(f"/api/v1/handover/{made['id']}/take").status_code == 409
    latest = viewer.get("/api/v1/noc").json()["handover"]
    assert latest["id"] == made["id"] and latest["taken_by"]


def test_follow_up_actions_add_tick_and_list():
    db = _db()
    row = _incident(db, f"ActionTest{uuid4().hex[:6]}")
    analyst = _client("analyst")
    url = f"/api/v1/incidents/{row.number}/actions"
    assert analyst.post(url, json={"text": ""}).status_code == 400
    assert analyst.post(url, json={"text": "x", "due": "next week"}).status_code == 400
    late = analyst.post(url, json={"text": "Add a second uplink", "owner": "Network team", "due": "2020-01-01"}).json()
    assert late["overdue"] and late["owner"] == "Network team"
    open_rows = analyst.get("/api/v1/actions").json()["actions"]
    assert any(a["id"] == late["id"] and a["incident"] == row.number for a in open_rows)
    done = analyst.patch(f"/api/v1/actions/{late['id']}", json={"done": True}).json()
    assert done["done"] and done["done_at"] and not done["overdue"]
    assert all(a["id"] != late["id"] for a in analyst.get("/api/v1/actions").json()["actions"])
    assert analyst.get(f"/api/v1/incidents/{row.number}/view").json()["actions"][0]["text"] == "Add a second uplink"
    md = analyst.get(f"/api/v1/incidents/{row.number}/postmortem").text
    assert "Add a second uplink *(done)* | Network team | 2020-01-01" in md


def test_cli_formats():
    from app.cli_ops import format_actions, format_handover, format_sla

    assert "NOT TAKEN OVER YET" in format_handover([{"id": 1, "created_by": "ana", "created_at": "2026-11-23T08:00", "notes": "n",
                                                      "open_incidents": [], "taken_by": "", "taken_at": None}])
    assert "2020-01-01 !" in format_actions([{"due": "2020-01-01", "overdue": True, "owner": "net", "incident": "INC-1_x", "text": "uplink"}])
    assert "critical" in format_sla({"enabled": True, "days": 30, "by_severity": {"critical": {"response_met": 1, "response_breached": 0,
                                     "resolve_met": 1, "resolve_breached": 0}}, "targets": {}})
