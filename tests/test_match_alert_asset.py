"""Alert-to-asset match: case-insensitive name, then IP. Documented order."""

from __future__ import annotations

from uuid import uuid4

from app.inventory import create_manual_asset
from app.seed import seed
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


def test_case_insensitive_hostname_matches_without_ip():
    db = _db()
    ip = _ip()
    host = create_manual_asset(
        db, hostname=f"Core-SW-{uuid4().hex[:6]}", ip=ip, type="Network device", actor="tester"
    )
    found = match_alert_asset(db, host.hostname.lower())
    assert found is not None
    assert found.id == host.id
    db.close()


def test_exact_asset_id_wins_before_ip():
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


def test_ip_match_when_name_unknown():
    db = _db()
    host = create_manual_asset(
        db, hostname=f"by-ip-{uuid4().hex[:6]}", ip=_ip(), type="Linux Server", actor="tester"
    )
    found = match_alert_asset(db, "unknown-host", ip=host.ip)
    assert found is not None
    assert found.id == host.id
    db.close()
