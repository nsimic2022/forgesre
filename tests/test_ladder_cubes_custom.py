"""Add/Edit column widths, Custom alarm ON/OFF, Ladder cubes on Assets and Incidents."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.asset_alarms import bundled_alert_skip_reason
from app.db import Base, SessionLocal, engine
from app.ladder_cubes import asset_ladder_cubes, incident_ladder_cubes
from app.main import app
from app.models import Asset, AssetPlayruleMute, Incident, Playrule
from app.seed import seed
from app.services import ingest_alertmanager, match_playrule, process_escalations, utcnow

ROOT = Path(__file__).resolve().parents[1]
CUBE_RE = re.compile(
    r'data-ladder-level="(\d)" data-ladder-tone="(green|red|grey)" title="([^"]*)"'
)


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _ip() -> str:
    raw = uuid4().int
    return f"10.{200 + raw % 40}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


def _rule(db, name: str, alertname: str) -> Playrule:
    rule = Playrule(name=name, enabled=True, severity="warning", condition={"alertname": alertname})
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def _alert(alertname: str, asset_id: str) -> dict:
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": alertname, "asset": asset_id, "severity": "warning"},
                "annotations": {"summary": alertname},
            }
        ],
    }


def _cubes(fragment: str) -> list[tuple[str, str, str]]:
    block = fragment.split('data-ladder-cubes', 1)[1]
    return CUBE_RE.findall(block)


def test_add_asset_column_widths_fill_the_page():
    _db().close()
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    block = css.split("/* Add / Edit asset:", 1)[1].split(".asset-form-block {", 1)[0]
    split = block.split(".asset-form-split {", 1)[1].split("}", 1)[0]
    left = block.split(".asset-form-half-left", 1)[1].split("}", 1)[0]
    right = block.split(".asset-form-half-right", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);" in split
    assert "width: 100%;" in split and "max-width: none;" in split
    assert "grid-template-columns: minmax(0, 1.7fr) minmax(0, 1fr);" in left
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 2fr) minmax(0, 1.65fr);" in right
    chip = css.split(".playrule-chip-text", 1)[1].split("}", 1)[0]
    assert "ellipsis" not in chip
    page = _client().get("/assets")
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
    assert "asset-form-split" not in _client().get("/").text
    standard = text.split("alarm-families", 1)[1].split("asset-form-custom", 1)[0]
    assert 'name="alarm_cpu_enabled"' in standard and 'name="alarm_disk_threshold"' in standard
    assert "playrule_on" not in standard


def test_custom_alarm_off_persists_and_does_not_attach():
    db = _db()
    token = uuid4().hex[:6]
    off_name = f"OffAlert{token}VeryLongRuleName"
    on_name = f"OnAlert{token}"
    off_rule = _rule(db, f"cube-off-{token}", off_name)
    on_rule = _rule(db, f"cube-on-{token}", on_name)
    global_same = _rule(db, f"cube-global-{token}", off_name)
    client = _client()
    asset_id = f"cube-a-{token}"
    ip = _ip()
    created = client.post(
        "/assets",
        data={
            "asset_id": asset_id,
            "hostname": asset_id,
            "ip": ip,
            "type": "Linux Server",
            "owner_email": "owner@dc.local",
            "alarms_present": "1",
            "alarm_up_enabled": "1",
            "alarm_cpu_enabled": "1",
            "alarm_cpu_threshold": "90",
            "alarm_memory_enabled": "1",
            "alarm_memory_threshold": "90",
            "alarm_disk_enabled": "1",
            "alarm_disk_threshold": "90",
            "playrules_present": "1",
            "playrule_switches": "1",
            "playrule_ids": [str(off_rule.id), str(on_rule.id)],
            "playrule_on": [str(off_rule.id), str(on_rule.id)],
        },
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    db.expire_all()
    asset = db.query(Asset).filter_by(asset_id=asset_id).one()
    alarms = dict(asset.alarms or {})
    assert alarms["cpu_percent"]["enabled"] is True
    assert alarms["cpu_percent"]["threshold"] == 90
    assert match_playrule(db, off_name, asset=asset).id == off_rule.id

    saved = client.post(
        f"/assets/{asset_id}/update",
        data={
            "hostname": asset_id,
            "ip": ip,
            "type": "Linux Server",
            "playrules_present": "1",
            "playrule_switches": "1",
            "playrule_ids": [str(off_rule.id), str(on_rule.id)],
            "playrule_on": [str(on_rule.id)],
        },
        follow_redirects=False,
    )
    assert saved.status_code in {302, 303}
    db.expire_all()
    asset = db.query(Asset).filter_by(asset_id=asset_id).one()
    assert asset.playrule_ids == [off_rule.id, on_rule.id]
    assert dict(asset.alarms or {}) == alarms
    assert bundled_alert_skip_reason(asset, "NodeCPUHigh") == ""
    muted = [row.playrule_id for row in db.query(AssetPlayruleMute).filter_by(asset_id=asset.id)]
    assert muted == [off_rule.id]
    assert match_playrule(db, off_name, asset=asset).id == global_same.id
    assert match_playrule(db, on_name, asset=asset).id == on_rule.id

    edit = client.get(f"/assets?edit={asset_id}").text
    chips = edit.split("data-playrule-list", 1)[1].split("</ol>", 1)[0]
    off_chip = chips.split(f'data-playrule-id="{off_rule.id}"', 1)[1].split("</li>", 1)[0]
    on_chip = chips.split(f'data-playrule-id="{on_rule.id}"', 1)[1].split("</li>", 1)[0]
    assert "is-off" in off_chip and ">OFF<" in off_chip
    assert "checked" not in off_chip.split("playrule-onoff", 1)[1].split("</div>", 1)[0]
    assert "is-on" in on_chip and "checked" in on_chip
    assert off_name in off_chip and on_name in on_chip

    missed = ingest_alertmanager(db, _alert(off_name, asset_id))[0]
    assert missed.playrule_id == global_same.id
    attached = ingest_alertmanager(db, _alert(on_name, asset_id))[0]
    assert attached.playrule_id == on_rule.id
    assert any(item.get("detail", "").endswith("(custom alarm)") for item in attached.timeline)

    only_id = f"cube-only-{token}"
    client.post(
        "/assets",
        data={
            "asset_id": only_id,
            "hostname": only_id,
            "ip": _ip(),
            "type": "Linux Server",
            "playrules_present": "1",
            "playrule_switches": "1",
            "playrule_ids": [str(on_rule.id)],
            "playrule_on": [],
        },
        follow_redirects=False,
    )
    db.expire_all()
    only = db.query(Asset).filter_by(asset_id=only_id).one()
    assert match_playrule(db, on_name, asset=only) is None
    quiet = ingest_alertmanager(db, _alert(on_name, only_id))[0]
    assert quiet.playrule_id is None

    turned = client.post(
        f"/assets/{only_id}/update",
        data={
            "hostname": only_id,
            "ip": only.ip,
            "type": "Linux Server",
            "playrules_present": "1",
            "playrule_switches": "1",
            "playrule_ids": [str(on_rule.id)],
            "playrule_on": [str(on_rule.id)],
            "alarms_present": "1",
            "alarm_cpu_enabled": "",
            "alarm_cpu_threshold": "90",
            "alarm_memory_enabled": "1",
            "alarm_memory_threshold": "90",
            "alarm_disk_enabled": "1",
            "alarm_disk_threshold": "90",
        },
        follow_redirects=False,
    )
    assert turned.status_code in {302, 303}
    db.expire_all()
    only = db.query(Asset).filter_by(asset_id=only_id).one()
    assert db.query(AssetPlayruleMute).filter_by(asset_id=only.id).count() == 0
    assert only.alarms["cpu_percent"]["enabled"] is False
    assert "alarm disabled" in bundled_alert_skip_reason(only, "NodeCPUHigh")
    quiet.status = "RESOLVED"
    db.commit()
    again = ingest_alertmanager(db, _alert(on_name, only_id))[0]
    assert again.playrule_id == on_rule.id
    db.close()


def test_asset_list_and_incident_ladder_cubes_after_a_fired_step():
    db = _db()
    client = _client()
    token = uuid4().hex[:6]
    alertname = f"LadCube{token}"
    rule = _rule(db, f"cube-lad-{token}", alertname)
    asset_id = f"cube-l-{token}"
    ip = _ip()
    created = client.post(
        "/assets",
        data={
            "asset_id": asset_id,
            "hostname": asset_id,
            "ip": ip,
            "type": "Linux Server",
            "owner_email": "owner@dc.local",
            "playrules_present": "1",
            "playrule_switches": "1",
            "playrule_ids": [str(rule.id)],
            "playrule_on": [str(rule.id)],
            "ladder_present": "1",
            "ladder_on_1": "1",
            "ladder_minutes_1": "0",
            "ladder_email_1": "l1@dc.local",
            "ladder_on_2": "1",
            "ladder_minutes_2": "15",
            "ladder_email_2": "l2@dc.local",
        },
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    db.expire_all()
    asset = db.query(Asset).filter_by(asset_id=asset_id).one()

    def asset_cubes() -> list[tuple[str, str, str]]:
        page = client.get("/assets", params={"q": asset_id, "per_page": "100"}).text
        table = page.split('<table class="asset-table"', 1)[1].split("</table>", 1)[0]
        row = table.split(f"/assets/{asset_id}", 1)[1].split("</tr>", 1)[0]
        return _cubes(row)

    before = asset_cubes()
    assert [tone for _level, tone, _title in before] == ["green", "green", "grey", "grey"]
    assert "L1 NOC · 0 min" == before[0][2]
    assert "not configured" in before[2][2]
    thead = client.get("/assets").text.split('<table class="asset-table"', 1)[1].split("</thead>", 1)[0]
    assert "ICMP / port / SNMP" in thead and thead.index("ICMP / port / SNMP") < thead.index('class="col-ladder"')
    assert "reach-sq" in (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8").split(".ladder-cubes", 1)[1]

    incident = ingest_alertmanager(db, _alert(alertname, asset_id))[0]
    opened = asset_cubes()
    assert [tone for _level, tone, _title in opened] == ["red", "green", "grey", "grey"]
    assert opened[0][2].endswith("escalated")

    def incident_cubes(number: str) -> list[tuple[str, str, str]]:
        page = client.get("/incidents", params={"q": number, "per_page": "100"}).text
        table = page.split("incidents-table", 1)[1]
        row = table.split(number, 1)[1].split("</tr>", 1)[0]
        return _cubes(row)

    climbed = incident_cubes(incident.number)
    assert [tone for _level, tone, _title in climbed] == ["red", "green", "grey", "grey"]
    assert "L2 Shift · 15 min" == climbed[1][2]
    head = client.get("/incidents").text.split("incidents-table", 1)[1].split("</thead>", 1)[0]
    assert head.index(">Status<") < head.index('class="col-ladder"') < head.index(">When<")

    incident.started_at = utcnow() - timedelta(minutes=20)
    incident.status = "ESCALATED"
    db.commit()
    db.refresh(incident)
    reached = asset_ladder_cubes(incident.asset, db)
    assert [cube["tone"] for cube in reached] == ["red", "red", "grey", "grey"]
    assert reached[1]["title"].endswith("escalated")
    not_mailed = incident_ladder_cubes(incident, db)
    assert not_mailed[1]["tone"] == "green"

    process_escalations(db)
    mailed = incident_cubes(incident.number)
    assert [tone for _level, tone, _title in mailed] == ["red", "red", "grey", "grey"]
    assert mailed[1][2].endswith("escalated")
    home = client.get("/", params={"per_page": "100"}).text
    assert "asset-form-split" not in home
    dash = home.split("data-dash-incident-table", 1)[1].split(incident.number, 1)[1].split("</tr>", 1)[0]
    assert [tone for _level, tone, _title in _cubes(dash)] == ["red", "red", "grey", "grey"]
    db.close()


def test_empty_asset_ladder_maps_default_warning_onto_incident_cubes():
    db = _db()
    client = _client()
    token = uuid4().hex[:6]
    alertname = f"DefCube{token}"
    _rule(db, f"cube-def-{token}", alertname)
    asset_id = f"cube-d-{token}"
    client.post(
        "/assets",
        data={"asset_id": asset_id, "hostname": asset_id, "ip": _ip(), "type": "Linux Server", "owner_email": "owner@dc.local"},
        follow_redirects=False,
    )
    incident = ingest_alertmanager(db, _alert(alertname, asset_id))[0]
    page = client.get("/assets", params={"q": asset_id, "per_page": "100"}).text
    table = page.split('<table class="asset-table"', 1)[1].split("</table>", 1)[0]
    row = table.split(f"/assets/{asset_id}", 1)[1].split("</tr>", 1)[0]
    assert [tone for _level, tone, _title in _cubes(row)] == ["grey", "grey", "grey", "grey"]

    listed = client.get("/incidents", params={"q": incident.number, "per_page": "100"}).text
    cubes = _cubes(listed.split("incidents-table", 1)[1].split(incident.number, 1)[1].split("</tr>", 1)[0])
    assert [tone for _level, tone, _title in cubes] == ["red", "green", "green", "grey"]
    assert cubes[0][2].startswith("L1 · 0 min · team") and cubes[0][2].endswith("escalated")
    assert cubes[1][2] == "L2 · 15 min · team-lead"
    assert cubes[2][2] == "L3 · 30 min · engineer"
    assert cubes[3][2] == "L4 · no such level"
    incident.started_at = utcnow() - timedelta(minutes=20)
    db.commit()
    process_escalations(db)
    listed = client.get("/incidents", params={"q": incident.number, "per_page": "100"}).text
    cubes = _cubes(listed.split("incidents-table", 1)[1].split(incident.number, 1)[1].split("</tr>", 1)[0])
    assert [tone for _level, tone, _title in cubes] == ["red", "red", "green", "grey"]
    db.close()
