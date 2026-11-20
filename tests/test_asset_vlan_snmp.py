"""Asset VLAN field + filter, per-asset SNMP version / community / v3 USM, the five-column Add/Edit
layout (Ports, Metrics, Alarms, Escalation), and the
snmp_exporter auth render path. Secrets never reach GET HTML, the asset API, journal, or audit."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from app.asset_snmp import dump_auth_block, snmp_from_form, write_auths_yaml
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Asset
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
    found = re.findall(r'/assets\?edit=(' + re.escape(PREFIX) + r'[^"&]+)', table)
    return set(found)


def test_snmp_from_form_unit_rules():
    assert snmp_from_form("", None, version="v3") is None
    assert snmp_from_form("1", None) == {"snmp_version": "v2c", "snmp_community_mode": "default"}
    saved = {"snmp_version": "v1", "snmp_community_mode": "custom", "snmp_community": "old"}
    assert snmp_from_form("1", saved, version="v2c", community_mode="custom", community="")["snmp_community"] == "old"
    v3 = {"snmp_version": "v3", "snmp_v3_user": "u", "snmp_v3_level": "authNoPriv", "snmp_v3_auth_proto": "MD5", "snmp_v3_auth_pass": "12345678"}
    with pytest.raises(ValueError):
        snmp_from_form("1", saved, version="v3", v3_user="u", v3_level="authNoPriv")
    assert snmp_from_form("1", v3, version="v3", v3_user="u", v3_level="authNoPriv", v3_auth_proto="MD5")["snmp_v3_auth_pass"] == "12345678"


def test_dump_auth_block_safe_dumps_v3_special_chars(tmp_path):
    secret = 'p"w:#1234 and : colon'
    block = dump_auth_block(
        {
            "forgesre_avs-x": {
                "version": 3,
                "username": "mon",
                "security_level": "authPriv",
                "auth_protocol": "SHA",
                "password": secret,
                "priv_protocol": "AES",
                "priv_password": secret,
            }
        }
    )
    parsed = yaml.safe_load("auths:\n" + block)
    assert parsed["auths"]["forgesre_avs-x"]["password"] == secret
    assert parsed["auths"]["forgesre_avs-x"]["priv_password"] == secret
    assert "{{" not in block and "%s" not in block
    dest = tmp_path / "snippet.yml"
    write_auths_yaml(dest, parsed["auths"])
    again = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert again["auths"]["forgesre_avs-x"]["password"] == secret


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
