"""Automatic ForgeAI per incident: one at a time, critical first, bursts grouped, states for the NOC."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from app.db import Base, SessionLocal, engine
from app.jobs import llm_order
from app.models import Incident, Job, utcnow
from app.noc import _forgeai_state, _llm_failed, _llm_queue, forgeai_leader


def _inc(db, title: str, minutes_ago: int, **kw) -> Incident:
    row = Incident(number=f"INC-AU-{uuid4().hex[:6]}", title=title, severity=kw.pop("severity", "CRITICAL"), status=kw.pop("status", "OPEN"),
                   fingerprint=f"au:{uuid4().hex}", started_at=utcnow() - timedelta(minutes=minutes_ago), **kw)
    db.add(row)
    db.commit()
    return row


def test_bursts_and_children_are_grouped():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tag = uuid4().hex[:6]
    first = _inc(db, f"Disk full {tag}", 20)
    again = _inc(db, f"Disk full {tag}", 10)
    later = _inc(db, f"Disk full {tag}", 0)
    child = _inc(db, f"Ping lost {tag}", 5, caused_by_id=first.id)
    lone = _inc(db, f"CPU high {tag}", 3)
    old = _inc(db, f"Fan {tag}", 60)
    new_same = _inc(db, f"Fan {tag}", 30)  # 30 min later: its own analysis
    try:
        assert forgeai_leader(db, first) is None and forgeai_leader(db, lone) is None
        assert forgeai_leader(db, again).id == first.id and forgeai_leader(db, later).id == first.id
        assert forgeai_leader(db, child).id == first.id
        assert forgeai_leader(db, new_same) is None and old.id
        assert _forgeai_state(again, {}, "local", set(), grouped=True) == "grouped"
    finally:
        for r in (child, later, again, first, lone, old, new_same):
            db.delete(r)
        db.commit()
        db.close()


def test_queue_order_failed_state_and_no_duplicate_work(monkeypatch):
    from app import services
    from app.settings import settings

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tag = uuid4().hex[:6]
    warn = _inc(db, f"Warn {tag}", 5, severity="WARNING")
    crit = _inc(db, f"Crit {tag}", 4)
    twin = _inc(db, f"Crit {tag}", 2)
    jobs = [Job(kind="investigate", object_id=warn.number, status="pending", payload={"use_llm": True, "severity": "WARNING"}),
            Job(kind="investigate", object_id=crit.number, status="pending", payload={"use_llm": True, "severity": "CRITICAL"}),
            Job(kind="forgeai_chat", object_id="chat-1", status="pending", payload={}),
            Job(kind="investigate", object_id=twin.number, status="error", payload={"use_llm": True}, created_at=utcnow())]
    db.add_all(jobs)
    db.commit()
    try:
        assert [j.kind for j in llm_order(jobs[:3])] == ["forgeai_chat", "investigate", "investigate"]
        assert llm_order(jobs[:2])[0].object_id == crit.number
        queue = _llm_queue(db)
        assert queue[crit.number] < queue[warn.number]
        assert twin.number in _llm_failed(db)
        assert _forgeai_state(twin, {}, "local", _llm_failed(db)) == "failed"
        # the system does not queue a second analysis for the same alarm within 15 minutes
        monkeypatch.setattr(type(settings), "ai_enabled", property(lambda self: True))
        monkeypatch.setattr(type(settings), "llm_url", property(lambda self: "http://127.0.0.1:8088/v1"))
        before = db.query(Job).filter(Job.object_id == twin.number).count()
        services.queue_llm_rewrite(db, twin)
        assert db.query(Job).filter(Job.object_id == twin.number).count() == before
        services.queue_llm_rewrite(db, warn, actor="ana@dc.local")  # pending job exists for warn: still one
    finally:
        for j in db.query(Job).filter(Job.object_id.in_([warn.number, crit.number, twin.number, "chat-1"])).all():
            db.delete(j)
        for r in (twin, crit, warn):
            db.delete(r)
        db.commit()
        db.close()


def test_forgerca_column_ignores_forgeai_answers():
    from app.models import Investigation
    from app.noc import _row

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    inc = _inc(db, f"Col {uuid4().hex[:6]}", 1)
    db.add_all([Investigation(incident_id=inc.id, provider="builtin-analyst", likely_cause="Rules: port down"),
                Investigation(incident_id=inc.id, provider="forgeai-llm", likely_cause="Model: port down")])
    db.commit()
    db.refresh(inc)
    try:
        row = _row(inc, {}, {}, "local", db)
        assert row["forgerca"] == "Rules: port down" and row["forgeai"] == "ready-local"
    finally:
        for i in list(inc.investigations):
            db.delete(i)
        db.delete(inc)
        db.commit()
        db.close()
