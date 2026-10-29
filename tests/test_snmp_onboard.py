"""Discovery SNMP devices: bounded GET below Add asset, generated onboard splice. Secrets stay out of HTML."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Asset, AuditLog
from app.seed import seed
from app.asset_extras import merge_extras, normalize_extras
from app.inventory import sd_snmp_onboard_targets, sd_snmp_targets
from app.snmp_onboard import (
    ONBOARD_BEGIN,
    ONBOARD_END,
    ONBOARD_JOB,
    ONBOARD_MODULE,
    SNMP_ONBOARD_OIDS_KEY,
    apply_onboard_module,
    cardinality_hint,
    collected_oids,
    encode_oid,
    identify_device,
    normalize_oid,
    onboard_module_yaml,
    parse_snmp_value,
    profile_for,
    recommend_metrics,
    restore_onboard_module,
    search_mib_object_types,
    snmp_get,
    snmp_get_packet,
    splice_onboard_module,
    table_index_label,
    vendor_from_sysobjectid,
    write_generator_yml,
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
    assert ident["vendor"] == "Cisco"
    rows = recommend_metrics("10.9.9.9", vendor="Cisco", sys_descr="IOS switch")
    by = {row["key"]: row for row in rows}
    assert by["ifNumber"]["ok"] and by["ifNumber"]["applyable"]
    assert by["ifNumber"]["in_mib"] is True
    assert by["ifNumber"]["profile"] == "switch" and by["ifNumber"]["profile_match"] is True
    assert by["ifOperStatus"]["in_prometheus"] is True
    assert by["ifOperStatus"]["recommend"] is False
    assert by["upsOutputSource"]["recommend"] is True
    assert by["upsOutputSource"]["applyable"] is True
    assert "ifInDiscards" in by and "ifHighSpeed" in by and "upsBatteryStatus" in by
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


def test_discovery_keeps_cubes_and_snmp_below_add_asset():
    client = _client()
    scan = client.get("/discovery")
    assert scan.status_code == 200
    html = scan.text
    assert "discovery-shell" not in html
    assert "discovery-menu" not in html
    assert 'class="discovery-actions"' in html
    assert ">Scan now<" in html and ">Zabbix<" in html and ">NetBox sync<" in html and ">GLPI<" in html
    assert ">Add asset<" in html
    assert 'class="discovery-snmp-break dash-graphs-spacer"' in html
    assert html.index(">Add asset<") < html.index('class="discovery-snmp-break') < html.index(">SNMP devices<")
    assert "OID tester" in html
    assert "Recommendations" in html
    assert "Identify" in html
    assert 'name="community"' in html
    assert SECRET not in html
    assert f'value="{SECRET}"' not in html
    assert 'type="password"' in html
    assert "Apply extra module" in html
    assert "forgesre_onboard" in html
    assert "forgesre-snmp-onboard" in html
    assert "MIB upload" in html
    assert "Upload MIB" in html
    assert "walk+indexes" in html
    bounced = client.get("/discovery/snmp", follow_redirects=False)
    assert bounced.status_code in {302, 303}
    assert bounced.headers.get("location", "").endswith("/discovery#snmp")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".discovery-shell {" not in css
    assert ".discovery-menu {" not in css
    break_css = css.split(".discovery-snmp-break {", 1)[1].split("}", 1)[0]
    assert "2.6em" in break_css
    assert "3px solid var(--line)" in break_css
    spacer = css.split(".dash-graphs-spacer {", 1)[1].split("}", 1)[0]
    assert "2.6em" in spacer


def test_identify_post_does_not_echo_community(monkeypatch):
    def fake_discover(ip, **kwargs):
        assert kwargs.get("community") == SECRET
        return {
            "ok": True,
            "hostname": ip,
            "added": True,
            "disabled_polling": True,
            "device": {"hostname": ip, "sysDescr": "lab", "sysName": "lab", "os": "ios", "hardware": "C9300", "vendor": "Cisco"},
            "ports": [{"ifIndex": 1, "ifOperStatus": "up"}],
            "processors": [],
            "mempools": [],
            "storage": [],
            "temperatures": [],
        }

    monkeypatch.setattr("app.librenms.discover_device", fake_discover)
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
    assert ">Scan now<" in response.text and ">Add asset<" in response.text
    assert "LibreNMS" in response.text


def test_probe_and_apply_generated_only(monkeypatch, tmp_path):
    src = (ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8")
    target = tmp_path / "snmp.yml"
    target.write_text(src, encoding="utf-8")
    monkeypatch.setenv("FORGESRE_GENERATED_DIR", str(tmp_path))
    monkeypatch.setattr("app.web.snmp_get", lambda ip, oid, **k: {"ok": True, "oid": oid, "error": "", "value": "48"})
    monkeypatch.setattr("app.web.reload_exporter", lambda *_a, **_k: True)
    monkeypatch.setattr("app.web.run_snmp_generator", lambda **_k: {"ok": False, "ran": False, "error": "skipped"})
    client = _client()
    db = SessionLocal()
    db.add(Asset(asset_id="onb-sw-1", hostname="onb-sw-1", ip="10.8.8.8", type="Switch"))
    db.commit()
    db.close()
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
            "asset_id": "onb-sw-1",
            "ip": "10.8.8.8",
            "oid_pick": ["1.3.6.1.2.1.2.1.0", "1.3.6.1.2.1.2.2.1.13"],
            "version": "v2c",
        },
    )
    assert applied.status_code == 200
    assert SECRET not in applied.text
    parsed = yaml.safe_load(target.read_text())
    assert ONBOARD_MODULE in parsed["modules"]
    assert "if_mib" in parsed["modules"]
    onboard = parsed["modules"][ONBOARD_MODULE]
    assert "1.3.6.1.2.1.2.1.0" in onboard.get("get", [])
    assert "1.3.6.1.2.1.2.2.1.13" in onboard.get("walk", [])
    committed = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text())
    assert set(committed["modules"]) == {"if_mib", "host"}
    db = SessionLocal()
    assert db.query(AuditLog).filter_by(action="snmp.onboard.apply").count() >= 1
    row = db.query(Asset).filter_by(asset_id="onb-sw-1").one()
    assert "1.3.6.1.2.1.2.1.0" in (row.extras or {}).get(SNMP_ONBOARD_OIDS_KEY, "")
    db.close()
    gen = yaml.safe_load((tmp_path / "generator.yml").read_text())
    assert "forgesre_onboard" in gen["modules"]


def test_apply_without_asset_stays_in_gui(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGESRE_GENERATED_DIR", str(tmp_path))
    (tmp_path / "snmp.yml").write_text((ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8"), encoding="utf-8")
    client = _client()
    bounced = client.post(
        "/discovery/snmp",
        data={"action": "apply", "ip": "10.8.8.9", "oid_pick": "1.3.6.1.2.1.2.1.0", "version": "v2c"},
    )
    assert bounced.status_code == 200
    assert "Pick an asset" in bounced.text
    assert ">Add asset<" in bounced.text


def test_snmp_page_requires_login():
    Base.metadata.create_all(bind=engine)
    guest = TestClient(app)
    response = guest.get("/discovery/snmp", follow_redirects=False)
    assert response.status_code in {302, 303}


def test_cache_pin_bumped():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v09-64" in base
    assert "app.js?v=v09-S" in base


def test_vendor_profile_and_table_indexes():
    assert vendor_from_sysobjectid("1.3.6.1.4.1.9.1.123") == "Cisco"
    assert vendor_from_sysobjectid("1.3.6.1.4.1.318.1.1") == "APC"
    assert vendor_from_sysobjectid("1.3.6.1.4.1.8072.3.2.10") == "net-snmp"
    assert vendor_from_sysobjectid("1.3.6.1.2.1.1.2.0") == ""
    assert profile_for(vendor="Cisco", sys_descr="IOS", answered_keys={"ifNumber"}) == "switch"
    assert profile_for(vendor="APC", sys_descr="Smart-UPS", answered_keys={"upsBatteryStatus"}) == "ups"
    assert profile_for(vendor="net-snmp", sys_descr="Linux box", answered_keys={"ssCpuIdle"}) == "linux-host"
    assert table_index_label("1.3.6.1.2.1.2.2.1.13") == "ifIndex"
    assert table_index_label("1.3.6.1.2.1.25.3.3.1.2") == "hrDeviceIndex"
    assert table_index_label("1.3.6.1.2.1.25.2.3.1.6") == "hrStorageIndex"
    assert table_index_label("1.3.6.1.2.1.99.1.1.1.4") == "entPhySensorIndex"
    assert table_index_label("1.3.6.1.4.1.9.9.1.1") == "index"


def test_onboard_module_yaml_tables_and_scalars():
    block = onboard_module_yaml(["1.3.6.1.2.1.2.1.0", "1.3.6.1.2.1.2.2.1.13", "1.3.6.1.2.1.2.2.1.13"])
    parsed = yaml.safe_load("modules:\n" + block)
    mod = parsed["modules"][ONBOARD_MODULE]
    assert mod["get"] == ["1.3.6.1.2.1.2.1.0"]
    assert mod["walk"] == ["1.3.6.1.2.1.2.2.1.13"]
    names = {m["name"]: m for m in mod["metrics"]}
    assert names["ifNumber"]["oid"] == "1.3.6.1.2.1.2.1"
    assert names["ifNumber"].get("indexes") is None
    assert names["ifInDiscards"]["indexes"][0]["labelname"] == "ifIndex"
    assert names["ifInDiscards"]["type"] == "counter"


def test_onboard_oids_survive_asset_save():
    stored = normalize_extras({"site": "DC1", "bogus": "x", SNMP_ONBOARD_OIDS_KEY: "1.3.6.1.2.1.2.1.0"})
    assert stored["site"] == "DC1"
    assert stored[SNMP_ONBOARD_OIDS_KEY] == "1.3.6.1.2.1.2.1.0"
    assert "bogus" not in stored
    merged = merge_extras(stored, {"site": "DC2"}, None)
    assert merged["site"] == "DC2"
    assert merged[SNMP_ONBOARD_OIDS_KEY] == "1.3.6.1.2.1.2.1.0"
    cleared = merge_extras(stored, {"site": "DC3", SNMP_ONBOARD_OIDS_KEY: ""}, None)
    assert SNMP_ONBOARD_OIDS_KEY not in cleared


def test_sd_onboard_only_attached_assets(tmp_path, monkeypatch):
    src = (ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8")
    target = tmp_path / "snmp.yml"
    target.write_text(splice_onboard_module(src, onboard_module_yaml(["1.3.6.1.2.1.2.1.0"])), encoding="utf-8")
    monkeypatch.setenv("FORGESRE_GENERATED_DIR", str(tmp_path))
    from app.asset_snmp import _rendered_cache

    _rendered_cache.update(key=None, names=frozenset())
    client = _client()
    db = SessionLocal()
    db.add_all(
        [
            Asset(
                asset_id="onb-sd-yes",
                hostname="onb-sd-yes",
                ip="10.8.8.11",
                type="Switch",
                extras={SNMP_ONBOARD_OIDS_KEY: "1.3.6.1.2.1.2.1.0"},
            ),
            Asset(asset_id="onb-sd-no", hostname="onb-sd-no", ip="10.8.8.12", type="Switch"),
        ]
    )
    db.commit()
    onboard = {row["labels"]["asset"]: row for row in sd_snmp_onboard_targets(db)}
    if_mib = {row["labels"]["asset"]: row for row in sd_snmp_targets(db)}
    assert onboard["onb-sd-yes"]["labels"]["snmp_module"] == ONBOARD_MODULE
    assert onboard["onb-sd-yes"]["targets"] == ["10.8.8.11"]
    assert "onb-sd-no" not in onboard
    assert "onb-sd-yes" in if_mib and "onb-sd-no" in if_mib
    db.close()
    token = {"Authorization": "Bearer forgesre-dev-webhook-token"}
    assert client.get("/api/v1/sd/snmp-onboard").status_code == 401
    body = client.get("/api/v1/sd/snmp-onboard", headers=token).json()
    ids = {row["labels"]["asset"] for row in body}
    assert "onb-sd-yes" in ids
    assert "onb-sd-no" not in ids


def test_prometheus_onboard_job_bundled():
    prom = (ROOT / "monitoring" / "prometheus.yml").read_text(encoding="utf-8")
    tpl = (ROOT / "monitoring" / "prometheus.yml.tpl").read_text(encoding="utf-8")
    for text in (prom, tpl):
        assert "job_name: forgesre-snmp-onboard" in text
        assert "/api/v1/sd/snmp-onboard" in text
        assert "module: [forgesre_onboard]" in text
        assert "scrape_interval: 60s" in text
    assert ONBOARD_JOB == "forgesre-snmp-onboard"
    committed = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text())
    assert set(committed["modules"]) == {"if_mib", "host"}


def test_mib_object_type_regex_and_upload(monkeypatch, tmp_path):
    text = (
        "IF-MIB DEFINITIONS ::= BEGIN\n"
        "ifInDiscards OBJECT-TYPE\n"
        "    SYNTAX Counter32\n"
        "    ::= { ifEntry 13 }\n"
        "ifOutErrors OBJECT-TYPE\n"
        "    SYNTAX Counter32\n"
        "    ::= { ifEntry 20 }\n"
        "END\n"
    )
    assert search_mib_object_types(text) == ["ifInDiscards", "ifOutErrors"]
    monkeypatch.setenv("FORGESRE_GENERATED_DIR", str(tmp_path))
    monkeypatch.setattr("app.web.run_snmp_generator", lambda **_k: {"ok": False, "ran": False, "error": "image skipped"})
    client = _client()
    uploaded = client.post(
        "/discovery/snmp/mib",
        data={"ip": "10.8.8.8", "version": "v2c"},
        files={"mib_file": ("IF-MIB.txt", text.encode(), "text/plain")},
    )
    assert uploaded.status_code == 200
    assert SECRET not in uploaded.text
    assert "ifInDiscards" in uploaded.text
    assert (tmp_path / "mibs" / "IF-MIB.txt").is_file()
    assert (tmp_path / "generator.yml").is_file()
    db = SessionLocal()
    assert db.query(AuditLog).filter_by(action="snmp.onboard.mib").count() >= 1
    db.close()


def test_scrape_includes_promql_count(monkeypatch):
    monkeypatch.setattr(
        "app.web.exporter_scrape",
        lambda *a, **k: {"ok": True, "error": "", "body": "ifInDiscards 1", "module": ONBOARD_MODULE, "auth": "public_v2", "target": "10.8.8.8"},
    )
    monkeypatch.setattr(
        "app.web.prometheus_onboard_status",
        lambda *a, **k: {"ok": True, "series": 4, "up": 1, "query": 'count({job="forgesre-snmp-onboard"})', "error": ""},
    )
    client = _client()
    response = client.post("/discovery/snmp", data={"action": "scrape", "ip": "10.8.8.8", "version": "v2c"})
    assert response.status_code == 200
    assert "4 series" in response.text
    assert "forgesre-snmp-onboard" in response.text
    assert SECRET not in response.text


def test_write_generator_yml_numeric_oids(tmp_path):
    path = write_generator_yml(["1.3.6.1.2.1.2.1.0", "1.3.6.1.2.1.2.2.1.13"], path=tmp_path / "generator.yml")
    parsed = yaml.safe_load(path.read_text())
    assert parsed["modules"]["forgesre_onboard"]["walk"] == ["1.3.6.1.2.1.2.1", "1.3.6.1.2.1.2.2.1.13"]
