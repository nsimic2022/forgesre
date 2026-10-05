"""V0.8: version string, appliance NET dash, Dashboard journal pager, asset NOC extras, Client playrules,
support coverage."""

from __future__ import annotations

import re
import subprocess
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.asset_extras import (
    DISPLAY_KEYS,
    EXTRA_KEYS,
    SUPPORT_KEYS,
    extras_from_form,
    extras_rows,
    normalize_extras,
    playrule_ids_from_form,
    support_status,
)
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
    assert "app.css?v=v08-8" in base
    assert "app.js?v=v08-6" in base
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
    assert set(DISPLAY_KEYS) == {
        "customer", "site", "backup_name", "backup_phone", "backup_email",
        "support_hours", "timezone", "contract", "runbook_note",
    }
    assert SUPPORT_KEYS == ("support", "support_from", "support_to", "support_lead_days")
    assert set(EXTRA_KEYS) == set(DISPLAY_KEYS) | set(SUPPORT_KEYS)
    assert playrule_ids_from_form("1", ["3"], "7") == [3, 7]
    assert playrule_ids_from_form("1", ["3"], "3") == [3]
    assert playrule_ids_from_form("1", [], "") == []


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_asset_form_is_three_columns_with_dropdown_playrules():
    db = _db()
    token = uuid4().hex[:6]
    enabled = _rule(db, f"v08-drop-{token}", f"Drop{token}")
    disabled = _rule(db, f"v08-off-{token}", f"Off{token}", enabled=False)
    enabled_id, disabled_id = enabled.id, disabled.id
    db.close()
    page = _client().get("/assets")
    assert page.status_code == 200
    text = page.text
    left = text.index("asset-form-left")
    middle = text.index("asset-form-middle")
    right = text.index("asset-form-right")
    assert left < middle < right
    for name in (
        "extra_customer", "extra_site", "extra_backup_name", "extra_backup_phone", "extra_backup_email",
        "extra_support_hours", "extra_timezone", "extra_contract", "extra_runbook_note",
        "extra_support", "extra_support_from", "extra_support_to", "extra_support_lead_days",
        "asset_id", "hostname", "owner_email", "owner_phone", "scrape_address", "notes",
    ):
        at = text.index(f'name="{name}"')
        assert left < at < middle, name
    assert text.index("data-asset-contacts") < text.index("data-asset-support") < middle
    assert middle < text.index("alarm-families") < right < text.index("data-client-playrules")
    assert "Standard alarms" in text
    assert "It cannot fire earlier than the Prometheus rule in alerts.yml." in text
    assert '<textarea name="extra_runbook_note"' in text

    support = _between(text, "data-asset-support", "asset-form-middle")
    for value in ("yes", "no", "internal"):
        assert f'<option value="{value}"' in support
    assert 'type="date"' in support
    assert '<option value="14" selected>14 days</option>' in support
    assert '<option value="7"' in support and '<option value="30"' in support

    playrules = _between(text, "data-client-playrules", "</form>")
    assert "Client playrules" in playrules
    assert "This does not create Prometheus thresholds." in playrules
    assert 'type="checkbox"' not in playrules
    assert '<select name="playrule_add" data-playrule-add' in playrules
    assert f'<option value="{enabled_id}"' in playrules
    assert f'<option value="{disabled_id}"' not in playrules
    assert "data-playrule-list" in playrules
    assert 'name="playrule_ids"' not in playrules
    empty = _between(playrules, "data-playrule-empty", "</p>")
    assert "hidden" not in empty
    assert "No client playrules — global Playrules apply." in empty

    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    block = css.split("/* Add / Edit asset: three columns", 1)[1].split(".asset-form-block {", 1)[0]
    assert "grid-template-columns: minmax(0, 2fr) minmax(14rem, 1fr) minmax(16rem, 1.1fr);" in block
    rules = block.split(".asset-form-middle,\n.asset-form-right {", 1)[1].split("}", 1)[0]
    assert "border-left: 1px solid var(--line);" in rules
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "bindClientPlayrules" in js and "[data-playrule-remove]" in js


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
    card = _between(detail.text, "data-asset-playrules", "</section>")
    assert "No client playrules — global Playrules apply." in card

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
    picker = edit.text.split("data-client-playrules", 1)[1].split("</form>", 1)[0]
    rows = picker.split("data-playrule-list", 1)[1].split("</ol>", 1)[0]
    assert rows.index(f'value="{other_rule.id}"') < rows.index(f'value="{client_rule.id}"')
    assert f'<input type="hidden" name="playrule_ids" value="{client_rule.id}">' in rows
    assert "· disabled" in rows
    assert "data-playrule-remove" in rows
    assert f'<option value="{client_rule.id}"' not in picker
    assert f'<option value="{other_rule.id}" data-name="{other_rule.name}"' in picker
    assert "hidden>No client playrules" in picker

    added = client.post(
        f"/assets/{linked_id}/update",
        data={
            "hostname": linked_id, "ip": linked.ip, "type": "Linux Server", "playrules_present": "1",
            "playrule_ids": [str(other_rule.id)], "playrule_add": str(global_rule.id),
        },
        follow_redirects=False,
    )
    assert added.status_code in {302, 303}
    db.expire_all()
    assert db.query(Asset).filter_by(asset_id=linked_id).one().playrule_ids == [other_rule.id, global_rule.id]
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


# --- Support coverage -------------------------------------------------------------


class _Row:
    def __init__(self, **extras):
        self.extras = extras


def test_support_status_states_and_lead_time():
    today = date(2026, 10, 5)

    def state(**extras):
        return support_status(_Row(**extras), today=today)

    unknown = state()
    assert (unknown["state"], unknown["tone"], unknown["warn"]) == ("unknown", "grey", False)
    assert state(support="yes")["state"] == "unknown"
    assert state(support="internal")["state"] == "unknown"

    inside = state(support="yes", support_from="2026-01-01", support_to="2027-01-01")
    assert (inside["state"], inside["label"], inside["tone"], inside["warn"]) == ("in", "In support", "ok", False)
    assert inside["detail"] == "Support until 2027-01-01"
    open_end = state(support="internal", support_from="2026-01-01")
    assert open_end["state"] == "in" and open_end["detail"] == "Internal support, no end date"
    assert state(support_to="2027-01-01")["state"] == "in"

    soon = state(support="yes", support_from="2026-01-01", support_to="2026-10-15")
    assert (soon["state"], soon["tone"], soon["warn"]) == ("expiring", "warn", True)
    assert soon["lead_days"] == 14 and soon["days_left"] == 10
    assert soon["call_note"] == "Expiring — Support ends 2026-10-15 (in 10 days)"
    assert state(support="yes", support_to="2026-10-19")["state"] == "expiring"
    assert state(support="yes", support_to="2026-10-20")["state"] == "in"
    assert state(support="yes", support_to="2026-10-05")["detail"].endswith("(today)")
    assert state(support="yes", support_to="2026-10-15", support_lead_days="7")["state"] == "in"
    assert state(support="yes", support_to="2026-11-01", support_lead_days="30")["state"] == "expiring"

    past = state(support="yes", support_from="2025-01-01", support_to="2026-10-04")
    assert (past["state"], past["label"], past["tone"], past["warn"]) == ("expired", "Out of support", "crit", True)
    assert past["call_note"] == "Out of support — vendor may not take a ticket"
    assert past["detail"] == "Support ended 2026-10-04"
    no_contract = state(support="no")
    assert no_contract["state"] == "expired" and no_contract["detail"] == "No support contract"
    assert state(support="no", support_to="2030-01-01")["state"] == "expired"
    assert state(support="yes", support_from="2026-11-01")["state"] == "expired"

    assert {unknown["state"], inside["state"], soon["state"], past["state"]} == {"unknown", "in", "expiring", "expired"}


def test_support_normalize_keeps_block_out_of_generic_rows():
    raw = {
        "support": " Internal ", "support_from": "2026-01-01", "support_to": "not-a-date",
        "support_lead_days": "45", "site": "DC1",
    }
    assert normalize_extras(raw) == {
        "site": "DC1", "support": "internal", "support_from": "2026-01-01", "support_lead_days": "14",
    }
    assert normalize_extras({"support": "maybe", "support_lead_days": "30"}) == {}
    assert normalize_extras({"support": "no", "support_lead_days": "7"})["support_lead_days"] == "7"
    keys = [key for key, _label, _value in extras_rows(_Row(**normalize_extras(raw)))]
    assert keys == ["site"]


def _support_form(kind: str, start: date | None, end: date | None, lead: int = 14) -> dict:
    return {
        "extra_support": kind,
        "extra_support_from": start.isoformat() if start else "",
        "extra_support_to": end.isoformat() if end else "",
        "extra_support_lead_days": str(lead),
    }


def test_support_shows_on_list_detail_incident_and_never_opens_an_incident():
    db = _db()
    client = _client()
    token = uuid4().hex[:6]
    today = date.today()
    octet = int(token[:2], 16) % 250 + 1
    cases = {
        "in": _support_form("yes", today - timedelta(days=100), today + timedelta(days=200)),
        "expiring": _support_form("yes", today - timedelta(days=100), today + timedelta(days=5)),
        "expired": _support_form("yes", today - timedelta(days=400), today - timedelta(days=1)),
        "unknown": _support_form("", None, None),
    }
    ids = {}
    for n, (state, form) in enumerate(cases.items()):
        asset_id = f"v08-s{state}-{token}"
        ids[state] = asset_id
        _post_asset(client, asset_id, f"10.210.{n + 1}.{octet}", **form)
    db.expire_all()
    before = db.query(Incident).count()

    stored = db.query(Asset).filter_by(asset_id=ids["expiring"]).one().extras
    assert stored["support"] == "yes" and stored["support_lead_days"] == "14"
    assert stored["support_to"] == (today + timedelta(days=5)).isoformat()
    assert "support" not in db.query(Asset).filter_by(asset_id=ids["unknown"]).one().extras

    listing = client.get("/assets?per_page=100&q=" + token).text
    for state, asset_id in ids.items():
        row = listing.split(f">{asset_id}<", 1)[1].split("</tr>", 1)[0]
        assert f'data-support-state="{state}"' in row, state

    labels = {"in": "In support", "expiring": "Support expiring", "expired": "Out of support", "unknown": "Support unknown"}
    for state, asset_id in ids.items():
        detail = client.get(f"/assets/{asset_id}").text
        assert f'data-support-state="{state}">{labels[state]}</span>' in detail, state
        panel = detail.split("data-asset-metrics", 1)[1]
        if state in {"expiring", "expired"}:
            assert "data-support-warning" in panel
            assert "no alert or incident is opened" in panel
        else:
            assert "data-support-warning" not in panel

    edit = client.get(f"/assets?edit={ids['expiring']}").text
    support = edit.split("data-asset-support", 1)[1].split("asset-form-middle", 1)[0]
    assert '<option value="yes" selected>' in support
    assert f'value="{(today + timedelta(days=5)).isoformat()}"' in support

    clone_id = f"v08-sclone-{token}"
    api_clone = client.post(f"/api/v1/assets/{ids['expired']}/clone", json={"asset_id": clone_id, "hostname": clone_id})
    assert api_clone.status_code == 200, api_clone.text
    assert api_clone.json()["extras"]["support_to"] == (today - timedelta(days=1)).isoformat()
    assert api_clone.json()["support_status"]["state"] == "expired"
    assert "support_to" in client.get(f"/assets?clone={ids['expired']}").text.split("data-asset-support", 1)[1]

    db.expire_all()
    assert db.query(Incident).count() == before
    alerts = (ROOT / "monitoring" / "alerts.yml").read_text(encoding="utf-8")
    assert "Support" not in alerts

    incident = ingest_alertmanager(db, _alert("V08SupportProbe", ids["expired"]))[0]
    who = client.get(f"/incidents/{incident.number}").text.split("Who to call", 1)[1].split("Send incident report", 1)[0]
    assert 'data-support-state="expired"' in who
    assert "Out of support — vendor may not take a ticket" in who
    assert "Support ended" in who
    assert "Support: Out of support — vendor may not take a ticket" in build_escalation_body(incident, "immediate", "team")
    plain = ingest_alertmanager(db, _alert("V08SupportProbe", ids["unknown"]))[0]
    plain_who = client.get(f"/incidents/{plain.number}").text.split("Who to call", 1)[1].split("Send incident report", 1)[0]
    assert "Unknown — support dates not filled" in plain_who
    assert "Support:" not in build_escalation_body(plain, "immediate", "team")
    db.close()


# --- Asset detail: full fact list + Client playrules card --------------------------

DETAIL_LABELS = (
    "Asset number", "Hostname", "Type", "Environment", "Customer / domain", "Site / DC / room", "Notes",
    "Owner / team", "Contact", "Email", "Phone", "Backup on-call name", "Backup on-call phone",
    "Backup on-call email", "Support hours", "Timezone", "License / contract / SLA", "Runbook note",
    "Under support", "From", "To", "Lead time",
)


def _dd(html: str, label: str) -> str:
    match = re.search(r"<dt>" + re.escape(label) + r"</dt>\s*<dd[^>]*>(.*?)</dd>", html, re.S)
    assert match, label
    return match.group(1).strip()


def _facts(html: str) -> str:
    return _between(html, 'class="asset-detail-main"', 'class="asset-detail-side"')


def _viewer_client() -> TestClient:
    from app.models import User
    from app.security import hash_password

    db = _db()
    email = "viewer-v08@forgesre.local"
    if db.query(User).filter_by(email=email).first() is None:
        db.add(User(email=email, name="V08 viewer", password_hash=hash_password("testpass"), role="viewer"))
        db.commit()
    db.close()
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": "testpass"}, follow_redirects=False)
    return client


def test_asset_detail_lists_every_add_field_with_dash_when_empty():
    db = _db()
    client = _client()
    token = uuid4().hex[:6]
    octet = int(token[:2], 16) % 250 + 1
    full_id = f"v08-df-{token}"
    _post_asset(
        client, full_id, f"10.211.1.{octet}", notes="Rack door sticks",
        contact_name="Ana", owner_phone="+381-11-555-0100",
        **_support_form("internal", date(2026, 1, 1), date(2030, 1, 1), lead=30),
    )
    bare_id = f"v08-de-{token}"
    resp = client.post(
        "/assets",
        data={"asset_id": bare_id, "hostname": bare_id, "ip": f"10.211.2.{octet}", "type": "Linux Server", "owner": ""},
        follow_redirects=False,
    )
    assert resp.status_code in {302, 303}

    full = _facts(client.get(f"/assets/{full_id}").text)
    for label in DETAIL_LABELS:
        assert _dd(full, label) not in {"", "—", "None"}, label
    for label, value in (
        ("Hostname", full_id), ("Customer / domain", "acme.local"), ("Site / DC / room", "BG-DC1 room 2 rack 14"),
        ("Backup on-call email", "mila@acme.local"), ("Timezone", "Europe/Belgrade"),
        ("License / contract / SLA", "SLA gold 4h"), ("Under support", "Internal — we hold it"),
        ("From", "2026-01-01"), ("To", "2030-01-01"), ("Lead time", "30 days"),
    ):
        assert _dd(full, label) == value, label
    assert "Ignore disk until 95%; then call Mila." in _dd(full, "Runbook note")
    assert 'data-support-state="in">In support</span>' in full
    assert "data-asset-facts=\"identity\"" in full and "data-asset-facts=\"support\"" in full

    bare = _facts(client.get(f"/assets/{bare_id}").text)
    for label in (
        "Customer / domain", "Site / DC / room", "Notes", "Contact", "Phone", "Backup on-call name",
        "Backup on-call phone", "Backup on-call email", "Support hours", "Timezone", "License / contract / SLA",
        "Runbook note", "Under support", "From", "To", "Lead time",
    ):
        assert _dd(bare, label) == "—", label
    assert _dd(bare, "Email").startswith("—")
    assert "No owner email" in _dd(bare, "Email")
    assert 'data-support-state="unknown">Support unknown</span>' in bare
    assert ">None<" not in bare
    db.close()


def test_asset_detail_without_stored_extras_still_renders():
    db = _db()
    client = _client()
    token = uuid4().hex[:6]
    asset_id = f"v08-dn-{token}"
    _post_asset(client, asset_id, f"10.212.1.{int(token[:2], 16) % 250 + 1}")
    db.expire_all()
    asset = db.query(Asset).filter_by(asset_id=asset_id).one()
    asset.extras = None
    asset.playrule_ids = None
    db.commit()
    page = client.get(f"/assets/{asset_id}")
    assert page.status_code == 200
    facts = _facts(page.text)
    assert _dd(facts, "Customer / domain") == "—"
    assert _dd(facts, "Under support") == "—"
    card = _between(page.text, "data-asset-playrules", "</section>")
    assert "No client playrules — global Playrules apply." in card
    db.close()


def test_asset_detail_client_playrules_card_sits_under_machine_metrics():
    db = _db()
    token = uuid4().hex[:6]
    enabled = _rule(db, f"v08-card-{token}", f"Card{token}")
    disabled = _rule(db, f"v08-cardoff-{token}", f"CardOff{token}", enabled=False)
    enabled_id, disabled_id = enabled.id, disabled.id
    db.close()
    client = _client()
    asset_id = f"v08-dc-{token}"
    _post_asset(client, asset_id, f"10.213.1.{int(token[:2], 16) % 250 + 1}")
    text = client.get(f"/assets/{asset_id}").text

    side = text.split('class="asset-detail-side"', 1)[1]
    assert side.index("asset-detail-metrics card") < side.index("asset-detail-playrules card")
    assert "Machine metrics" in side.split("asset-detail-playrules", 1)[0]
    assert "Client playrules" not in _facts(text)

    card = _between(text, "data-asset-playrules", "</section>")
    assert '<div class="metric-panel-head">' in card and "<h2>Client playrules" in card
    assert "Does not create Prometheus thresholds" in card
    assert f'action="/assets/{asset_id}/playrules"' in card
    assert '<input type="hidden" name="playrules_present" value="1">' in card
    assert '<select name="playrule_add" data-playrule-add' in card
    assert f'<option value="{enabled_id}"' in card
    assert f'<option value="{disabled_id}"' not in card
    assert "data-playrule-list" in card
    empty = _between(card, "data-playrule-empty", "</p>")
    assert "hidden" not in empty
    assert "No client playrules — global Playrules apply." in empty

    viewer = _between(_viewer_client().get(f"/assets/{asset_id}").text, "data-asset-playrules", "</section>")
    assert "No client playrules — global Playrules apply." in viewer
    assert "<form" not in viewer and "playrule_add" not in viewer

    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".asset-detail-side {" in css and ".asset-detail-playrules {" in css


def test_asset_detail_playrules_post_adds_and_removes_without_touching_other_fields():
    db = _db()
    token = uuid4().hex[:6]
    first = _rule(db, f"v08-pa-{token}", f"PA{token}")
    second = _rule(db, f"v08-pb-{token}", f"PB{token}")
    first_id, second_id = first.id, second.id
    first_name, second_name = first.name, second.name
    client = _client()
    asset_id = f"v08-dp-{token}"
    _post_asset(client, asset_id, f"10.214.1.{int(token[:2], 16) % 250 + 1}", notes="keep me")
    db.expire_all()
    before = db.query(Asset).filter_by(asset_id=asset_id).one()
    snapshot = (before.hostname, before.ip, before.type, before.scrape_address, before.notes, dict(before.extras), dict(before.alarms or {}))

    def post(**data):
        resp = client.post(f"/assets/{asset_id}/playrules", data={"playrules_present": "1", **data}, follow_redirects=False)
        assert resp.status_code in {302, 303}, resp.text
        assert resp.headers["location"].startswith(f"/assets/{asset_id}?notice=")
        db.expire_all()
        return db.query(Asset).filter_by(asset_id=asset_id).one()

    assert post(playrule_add=str(first_id)).playrule_ids == [first_id]
    after = post(playrule_ids=[str(first_id)], playrule_add=str(second_id))
    assert after.playrule_ids == [first_id, second_id]
    assert (after.hostname, after.ip, after.type, after.scrape_address, after.notes, dict(after.extras), dict(after.alarms or {})) == snapshot

    card = _between(client.get(f"/assets/{asset_id}").text, "data-asset-playrules", "</section>")
    rows = _between(card, "data-playrule-list", "</ol>")
    assert rows.index(f'value="{first_id}"') < rows.index(f'value="{second_id}"')
    assert f"{first_name} <span class=\"muted\">· PA{token}" in rows
    assert f"{second_name} <span class=\"muted\">· PB{token}" in rows
    assert "data-playrule-remove" in rows
    assert f'<option value="{first_id}" data-name="{first_name}" data-meta="PA{token}" disabled>' in card
    assert "hidden>No client playrules" in card

    assert post(playrule_ids=[str(second_id), "999999"]).playrule_ids == [second_id]
    assert post().playrule_ids == []
    assert match_playrule(db, f"PB{token}", asset=db.query(Asset).filter_by(asset_id=asset_id).one()).id == second_id

    denied = _viewer_client().post(
        f"/assets/{asset_id}/playrules", data={"playrules_present": "1", "playrule_add": str(first_id)}, follow_redirects=False
    )
    assert denied.status_code == 403
    db.expire_all()
    assert db.query(Asset).filter_by(asset_id=asset_id).one().playrule_ids == []
    assert client.post("/assets/v08-missing-asset/playrules", data={"playrules_present": "1"}).status_code == 404
    db.close()
