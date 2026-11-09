"""Alert-to-asset match: Zabbix host id, exact name, case-insensitive name, IP fallback.

T1–T12 from the post-#133 review. match_alert_asset only — no ingest, no schema change.
"""

from __future__ import annotations

from uuid import uuid4

from app.inventory import create_manual_asset
from app.models import Asset
from app.seed import DEMO_ASSET, seed
from app.services import match_alert_asset


def _db():
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _ip() -> str:
    n = uuid4().int
    return f"10.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0x7F or 1}"


def _host(db, hostname: str, *, ip: str | None = None, zabbix_hostid: str = "") -> Asset:
    row = create_manual_asset(
        db,
        hostname=hostname,
        ip=ip or _ip(),
        type="Linux Server",
        actor="tester",
    )
    if zabbix_hostid:
        row.zabbix_hostid = zabbix_hostid
        db.commit()
        db.refresh(row)
    return row


def test_t1_zabbix_hostid_wins_over_name_and_ip():
    db = _db()
    hid_a, hid_b = f"h{uuid4().hex[:10]}", f"h{uuid4().hex[:10]}"
    first = _host(db, f"edge-a-{uuid4().hex[:6]}", zabbix_hostid=hid_a)
    second = _host(db, f"edge-b-{uuid4().hex[:6]}", zabbix_hostid=hid_b)
    found = match_alert_asset(db, second.hostname, ip=second.ip, zabbix_hostid=hid_a)
    assert found is not None
    assert found.id == first.id
    db.close()


def test_t2_unknown_hostid_falls_through_to_exact_hostname():
    db = _db()
    host = _host(db, f"edge-a-{uuid4().hex[:6]}", zabbix_hostid=f"h{uuid4().hex[:10]}")
    found = match_alert_asset(db, host.hostname, zabbix_hostid=f"h{uuid4().hex[:10]}")
    assert found is not None
    assert found.id == host.id
    db.close()


def test_t3_exact_hostname_match():
    db = _db()
    host = _host(db, f"Core-SW-{uuid4().hex[:6]}")
    found = match_alert_asset(db, host.hostname)
    assert found is not None
    assert found.id == host.id
    db.close()


def test_t4_exact_asset_id_match():
    db = _db()
    host = _host(db, f"sw-core-{uuid4().hex[:6]}")
    found = match_alert_asset(db, host.asset_id)
    assert found is not None
    assert found.id == host.id
    db.close()


def test_case_insensitive_hostname_matches_without_ip():
    """T5 — the #133 bug: case-insensitive hostname, no IP."""
    db = _db()
    host = create_manual_asset(
        db, hostname=f"Core-SW-{uuid4().hex[:6]}", ip=_ip(), type="Network device", actor="tester"
    )
    found = match_alert_asset(db, host.hostname.lower())
    assert found is not None
    assert found.id == host.id
    db.close()


def test_t6_case_insensitive_asset_id_without_ip():
    db = _db()
    host = _host(db, f"Edge-{uuid4().hex[:6]}")
    found = match_alert_asset(db, host.asset_id.swapcase())
    assert found is not None
    assert found.id == host.id
    db.close()


def test_ip_match_when_name_unknown():
    """T7 — IP fallback when the name does not match."""
    db = _db()
    host = create_manual_asset(
        db, hostname=f"by-ip-{uuid4().hex[:6]}", ip=_ip(), type="Linux Server", actor="tester"
    )
    found = match_alert_asset(db, "unknown-host", ip=host.ip)
    assert found is not None
    assert found.id == host.id
    db.close()


def test_t8_loopback_ip_is_ignored():
    db = _db()
    _host(db, f"loop-{uuid4().hex[:6]}", ip=_ip())
    assert match_alert_asset(db, "unknown-host", ip="127.0.0.1") is None
    assert match_alert_asset(db, "unknown-host", ip="localhost") is None
    db.close()


def test_t9_lab_demo_ip_is_skipped():
    db = _db()
    lab = db.query(Asset).filter_by(asset_id=DEMO_ASSET).one()
    found = match_alert_asset(db, "unknown-host", ip=lab.ip)
    assert found is None or found.asset_id != DEMO_ASSET
    db.close()


def test_t10_duplicate_hostnames_return_one_of_the_colliding_rows():
    db = _db()
    shared = f"Core-SW-{uuid4().hex[:6]}"
    first = _host(db, f"{shared}-a")
    second = _host(db, f"{shared}-b")
    first.hostname = shared
    second.hostname = shared
    db.commit()
    found = match_alert_asset(db, shared.lower())
    assert found is not None
    assert found.id in {first.id, second.id}
    assert found.hostname.lower() == shared.lower()
    db.close()


def test_exact_asset_id_wins_before_ip():
    """T11 (asset_id) — name must not select another asset by IP."""
    db = _db()
    named = create_manual_asset(
        db, hostname=f"named-{uuid4().hex[:6]}", ip=_ip(), type="Linux Server", actor="tester"
    )
    other = create_manual_asset(
        db, hostname=f"other-{uuid4().hex[:6]}", ip=_ip(), type="Linux Server", actor="tester"
    )
    found = match_alert_asset(db, named.asset_id, ip=other.ip)
    assert found is not None
    assert found.id == named.id
    db.close()


def test_t11_exact_hostname_wins_over_other_asset_ip():
    db = _db()
    named = _host(db, f"named-{uuid4().hex[:6]}")
    other = _host(db, f"other-{uuid4().hex[:6]}")
    found = match_alert_asset(db, named.hostname, ip=other.ip)
    assert found is not None
    assert found.id == named.id
    db.close()


def test_t12_zabbix_hostid_beats_conflicting_hostname_and_ip():
    db = _db()
    hid_a, hid_b = f"h{uuid4().hex[:10]}", f"h{uuid4().hex[:10]}"
    first = _host(db, f"foo-{uuid4().hex[:6]}", zabbix_hostid=hid_a)
    second = _host(db, f"bar-{uuid4().hex[:6]}", zabbix_hostid=hid_b)
    found = match_alert_asset(db, second.hostname, ip=second.ip, zabbix_hostid=hid_a)
    assert found is not None
    assert found.id == first.id
    db.close()
