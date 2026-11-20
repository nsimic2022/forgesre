"""Per-asset Ladder: form columns, nav, Escalated cube, persist, mail vs Default, ack, Zabbix lookup."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.asset_ladder import ladder_steps_from_form, replace_asset_ladder
from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.main import app
from app.migrate import migrate
from app.models import EscalationPolicy, Incident, Notification, Playrule
from app.seed import seed
from app.services import ingest_alertmanager, process_escalations, utcnow


def _db():
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _ip() -> str:
    raw = uuid4().int
    return f"10.{180 + raw % 20}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


def _asset_form(asset_id: str, **extra) -> dict:
    data = {
        "asset_id": asset_id,
        "hostname": asset_id,
        "ip": _ip(),
        "type": "Linux Server",
        "ladder_present": "1",
        "playrules_present": "1",
        "alarms_present": "1",
        "comms_present": "1",
        "extras_present": "1",
    }
    data.update(extra)
    return data


def _notes(db, incident: Incident) -> dict[str, str]:
    db.expire_all()
    return {row.step_key: row.target for row in db.query(Notification).filter_by(incident_id=incident.id)}


def _mail(monkeypatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr("app.settings.Settings.email_enabled", True)
    monkeypatch.setattr("app.settings.Settings.smtp_host", "smtp.example.test")
    monkeypatch.setattr("app.services._send_smtp", lambda target, *_a, **_k: sent.append(target))
    return sent


def _fire(db, asset_id: str, alertname: str, **labels):
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": alertname, "asset": asset_id, "severity": "warning", **labels},
                "annotations": {"summary": alertname},
            }
        ],
    }
    return ingest_alertmanager(db, payload)[0]


def test_ladder_form_parser_keeps_on_levels_and_drops_off_or_blank():
    steps = ladder_steps_from_form(
        "1",
        [
            {"on": "1", "minutes": "0", "emails": ["", "noc@dc.local"], "picks": ["", ""]},
            {"on": "", "minutes": "15", "emails": ["shift@dc.local"], "picks": []},
            {"on": "1", "minutes": "", "emails": ["eng@dc.local", "eng@dc.local"], "picks": ["lead@dc.local"]},
            {"on": "1", "minutes": "60", "emails": ["not-an-email"], "picks": []},
        ],
    )
    assert steps == [
        {"level": 1, "after_minutes": 0, "emails": ["noc@dc.local"], "enabled": True},
        {"level": 3, "after_minutes": 0, "emails": ["lead@dc.local", "eng@dc.local"], "enabled": True},
    ]
    roles = ladder_steps_from_form(
        "1",
        [
            {"on": "1", "minutes": "0", "target": "team"},
            {"on": "1", "minutes": "15", "target": "l2@dc.local"},
            {"on": "", "minutes": "30", "target": "engineer"},
            {"on": "1", "minutes": "60", "target": "not-a-role"},
        ],
    )
    assert roles == [
        {"level": 1, "after_minutes": 0, "emails": ["team"], "enabled": True},
        {"level": 2, "after_minutes": 15, "emails": ["l2@dc.local"], "enabled": True},
    ]
    assert ladder_steps_from_form("", [{"on": "1", "minutes": "0", "emails": ["a@dc.local"], "picks": []}]) is None


def test_asset_ladder_mail_beats_default_and_ack_stops_and_off_is_honored(monkeypatch):
    db = _db()
    sent = _mail(monkeypatch)
    alertname = f"Lad{uuid4().hex[:6]}"
    policy = EscalationPolicy(
        name=alertname,
        slug=alertname.lower(),
        steps=[
            {"after_minutes": 0, "target": "policy@dc.local", "channel": "email"},
            {"after_minutes": 15, "target": "later-policy@dc.local", "channel": "email"},
        ],
    )
    db.add(policy)
    db.flush()
    db.add(
        Playrule(
            name=alertname,
            enabled=True,
            severity="warning",
            condition={"alertname": alertname},
            escalation_policy_id=policy.id,
        )
    )
    db.commit()
    plain = create_manual_asset(db, hostname=f"plain-{uuid4().hex[:4]}", ip=_ip(), type="Linux Server", owner_email="owner@dc.local", asset_id=f"plain-{uuid4().hex[:6]}")
    owned = create_manual_asset(db, hostname=f"own-{uuid4().hex[:4]}", ip=_ip(), type="Linux Server", owner_email="owner@dc.local", asset_id=f"own-{uuid4().hex[:6]}")
    replace_asset_ladder(
        db,
        owned,
        [
            {"level": 1, "after_minutes": 0, "emails": ["l1@dc.local"], "enabled": True},
            {"level": 2, "after_minutes": 10, "emails": ["l2@dc.local"], "enabled": True},
        ],
    )
    db.commit()

    plain_inc = _fire(db, plain.asset_id, alertname)
    assert _notes(db, plain_inc)["immediate"] == "policy@dc.local"
    assert "owner@dc.local" not in sent
    assert "l1@dc.local" not in sent

    owned_inc = _fire(db, owned.asset_id, alertname)
    assert _notes(db, owned_inc) == {"immediate": "l1@dc.local"}
    assert owned_inc.status == "OPEN"
    assert sent.count("l1@dc.local") == 1
    assert sent.count("policy@dc.local") == 1
    assert "owner@dc.local" not in sent

    owned_inc.started_at = utcnow() - timedelta(minutes=20)
    db.commit()
    process_escalations(db)
    assert _notes(db, owned_inc)["10m"] == "l2@dc.local"
    assert "later-policy@dc.local" not in sent
    db.expire_all()
    owned_inc = db.get(Incident, owned_inc.id)
    assert owned_inc.status == "ESCALATED"

    acked = create_manual_asset(db, hostname=f"ack-{uuid4().hex[:4]}", ip=_ip(), type="Linux Server", owner_email="ack-owner@dc.local", asset_id=f"ack-{uuid4().hex[:6]}")
    replace_asset_ladder(
        db,
        acked,
        [
            {"level": 1, "after_minutes": 0, "emails": ["ack-l1@dc.local"], "enabled": True},
            {"level": 2, "after_minutes": 10, "emails": ["ack-l2@dc.local"], "enabled": True},
        ],
    )
    db.commit()
    ack_inc = _fire(db, acked.asset_id, alertname)
    ack_inc.ack_at = utcnow()
    ack_inc.started_at = utcnow() - timedelta(minutes=30)
    db.commit()
    process_escalations(db)
    assert _notes(db, ack_inc) == {"immediate": "ack-l1@dc.local"}
    assert "ack-l2@dc.local" not in sent

    replace_asset_ladder(db, owned, [{"level": 1, "after_minutes": 0, "emails": ["only@dc.local"], "enabled": True}])
    db.commit()
    again = _fire(db, owned.asset_id, alertname + "x")
    assert _notes(db, again) == {"immediate": "only@dc.local"}
    again.started_at = utcnow() - timedelta(minutes=30)
    db.commit()
    process_escalations(db)
    assert "l2@dc.local" not in _notes(db, again).values()
    assert set(_notes(db, again).values()) == {"only@dc.local"}
    db.close()


def test_zabbix_ingest_uses_the_same_asset_ladder(monkeypatch):
    db = _db()
    sent = _mail(monkeypatch)
    host = create_manual_asset(
        db,
        hostname=f"zbx-{uuid4().hex[:4]}",
        ip=_ip(),
        type="Linux Server",
        owner_email="zbx-owner@dc.local",
        asset_id=f"zbx-{uuid4().hex[:6]}",
    )
    host.zabbix_hostid = "424242"
    replace_asset_ladder(db, host, [{"level": 1, "after_minutes": 0, "emails": ["zbx-l1@dc.local"], "enabled": True}])
    db.commit()
    fingerprint = f"zabbix:77:{uuid4().hex[:6]}"
    created = ingest_alertmanager(
        db,
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "Zabbix CPU",
                        "severity": "warning",
                        "asset": "not-the-asset-id",
                        "zabbix_hostid": "424242",
                    },
                    "annotations": {"summary": "Zabbix CPU"},
                    "forge": {"fingerprint": fingerprint, "source": "zabbix"},
                }
            ],
        },
        source="zabbix",
    )
    assert len(created) == 1
    assert created[0].asset_id == host.id
    assert created[0].source == "zabbix"
    assert _notes(db, created[0]) == {"immediate": "zbx-l1@dc.local"}
    assert sent == ["zbx-l1@dc.local"]
    assert "zbx-owner@dc.local" not in sent
    db.close()
