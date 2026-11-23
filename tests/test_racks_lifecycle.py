"""Rack elevation and lifecycle dates (support / warranty, end of life, licence)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.asset_extras import normalize_extras
from app.cli_ops import format_lifecycle, format_racks
from app.db import Base, SessionLocal, engine
from app.lifecycle import ALERTNAME, check_lifecycle, items_for, overview
from app.models import Asset, CheckResult
from app.racks import racks
from app.seed import seed
from test_incident_tools import _client

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 23)


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    db = SessionLocal()
    for asset in db.query(Asset).filter(Asset.asset_id.like("rk-%")).all():
        db.delete(asset)
    db.query(CheckResult).filter(CheckResult.target.like("rk-%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def _asset(db, asset_id: str, **extras) -> Asset:
    row = Asset(asset_id=asset_id, hostname=asset_id, ip="", type="Other", extras=normalize_extras(extras))
    db.add(row)
    db.commit()
    return row


def test_extras_keep_valid_dates_and_units_only():
    got = normalize_extras({"eol_date": "2027-03-01T00:00", "license_to": "soon", "rack_u": "61", "u_height": "2"})
    assert got == {"eol_date": "2027-03-01", "u_height": "2"}


def test_racks_place_devices_and_report_overlaps():
    db = _db()
    _asset(db, "rk-esx", site="RKDC", row="A", rack="RK1", rack_u="10", u_height="2")
    _asset(db, "rk-nas", site="RKDC", row="A", rack="RK1", rack_u="11")
    _asset(db, "rk-pdu", site="RKDC", row="A", rack="RK1")
    _asset(db, "rk-top", site="RKDC", row="A", rack="RK1", rack_u="42", u_height="2")
    data = racks(db, "RKDC")
    rack = next(r for r in data["racks"] if r["rack"] == "RK1")
    assert [d["asset_id"] for d in rack["devices"]] == ["rk-top", "rk-nas", "rk-esx"]
    assert [d["asset_id"] for d in rack["unplaced"]] == ["rk-pdu"]
    assert rack["overlaps"] == ["U11: rk-nas and rk-esx"]
    assert rack["too_high"] == ["rk-top"]
    text = format_racks(data)
    assert "RKDC / A / RK1" in text and "U10-11" in text and "no rack position" in text and "overlap U11: rk-nas and rk-esx" in text
    db.close()


def test_lifecycle_items_and_statuses():
    db = _db()
    a = _asset(db, "rk-fw", support_to="2026-12-01", support_lead_days="14", eol_date="2027-06-01", license_to="2026-11-20")
    items = {i["kind"]: i for i in items_for(a, TODAY, ahead=30)}
    assert items["support"]["status"] == "expiring" and items["support"]["days_left"] == 8
    assert items["eol"]["status"] == "ok"
    assert items["license"]["status"] == "expired" and items["license"]["days_left"] == -3
    data = overview(db, TODAY)
    mine = [r for r in data["rows"] if r["asset"] == "rk-fw"]
    assert [r["kind"] for r in mine] == ["license", "support"]
    assert "rk-fw" in format_lifecycle(data)
    db.close()


def test_lifecycle_check_fires_and_resolves():
    db = _db()
    _asset(db, "rk-sw", license_to="2026-11-20", eol_date="2030-01-01")
    alerts = [a for a in check_lifecycle(db, TODAY) if a["labels"]["asset"] == "rk-sw"]
    by = {a["labels"]["lifecycle"]: a for a in alerts}
    assert by["license"]["status"] == "firing" and by["license"]["labels"]["severity"] == "high"
    assert by["license"]["labels"]["alertname"] == ALERTNAME
    assert by["eol"]["status"] == "resolved"
    assert db.query(CheckResult).filter_by(kind="lifecycle", target="rk-sw:license").one().status == "error"
    db.close()


def test_api_racks_and_lifecycle_for_viewer():
    client = _client("viewer")
    assert set(client.get("/api/v1/racks").json()) == {"racks", "units"}
    assert set(client.get("/api/v1/lifecycle").json()) >= {"rows", "tracked", "warn_days"}


def test_cli_and_docs_know_the_commands():
    cli = (ROOT / "scripts" / "forgesre").read_text(encoding="utf-8")
    assert "lifecycle|racks)" in cli
    assert "lifecycle racks" in (ROOT / "scripts" / "forgesre-completion.bash").read_text(encoding="utf-8")
    alerting = (ROOT / "docs" / "alerting.md").read_text(encoding="utf-8")
    assert ALERTNAME in alerting
