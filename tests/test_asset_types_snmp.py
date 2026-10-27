"""Add-asset Type list, empty default scrape for non-servers, per-asset SNMP port, Custom support,
address-book email picks, runbook wording."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.asset_extras import support_status
from app.asset_extras import extras_from_form
from app.asset_types import ASSET_TYPE_CHOICES, snmp_family, snmp_poll_on, snmp_port_for, snmp_target
from app.db import Base, SessionLocal, engine
from app.inventory import default_scrape_address, is_snmp_asset, sd_snmp_targets, sd_targets
from app.main import app
from app.models import Asset, Incident, MailContact
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
NEW_TYPES = ("Storage", "QNAP/NAS", "Switch", "Router", "Firewall", "Printer", "Hypervisor", "Other")
KEPT_TYPES = ("Auto (detect exporter)", "Linux Server", "Windows Server", "Network device", "Web/appliance")


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
    db = SessionLocal()
    for asset in db.query(Asset).filter(Asset.asset_id.like("ats-%")).all():
        db.delete(asset)
    db.query(MailContact).filter(MailContact.email.like("ats-%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def _ip() -> str:
    raw = uuid4().int
    return f"10.{170 + raw % 20}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


def _post(client: TestClient, asset_id: str, type_: str, ip: str | None = None, **extra):
    data = {"asset_id": asset_id, "hostname": asset_id, "ip": ip or _ip(), "type": type_, **extra}
    return client.post("/assets", data=data, follow_redirects=False)


def _form_type_select(page: str) -> str:
    return page.split("data-detect-type>", 1)[1].split("</select>", 1)[0]


def _row(listing: str, asset_id: str) -> str:
    return listing.split(f">{asset_id}<", 1)[1].split("</tr>", 1)[0]


def test_type_dropdown_lists_kept_and_new_types_grouped():
    for name in KEPT_TYPES + NEW_TYPES:
        assert name in ASSET_TYPE_CHOICES, name
    _db().close()
    page = _client().get("/discovery").text
    select = _form_type_select(page)
    assert '<select name="type" class="nice-select" data-detect-type>' in page
    assert "<optgroup" in select
    for name in KEPT_TYPES + NEW_TYPES:
        assert f'<option value="{name}"' in select, name


def test_saved_custom_type_still_selectable_on_edit():
    db = _db()
    db.add(Asset(asset_id="ats-ups", hostname="ats-ups", ip=_ip(), type="UPS (APC)", monitoring_profile="", scrape_address=""))
    db.commit()
    db.close()
    page = _client().get("/assets?edit=ats-ups").text
    assert '<option value="UPS (APC)" selected>' in _form_type_select(page)


def test_default_scrape_only_for_linux_and_windows():
    ip = "10.9.9.9"
    assert default_scrape_address("Linux Server", ip) == f"{ip}:9100"
    assert default_scrape_address("Windows Server", ip) == f"{ip}:9182"
    for name in NEW_TYPES + ("Network device", "Web/appliance", "Auto (detect exporter)"):
        assert default_scrape_address(name, ip) == "", name


def test_qnap_and_switch_save_with_empty_scrape_and_no_http_sd():
    db = _db()
    client = _client()
    qnap_ip, sw_ip, linux_ip = _ip(), _ip(), _ip()
    assert _post(client, "ats-qnap", "QNAP/NAS", qnap_ip).status_code in {302, 303}
    assert _post(client, "ats-sw", "Switch", sw_ip).status_code in {302, 303}
    assert _post(client, "ats-lnx", "Linux Server", linux_ip).status_code in {302, 303}
    assert _post(client, "ats-hv", "Hypervisor", _ip()).status_code in {302, 303}
    db.expire_all()
    assert db.query(Asset).filter_by(asset_id="ats-qnap").one().scrape_address == ""
    assert db.query(Asset).filter_by(asset_id="ats-sw").one().scrape_address == ""
    assert db.query(Asset).filter_by(asset_id="ats-hv").one().scrape_address == ""
    assert db.query(Asset).filter_by(asset_id="ats-lnx").one().scrape_address == f"{linux_ip}:9100"
    http = [t["targets"][0] for t in sd_targets(db)]
    assert not any(t.startswith(qnap_ip) or t.startswith(sw_ip) for t in http)
    assert f"{linux_ip}:9100" in http
    db.close()


def test_qnap_with_typed_odd_port_scrape_keeps_it():
    db = _db()
    ip = _ip()
    assert _post(_client(), "ats-qnap-exp", "QNAP/NAS", ip, scrape_address=f"{ip}:9101").status_code in {302, 303}
    db.expire_all()
    row = db.query(Asset).filter_by(asset_id="ats-qnap-exp").one()
    assert row.scrape_address == f"{ip}:9101"
    assert any(t["targets"] == [f"{ip}:9101"] for t in sd_targets(db))
    db.close()


def test_ip_field_refuses_port():
    _db().close()
    resp = _post(_client(), "ats-bad-ip", "Linux Server", "10.1.2.3:9100")
    assert resp.status_code in {302, 303}
    assert "notice=" in (resp.headers.get("location") or "")
    assert "port" in (resp.headers.get("location") or "").lower()


def test_snmp_family_and_effective_port():
    for name in ("Network device", "Switch", "Router", "Firewall", "Storage", "QNAP/NAS", "Printer"):
        assert snmp_family(name), name
    for name in ("Linux Server", "Windows Server", "Hypervisor", "Web/appliance", "Other", "Auto (detect exporter)"):
        assert not snmp_family(name), name
    assert not snmp_family("Hypervisor", "network-switch")
    assert snmp_port_for(Asset(type="Switch", ip="1.1.1.1")) == 161
    assert snmp_port_for(Asset(type="Linux Server", ip="1.1.1.1")) == 0
    assert snmp_port_for(Asset(type="Linux Server", ip="1.1.1.1", snmp_port=1161)) == 1161
    assert snmp_port_for(Asset(type="Other", ip="1.1.1.1", extras={"snmp_poll": "1"})) == 161
    assert snmp_port_for(Asset(type="Hypervisor", ip="1.1.1.1", extras={"snmp_poll": "1"})) == 161
    assert snmp_port_for(Asset(type="Other", ip="1.1.1.1")) == 0
    assert snmp_poll_on({"snmp_poll": "1"})
    assert not snmp_poll_on({})
    assert extras_from_form("1", snmp_poll="1")["snmp_poll"] == "1"
    assert "snmp_poll" not in extras_from_form("1", site="DC1")
    assert snmp_target("1.1.1.1", 161) == "1.1.1.1"
    assert snmp_target("1.1.1.1", 1161) == "1.1.1.1:1161"


def test_sd_snmp_targets_honor_per_asset_port():
    db = _db()
    client = _client()
    sw_ip, nas_ip, linux_ip, linux_snmp_ip, hv_ip = _ip(), _ip(), _ip(), _ip(), _ip()
    assert _post(client, "ats-sw2", "Switch", sw_ip).status_code in {302, 303}
    assert _post(client, "ats-nas2", "QNAP/NAS", nas_ip, snmp_port="1161").status_code in {302, 303}
    assert _post(client, "ats-lnx2", "Linux Server", linux_ip).status_code in {302, 303}
    assert _post(client, "ats-lnx-snmp", "Linux Server", linux_snmp_ip, snmp_port="161").status_code in {302, 303}
    assert _post(client, "ats-hv2", "Hypervisor", hv_ip).status_code in {302, 303}
    db.expire_all()
    by_asset = {t["labels"]["asset"]: t for t in sd_snmp_targets(db)}
    assert by_asset["ats-sw2"]["targets"] == [sw_ip]
    assert by_asset["ats-sw2"]["labels"]["snmp_port"] == "161"
    assert by_asset["ats-nas2"]["targets"] == [f"{nas_ip}:1161"]
    assert by_asset["ats-nas2"]["labels"]["snmp_port"] == "1161"
    assert by_asset["ats-lnx-snmp"]["targets"] == [linux_snmp_ip]
    assert "ats-lnx2" not in by_asset
    assert "ats-hv2" not in by_asset
    assert is_snmp_asset(db.query(Asset).filter_by(asset_id="ats-lnx2").one()) is False

    sd = client.get("/api/v1/sd/snmp", headers={"Authorization": "Bearer forgesre-dev-webhook-token"}).json()
    assert any(item["targets"] == [f"{nas_ip}:1161"] for item in sd)
    api = client.get("/api/v1/assets/ats-nas2").json()
    assert api["snmp"] is True and api["snmp_port"] == 1161 and api["snmp_label"] == "snmp :1161"
    db.close()


def test_edit_keeps_or_clears_snmp_port():
    db = _db()
    client = _client()
    ip = _ip()
    assert _post(client, "ats-edit", "Storage", ip, snmp_port="2161").status_code in {302, 303}
    form = client.get("/assets?edit=ats-edit").text
    comms = form.split("data-asset-comms", 1)[1].split("alarm-families", 1)[0]
    assert 'name="snmp_port"' in comms and 'value="2161"' in comms
    resp = client.post("/assets/ats-edit/update", data={"hostname": "ats-edit", "ip": ip, "type": "Storage", "comms_present": "1", "snmp_port": ""}, follow_redirects=False)
    assert resp.status_code in {302, 303}
    db.expire_all()
    row = db.query(Asset).filter_by(asset_id="ats-edit").one()
    assert not row.snmp_port
    assert snmp_port_for(row) == 161
    client.post("/assets/ats-edit/update", data={"hostname": "ats-edit", "ip": ip, "type": "Storage", "snmp_port": "3161"}, follow_redirects=False)
    db.expire_all()
    assert not db.query(Asset).filter_by(asset_id="ats-edit").one().snmp_port
    db.close()


def test_assets_list_shows_snmp_port_only_when_configured():
    db = _db()
    client = _client()
    assert _post(client, "ats-l-sw", "Router", _ip()).status_code in {302, 303}
    assert _post(client, "ats-l-nas", "QNAP/NAS", _ip(), snmp_port="1161").status_code in {302, 303}
    lnx_ip = _ip()
    assert _post(client, "ats-l-lnx", "Linux Server", lnx_ip).status_code in {302, 303}
    assert _post(client, "ats-l-both", "Linux Server", _ip(), snmp_port="161").status_code in {302, 303}
    listing = client.get("/assets?per_page=100&q=ats-l-").text
    sw = _row(listing, "ats-l-sw")
    nas = _row(listing, "ats-l-nas")
    linux = _row(listing, "ats-l-lnx")
    assert "reach-sq" in sw and ">ICMP<" in sw and ">SNMP<" in sw and "UDP 161" in sw
    assert "snmp :" not in sw and ":161" not in sw
    assert ">SNMP<" in nas and "UDP 1161" in nas and "snmp :" not in nas
    assert ">ICMP<" in linux and ">9100<" in linux and "snmp :" not in linux and ">SNMP<" not in linux
    both = _row(listing, "ats-l-both")
    assert ">9100<" in both and "data-snmp-label" in both and ">SNMP<" in both and "UDP 161" in both
    assert ":9100" not in both and "snmp :" not in both
    reach = {row["asset_id"]: row for row in client.get("/api/v1/assets/reachability", params={"refresh": "false"}).json()}
    assert reach["ats-l-nas"]["snmp_label"] == "snmp :1161"
    assert reach["ats-l-lnx"]["snmp_label"] == ""
    db.close()


def test_support_custom_persists_and_shows_pill_without_incident():
    db = _db()
    before = db.query(Incident).count()
    client = _client()
    form = {"extras_present": "1", "extra_support": "custom", "extra_support_custom": "weekdays 8–16"}
    assert _post(client, "ats-sup", "Linux Server", **form).status_code in {302, 303}
    db.expire_all()
    row = db.query(Asset).filter_by(asset_id="ats-sup").one()
    assert row.extras["support"] == "custom"
    assert row.extras["support_custom"] == "weekdays 8–16"
    status = support_status(row)
    assert status["state"] == "in" and status["label"] == "Custom" and status["custom_note"] == "weekdays 8–16"
    assert "weekdays 8–16" in status["call_note"]

    listing = _row(client.get("/assets?per_page=100&q=ats-sup").text, "ats-sup")
    assert 'data-support-state="in"' in listing and ">Custom</span>" in listing and "weekdays 8–16" in listing
    stale = client.get("/assets/ats-sup", follow_redirects=False)
    assert stale.status_code == 303
    assert stale.headers["location"] == "/assets?edit=ats-sup"
    edit = client.get("/assets?edit=ats-sup").text
    assert "Custom — describe below" in edit and "weekdays 8–16" in edit
    support = edit.split("data-asset-support", 1)[1].split("asset-form-middle", 1)[0]
    assert '<option value="custom" selected>' in support
    assert 'name="extra_support_custom" value="weekdays 8–16"' in support

    missing = _post(client, "ats-sup-bad", "Linux Server", extras_present="1", extra_support="custom")
    assert missing.status_code in {302, 303}
    assert "notice=" in (missing.headers.get("location") or "")
    assert "Custom" in (missing.headers.get("location") or "")
    db.expire_all()
    assert db.query(Incident).count() == before
    db.close()


def test_support_custom_dates_still_expire():
    row = Asset(extras={"support": "custom", "support_custom": "NBD", "support_to": (date.today() - timedelta(days=1)).isoformat()})
    assert support_status(row)["state"] == "expired"
    soon = Asset(extras={"support": "custom", "support_custom": "NBD", "support_to": (date.today() + timedelta(days=3)).isoformat()})
    assert support_status(soon)["state"] == "expiring"


def test_contact_email_dropdowns_use_mail_book():
    db = _db()
    db.add(MailContact(email="ats-storage@dc.local", name="Storage on-call"))
    db.commit()
    client = _client()
    page = client.get("/discovery").text
    contacts = page.split("data-asset-contacts", 1)[1].split("data-asset-support", 1)[0]
    for name in ("owner_email_pick", "extra_backup_email_pick"):
        select = contacts.split(f'<select name="{name}"', 1)[1].split("</select>", 1)[0]
        assert '<option value="ats-storage@dc.local"' in select, name
        assert "Type a new email" in select
    assert 'name="owner_email" type="email"' in contacts
    assert 'name="extra_backup_email" type="email"' in contacts

    resp = _post(
        client, "ats-mail", "Linux Server",
        owner_email_pick="ats-storage@dc.local", owner_email="",
        extras_present="1", extra_backup_email_pick="", extra_backup_email="ats-new@dc.local",
    )
    assert resp.status_code in {302, 303}
    db.expire_all()
    row = db.query(Asset).filter_by(asset_id="ats-mail").one()
    assert row.owner_email == "ats-storage@dc.local"
    assert row.extras["backup_email"] == "ats-new@dc.local"
    edit = client.get("/assets?edit=ats-mail").text
    owner = edit.split('<select name="owner_email_pick"', 1)[1].split("</select>", 1)[0]
    assert '<option value="ats-storage@dc.local" data-label="Storage on-call" selected>' in owner
    db.close()


def test_runbook_note_is_labelled_human_guidance():
    _db().close()
    page = _client().get("/discovery").text
    notes = page.split("asset-form-notes", 1)[1].split("</section>", 1)[0]
    assert "for humans" not in notes
    assert ">Notes<" in notes
    assert "3am" not in notes and "Never executed" not in notes and "not a rule" not in notes


def test_any_type_snmp_poll_joins_sd():
    db = _db()
    client = _client()
    other_ip, hv_ip, lnx_ip, skip_ip = _ip(), _ip(), _ip(), _ip()
    assert _post(client, "ats-other-poll", "Other", other_ip, extras_present="1", snmp_poll="1").status_code in {302, 303}
    assert _post(client, "ats-hv-poll", "Hypervisor", hv_ip, extras_present="1", snmp_poll="1").status_code in {302, 303}
    assert _post(client, "ats-lnx-poll", "Linux Server", lnx_ip, extras_present="1", snmp_poll="1").status_code in {302, 303}
    assert _post(client, "ats-other-off", "Other", skip_ip).status_code in {302, 303}
    db.expire_all()
    other = db.query(Asset).filter_by(asset_id="ats-other-poll").one()
    assert other.extras.get("snmp_poll") == "1"
    assert is_snmp_asset(other) is True
    by_asset = {t["labels"]["asset"]: t for t in sd_snmp_targets(db)}
    assert by_asset["ats-other-poll"]["targets"] == [other_ip]
    assert by_asset["ats-other-poll"]["labels"]["snmp_port"] == "161"
    assert by_asset["ats-hv-poll"]["targets"] == [hv_ip]
    assert by_asset["ats-lnx-poll"]["targets"] == [lnx_ip]
    assert "ats-other-off" not in by_asset
    form = client.get("/assets?edit=ats-other-poll").text
    comms = form.split("data-asset-comms", 1)[1].split("alarm-families", 1)[0]
    assert 'name="snmp_poll"' in comms and "checked" in comms
    db.close()


def test_snmp_probe_json_any_ip():
    _db().close()
    client = _client()
    import discovery as discovery_mod

    orig = discovery_mod.probe_snmp_udp
    calls = []

    def fake(ip, community=None, timeout=0.4, port=161):
        calls.append((ip, community, port))
        return ip == "10.55.55.8"

    discovery_mod.probe_snmp_udp = fake
    try:
        page = client.get("/discovery").text
        comms = page.split("data-asset-comms", 1)[1].split("alarm-families", 1)[0]
        assert 'name="snmp_poll"' in comms
        assert "Poll SNMP" in comms
        assert "data-snmp-probe" in comms and ">Probe<" in comms
        miss = client.post("/assets/snmp-probe", data={"ip": "", "snmp_port": ""})
        assert miss.status_code == 200
        assert miss.json() == {"ok": False, "detail": "no IP", "port": 161}
        ok = client.post("/assets/snmp-probe", data={"ip": "10.55.55.8", "snmp_port": "161", "community": "public"})
        assert ok.status_code == 200
        body = ok.json()
        assert body["ok"] is True and body["port"] == 161 and "UDP/161" in body["detail"]
        no = client.post("/assets/snmp-probe", data={"ip": "10.55.55.9", "snmp_port": "1161"})
        assert no.status_code == 200
        assert no.json()["ok"] is False and no.json()["port"] == 1161
        assert ("10.55.55.8", "public", 161) in calls
        assert ("10.55.55.9", None, 1161) in calls
    finally:
        discovery_mod.probe_snmp_udp = orig


def test_css_and_js_v08_14():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v09-59" in base and "app.js?v=v09-R" in base
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert "select.nice-select" in css and ".reach-snmp" in css and ".snmp-poll-row" in css
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "bindAssetFormPicks" in js and "snmp_label" in js and "data-snmp-probe" in js
