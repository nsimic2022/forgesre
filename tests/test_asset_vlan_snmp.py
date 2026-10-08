"""Asset VLAN field + filter, per-asset SNMP version / community / v3 USM, the three-column Add/Edit
layout (Comms only in the middle; Standard alarms then Client playrules on the right), and the
snmp_exporter auth render path. Secrets never reach GET HTML, the asset API, journal, or audit."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from app.asset_snmp import auth_spec, desired_auth, effective_auth, snmp_from_form
from app.db import Base, SessionLocal, engine
from app.inventory import sd_snmp_targets, snmp_auths
from app.main import app
from app.models import Asset, AuditLog, JournalEntry
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "avs-"
TOKEN = {"Authorization": "Bearer forgesre-dev-webhook-token"}
SECRET_COMMUNITY = "s3cr3t-comm-" + uuid4().hex[:6]
SECRET_AUTH = "authpass-" + uuid4().hex[:8]
SECRET_PRIV = "privpass-" + uuid4().hex[:8]


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
def _cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGESRE_GENERATED_DIR", str(tmp_path))
    yield
    db = SessionLocal()
    for asset in db.query(Asset).filter(Asset.asset_id.like(f"{PREFIX}%")).all():
        db.delete(asset)
    db.commit()
    db.close()


def _ip() -> str:
    raw = uuid4().int
    return f"10.{200 + raw % 20}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


def _base(asset_id: str, type_: str = "Switch", **extra) -> dict:
    return {
        "asset_id": asset_id,
        "hostname": asset_id,
        "ip": _ip(),
        "type": type_,
        "extras_present": "1",
        "comms_present": "1",
        **extra,
    }


def _row(db, asset_id: str) -> Asset:
    db.expire_all()
    return db.query(Asset).filter_by(asset_id=asset_id).one()


def _write_rendered(tmp_path: Path, names: list[str]) -> None:
    auths = {name: {"version": 2, "community": "x"} for name in ["public_v2", "public_v1", *names]}
    (tmp_path / "snmp.yml").write_text(yaml.safe_dump({"auths": auths, "modules": {}}))


def _ids(page: str) -> set[str]:
    table = page.split('<table class="asset-table"', 1)[1].split("</table>", 1)[0]
    found = (cell.split("<", 1)[0] for cell in table.split("data-list-open>")[1:])
    return {asset_id for asset_id in found if asset_id.startswith(PREFIX)}


def test_vlan_persists_on_add_edit_detail_and_filters():
    db = _db()
    client = _client()
    assert client.post("/assets", data=_base(f"{PREFIX}v10", extra_vlan=" 10 "), follow_redirects=False).status_code in {302, 303}
    assert client.post("/assets", data=_base(f"{PREFIX}v1020", extra_vlan="10.20"), follow_redirects=False).status_code in {302, 303}
    assert client.post("/assets", data=_base(f"{PREFIX}none"), follow_redirects=False).status_code in {302, 303}
    assert _row(db, f"{PREFIX}v10").extras["vlan"] == "10"
    assert "vlan" not in (_row(db, f"{PREFIX}none").extras or {})

    detail = client.get(f"/assets/{PREFIX}v1020").text
    assert '<dt>VLAN</dt><dd class="extra-vlan">10.20</dd>' in detail
    assert '<dt>VLAN</dt><dd class="extra-vlan">—</dd>' in client.get(f"/assets/{PREFIX}none").text

    edit = client.get(f"/assets?edit={PREFIX}v10").text
    left = edit.split("asset-form-left", 1)[1].split("asset-form-middle", 1)[0]
    assert 'name="extra_vlan" value="10"' in left
    assert left.index('name="extra_site"') < left.index('name="extra_vlan"')
    row = _row(db, f"{PREFIX}v10")
    resp = client.post(
        f"/assets/{PREFIX}v10/update",
        data={"hostname": row.hostname, "ip": row.ip, "type": "Switch", "extras_present": "1", "extra_vlan": "30"},
        follow_redirects=False,
    )
    assert resp.status_code in {302, 303}
    assert _row(db, f"{PREFIX}v10").extras["vlan"] == "30"

    page = client.get("/assets?vlan=10.20").text
    assert _ids(page) == {f"{PREFIX}v1020"}
    assert _ids(client.get("/assets?vlan=30&type=Switch").text) == {f"{PREFIX}v10"}
    assert _ids(client.get(f"/assets?q=10.20").text) >= {f"{PREFIX}v1020"}
    form = page.split('<form method="get" action="/assets"', 1)[1].split("</form>", 1)[0]
    bar = [form.index(f'data-asset-filter="{key}"') for key in ("type", "source", "site", "vlan", "customer")]
    assert bar == sorted(bar)
    vlan = form.split('data-asset-filter="vlan">', 1)[1].split("</select>", 1)[0]
    assert '<option value="">VLAN</option>' in vlan
    assert "All VLANs" not in vlan
    assert '<option value="10.20" selected>' in vlan and '<option value="30">' in vlan
    assert '<a class="muted list-reset" href="/assets">Reset</a>' in form
    assert "&amp;vlan=10.20" in page.split('<table class="asset-table"', 1)[1]
    db.close()


def test_layout_comms_alone_in_middle_alarms_then_playrules_right():
    _db().close()
    text = _client().get("/assets").text
    middle = text.split("asset-form-middle", 1)[1].split("asset-form-right", 1)[0]
    right = text.split("asset-form-right", 1)[1].split("</form>", 1)[0]
    for name in ("scrape_address", "snmp_port", "snmp_version", "snmp_community_mode", "snmp_community",
                 "snmp_v3_user", "snmp_v3_level", "snmp_v3_auth_proto", "snmp_v3_auth_pass",
                 "snmp_v3_priv_proto", "snmp_v3_priv_pass"):
        assert f'name="{name}"' in middle, name
    assert middle.index('name="snmp_port"') < middle.index('name="snmp_version"') < middle.index('name="snmp_community_mode"')
    assert "alarm-families" not in middle and "data-client-playrules" not in middle
    assert "data-asset-form-reserved" in middle
    assert right.index("data-alarm-families") < right.index("data-asset-form-rule") < right.index("data-client-playrules")
    assert 'name="alarms_present"' in right
    for version in ("v1", "v2c", "v3"):
        assert f'<option value="{version}"' in middle
    assert "ICMP / port / SNMP" in text and ">Port<" not in text
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".asset-form-reserved { flex: 1 1 auto; min-height: 12rem; }" in css
    assert "hr.asset-form-rule" in css
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v08-21" in base and "app.js?v=v08-21" in base
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert "[data-snmp-version]" in js and "[data-snmp-v3-level]" in js


def test_snmp_v2c_custom_community_saved_hidden_and_drives_exporter_auth(tmp_path):
    db = _db()
    client = _client()
    aid = f"{PREFIX}c2"
    data = _base(aid, snmp_version="v2c", snmp_community_mode="custom", snmp_community=SECRET_COMMUNITY)
    assert client.post("/assets", data=data, follow_redirects=False).status_code in {302, 303}
    row = _row(db, aid)
    assert row.extras["snmp_version"] == "v2c"
    assert row.extras["snmp_community_mode"] == "custom"
    assert row.extras["snmp_community"] == SECRET_COMMUNITY
    assert row.scrape_address == ""

    sd = {item["labels"]["asset"]: item for item in client.get("/api/v1/sd/snmp", headers=TOKEN).json()}
    assert sd[aid]["labels"]["snmp_auth"] == "public_v2"
    detail = client.get(f"/assets/{aid}").text
    assert "data-snmp-auth-pending" in detail and f"forgesre_{aid}" in detail

    assert client.get("/api/v1/sd/snmp-auths").status_code == 401
    auths = client.get("/api/v1/sd/snmp-auths", headers=TOKEN).json()["auths"]
    assert auths[f"forgesre_{aid}"] == {"version": 2, "community": SECRET_COMMUNITY}

    _write_rendered(tmp_path, [f"forgesre_{aid}"])
    sd = {item["labels"]["asset"]: item for item in client.get("/api/v1/sd/snmp", headers=TOKEN).json()}
    assert sd[aid]["labels"]["snmp_auth"] == f"forgesre_{aid}"
    detail = client.get(f"/assets/{aid}").text
    assert "data-snmp-auth-pending" not in detail
    assert "Custom community (saved, hidden)" in detail

    for page in (detail, client.get(f"/assets?edit={aid}").text, client.get("/assets").text, client.get(f"/assets?clone={aid}").text):
        assert SECRET_COMMUNITY not in page
    assert "saved — leave blank to keep" in client.get(f"/assets?edit={aid}").text
    api = client.get(f"/api/v1/assets/{aid}")
    assert api.status_code == 200 and SECRET_COMMUNITY not in api.text
    assert SECRET_COMMUNITY not in client.get("/api/v1/assets").text

    resp = client.post(
        f"/assets/{aid}/update",
        data={"hostname": aid, "ip": row.ip, "type": "Switch", "comms_present": "1",
              "snmp_version": "v2c", "snmp_community_mode": "custom", "snmp_community": ""},
        follow_redirects=False,
    )
    assert resp.status_code in {302, 303}
    assert _row(db, aid).extras["snmp_community"] == SECRET_COMMUNITY

    resp = client.post(
        f"/assets/{aid}/update",
        data={"hostname": aid, "ip": row.ip, "type": "Switch", "extras_present": "1", "extra_site": "DC9"},
        follow_redirects=False,
    )
    assert resp.status_code in {302, 303}
    kept = _row(db, aid).extras
    assert kept["site"] == "DC9" and kept["snmp_community"] == SECRET_COMMUNITY

    resp = client.post(
        f"/assets/{aid}/update",
        data={"hostname": aid, "ip": row.ip, "type": "Switch", "comms_present": "1", "snmp_version": "v2c", "snmp_community_mode": "default"},
        follow_redirects=False,
    )
    assert resp.status_code in {302, 303}
    back = _row(db, aid)
    assert "snmp_community" not in back.extras and desired_auth(back) == "public_v2"

    db.expire_all()
    for report in db.query(JournalEntry).all():
        assert SECRET_COMMUNITY not in f"{report.summary} {report.detail}"
    for entry in db.query(AuditLog).all():
        assert SECRET_COMMUNITY not in str(entry.data)
    assert SECRET_COMMUNITY not in client.get("/journal").text
    db.close()


def test_snmp_v1_default_and_v3_usm_saved(tmp_path):
    db = _db()
    client = _client()
    v1 = f"{PREFIX}v1"
    assert client.post("/assets", data=_base(v1, snmp_version="v1", snmp_community_mode="default"), follow_redirects=False).status_code in {302, 303}
    row = _row(db, v1)
    assert row.extras["snmp_version"] == "v1" and desired_auth(row) == "public_v1" and auth_spec(row) is None
    assert effective_auth(row, frozenset()) == "public_v2"
    assert effective_auth(row, frozenset({"public_v1"})) == "public_v1"

    v3 = f"{PREFIX}u3"
    data = _base(
        v3,
        snmp_version="v3",
        snmp_v3_user="monitor",
        snmp_v3_level="authPriv",
        snmp_v3_auth_proto="SHA256",
        snmp_v3_auth_pass=SECRET_AUTH,
        snmp_v3_priv_proto="AES256",
        snmp_v3_priv_pass=SECRET_PRIV,
    )
    assert client.post("/assets", data=data, follow_redirects=False).status_code in {302, 303}
    row = _row(db, v3)
    assert row.extras["snmp_v3_user"] == "monitor" and row.extras["snmp_v3_level"] == "authPriv"
    assert auth_spec(row) == {
        "version": 3,
        "username": "monitor",
        "security_level": "authPriv",
        "auth_protocol": "SHA256",
        "password": SECRET_AUTH,
        "priv_protocol": "AES256",
        "priv_password": SECRET_PRIV,
    }
    assert snmp_auths(db)[f"forgesre_{v3}"]["priv_password"] == SECRET_PRIV
    edit = client.get(f"/assets?edit={v3}").text
    detail = client.get(f"/assets/{v3}").text
    for page in (edit, detail):
        assert SECRET_AUTH not in page and SECRET_PRIV not in page
    assert '<option value="v3" selected>' in edit and 'value="monitor"' in edit
    assert "user monitor · authPriv · auth SHA256 · priv AES256" in " ".join(detail.split())

    resp = client.post(
        f"/assets/{v3}/update",
        data={"hostname": v3, "ip": row.ip, "type": "Switch", "comms_present": "1", "snmp_version": "v3",
              "snmp_v3_user": "monitor", "snmp_v3_level": "authNoPriv", "snmp_v3_auth_proto": "SHA"},
        follow_redirects=False,
    )
    assert resp.status_code in {302, 303}
    row = _row(db, v3)
    assert row.extras["snmp_v3_auth_pass"] == SECRET_AUTH and "snmp_v3_priv_pass" not in row.extras

    bad = client.post("/assets", data=_base(f"{PREFIX}bad", snmp_version="v3", snmp_v3_user=""), follow_redirects=False)
    assert bad.status_code == 400
    short = client.post(
        "/assets",
        data=_base(f"{PREFIX}short", snmp_version="v3", snmp_v3_user="u", snmp_v3_level="authNoPriv", snmp_v3_auth_pass="123"),
        follow_redirects=False,
    )
    assert short.status_code == 400
    nocomm = client.post("/assets", data=_base(f"{PREFIX}nocomm", snmp_community_mode="custom"), follow_redirects=False)
    assert nocomm.status_code == 400
    db.close()


def test_snmp_from_form_unit_rules():
    assert snmp_from_form("", None, version="v3") is None
    assert snmp_from_form("1", None) == {"snmp_version": "v2c", "snmp_community_mode": "default"}
    saved = {"snmp_version": "v1", "snmp_community_mode": "custom", "snmp_community": "old"}
    assert snmp_from_form("1", saved, version="v2c", community_mode="custom", community="")["snmp_community"] == "old"
    v3 = {"snmp_version": "v3", "snmp_v3_user": "u", "snmp_v3_level": "authNoPriv", "snmp_v3_auth_proto": "MD5", "snmp_v3_auth_pass": "12345678"}
    with pytest.raises(ValueError):
        snmp_from_form("1", saved, version="v3", v3_user="u", v3_level="authNoPriv")
    assert snmp_from_form("1", v3, version="v3", v3_user="u", v3_level="authNoPriv", v3_auth_proto="MD5")["snmp_v3_auth_pass"] == "12345678"


def test_empty_scrape_kept_for_nas_and_switch_with_snmp_settings():
    db = _db()
    client = _client()
    for aid, type_ in ((f"{PREFIX}nas", "QNAP/NAS"), (f"{PREFIX}sw", "Switch")):
        data = _base(aid, type_, snmp_version="v2c", snmp_community_mode="custom", snmp_community=SECRET_COMMUNITY)
        assert client.post("/assets", data=data, follow_redirects=False).status_code in {302, 303}
        assert _row(db, aid).scrape_address == ""
    targets = {item["labels"]["asset"] for item in sd_snmp_targets(db)}
    assert {f"{PREFIX}nas", f"{PREFIX}sw"} <= targets
    db.close()


def test_render_snmp_auths_splices_block_and_keeps_it_when_core_is_down(tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import render_snmp_auths as r

    template = (ROOT / "monitoring" / "snmp.yml.tpl").read_text().replace("__SNMP_COMMUNITY__", "public")
    target = tmp_path / "snmp.yml"
    target.write_text(template)
    auths = {"forgesre_avs-x": {"version": 3, "username": "mon", "security_level": "authNoPriv", "auth_protocol": "SHA", "password": 'p"w:#1234'}}
    monkeypatch.setattr(r, "fetch_auths", lambda port, token, timeout=5.0: auths)
    monkeypatch.setattr(r, "reload_exporter", lambda url="": True)
    status = r.apply(target, "8080", "tok")
    assert "1" in status and 'p"w' not in status
    parsed = yaml.safe_load(target.read_text())
    assert parsed["auths"]["forgesre_avs-x"]["password"] == 'p"w:#1234'
    assert parsed["auths"]["public_v2"] == {"version": 2, "community": "public"}
    assert parsed["auths"]["public_v1"] == {"version": 1, "community": "public"}
    assert "if_mib" in parsed["modules"]
    assert oct(os.stat(target).st_mode & 0o777) == "0o600"

    block = r.asset_block(target.read_text())
    target.write_text(template)
    monkeypatch.setattr(r, "fetch_auths", lambda port, token, timeout=5.0: None)
    status = r.apply(target, "8080", "tok", previous_block=block)
    assert "kept 1" in status
    assert "forgesre_avs-x" in yaml.safe_load(target.read_text())["auths"]


def test_cli_and_update_wire_snmp_auths():
    forgesre = (ROOT / "scripts" / "forgesre").read_text(encoding="utf-8")
    assert "snmp-auths)" in forgesre and "render-snmp-auths.sh" in forgesre
    assert "snmp-auths" in (ROOT / "scripts" / "forgesre-completion.bash").read_text(encoding="utf-8")
    update = (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    assert "render-snmp-auths.sh" in update and "install.sh" not in update.split("render-snmp-auths.sh", 1)[1].split("\n", 1)[0]
    render = (ROOT / "scripts" / "render-monitoring.sh").read_text(encoding="utf-8")
    assert "render_snmp_auths.apply" in render
    for name in ("render-snmp-auths.sh", "render-monitoring.sh", "update.sh"):
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)
    for name in ("snmp.yml", "snmp.yml.tpl"):
        text = (ROOT / "monitoring" / name).read_text(encoding="utf-8")
        assert "# forgesre-asset-auths begin" in text and "# forgesre-asset-auths end" in text
    parsed = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8"))
    assert set(parsed["auths"]) == {"public_v2", "public_v1"}
