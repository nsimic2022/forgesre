"""V0.8: version string, appliance NET dash, Dashboard journal pager, asset NOC extras, Client playrules."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.asset_extras import EXTRA_KEYS, extras_from_form, normalize_extras, playrule_ids_from_form
from app.db import Base, SessionLocal, engine
from app.host_resources import network_status
from app.journal import report
from app.main import app
from app.models import Asset, Incident, JournalEntry, Notification, Playrule
from app.notifications import build_escalation_body
from app.seed import seed
from app.services import ingest_alertmanager, match_playrule

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    assets = db.query(Asset).filter(Asset.asset_id.like("v08-%")).all()
    ids = [a.id for a in assets]
    if ids:
        incidents = db.query(Incident).filter(Incident.asset_id.in_(ids)).all()
        for inc in incidents:
            db.query(Notification).filter(Notification.incident_id == inc.id).delete(synchronize_session=False)
            db.delete(inc)
        db.flush()
        for asset in assets:
            db.delete(asset)
    db.query(Playrule).filter(Playrule.name.like("v08-%")).delete(synchronize_session=False)
    db.query(JournalEntry).filter(JournalEntry.action == "v08-pager").delete(synchronize_session=False)
    db.commit()
    db.close()


EXTRAS_FORM = {
    "extras_present": "1",
    "extra_customer": "acme.local",
    "extra_site": "BG-DC1 room 2 rack 14",
    "extra_backup_name": "Mila Backup",
    "extra_backup_phone": "+381-11-555-0102",
    "extra_backup_email": "mila@acme.local",
    "extra_support_hours": "24/7",
    "extra_timezone": "Europe/Belgrade",
    "extra_contract": "SLA gold 4h",
    "extra_runbook_note": "Ignore disk until 95%; then call Mila.",
}


def _post_asset(client: TestClient, asset_id: str, ip: str, **extra) -> None:
    data = {
        "asset_id": asset_id,
        "hostname": asset_id,
        "ip": ip,
        "type": "Linux Server",
        "owner_email": "owner@acme.local",
        **EXTRAS_FORM,
        **extra,
    }
    resp = client.post("/assets", data=data, follow_redirects=False)
    assert resp.status_code in {302, 303}, resp.text


def _alert(alertname: str, asset_id: str) -> dict:
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": alertname, "asset": asset_id, "severity": "warning"},
                "annotations": {"summary": f"{alertname} on {asset_id}"},
            }
        ],
    }


# --- version ---------------------------------------------------------------


def test_version_is_0_8_on_product_surfaces():
    from app.main import app as fastapi_app

    assert fastapi_app.version == "0.8.0"
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "<span>v0.8</span>" in base
    assert "v0.7" not in base
    assert "app.css?v=v08-1" in base
    assert "app.js?v=v08-1" in base
    for rel in ("scripts/install.sh", "scripts/render-monitoring.sh", "scripts/forgesre"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "0.8.0" in text
        assert "0.7.0" not in text
    assert "Current product: V0.8" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert (ROOT / "docs" / "v0.8.md").exists()
    assert "[V0.8](v0.8.md)" in (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    help_text = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help", "version"], text=True)
    assert "0.8.0" in help_text


def test_dashboard_footer_shows_v08():
    _db().close()
    home = _client().get("/")
    assert home.status_code == 200
    assert "ForgeSRE <span>v0.8</span>" in home.text


# --- appliance NET ------------------------------------------------------------

ROUTE_HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"


def _sys_net(tmp_path: Path, iface: str, operstate: str, carrier: str | None = None) -> str:
    d = tmp_path / iface
    d.mkdir(parents=True, exist_ok=True)
    (d / "operstate").write_text(operstate + "\n")
    if carrier is not None:
        (d / "carrier").write_text(carrier + "\n")
    return str(tmp_path)


def test_net_green_with_default_route_and_link_up(tmp_path):
    route = ROUTE_HEADER + "ens160\t00000000\t010A0A0A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    out = network_status(route_text=route, ipv6_route_text="", sys_net=_sys_net(tmp_path, "ens160", "up"))
    assert out["level"] == "ok"
    assert out["iface"] == "ens160"
    assert out["gateway"] == "10.10.10.1"
    assert out["reading"] == "ens160 up · gw 10.10.10.1"


def test_net_red_without_default_route(tmp_path):
    route = ROUTE_HEADER + "ens160\t000A0A0A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    out = network_status(route_text=route, ipv6_route_text="", sys_net=_sys_net(tmp_path, "ens160", "up"))
    assert out["level"] == "crit"
    assert out["reading"] == "no default route"


def test_net_red_when_default_iface_down(tmp_path):
    route = ROUTE_HEADER + "ens160\t00000000\t010A0A0A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    out = network_status(route_text=route, ipv6_route_text="", sys_net=_sys_net(tmp_path, "ens160", "down"))
    assert out["level"] == "crit"
    assert out["reading"] == "ens160 down"


def test_net_ignores_docker_default_and_uses_carrier_when_operstate_unknown(tmp_path):
    route = (
        ROUTE_HEADER
        + "docker0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        + "eth0\t00000000\t0101A8C0\t0003\t0\t0\t50\t00000000\t0\t0\t0\n"
    )
    sys_net = _sys_net(tmp_path, "eth0", "unknown", carrier="1")
    out = network_status(route_text=route, ipv6_route_text="", sys_net=sys_net)
    assert out["level"] == "ok"
    assert out["iface"] == "eth0"
    only_docker = ROUTE_HEADER + "docker0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    assert network_status(route_text=only_docker, ipv6_route_text="", sys_net=sys_net)["level"] == "crit"


def test_net_ipv6_default_route_when_no_ipv4(tmp_path):
    v6 = (
        "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
        "fe800000000000000000000000000001 00000400 00000001 00000000 00000003 ens192\n"
    )
    out = network_status(route_text=ROUTE_HEADER, ipv6_route_text=v6, sys_net=_sys_net(tmp_path, "ens192", "up"))
    assert out["level"] == "ok"
    assert out["family"] == "ipv6"


def test_dashboard_has_net_dash_and_api_level():
    _db().close()
    client = _client()
    home = client.get("/")
    card = home.text.split("data-appliance-card", 1)[1].split("</aside>", 1)[0]
    for key in ("cpu", "ram", "hdd", "net"):
        assert f'data-metric="{key}"' in card
    assert card.index('data-metric="hdd"') < card.index('data-metric="net"')
    assert '<span class="metric-name">NET</span>' in card
    assert "no internet probe" in card
    assert "Network throughput is not measured." in card
    data = client.get("/api/v1/system/resources").json()
    assert data["levels"]["net"] in {"ok", "crit"}
    assert data["net"]["reading"]
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'set("net", levels.net' in js


# --- Dashboard journal pager ---------------------------------------------------


def _journal_section(html: str) -> str:
    return html.split('id="journal"', 1)[1].split("</section>", 1)[0]


def test_dashboard_journal_has_list_chrome_and_pages():
    db = _db()
    token = uuid4().hex[:8]
    for i in range(25):
        report(db, "jobs", "v08-pager", "ok", summary=f"V08 journal {token} {i:02d}")
    total = db.query(JournalEntry).count()
    db.close()
    client = _client()
    home = client.get("/")
    section = _journal_section(home.text)
    assert "data-select-page" in section
    assert section.count('name="selected"') == 10
    assert f"Showing 1–10 of {total}" in section
    assert 'name="journal_per_page"' in section
    for n in (10, 20, 50, 100):
        assert f'<option value="{n}"' in section
    assert "journal_page=2" in section
    assert ">Open full journal</a>" in section
    assert section.index("pager-bar") < section.index("Open full journal")

    twenty = _journal_section(client.get("/?journal_per_page=20").text)
    assert twenty.count('name="selected"') == 20
    assert f"Showing 1–20 of {total}" in twenty

    clamped = _journal_section(client.get("/?journal_per_page=7").text)
    assert clamped.count('name="selected"') == 10
    huge = _journal_section(client.get("/?journal_per_page=9999").text)
    assert huge.count('name="selected"') == min(100, total)
    junk = _journal_section(client.get("/?journal_per_page=abc&journal_page=zzz").text)
    assert junk.count('name="selected"') == 10

    page_two = _journal_section(client.get("/?journal_page=2").text)
    first_ids = set(re.findall(r'name="selected" value="(\d+)"', section))
    second_ids = set(re.findall(r'name="selected" value="(\d+)"', page_two))
    assert first_ids and second_ids and first_ids.isdisjoint(second_ids)



# --- asset extras --------------------------------------------------------------


def test_extras_normalize_and_form_helpers():
    assert normalize_extras({"site": "  DC1 ", "bogus": "x", "timezone": ""}) == {"site": "DC1"}
    assert normalize_extras(None) == {}
    assert extras_from_form("", site="x") is None
    assert extras_from_form("1", site="DC1")["site"] == "DC1"
    assert playrule_ids_from_form("", ["1"]) is None
    assert playrule_ids_from_form("1", []) == []
    assert playrule_ids_from_form("1", ["3", "x", "3", "-1", "5"]) == [3, 5]
    assert len(normalize_extras({"runbook_note": "a" * 9000})["runbook_note"]) == 4000
    assert set(EXTRA_KEYS) == {
        "customer", "site", "backup_name", "backup_phone", "backup_email",
        "support_hours", "timezone", "contract", "runbook_note",
    }


def test_asset_form_is_two_columns_with_new_fields():
    _db().close()
    page = _client().get("/assets")
    assert page.status_code == 200
    text = page.text
    left = text.index("asset-form-left")
    right = text.index("asset-form-right")
    for name in (
        "extra_customer", "extra_site", "extra_backup_name", "extra_backup_phone", "extra_backup_email",
        "extra_support_hours", "extra_timezone", "extra_contract", "extra_runbook_note",
        "asset_id", "hostname", "owner_email", "owner_phone", "scrape_address", "notes",
    ):
        at = text.index(f'name="{name}"')
        assert left < at < right, name
    assert right < text.index("alarm-families") < text.index("data-client-playrules")
    assert "Standard alarms" in text
    assert "It cannot fire earlier than the Prometheus rule in alerts.yml." in text
    assert "Client playrules" in text
    assert "This does not create Prometheus thresholds." in text
    assert '<textarea name="extra_runbook_note"' in text
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".asset-form-split" in css
    assert "border-left: 1px solid var(--line);" in css.split(".asset-form-right {", 1)[1].split("}", 1)[0]


def test_asset_extras_persist_edit_clone_and_show_on_incident():
    db = _db()
    client = _client()
    token = uuid4().hex[:6]
    asset_id = f"v08-x-{token}"
    _post_asset(client, asset_id, f"10.208.{int(token[:2], 16)}.{int(token[2:4], 16) % 250 + 1}")
    db.expire_all()
    asset = db.query(Asset).filter_by(asset_id=asset_id).one()
    assert asset.extras["site"] == "BG-DC1 room 2 rack 14"
    assert asset.extras["runbook_note"] == "Ignore disk until 95%; then call Mila."
    assert asset.netbox_id == ""

    detail = client.get(f"/assets/{asset_id}")
    assert "Backup on-call phone" in detail.text and "+381-11-555-0102" in detail.text
    assert "None — global alertname match" in detail.text

    edit = client.get(f"/assets?edit={asset_id}")
    assert 'value="Europe/Belgrade"' in edit.text
    assert "Ignore disk until 95%; then call Mila.</textarea>" in edit.text

    updated = client.post(
        f"/assets/{asset_id}/update",
        data={"hostname": asset_id, "ip": asset.ip, "type": "Linux Server", **EXTRAS_FORM, "extra_timezone": "UTC", "extra_site": ""},
        follow_redirects=False,
    )
    assert updated.status_code in {302, 303}
    db.expire_all()
    asset = db.query(Asset).filter_by(asset_id=asset_id).one()
    assert asset.extras["timezone"] == "UTC"
    assert "site" not in asset.extras

    clone_page = client.get(f"/assets?clone={asset_id}")
    assert 'value="UTC"' in clone_page.text
    assert 'value="mila@acme.local"' in clone_page.text
    clone_id = f"v08-c-{token}"
    api_clone = client.post(f"/api/v1/assets/{asset_id}/clone", json={"asset_id": clone_id, "hostname": clone_id})
    assert api_clone.status_code == 200, api_clone.text
    assert api_clone.json()["asset_id"] == clone_id
    assert api_clone.json()["extras"]["backup_name"] == "Mila Backup"
    assert api_clone.json()["extras"]["timezone"] == "UTC"

    created = ingest_alertmanager(db, _alert("V08ExtrasProbe", asset_id))
    assert len(created) == 1
    incident = created[0]
    page = client.get(f"/incidents/{incident.number}")
    who = page.text.split("Who to call", 1)[1].split("Send incident report", 1)[0]
    for label, value in (
        ("Customer / domain", "acme.local"),
        ("Backup on-call name", "Mila Backup"),
        ("Backup on-call phone", "+381-11-555-0102"),
        ("Backup on-call email", "mila@acme.local"),
        ("Support hours", "24/7"),
        ("Timezone", "UTC"),
        ("License / contract / SLA", "SLA gold 4h"),
    ):
        assert label in who and value in who, label
    assert "data-runbook-note" in who
    assert "Ignore disk until 95%; then call Mila." in who
    body = build_escalation_body(incident, "immediate", "team")
    assert "Backup on-call phone: +381-11-555-0102" in body
    assert "Runbook note: Ignore disk until 95%; then call Mila." in body
    db.close()


# --- Client playrules ------------------------------------------------------------


def _rule(db, name: str, alertname: str, *, enabled: bool = True) -> Playrule:
    rule = Playrule(name=name, enabled=enabled, severity="warning", condition={"alertname": alertname})
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def test_playrule_picker_does_not_change_match_for_unlinked_assets():
    db = _db()
    token = uuid4().hex[:6]
    alertname = f"V08Alert{token}"
    global_rule = _rule(db, f"v08-global-{token}", alertname)
    client_rule = _rule(db, f"v08-client-{token}", alertname)
    other_rule = _rule(db, f"v08-other-{token}", f"Other{token}")
    client = _client()

    plain_id = f"v08-p-{token}"
    linked_id = f"v08-l-{token}"
    _post_asset(client, plain_id, f"10.209.1.{int(token[:2], 16) % 250 + 1}")
    _post_asset(
        client,
        linked_id,
        f"10.209.2.{int(token[:2], 16) % 250 + 1}",
        playrules_present="1",
        playrule_ids=[str(other_rule.id), str(client_rule.id), "999999"],
    )
    db.expire_all()
    plain = db.query(Asset).filter_by(asset_id=plain_id).one()
    linked = db.query(Asset).filter_by(asset_id=linked_id).one()
    assert not plain.playrule_ids
    assert linked.playrule_ids == [other_rule.id, client_rule.id]

    assert match_playrule(db, alertname) is global_rule
    assert match_playrule(db, alertname, asset=None) is global_rule
    assert match_playrule(db, alertname, asset=plain) is global_rule
    assert match_playrule(db, alertname, asset=linked) is client_rule
    assert match_playrule(db, f"Other{token}", asset=plain) is other_rule
    assert match_playrule(db, f"NoRule{token}", asset=linked) is None

    plain_inc = ingest_alertmanager(db, _alert(alertname, plain_id))[0]
    linked_inc = ingest_alertmanager(db, _alert(alertname, linked_id))[0]
    assert plain_inc.playrule_id == global_rule.id
    assert linked_inc.playrule_id == client_rule.id
    timeline = {item["id"]: item["detail"] for item in linked_inc.timeline}
    assert timeline["playrule"].endswith("(asset client playrule)")
    plain_timeline = {item["id"]: item["detail"] for item in plain_inc.timeline}
    assert plain_timeline["playrule"] == global_rule.name

    page = client.get(f"/incidents/{linked_inc.number}")
    assert client_rule.name in page.text.split("Who to call", 1)[1].split("Send incident report", 1)[0]

    client_rule.enabled = False
    db.commit()
    assert match_playrule(db, alertname, asset=linked) is global_rule

    edit = client.get(f"/assets?edit={linked_id}")
    picker = edit.text.split("data-client-playrules", 1)[1]
    assert f'name="playrule_ids" value="{client_rule.id}" checked' in picker
    assert "disabled" in picker
    cleared = client.post(
        f"/assets/{linked_id}/update",
        data={"hostname": linked_id, "ip": linked.ip, "type": "Linux Server", "playrules_present": "1"},
        follow_redirects=False,
    )
    assert cleared.status_code in {302, 303}
    db.expire_all()
    after = db.query(Asset).filter_by(asset_id=linked_id).one()
    assert after.playrule_ids == []
    assert after.extras["backup_name"] == "Mila Backup"
    db.close()
