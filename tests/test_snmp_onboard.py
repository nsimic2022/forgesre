"""Discovery SNMP devices: bounded GET, left menu, generated onboard splice. Secrets stay out of HTML."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Asset, AuditLog
from app.seed import seed
from app.snmp_onboard import (
    ONBOARD_BEGIN,
    ONBOARD_END,
    ONBOARD_MODULE,
    apply_onboard_module,
    cardinality_hint,
    collected_oids,
    encode_oid,
    identify_device,
    normalize_oid,
    onboard_module_yaml,
    parse_snmp_value,
    recommend_metrics,
    restore_onboard_module,
    snmp_get,
    snmp_get_packet,
    splice_onboard_module,
)

ROOT = Path(__file__).resolve().parents[1]
SECRET = "s3cr3t-comm-onboard"


def _client() -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.close()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _ber(tag: int, payload: bytes) -> bytes:
    return bytes([tag, len(payload)]) + payload


def _response(value: str, community: str = SECRET) -> bytes:
    oid = encode_oid("1.3.6.1.2.1.1.1.0")
    vb = _ber(0x30, oid + _ber(0x04, value.encode()))
    vbl = _ber(0x30, vb)
    pdu = _ber(0xA2, bytes([0x02, 0x01, 1, 0x02, 0x01, 0, 0x02, 0x01, 0]) + vbl)
    return _ber(0x30, bytes([0x02, 0x01, 1]) + _ber(0x04, community.encode()) + pdu)


def test_encode_oid_sysdescr():
    assert encode_oid("1.3.6.1.2.1.1.1.0") == bytes.fromhex("06082b06010201010100")
    assert normalize_oid(" .1.3.6.1.2.1.1.1.0 ") == "1.3.6.1.2.1.1.1.0"


def test_parse_skips_community_secret():
    packet = _response("lab-switch")
    assert parse_snmp_value(packet) == "lab-switch"
    assert SECRET not in parse_snmp_value(packet)


def test_snmp_get_packet_is_get_not_walk():
    packet = snmp_get_packet("1.3.6.1.2.1.1.1.0", "public", get_next=False)
    assert packet[0] == 0x30
    assert 0xA0 in packet
    assert 0xA1 not in packet
    nxt = snmp_get_packet("1.3.6.1.2.1.1.1.0", "public", get_next=True)
    assert 0xA1 in nxt


def test_snmp_get_uses_send_hook():
    got = snmp_get("10.1.1.1", "1.3.6.1.2.1.1.1.0", community=SECRET, send=lambda *_a: _response("core-sw"))
    assert got["ok"] is True
    assert got["value"] == "core-sw"
    assert SECRET not in got["value"]
    silent = snmp_get("10.1.1.1", "1.3.6.1.2.1.1.1.0", send=lambda *_a: None)
    assert silent["ok"] is False
    assert silent["error"] == "no SNMP reply"


def test_identify_and_recommend_with_mock(monkeypatch):
    answers = {
        "1.3.6.1.2.1.1.1.0": "IOS switch",
        "1.3.6.1.2.1.1.2.0": "1.3.6.1.4.1.9",
        "1.3.6.1.2.1.1.3.0": "12345",
        "1.3.6.1.2.1.1.5.0": "core-sw",
        "1.3.6.1.2.1.2.1.0": "48",
        "1.3.6.1.2.1.33.1.4.1.0": "3",
    }

    def fake(ip, oid, **kwargs):
        val = answers.get(oid, "")
        return {"ok": bool(val), "oid": oid, "error": "" if val else "no SNMP reply", "value": val}

    monkeypatch.setattr("app.snmp_onboard.snmp_get", fake)
    ident = identify_device("10.9.9.9")
    assert ident["ok"] and ident["sysDescr"] == "IOS switch" and ident["sysName"] == "core-sw"
    rows = recommend_metrics("10.9.9.9")
    by = {row["key"]: row for row in rows}
    assert by["ifNumber"]["ok"] and by["ifNumber"]["applyable"]
    assert by["ifNumber"]["in_mib"] is True
    assert by["ifOperStatus"]["in_prometheus"] is True
    assert by["ifOperStatus"]["recommend"] is False
    assert by["upsOutputSource"]["recommend"] is True
    assert by["upsOutputSource"]["applyable"] is True
    assert cardinality_hint("48", 1).startswith("ifNumber=48")


def test_collected_oids_from_committed_yml():
    oids = collected_oids(ROOT / "monitoring" / "snmp.yml")
    assert "1.3.6.1.2.1.1.3.0" in oids or "1.3.6.1.2.1.1.3" in oids
    assert any(oid.startswith("1.3.6.1.2.1.2.2.1.8") for oid in oids)


def test_committed_snmp_yml_keeps_two_modules_and_onboard_markers():
    yml = (ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8")
    tpl = (ROOT / "monitoring" / "snmp.yml.tpl").read_text(encoding="utf-8")
    assert tpl.replace("__SNMP_COMMUNITY__", "public") == yml
    assert ONBOARD_BEGIN in yml and ONBOARD_END in yml and ONBOARD_BEGIN in tpl
    parsed = yaml.safe_load(yml.replace("__SNMP_COMMUNITY__", "public"))
    assert set(parsed["modules"]) == {"if_mib", "host"}
    assert ONBOARD_MODULE not in parsed["modules"]


def test_splice_apply_restore_rollback(tmp_path):
    src = (ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8")
    target = tmp_path / "snmp.yml"
    target.write_text(src, encoding="utf-8")
    block = onboard_module_yaml(["1.3.6.1.2.1.2.1.0"])
    assert f"  {ONBOARD_MODULE}:" in block
    written = splice_onboard_module(src, block)
    parsed = yaml.safe_load(written)
    assert "if_mib" in parsed["modules"] and ONBOARD_MODULE in parsed["modules"]
    result = apply_onboard_module(["1.3.6.1.2.1.2.1.0"], path=target)
    assert result["ok"] is True
    assert ONBOARD_MODULE in yaml.safe_load(target.read_text())["modules"]
    restored = restore_onboard_module(path=target)
    assert restored["ok"] is True
    assert set(yaml.safe_load(target.read_text())["modules"]) == {"if_mib", "host"}


def test_apply_refuses_without_if_mib(tmp_path):
    target = tmp_path / "snmp.yml"
    target.write_text("modules:\n  other: {}\n" + ONBOARD_BEGIN + "\n" + ONBOARD_END + "\n", encoding="utf-8")
    result = apply_onboard_module(["1.3.6.1.2.1.2.1.0"], path=target)
    assert result["ok"] is False
    assert "if_mib" in result["error"]


def test_discovery_menu_and_snmp_page():
    client = _client()
    scan = client.get("/discovery")
    assert scan.status_code == 200
    assert 'class="discovery-shell"' in scan.text
    assert 'class="discovery-menu"' in scan.text
    assert 'href="/discovery/snmp"' in scan.text
    assert ">SNMP devices<" in scan.text
    assert 'href="/discovery#netbox"' in scan.text
    assert 'id="zabbix"' in scan.text
    assert 'action="/discovery/zabbix-sync"' in scan.text
    assert 'class="discovery-actions"' in scan.text
    page = client.get("/discovery/snmp")
    assert page.status_code == 200
    html = page.text
    assert ">SNMP devices<" in html
    assert "OID tester" in html
    assert "Recommendations" in html
    assert "Identify" in html
    assert 'name="community"' in html
    assert SECRET not in html
    assert f'value="{SECRET}"' not in html
    assert 'type="password"' in html
    assert "Apply extra module" in html
    assert "forgesre_onboard" in html
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    shell = css.split(".discovery-shell {", 1)[1].split("}", 1)[0]
    assert "11.5rem" in shell
    menu = css.split(".discovery-menu {", 1)[1].split("}", 1)[0]
    assert "sticky" in menu


def test_identify_post_does_not_echo_community(monkeypatch):
    def fake_ident(ip, **kwargs):
        assert kwargs.get("community") == SECRET
        return {
            "ok": True,
            "sysDescr": "lab",
            "sysName": "lab",
            "rows": [{"key": "sysDescr", "name": "sysDescr", "ok": True, "value": "lab", "error": ""}],
        }

    def fake_rec(ip, **kwargs):
        return [
            {
                "key": "ifNumber",
                "name": "ifNumber",
                "oid": "1.3.6.1.2.1.2.1.0",
                "ok": True,
                "value": "2",
                "error": "",
                "help": "Interface count (cardinality)",
                "kind": "scalar",
                "in_mib": True,
                "in_prometheus": False,
                "recommend": True,
                "applyable": True,
            }
        ]

    monkeypatch.setattr("app.web.identify_device", fake_ident)
    monkeypatch.setattr("app.web.recommend_metrics", fake_rec)
    client = _client()
    response = client.post(
        "/discovery/snmp",
        data={"action": "identify", "ip": "10.8.8.8", "port": "161", "version": "v2c", "community": SECRET, "timeout": "0.8"},
    )
    assert response.status_code == 200
    assert "lab" in response.text
    assert "ifNumber" in response.text
    assert SECRET not in response.text
    assert "recommend" in response.text


def test_probe_and_apply_generated_only(monkeypatch, tmp_path):
    src = (ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8")
    target = tmp_path / "snmp.yml"
    target.write_text(src, encoding="utf-8")
    monkeypatch.setenv("FORGESRE_GENERATED_DIR", str(tmp_path))
    monkeypatch.setattr("app.web.snmp_get", lambda ip, oid, **k: {"ok": True, "oid": oid, "error": "", "value": "48"})
    monkeypatch.setattr("app.web.reload_exporter", lambda *_a, **_k: True)
    client = _client()
    probe = client.post(
        "/discovery/snmp",
        data={"action": "probe", "ip": "10.8.8.8", "oid": "1.3.6.1.2.1.2.1.0", "version": "v2c", "community": SECRET},
    )
    assert probe.status_code == 200
    assert "48" in probe.text
    assert SECRET not in probe.text
    applied = client.post(
        "/discovery/snmp",
        data={
            "action": "apply",
            "ip": "10.8.8.8",
            "oid_pick": "1.3.6.1.2.1.2.1.0",
            "version": "v2c",
        },
    )
    assert applied.status_code == 200
    parsed = yaml.safe_load(target.read_text())
    assert ONBOARD_MODULE in parsed["modules"]
    assert "if_mib" in parsed["modules"]
    committed = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text())
    assert set(committed["modules"]) == {"if_mib", "host"}
    db = SessionLocal()
    assert db.query(AuditLog).filter_by(action="snmp.onboard.apply").count() >= 1
    db.close()


def test_snmp_page_requires_login():
    Base.metadata.create_all(bind=engine)
    guest = TestClient(app)
    response = guest.get("/discovery/snmp", follow_redirects=False)
    assert response.status_code in {302, 303}


def test_cache_pin_bumped():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v09-63" in base
    assert "app.js?v=v09-S" in base
