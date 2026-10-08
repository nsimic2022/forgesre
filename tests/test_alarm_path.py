"""Alarm path: group-aware resolve, step-0 address, ack vs automatic RCA, jobs after restart, webhooks off the loop."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func

import app.api as api_mod
import app.main as main_mod
from app.db import Base, SessionLocal, engine
from app.history import dashboard_incident_tiles, incident_query
from app.inventory import create_manual_asset
from app.jobs import active_discovery_scan, enqueue, recover_interrupted_jobs, run_pending_jobs
from app.main import app
from app.migrate import migrate
from app.models import (
    Asset,
    EscalationPolicy,
    Incident,
    IncidentEvent,
    Job,
    JournalEntry,
    Notification,
    Playrule,
    utcnow,
)
from app.seed import seed
from app.services import escalation_steps, ingest_alertmanager, process_escalations, run_investigation
from app.settings import settings

WEBHOOK = "/api/v1/webhooks/alertmanager"


@pytest.fixture(autouse=True)
def _drop_queued_jobs():
    """Ingest queues an RCA job per incident; later modules run run_pending_jobs(limit=8) on the shared queue."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    start = db.query(func.max(Job.id)).scalar() or 0
    db.close()
    yield
    db = SessionLocal()
    db.query(Job).filter(Job.id > start, Job.status == "pending").delete(synchronize_session=False)
    db.commit()
    db.close()


def _db():
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    return db


def _login() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _host(db, prefix: str, **extra) -> Asset:
    return create_manual_asset(
        db,
        hostname=f"{prefix}-{uuid4().hex[:6]}",
        ip="10.66.%d.%d" % (uuid4().int % 250 + 1, uuid4().int % 230 + 20),
        type=extra.pop("type", "Linux Server"),
        actor="tester",
        **extra,
    )


def _series(alertname: str, asset: str, status: str, **labels) -> dict:
    return {
        "status": status,
        "labels": {"alertname": alertname, "asset": asset, "severity": "critical", **labels},
        "annotations": {"summary": f"{alertname} on {asset}"},
    }


def _group(*alerts: dict) -> dict:
    status = "firing" if any(item["status"] == "firing" for item in alerts) else "resolved"
    return {"status": status, "groupLabels": {}, "alerts": list(alerts)}


def _active(db, fingerprint: str) -> list[Incident]:
    db.expire_all()
    return (
        db.query(Incident)
        .filter(Incident.fingerprint == fingerprint, Incident.status.in_(["OPEN", "INVESTIGATING", "ESCALATED"]))
        .all()
    )


def _mail_capture(monkeypatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr("app.settings.Settings.email_enabled", True)
    monkeypatch.setattr("app.settings.Settings.smtp_host", "smtp.example.test")
    monkeypatch.setattr("app.services._send_smtp", lambda target, *_a, **_k: sent.append(target))
    return sent


def _policy_rule(db, steps: list[dict]) -> str:
    slug = f"ap-{uuid4().hex[:8]}"
    policy = EscalationPolicy(name=slug, slug=slug, steps=steps)
    db.add(policy)
    db.flush()
    alertname = f"AlarmPath{uuid4().hex[:6]}"
    db.add(Playrule(name=slug, enabled=True, severity="warning", condition={"alertname": alertname}, escalation_policy_id=policy.id))
    db.commit()
    return alertname


def _notes(db, incident: Incident) -> dict[str, str]:
    db.expire_all()
    return {row.step_key: row.target for row in db.query(Notification).filter_by(incident_id=incident.id)}


# --- B1: group-aware resolve -------------------------------------------------


def test_one_port_recovering_keeps_switch_incident_open_in_either_order():
    db = _db()
    switch = _host(db, "ap-sw", type="Network device")
    name = "NetworkInterfaceDown"
    fingerprint = f"{name}:{switch.asset_id}"
    opened = ingest_alertmanager(
        db,
        _group(
            _series(name, switch.asset_id, "firing", ifName="ge-0/0/1"),
            _series(name, switch.asset_id, "firing", ifName="ge-0/0/2"),
        ),
    )
    assert len(opened) == 1
    incident = opened[0]
    assert incident.fingerprint == fingerprint

    firing_first = _group(
        _series(name, switch.asset_id, "firing", ifName="ge-0/0/1"),
        _series(name, switch.asset_id, "resolved", ifName="ge-0/0/2"),
    )
    resolved_first = _group(
        _series(name, switch.asset_id, "resolved", ifName="ge-0/0/2"),
        _series(name, switch.asset_id, "firing", ifName="ge-0/0/1"),
    )
    for payload in (firing_first, resolved_first, firing_first, resolved_first):
        assert ingest_alertmanager(db, payload) == []
        rows = _active(db, fingerprint)
        assert [row.id for row in rows] == [incident.id]
        assert rows[0].status == "OPEN"
        assert rows[0].ended_at is None
    db.refresh(incident)
    assert not any(item["id"] == "refire" for item in incident.timeline)
    assert db.query(IncidentEvent).filter_by(incident_id=incident.id, kind="resolved").count() == 0
    assert db.query(Incident).filter(Incident.fingerprint == fingerprint).count() == 1

    assert ingest_alertmanager(
        db,
        _group(
            _series(name, switch.asset_id, "resolved", ifName="ge-0/0/1"),
            _series(name, switch.asset_id, "resolved", ifName="ge-0/0/2"),
        ),
    ) == []
    db.expire_all()
    db.refresh(incident)
    assert incident.status == "RESOLVED"
    assert incident.ended_at is not None
    assert db.query(IncidentEvent).filter_by(incident_id=incident.id, kind="resolved").count() == 1
    db.close()


def test_first_payload_with_resolved_series_first_still_opens():
    db = _db()
    host = _host(db, "ap-fs")
    name = "NodeFilesystemUsageHigh"
    created = ingest_alertmanager(
        db,
        _group(
            _series(name, host.asset_id, "resolved", mountpoint="/var"),
            _series(name, host.asset_id, "firing", mountpoint="/"),
        ),
    )
    assert len(created) == 1
    db.refresh(created[0])
    assert created[0].status == "OPEN"
    assert created[0].ended_at is None
    db.close()


def test_single_series_still_opens_updates_and_resolves():
    db = _db()
    host = _host(db, "ap-one")
    name = f"ApSingle{uuid4().hex[:6]}"
    fingerprint = f"{name}:{host.asset_id}"
    first = ingest_alertmanager(db, _group(_series(name, host.asset_id, "firing")))
    assert len(first) == 1
    assert ingest_alertmanager(db, _group(_series(name, host.asset_id, "firing"))) == []
    assert [row.id for row in _active(db, fingerprint)] == [first[0].id]
    assert ingest_alertmanager(db, _group(_series(name, host.asset_id, "resolved"))) == []
    db.refresh(first[0])
    assert first[0].status == "RESOLVED"
    again = ingest_alertmanager(db, _group(_series(name, host.asset_id, "firing")))
    assert len(again) == 1 and again[0].id != first[0].id
    assert any(item["id"] == "refire" for item in again[0].timeline)
    db.close()


def test_zabbix_events_in_one_batch_stay_sequential():
    db = _db()
    host = f"zt-ap-{uuid4().hex[:6]}"
    fingerprint = f"zabbix:9{uuid4().int % 10**8}:{host}"

    def event(status: str) -> dict:
        return {
            "status": status,
            "labels": {"alertname": "zt CPU high", "asset": host, "severity": "HIGH", "source": "zabbix"},
            "annotations": {"summary": "zt CPU high"},
            "forge": {"fingerprint": fingerprint, "source": "zabbix"},
        }

    created = ingest_alertmanager(db, {"alerts": [event("firing"), event("resolved")]}, source="zabbix")
    assert len(created) == 1
    assert created[0].fingerprint == fingerprint
    db.expire_all()
    row = db.get(Incident, created[0].id)
    assert row.status == "RESOLVED"
    db.query(Notification).filter(Notification.incident_id == row.id).delete(synchronize_session=False)
    db.query(Job).filter(Job.object_id == row.number).delete(synchronize_session=False)
    db.delete(row)
    db.commit()
    db.close()


# --- B2: step-0 address ----------------------------------------------------------


def test_policy_step0_address_is_the_open_mail(monkeypatch):
    db = _db()
    sent = _mail_capture(monkeypatch)
    alertname = _policy_rule(db, [{"after_minutes": 0, "target": "noc@dc.local"}, {"after_minutes": 15, "target": "team-lead"}])
    owned = _host(db, "ap-noc", owner_email="owner@dc.local")
    incident = ingest_alertmanager(db, _group(_series(alertname, owned.asset_id, "firing")))[0]
    assert _notes(db, incident) == {"immediate": "noc@dc.local"}
    assert sent == ["noc@dc.local"]
    row = db.query(Notification).filter_by(incident_id=incident.id, step_key="immediate").one()
    assert row.status == "sent"

    nobody = _host(db, "ap-noc-none")
    other = ingest_alertmanager(db, _group(_series(alertname, nobody.asset_id, "firing")))[0]
    row = db.query(Notification).filter_by(incident_id=other.id, step_key="immediate").one()
    assert row.target == "noc@dc.local" and row.status == "sent"
    assert sent == ["noc@dc.local", "noc@dc.local"]

    incident.started_at = utcnow() - timedelta(minutes=1)
    db.commit()
    process_escalations(db)
    assert _notes(db, incident) == {"immediate": "noc@dc.local"}
    assert sent.count("noc@dc.local") == 2
    db.close()


def test_policy_without_zero_step_sends_nothing_at_open(monkeypatch):
    db = _db()
    sent = _mail_capture(monkeypatch)
    alertname = _policy_rule(db, [{"after_minutes": 10, "target": "late@dc.local"}])
    host = _host(db, "ap-late", owner_email="late-owner@dc.local")
    incident = ingest_alertmanager(db, _group(_series(alertname, host.asset_id, "firing")))[0]
    assert _notes(db, incident) == {}
    assert sent == []
    process_escalations(db)
    assert _notes(db, incident) == {}
    incident.started_at = utcnow() - timedelta(minutes=11)
    db.commit()
    process_escalations(db)
    assert _notes(db, incident) == {"10m": "late@dc.local"}
    assert sent.count("late@dc.local") == 1
    assert "late-owner@dc.local" not in sent
    db.close()


def test_two_steps_at_the_same_minute_both_mail(monkeypatch):
    db = _db()
    sent = _mail_capture(monkeypatch)
    alertname = _policy_rule(
        db,
        [
            {"after_minutes": 0, "target": "team"},
            {"after_minutes": 5, "target": "a@dc.local"},
            {"after_minutes": 5, "target": "b@dc.local"},
        ],
    )
    host = _host(db, "ap-pair", owner_email="pair-day@dc.local")
    incident = ingest_alertmanager(db, _group(_series(alertname, host.asset_id, "firing")))[0]
    assert [step["step_key"] for step in escalation_steps(incident)] == ["immediate", "5m", "5m-2"]
    incident.started_at = utcnow() - timedelta(minutes=6)
    db.commit()
    process_escalations(db)
    process_escalations(db)
    assert _notes(db, incident) == {"immediate": "pair-day@dc.local", "5m": "a@dc.local", "5m-2": "b@dc.local"}
    mine = {"a@dc.local", "b@dc.local", "pair-day@dc.local"}
    assert sorted(item for item in sent if item in mine) == ["a@dc.local", "b@dc.local", "pair-day@dc.local"]
    db.close()


def test_default_ladder_without_playrule_still_mails_owner_at_open(monkeypatch):
    db = _db()
    sent = _mail_capture(monkeypatch)
    host = _host(db, "ap-def", owner_email="def@dc.local")
    incident = ingest_alertmanager(db, _group(_series(f"ApNoRule{uuid4().hex[:6]}", host.asset_id, "firing")))[0]
    assert incident.playrule_id is None
    assert _notes(db, incident) == {"immediate": "def@dc.local"}
    assert sent == ["def@dc.local"]
    db.close()


def test_existing_role_step0_then_address_step5_ladder_unchanged(monkeypatch):
    db = _db()
    sent = _mail_capture(monkeypatch)
    alertname = _policy_rule(db, [{"after_minutes": 0, "target": "team"}, {"after_minutes": 5, "target": "night5@dc.local"}])
    host = _host(db, "ap-role", owner_email="role-day@dc.local")
    incident = ingest_alertmanager(db, _group(_series(alertname, host.asset_id, "firing")))[0]
    assert _notes(db, incident) == {"immediate": "role-day@dc.local"}
    incident.started_at = utcnow() - timedelta(minutes=6)
    db.commit()
    process_escalations(db)
    assert _notes(db, incident) == {"immediate": "role-day@dc.local", "5m": "night5@dc.local"}
    assert [item for item in sent if item in {"role-day@dc.local", "night5@dc.local"}] == ["role-day@dc.local", "night5@dc.local"]
    db.close()


# --- B3: ack vs automatic RCA -----------------------------------------------------


def _unacked_ids(db) -> set[int]:
    return {row.id for row in incident_query(db, unacked_only=True).all()}


def _open_tile(db) -> int:
    return next(tile["count"] for tile in dashboard_incident_tiles(db) if tile["key"] == "open")


def test_automatic_rca_is_not_an_ack_and_escalation_keeps_going():
    db = _db()
    host = _host(db, "ap-rca", owner_email="rca@dc.local")
    incident = ingest_alertmanager(db, _group(_series(f"ApRca{uuid4().hex[:6]}", host.asset_id, "firing")))[0]
    job = db.query(Job).filter_by(kind="investigate", object_id=incident.number).one()
    assert job.status == "pending" and (job.payload or {}).get("actor") == "system"
    tile_before = _open_tile(db)
    run_investigation(db, incident, actor="system", use_llm=False)
    db.expire_all()
    incident = db.get(Incident, incident.id)
    assert incident.investigations
    assert incident.status == "OPEN"
    assert incident.ack_at is None and not incident.ack_by
    assert incident.id in _unacked_ids(db)
    assert _open_tile(db) == tile_before

    client = _login()
    page = client.get(f"/incidents/{incident.number}")
    assert 'value="INVESTIGATING" class="todo">Acknowledge' in page.text
    listed = client.get("/incidents?status=unacked&per_page=100")
    assert listed.status_code == 200
    assert f"/incidents/{incident.number}?status=unacked" in listed.text

    incident.started_at = utcnow() - timedelta(minutes=16)
    db.commit()
    process_escalations(db)
    db.expire_all()
    incident = db.get(Incident, incident.id)
    assert set(_notes(db, incident)) == {"immediate", "15m"}
    assert incident.status == "ESCALATED"
    assert incident.id in _unacked_ids(db)
    db.close()


def test_human_acknowledge_sets_ack_at_and_stops_the_ladder():
    db = _db()
    host = _host(db, "ap-ack", owner_email="ack@dc.local")
    incident = ingest_alertmanager(db, _group(_series(f"ApAck{uuid4().hex[:6]}", host.asset_id, "firing")))[0]
    run_investigation(db, incident, actor="system", use_llm=False)
    tile_before = _open_tile(db)
    client = _login()
    posted = client.post(f"/incidents/{incident.number}/status", data={"status": "INVESTIGATING"}, follow_redirects=False)
    assert posted.status_code == 302
    db.expire_all()
    incident = db.get(Incident, incident.id)
    assert incident.status == "INVESTIGATING"
    assert incident.ack_at is not None and incident.ack_by == "admin@forgesre.local"
    assert incident.id not in _unacked_ids(db)
    assert _open_tile(db) == tile_before - 1
    page = client.get(f"/incidents/{incident.number}")
    assert 'value="INVESTIGATING" class="done">Acknowledge' in page.text

    incident.started_at = utcnow() - timedelta(minutes=40)
    db.commit()
    process_escalations(db)
    assert set(_notes(db, incident)) == {"immediate"}
    db.close()


def test_acknowledge_on_escalated_keeps_escalated():
    db = _db()
    host = _host(db, "ap-esc", owner_email="esc@dc.local")
    incident = ingest_alertmanager(db, _group(_series(f"ApEsc{uuid4().hex[:6]}", host.asset_id, "firing")))[0]
    incident.started_at = utcnow() - timedelta(minutes=16)
    db.commit()
    process_escalations(db)
    db.expire_all()
    incident = db.get(Incident, incident.id)
    assert incident.status == "ESCALATED" and incident.ack_at is None
    client = _login()
    assert 'value="INVESTIGATING" class="todo">Acknowledge' in client.get(f"/incidents/{incident.number}").text
    client.post(f"/incidents/{incident.number}/status", data={"status": "INVESTIGATING"}, follow_redirects=False)
    db.expire_all()
    incident = db.get(Incident, incident.id)
    assert incident.status == "ESCALATED"
    assert incident.ack_at is not None and incident.ack_by == "admin@forgesre.local"
    assert incident.id not in _unacked_ids(db)
    assert 'value="INVESTIGATING" class="done">Acknowledge' in client.get(f"/incidents/{incident.number}").text
    db.close()


def test_acknowledge_on_resolved_does_not_reopen():
    db = _db()
    host = _host(db, "ap-res")
    name = f"ApRes{uuid4().hex[:6]}"
    incident = ingest_alertmanager(db, _group(_series(name, host.asset_id, "firing")))[0]
    ingest_alertmanager(db, _group(_series(name, host.asset_id, "resolved")))
    client = _login()
    client.post(f"/incidents/{incident.number}/status", data={"status": "INVESTIGATING"}, follow_redirects=False)
    db.expire_all()
    incident = db.get(Incident, incident.id)
    assert incident.status == "RESOLVED"
    assert incident.ack_at is not None
    db.close()


def test_open_tile_counts_every_unacked_active_status():
    db = _db()
    from app.services import next_incident_number

    token = uuid4().hex[:6]
    rows = {}
    for key, status, acked in [
        ("open", "OPEN", False),
        ("inv", "INVESTIGATING", False),
        ("esc", "ESCALATED", False),
        ("inv-acked", "INVESTIGATING", True),
        ("res", "RESOLVED", False),
    ]:
        row = Incident(
            number=next_incident_number(db),
            title=f"ap tile {key} {token}",
            severity="WARNING",
            status=status,
            fingerprint=f"ap-tile:{token}:{key}",
            ack_at=utcnow() if acked else None,
            ack_by="ana@dc.local" if acked else "",
        )
        db.add(row)
        db.flush()
        rows[key] = row.id
    db.commit()
    unacked = _unacked_ids(db)
    assert {rows["open"], rows["inv"], rows["esc"]} <= unacked
    assert rows["inv-acked"] not in unacked and rows["res"] not in unacked
    assert _open_tile(db) == len(unacked)
    client = _login()
    home = client.get("/")
    assert 'href="/incidents?status=unacked" data-tile="open"' in home.text
    listed = client.get("/incidents?status=unacked&per_page=100").text
    assert f"ap tile inv {token}" in listed
    assert f"ap tile inv-acked {token}" not in listed
    assert '<option value="unacked" selected>Not acknowledged</option>' in listed
    db.close()


# --- B4: jobs after Core restart ---------------------------------------------------


def _running(db, kind: str, object_id: str, *, attempts: int = 1, payload: dict | None = None) -> Job:
    row = Job(kind=kind, status="running", object_type="incident", object_id=object_id, payload=payload or {}, attempts=attempts, started_at=utcnow())
    db.add(row)
    db.commit()
    return row


def test_recover_interrupted_jobs_requeues_or_fails_running_rows():
    from app.web import llm_job_pending

    db = _db()
    number = f"INC-AP-{uuid4().hex[:6]}"
    llm = _running(db, "investigate", number, payload={"actor": "system", "force": True, "use_llm": True})
    assert llm_job_pending(db, number)
    assert enqueue(db, "investigate", number).id == llm.id
    tired = _running(db, "investigate", f"INC-AP-{uuid4().hex[:6]}", attempts=3)
    odd_id = f"x-{uuid4().hex[:6]}"
    odd = _running(db, "export", odd_id)

    result = recover_interrupted_jobs(db)
    assert result["requeued"] >= 1 and result["failed"] >= 2
    db.expire_all()
    assert db.query(Job).filter(Job.status == "running").count() == 0
    llm = db.get(Job, llm.id)
    assert llm.status == "pending" and "interrupted by Core restart" in llm.error
    assert llm.started_at is None
    for row_id in (tired.id, odd.id):
        row = db.get(Job, row_id)
        assert row.status == "error" and "interrupted by Core restart" in row.error
        assert row.finished_at is not None
    again = enqueue(db, "export", odd_id, object_type="incident")
    assert again is not None and again.id != odd.id and again.status == "pending"
    recovered = db.query(JournalEntry).filter_by(module="jobs", action="recover").order_by(JournalEntry.id.desc()).first()
    assert recovered is not None and recovered.status == "error"
    assert f"#{llm.id} investigate {number} → pending" in recovered.detail
    db.query(Job).filter(Job.id.in_([llm.id, tired.id, odd.id, again.id])).delete(synchronize_session=False)
    db.query(JournalEntry).filter_by(module="jobs", action="recover").delete(synchronize_session=False)
    db.commit()
    assert recover_interrupted_jobs(db) == {"requeued": 0, "failed": 0}
    db.close()


def test_requeued_rca_job_runs_and_rerun_can_queue():
    from app.web import llm_job_pending

    db = _db()
    host = _host(db, "ap-job")
    incident = ingest_alertmanager(db, _group(_series(f"ApJob{uuid4().hex[:6]}", host.asset_id, "firing")))[0]
    job = db.query(Job).filter_by(kind="investigate", object_id=incident.number).one()
    job.status = "running"
    job.attempts = 1
    db.commit()
    assert llm_job_pending(db, incident.number)
    recover_interrupted_jobs(db)
    db.expire_all()
    assert db.get(Job, job.id).status == "pending"
    others = db.query(Job).filter(Job.status == "pending", Job.id != job.id).all()
    for row in others:
        row.status = "parked"
    db.commit()
    try:
        assert run_pending_jobs(db, limit=1) == 1
        db.expire_all()
        assert db.get(Job, job.id).status == "done"
        assert not llm_job_pending(db, incident.number)
        fresh = enqueue(db, "investigate", incident.number, payload={"actor": "tester", "force": True, "use_llm": False})
        assert fresh.id != job.id and fresh.status == "pending"
        db.delete(fresh)
    finally:
        for row in others:
            row.status = "pending"
        db.query(JournalEntry).filter_by(module="jobs", action="recover").delete(synchronize_session=False)
        db.commit()
    db.close()


def test_core_lifespan_resets_running_jobs(monkeypatch):
    db = _db()
    scan = active_discovery_scan(db)
    if scan is not None:
        scan.status = "done"
        db.commit()
    stuck = _running(db, "discovery_scan", "scan", payload={"actor": "tester", "cidrs": []})
    for loop in ("_escalation_loop", "_jobs_loop", "_discovery_loop"):
        monkeypatch.setattr(main_mod, loop, lambda stop: None)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}
    db.expire_all()
    row = db.get(Job, stuck.id)
    assert row.status == "pending" and "interrupted by Core restart" in row.error
    assert active_discovery_scan(db).id == stuck.id
    row.status = "done"
    db.query(JournalEntry).filter_by(module="jobs", action="recover").delete(synchronize_session=False)
    db.commit()
    db.close()


# --- B5: webhooks off the event loop ---------------------------------------------------


def _zabbix_token(monkeypatch, token: str = "ap-zbx-token") -> str:
    monkeypatch.setattr(type(settings), "zabbix_webhook_token", property(lambda self: token))
    return token


def test_webhook_ingest_runs_in_a_worker_thread(monkeypatch):
    seen: list[tuple[str, str]] = []

    def fake_ingest(db, payload, *, source="prometheus"):
        try:
            asyncio.get_running_loop()
            where = "event-loop"
        except RuntimeError:
            where = "worker"
        seen.append((source, where))
        return []

    monkeypatch.setattr(api_mod, "ingest_alertmanager", fake_ingest)
    token = _zabbix_token(monkeypatch)
    client = TestClient(app)
    am = client.post(
        WEBHOOK,
        json=_group(_series("ApThread", "nowhere", "firing")),
        headers={"Authorization": f"Bearer {settings.webhook_token}"},
    )
    assert am.status_code == 200 and am.json() == {"accepted": True, "incidents": []}
    zbx = client.post(
        "/api/v1/webhooks/zabbix",
        json={"trigger_name": "ap thread", "host": "nowhere", "event_value": "1", "trigger_id": "4242"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert zbx.status_code == 200 and zbx.json()["source"] == "zabbix"
    assert seen == [("prometheus", "worker"), ("zabbix", "worker")]


def test_webhook_auth_still_checked_before_ingest(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(api_mod, "ingest_alertmanager", lambda *a, **k: calls.append("x") or [])
    _zabbix_token(monkeypatch)
    client = TestClient(app)
    assert client.post(WEBHOOK, json={"alerts": []}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/v1/webhooks/zabbix", json={"host": "h"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert calls == []


def test_alertmanager_token_is_compared_in_constant_time(monkeypatch):
    seen: list[tuple[bytes, bytes]] = []
    real = api_mod.hmac.compare_digest

    def recording(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(api_mod.hmac, "compare_digest", recording)
    monkeypatch.setattr(api_mod, "ingest_alertmanager", lambda *a, **k: [])
    client = TestClient(app)
    good = {"Authorization": f"Bearer {settings.webhook_token}"}
    bad = {"Authorization": "Bearer nope"}
    assert client.post(WEBHOOK, json={"alerts": []}, headers=bad).status_code == 401
    assert client.post(WEBHOOK, json={"alerts": []}, headers=good).status_code == 200
    assert client.get("/api/v1/sd/prometheus", headers=bad).status_code == 401
    assert client.get("/api/v1/sd/prometheus", headers=good).status_code == 200
    assert (b"nope", settings.webhook_token.encode()) in seen
    assert (settings.webhook_token.encode(), settings.webhook_token.encode()) in seen


def test_slow_webhook_ingest_does_not_stall_health(monkeypatch):
    def slow_ingest(db, payload, *, source="prometheus"):
        time.sleep(1.5)
        return []

    monkeypatch.setattr(api_mod, "ingest_alertmanager", slow_ingest)

    async def scenario() -> tuple[float, bool, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://core") as client:
            hook = asyncio.create_task(
                client.post(
                    WEBHOOK,
                    json=_group(_series("ApSlow", "nowhere", "firing")),
                    headers={"Authorization": f"Bearer {settings.webhook_token}"},
                )
            )
            await asyncio.sleep(0.3)
            started = time.monotonic()
            health = await client.get("/api/v1/health")
            took = time.monotonic() - started
            assert health.json() == {"status": "ok"}
            still_ingesting = not hook.done()
            response = await hook
            return took, still_ingesting, response.status_code

    took, still_ingesting, status = asyncio.run(scenario())
    assert status == 200
    assert still_ingesting, "health answered only after the webhook finished"
    assert took < 0.75
