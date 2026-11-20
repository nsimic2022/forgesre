"""Add devices: SNMP identify, the add-then-verify jobs, and SNMP auths applied from Core."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Job, User
from app.security import hash_password
from app.seed import seed


def _client(role: str = "engineer") -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def _response(values: dict[str, bytes]) -> bytes:
    """A GetResponse built with the same BER helpers, for parse_response."""
    from app.snmp_get import _ber, _oid

    vbs = b"".join(_ber(0x30, _oid(oid) + raw) for oid, raw in values.items())
    pdu = _ber(0xA2, b"\x02\x01\x01\x02\x01\x00\x02\x01\x00" + _ber(0x30, vbs))
    return _ber(0x30, b"\x02\x01\x01" + _ber(0x04, b"public") + pdu)


def test_snmp_get_builds_and_parses_system_values():
    from app.snmp_get import _ber, _oid, get_packet, guess_type, parse_response

    packet = get_packet("s3cret", ["1.3.6.1.2.1.1.5.0"])
    assert packet[0] == 0x30 and b"s3cret" in packet
    data = _response({
        "1.3.6.1.2.1.1.1.0": _ber(0x04, b"Cisco IOS Software, Catalyst 2960 Switch"),
        "1.3.6.1.2.1.1.2.0": _oid("1.3.6.1.4.1.9.1.1208"),
        "1.3.6.1.2.1.1.5.0": _ber(0x04, b"sw-acc-07.dc.local"),
    })
    values = parse_response(data)
    assert values["1.3.6.1.2.1.1.5.0"] == "sw-acc-07.dc.local"
    assert values["1.3.6.1.2.1.1.2.0"] == "1.3.6.1.4.1.9.1.1208"
    assert guess_type(values["1.3.6.1.2.1.1.1.0"]) == ("Cisco", "Switch")
    assert guess_type("FortiGate-60F v7.2") == ("Fortinet", "Firewall")


def test_identify_uses_the_given_community_and_suggests_a_switch(monkeypatch):
    import discovery

    from app import onboarding, snmp_get

    monkeypatch.setattr(discovery, "probe_host", lambda ip, timeout=0.5: {"open_ports": [22, 161], "snmp_ok": True, "proposed_role": "Possible network device", "alive": True, "exporter_kind": "network"})
    seen = {}

    def fake_info(ip, community, version="v2c", port=161, timeout=1.5):
        seen.update(community=community, version=version, port=port)
        return {"sys_descr": "Cisco IOS Software, Catalyst 9300 Switch", "sys_object_id": "1.3.6.1.4.1.9", "sys_name": "sw-core-01.dc.local", "sys_location": "DC1 row A"}

    monkeypatch.setattr(snmp_get, "system_info", fake_info)
    out = onboarding.identify("10.0.0.7", {"snmp_version": "v2c", "snmp_community_mode": "custom", "snmp_community": "noc-ro"}, 1161)
    assert seen == {"community": "noc-ro", "version": "v2c", "port": 1161}
    assert out["suggested"]["hostname"] == "sw-core-01" and out["suggested"]["type"] == "Switch"
    assert out["suggested"]["vendor"] == "Cisco" and out["suggested"]["location"] == "DC1 row A"
    assert "snmp_exporter" in out["watch"]


def test_identify_job_keeps_no_snmp_secret_and_returns_the_result(monkeypatch):
    from app import onboarding
    from app.jobs import run_pending_jobs

    client = _client()
    monkeypatch.setattr(onboarding, "identify", lambda ip, snmp, port: {"ip": ip, "got": sorted(snmp)})
    assert client.post("/api/v1/onboard/identify", json={"ip": "nope"}).status_code == 400
    job_id = client.post("/api/v1/onboard/identify", json={"ip": "10.1.2.3", "snmp": {"snmp_version": "v2c", "snmp_community_mode": "custom", "snmp_community": "topsecret"}}).json()["job"]
    db = SessionLocal()
    run_pending_jobs(db, limit=10)
    row = db.get(Job, job_id)
    assert row.status == "done" and "topsecret" not in str(row.payload)
    db.close()
    data = client.get(f"/api/v1/onboard/jobs/{job_id}").json()
    assert data["result"] == {"ip": "10.1.2.3", "got": ["snmp_community", "snmp_community_mode", "snmp_version"]}
    assert _client("analyst").post("/api/v1/onboard/identify", json={"ip": "10.1.2.3"}).status_code == 403


def test_checklist_orders_steps_and_explains_failures():
    from app.onboarding import checklist

    report = {"asset_id": "a1", "overall": "FAIL", "overall_reason": "x",
              "icmp": {"ok": True, "detail": "0.4 ms"}, "port": {"ok": False, "detail": "9100 refused"},
              "prom": {"ok": None, "detail": "not yet"}, "core": {"ok": None, "detail": ""}}
    steps = checklist(report)["steps"]
    assert [s["key"] for s in steps][:3] == ["icmp", "port", "family"]
    assert steps[0]["state"] == "ok" and steps[0]["fix"] == ""
    assert steps[1]["state"] == "fail" and "node_exporter" in steps[1]["fix"]
    assert next(s for s in steps if s["key"] == "prom")["state"] == "wait"
    assert next(s for s in steps if s["key"] == "core")["state"] == "skip"


def test_snmp_auths_are_written_by_core_and_the_exporter_reloads(tmp_path: Path, monkeypatch):
    from app import snmp_apply

    path = tmp_path / "snmp.yml"
    path.write_text("auths:\n  public_v2:\n    version: 2\n" + snmp_apply.BEGIN + " (do not edit)\n" + snmp_apply.END + "\nmodules: {}\n", encoding="utf-8")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    aid = f"sw-{uuid4().hex[:6]}"
    db.add(Asset(asset_id=aid, hostname=aid, ip="10.77.0.1", type="Switch",
                 extras={"snmp_version": "v2c", "snmp_community_mode": "custom", "snmp_community": "x'y: z"}))
    db.commit()
    reloads = []
    monkeypatch.setattr(snmp_apply, "reload_exporter", lambda url=snmp_apply.RELOAD_URL: reloads.append(url) or True)
    result = snmp_apply.apply_auths(db, path)
    text = path.read_text(encoding="utf-8")
    assert result["status"] == "ok" and result["reloaded"] and reloads
    assert f"forgesre_{aid}" in text and "modules: {}" in text
    import yaml

    assert yaml.safe_load(text)["auths"][f"forgesre_{aid}"]["community"] == "x'y: z"
    assert snmp_apply.apply_auths(db, path)["status"] == "unchanged"
    broken = tmp_path / "plain.yml"
    broken.write_text("auths: {}\n", encoding="utf-8")
    assert "render-monitoring" in snmp_apply.apply_auths(db, broken)["why"]
    db.close()


def test_saving_a_custom_community_queues_snmp_apply(monkeypatch):
    client = _client()
    db = SessionLocal()
    for job in db.query(Job).filter(Job.kind == "snmp_apply", Job.status == "pending").all():
        db.delete(job)
    db.commit()
    aid = f"fw-{uuid4().hex[:6]}"
    created = client.post("/api/v1/assets", json={"hostname": aid, "asset_id": aid, "ip": "10.78.0.1", "type": "Firewall",
                                                  "extras": {"snmp_version": "v2c", "snmp_community_mode": "custom", "snmp_community": "fw-ro"}})
    assert created.status_code == 200, created.text
    assert db.query(Job).filter_by(kind="snmp_apply", status="pending").count() == 1
    verify = client.post(f"/api/v1/assets/{aid}/verify-job").json()
    assert verify["job"] and db.get(Job, verify["job"]).kind == "asset_verify"
    db.close()
