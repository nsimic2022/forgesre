"""One inventory, many feeders: match by serial / MAC / name / IP, field ownership, conflicts for review."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.asset_sources import SourceRecord, build_index, match, reconcile
from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, AssetConflict, AssetIdentity, DiscoveryCandidate, User
from app.security import hash_password
from app.seed import seed


def _tag() -> str:
    # letters only: other tests search the inventory by asset number
    return "".join(chr(97 + int(c, 16) % 26) for c in uuid4().hex[:6])


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _drop(db, *ids: str) -> None:
    for asset_id in ids:
        row = db.query(Asset).filter_by(asset_id=asset_id).first()
        if row is not None:
            db.query(AssetIdentity).filter_by(asset_id=row.id).delete()
            db.query(AssetConflict).filter_by(asset_id=row.id).delete()
            db.delete(row)
    db.commit()


def test_serial_from_librenms_lets_glpi_match_a_renamed_device():
    db = _db()
    t = _tag()
    sw = Asset(asset_id=f"sw-{t}", hostname=f"sw-{t}", ip="198.51.100.10", type="Switch")
    db.add(sw)
    db.commit()
    try:
        index = build_index(db)
        lib = SourceRecord(source="librenms", source_id="17", name="198.51.100.10", sys_name=f"sw-{t}", ip="198.51.100.10", serial="fox 1234 abcd")
        asset, why = match(index, lib)
        assert asset.id == sw.id and why == ""
        reconcile(db, index, asset, lib)
        db.commit()
        # GLPI knows it under another name and no IP: the serial still finds it
        glpi = SourceRecord(source="glpi", source_id="NetworkEquipment:5", name=f"core-{t}", serial="FOX1234ABCD", fields={"site": "DC1"})
        asset, why = match(build_index(db), glpi)
        assert asset.id == sw.id
        out = reconcile(db, build_index(db), asset, glpi)
        db.commit()
        assert out == {"filled": 1, "updated": 0, "conflicts": 0}
        assert sw.extras["site"] == "DC1" and sw.glpi_id == "NetworkEquipment:5"
    finally:
        _drop(db, f"sw-{t}")
        db.close()


def test_owned_fields_follow_the_source_until_a_person_changes_them():
    db = _db()
    t = _tag()
    srv = Asset(asset_id=f"srv-{t}", hostname=f"srv-{t}", ip="198.51.100.20", type="Linux Server")
    db.add(srv)
    db.commit()
    try:
        rec = SourceRecord(source="glpi", source_id=f"Computer:{t}", name=f"srv-{t}", ip="198.51.100.20", fields={"site": "DC1", "customer": "Acme"})
        reconcile(db, build_index(db), srv, rec)
        rec.fields = {"site": "DC2", "customer": "Acme"}  # moved in GLPI: GLPI owns site, nobody changed it here
        assert reconcile(db, build_index(db), srv, rec)["updated"] == 1 and srv.extras["site"] == "DC2"
        srv.extras = {**srv.extras, "site": "DC2 room B"}  # a person edits it
        rec.fields = {"site": "DC3"}
        assert reconcile(db, build_index(db), srv, rec)["conflicts"] == 1 and srv.extras["site"] == "DC2 room B"
        rec.ip = "198.51.100.99"  # a different IP is never applied
        reconcile(db, build_index(db), srv, rec)
        db.commit()
        assert srv.ip == "198.51.100.20"
        kinds = {(c.kind, c.field) for c in db.query(AssetConflict).filter_by(asset_id=srv.id, status="open")}
        assert kinds == {("field", "site"), ("ip", "ip")}
        # Zabbix does not own site: its different value is a conflict, not an overwrite
        zrec = SourceRecord(source="zabbix", source_id="9001", name=f"srv-{t}", fields={"customer": "Other"})
        assert reconcile(db, build_index(db), srv, zrec)["conflicts"] == 1 and srv.extras["customer"] == "Acme"
        db.commit()
    finally:
        _drop(db, f"srv-{t}")
        db.close()


def test_two_keys_two_assets_is_a_match_conflict_never_a_merge():
    db = _db()
    t = _tag()
    a = Asset(asset_id=f"aa-{t}", hostname=f"aa-{t}", ip="198.51.100.30", type="Linux Server")
    b = Asset(asset_id=f"bb-{t}", hostname=f"bb-{t}", ip="198.51.100.31", type="Linux Server")
    db.add_all([a, b])
    db.commit()
    try:
        rec = SourceRecord(source="netbox", source_id="77", name=f"aa-{t}", ip="198.51.100.31")
        asset, why = match(build_index(db), rec)
        assert asset.id == a.id and f"bb-{t}" in why
        reconcile(db, build_index(db), asset, rec, disagreement=why)
        db.commit()
        assert db.query(AssetConflict).filter_by(asset_id=a.id, kind="match", status="open").count() == 1
        assert a.ip == "198.51.100.30"
    finally:
        _drop(db, f"aa-{t}", f"bb-{t}")
        db.close()


def test_review_mode_lists_new_glpi_devices_in_discovery(monkeypatch):
    from app.glpi_sync import sync_glpi

    db = _db()
    t = _tag()
    monkeypatch.setattr("app.settings.Settings.glpi_enabled", True)
    monkeypatch.setattr("app.settings.Settings.inventory_new_from_sources", "review")
    monkeypatch.setattr("app.glpi_sync.list_inventory", lambda *a, **k: [
        {"glpi_id": f"Computer:{t}", "itemtype": "Computer", "name": f"new-{t}", "ip": "198.51.100.40", "serial": "", "location": "DC1"}])
    try:
        result = sync_glpi(db, force=True)
        assert result["held"] == 1 and result["created"] == 0
        assert db.query(Asset).filter_by(hostname=f"new-{t}").count() == 0
        row = db.query(DiscoveryCandidate).filter_by(ip="198.51.100.40").one()
        assert row.source == "glpi" and row.hostname == f"new-{t}"
        row.status = "ignored"
        db.commit()
        assert sync_glpi(db, force=True)["held"] == 1
        assert db.query(DiscoveryCandidate).filter_by(ip="198.51.100.40").one().status == "ignored"
    finally:
        db.query(DiscoveryCandidate).filter_by(ip="198.51.100.40").delete()
        db.commit()
        db.close()


def test_conflicts_api_keeps_ours_or_takes_theirs():
    db = _db()
    t = _tag()
    srv = Asset(asset_id=f"api-{t}", hostname=f"api-{t}", ip="198.51.100.50", type="Linux Server", extras={"site": "Mine"})
    db.add(srv)
    db.commit()
    reconcile(db, build_index(db), srv, SourceRecord(source="zabbix", source_id="5", name=f"api-{t}", fields={"site": "Theirs"}))
    reconcile(db, build_index(db), srv, SourceRecord(source="zabbix", source_id="5", name=f"api-{t}", ip="198.51.100.51"))
    db.commit()
    email = f"eng-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name="e", password_hash=hash_password("pw"), role="engineer"))
    db.commit()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    try:
        data = client.get("/api/v1/inventory/sources").json()
        mine = [c for c in data["conflicts"] if c["asset_id"] == f"api-{t}"]
        assert {c["field"] for c in mine} == {"Site", "IP"}
        assert {s["source"] for s in data["sources"]} == {"glpi", "zabbix", "netbox", "librenms"}
        site = next(c for c in mine if c["field"] == "Site")
        ip = next(c for c in mine if c["field"] == "IP")
        assert client.post(f"/api/v1/inventory/conflicts/{site['id']}", json={"action": "theirs"}).json()["status"] == "theirs"
        assert client.post(f"/api/v1/inventory/conflicts/{ip['id']}", json={"action": "ours"}).json()["status"] == "ours"
        assert client.post(f"/api/v1/inventory/conflicts/{ip['id']}", json={"action": "theirs"}).status_code == 400
        db.expire_all()
        fresh = db.query(Asset).filter_by(asset_id=f"api-{t}").one()
        assert fresh.extras["site"] == "Theirs" and fresh.ip == "198.51.100.50"
        # the same IP from Zabbix again stays decided
        reconcile(db, build_index(db), fresh, SourceRecord(source="zabbix", source_id="5", name=f"api-{t}", ip="198.51.100.51"))
        db.commit()
        assert db.query(AssetConflict).filter_by(asset_id=fresh.id, status="open").count() == 0
    finally:
        _drop(db, f"api-{t}")
        db.close()
