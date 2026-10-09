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
from app.models import Asset, AssetLadderStep, EscalationPolicy, Incident, Notification, Playrule
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
    assert ladder_steps_from_form("", [{"on": "1", "minutes": "0", "emails": ["a@dc.local"], "picks": []}]) is None


def test_add_asset_columns_nav_rules_and_escalated_cube():
    db = _db()
    client = _client()
    page = client.get("/assets")
    assert page.status_code == 200
    text = page.text
    order = [
        text.index("asset-form-left"),
        text.index("asset-form-middle"),
        text.index("asset-form-right"),
        text.index("asset-form-custom"),
        text.index("asset-form-ladder"),
    ]
    assert order == sorted(order)
    assert "Identity" in text and "Comms / monitoring" in text
    assert "Standard alarms" in text and "Custom alarms" in text
    assert ">Ladder " in text.split("data-asset-ladder", 1)[1]
    assert "Client playrules" not in text
    assert 'href="/escalation">Default ladder</a>' in text
    nav = text.split('<aside class="nav">', 1)[1].split("</aside>", 1)[0]
    assert ">Rules<" in nav and ">Playbooks<" in nav
    assert ">Playrules<" not in nav and ">Escalation<" not in nav
    assert ">Journal<" not in nav and 'href="/history"' not in nav
    assert 'href="/playrules"' in nav and 'href="/escalation"' not in nav
    rules = client.get("/playrules")
    assert rules.status_code == 200
    assert "<h1>Rules" in rules.text or ">Rules<" in rules.text.split("<h1>", 1)[1][:40]
    assert "<title>Rules · ForgeSRE</title>" in rules.text
    default = client.get("/escalation")
    assert default.status_code == 200
    assert "Default warning" in default.text
    assert "<h1>Default ladder" in default.text
    home = client.get("/")
    alarms = home.text.split("dash-tiles-incidents", 1)[1].split("</section>", 1)[0]
    assert 'data-tile="escalated"' in alarms
    assert 'href="/incidents?status=ESCALATED"' in alarms
    assert ">Open<" not in alarms
    db.close()


def test_ladder_persists_on_save_and_off_level_is_deleted():
    db = _db()
    client = _client()
    asset_id = f"lad-{uuid4().hex[:6]}"
    created = client.post(
        "/assets",
        data=_asset_form(
            asset_id,
            ladder_on_1="1",
            ladder_minutes_1="0",
            ladder_email_1="l1@dc.local",
            ladder_on_2="1",
            ladder_minutes_2="15",
            ladder_email_2=["l2a@dc.local", "l2b@dc.local"],
        ),
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    db.expire_all()
    asset = db.query(Asset).filter_by(asset_id=asset_id).one()
    rows = {row.level: row for row in db.query(AssetLadderStep).filter_by(asset_id=asset.id)}
    assert set(rows) == {1, 2}
    assert rows[1].after_minutes == 0 and rows[1].emails == ["l1@dc.local"]
    assert rows[2].after_minutes == 15 and rows[2].emails == ["l2a@dc.local", "l2b@dc.local"]
    edit = client.get(f"/assets?edit={asset_id}").text
    ladder = edit.split("data-asset-ladder", 1)[1].split("</form>", 1)[0]
    assert 'name="ladder_email_1"' in ladder and "l1@dc.local" in ladder
    assert ladder.count('name="ladder_email_2"') == 2
    updated = client.post(
        f"/assets/{asset_id}/update",
        data={
            "hostname": asset_id,
            "ip": asset.ip,
            "type": "Linux Server",
            "ladder_present": "1",
            "ladder_on_1": "1",
            "ladder_minutes_1": "5",
            "ladder_email_1": "l1b@dc.local",
        },
        follow_redirects=False,
    )
    assert updated.status_code in {302, 303}
    db.expire_all()
    rows = {row.level: row for row in db.query(AssetLadderStep).filter_by(asset_id=asset.id)}
    assert set(rows) == {1}
    assert rows[1].after_minutes == 5 and rows[1].emails == ["l1b@dc.local"]
    db.close()


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
