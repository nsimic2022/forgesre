"""Postmortem draft: facts from the incident, gaps marked, file download."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from app.changes import record
from app.db import Base, SessionLocal, engine
from app.models import Asset, Incident, utcnow
from tests.test_report_cron import _signed_in


def test_postmortem_has_facts_and_marks_gaps():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tag = uuid4().hex[:6]
    sw = Asset(asset_id=f"pm-sw-{tag}", hostname=f"pm-sw-{tag}", ip="10.66.1.1", type="Switch")
    db.add(sw)
    db.flush()
    db.add_all([Asset(asset_id=f"pm-s{n}-{tag}", hostname=f"pm-s{n}-{tag}", ip=f"10.66.1.{n + 10}", type="Linux Server", parent_id=sw.id) for n in range(3)])
    start = utcnow() - timedelta(hours=2)
    inc = Incident(number=f"INC-P{tag}", title="Uplink down", severity="CRITICAL", status="RESOLVED", fingerprint=f"pm:{tag}",
                   asset_id=sw.id, started_at=start, ack_at=start + timedelta(minutes=7), ack_by="ana@dc.local",
                   resolved_at=start + timedelta(minutes=95), resolved_by="ana@dc.local", root_cause="Failed SFP on Gi1/0/48",
                   reviewed_by="marko@dc.local",
                   timeline=[{"id": "x", "title": "Ladder", "detail": "L2 mailed", "at": (start + timedelta(minutes=5)).isoformat()}])
    db.add(inc)
    db.commit()
    record(db, title=f"firmware push {tag}", kind="config", asset_ref=f"pm-sw-{tag}", at=start - timedelta(minutes=30), url="https://ci.example/9")
    db.close()
    client = _signed_in("viewer")
    data = client.get(f"/api/v1/incidents/INC-P{tag}/postmortem").json()
    md = data["markdown"]
    assert data["filename"] == f"postmortem-INC-P{tag}.md"
    assert md.startswith(f"# Postmortem: INC-P{tag} · Uplink down")
    assert "after 7 min" in md and "after 1 h 35 min" in md
    assert "3 asset(s) sit behind" in md
    assert "Failed SFP on Gi1/0/48 *(from the incident review by marko@dc.local)*" in md
    assert f"firmware push {tag} ([link](https://ci.example/9))" in md
    assert "Ladder · L2 mailed" in md and "Acknowledged by ana@dc.local" in md
    assert md.count("[to fill]") >= 5
    file = client.get(f"/api/v1/incidents/INC-P{tag}/postmortem?download=1")
    assert file.status_code == 200 and file.headers["content-type"].startswith("text/markdown")
    assert f'filename="postmortem-INC-P{tag}.md"' in file.headers["content-disposition"]
    assert client.get("/api/v1/incidents/INC-NOPE/postmortem").status_code == 404
