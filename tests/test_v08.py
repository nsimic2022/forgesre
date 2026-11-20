"""V0.8: version string, appliance NET dash, asset NOC extras, Client playrules,
support coverage."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.asset_extras import (
    DISPLAY_KEYS,
    EXTRA_KEYS,
    SUPPORT_KEYS,
    extras_from_form,
    extras_rows,
    normalize_extras,
    playrule_ids_from_form,
    support_status,
)
from app.db import Base, SessionLocal, engine
from app.host_resources import network_status
from app.main import app
from app.models import Asset, Incident, Notification, Playrule
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"


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
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    assets = db.query(Asset).filter(Asset.asset_id.like("v08-%")).all()
    ids = [a.id for a in assets]
    if ids:
        incidents = db.query(Incident).filter(Incident.asset_id.in_(ids)).all()
        for inc in incidents:
            db.query(Notification).filter(Notification.incident_id == inc.id).delete(synchronize_session=False)
            db.delete(inc)
        db.flush()
        for asset in assets:
            db.delete(asset)
    db.query(Playrule).filter(Playrule.name.like("v08-%")).delete(synchronize_session=False)
    db.commit()
    db.close()


EXTRAS_FORM = {
    "extras_present": "1",
    "extra_customer": "acme.local",
    "extra_site": "BG-DC1 room 2 rack 14",
    "extra_backup_name": "Mila Backup",
    "extra_backup_phone": "+381-11-555-0102",
    "extra_backup_email": "mila@acme.local",
    "extra_support_hours": "24/7",
    "extra_timezone": "Europe/Belgrade",
    "extra_contract": "SLA gold 4h",
    "extra_runbook_note": "Ignore disk until 95%; then call Mila.",
}


def _post_asset(client: TestClient, asset_id: str, ip: str, **extra) -> None:
    data = {
        "asset_id": asset_id,
        "hostname": asset_id,
        "ip": ip,
        "type": "Linux Server",
        "owner_email": "owner@acme.local",
        **EXTRAS_FORM,
        **extra,
    }
    resp = client.post("/assets", data=data, follow_redirects=False)
    assert resp.status_code in {302, 303}, resp.text


def _alert(alertname: str, asset_id: str) -> dict:
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": alertname, "asset": asset_id, "severity": "warning"},
                "annotations": {"summary": f"{alertname} on {asset_id}"},
            }
        ],
    }


# --- version ---------------------------------------------------------------


# --- appliance NET ------------------------------------------------------------

ROUTE_HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"


def _sys_net(tmp_path: Path, iface: str, operstate: str, carrier: str | None = None) -> str:
    d = tmp_path / iface
    d.mkdir(parents=True, exist_ok=True)
    (d / "operstate").write_text(operstate + "\n")
    if carrier is not None:
        (d / "carrier").write_text(carrier + "\n")
    return str(tmp_path)


def test_net_green_with_default_route_and_link_up(tmp_path):
    route = ROUTE_HEADER + "ens160\t00000000\t010A0A0A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    out = network_status(route_text=route, ipv6_route_text="", sys_net=_sys_net(tmp_path, "ens160", "up"))
    assert out["level"] == "ok"
    assert out["iface"] == "ens160"
    assert out["gateway"] == "10.10.10.1"
    assert out["reading"] == "ens160 up · gw 10.10.10.1"


def test_net_red_without_default_route(tmp_path):
    route = ROUTE_HEADER + "ens160\t000A0A0A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    out = network_status(route_text=route, ipv6_route_text="", sys_net=_sys_net(tmp_path, "ens160", "up"))
    assert out["level"] == "crit"
    assert out["reading"] == "no default route"


def test_net_red_when_default_iface_down(tmp_path):
    route = ROUTE_HEADER + "ens160\t00000000\t010A0A0A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    out = network_status(route_text=route, ipv6_route_text="", sys_net=_sys_net(tmp_path, "ens160", "down"))
    assert out["level"] == "crit"
    assert out["reading"] == "ens160 down"


def test_net_ignores_docker_default_and_uses_carrier_when_operstate_unknown(tmp_path):
    route = (
        ROUTE_HEADER
        + "docker0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        + "eth0\t00000000\t0101A8C0\t0003\t0\t0\t50\t00000000\t0\t0\t0\n"
    )
    sys_net = _sys_net(tmp_path, "eth0", "unknown", carrier="1")
    out = network_status(route_text=route, ipv6_route_text="", sys_net=sys_net)
    assert out["level"] == "ok"
    assert out["iface"] == "eth0"
    only_docker = ROUTE_HEADER + "docker0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    assert network_status(route_text=only_docker, ipv6_route_text="", sys_net=sys_net)["level"] == "crit"


def test_net_ipv6_default_route_when_no_ipv4(tmp_path):
    v6 = (
        "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
        "fe800000000000000000000000000001 00000400 00000001 00000000 00000003 ens192\n"
    )
    out = network_status(route_text=ROUTE_HEADER, ipv6_route_text=v6, sys_net=_sys_net(tmp_path, "ens192", "up"))
    assert out["level"] == "ok"
    assert out["family"] == "ipv6"


# --- Dashboard has incidents, not a journal preview ---------------------------


# --- asset extras --------------------------------------------------------------


def test_extras_normalize_and_form_helpers():
    assert normalize_extras({"site": "  DC1 ", "bogus": "x", "timezone": ""}) == {"site": "DC1"}
    assert normalize_extras(None) == {}
    assert extras_from_form("", site="x") is None
    assert extras_from_form("1", site="DC1")["site"] == "DC1"
    assert playrule_ids_from_form("", ["1"]) is None
    assert playrule_ids_from_form("1", []) == []
    assert playrule_ids_from_form("1", ["3", "x", "3", "-1", "5"]) == [3, 5]
    assert len(normalize_extras({"runbook_note": "a" * 9000})["runbook_note"]) == 4000
    assert set(DISPLAY_KEYS) == {
        "customer", "service", "site", "vlan", "row", "rack", "backup_name", "backup_phone", "backup_email",
        "support_hours", "timezone", "contract", "runbook_note",
    }
    assert SUPPORT_KEYS == ("support", "support_custom", "support_from", "support_to", "support_lead_days")
    assert set(EXTRA_KEYS) == set(DISPLAY_KEYS) | set(SUPPORT_KEYS)
    assert playrule_ids_from_form("1", ["3"], "7") == [3, 7]
    assert playrule_ids_from_form("1", ["3"], "3") == [3]
    assert playrule_ids_from_form("1", [], "") == []


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


# --- Client playrules ------------------------------------------------------------


def _rule(db, name: str, alertname: str, *, enabled: bool = True) -> Playrule:
    rule = Playrule(name=name, enabled=enabled, severity="warning", condition={"alertname": alertname})
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


# --- Support coverage -------------------------------------------------------------


class _Row:
    def __init__(self, **extras):
        self.extras = extras


def test_support_status_states_and_lead_time():
    today = date(2026, 10, 5)

    def state(**extras):
        return support_status(_Row(**extras), today=today)

    unknown = state()
    assert (unknown["state"], unknown["tone"], unknown["warn"]) == ("unknown", "grey", False)
    assert state(support="yes")["state"] == "unknown"
    assert state(support="internal")["state"] == "unknown"

    inside = state(support="yes", support_from="2026-01-01", support_to="2027-01-01")
    assert (inside["state"], inside["label"], inside["tone"], inside["warn"]) == ("in", "In support", "ok", False)
    assert inside["detail"] == "Support until 2027-01-01"
    open_end = state(support="internal", support_from="2026-01-01")
    assert open_end["state"] == "in" and open_end["detail"] == "Internal support, no end date"
    assert state(support_to="2027-01-01")["state"] == "in"

    soon = state(support="yes", support_from="2026-01-01", support_to="2026-10-15")
    assert (soon["state"], soon["tone"], soon["warn"]) == ("expiring", "warn", True)
    assert soon["lead_days"] == 14 and soon["days_left"] == 10
    assert soon["call_note"] == "Expiring — Support ends 2026-10-15 (in 10 days)"
    assert state(support="yes", support_to="2026-10-19")["state"] == "expiring"
    assert state(support="yes", support_to="2026-10-20")["state"] == "in"
    assert state(support="yes", support_to="2026-10-05")["detail"].endswith("(today)")
    assert state(support="yes", support_to="2026-10-15", support_lead_days="7")["state"] == "in"
    assert state(support="yes", support_to="2026-11-01", support_lead_days="30")["state"] == "expiring"

    past = state(support="yes", support_from="2025-01-01", support_to="2026-10-04")
    assert (past["state"], past["label"], past["tone"], past["warn"]) == ("expired", "Out of support", "crit", True)
    assert past["call_note"] == "Out of support — vendor may not take a ticket"
    assert past["detail"] == "Support ended 2026-10-04"
    no_contract = state(support="no")
    assert no_contract["state"] == "expired" and no_contract["detail"] == "No support contract"
    assert state(support="no", support_to="2030-01-01")["state"] == "expired"
    assert state(support="yes", support_from="2026-11-01")["state"] == "expired"

    assert {unknown["state"], inside["state"], soon["state"], past["state"]} == {"unknown", "in", "expiring", "expired"}


def test_support_normalize_keeps_block_out_of_generic_rows():
    raw = {
        "support": " Internal ", "support_from": "2026-01-01", "support_to": "not-a-date",
        "support_lead_days": "45", "site": "DC1",
    }
    assert normalize_extras(raw) == {
        "site": "DC1", "support": "internal", "support_from": "2026-01-01", "support_lead_days": "14",
    }
    assert normalize_extras({"support": "maybe", "support_lead_days": "30"}) == {}
    assert normalize_extras({"support": "no", "support_lead_days": "7"})["support_lead_days"] == "7"
    keys = [key for key, _label, _value in extras_rows(_Row(**normalize_extras(raw)))]
    assert keys == ["site"]


def _support_form(kind: str, start: date | None, end: date | None, lead: int = 14) -> dict:
    return {
        "extra_support": kind,
        "extra_support_from": start.isoformat() if start else "",
        "extra_support_to": end.isoformat() if end else "",
        "extra_support_lead_days": str(lead),
    }


# --- Edit pane: fields + custom alarms (old /assets/{id} fact page is gone) --------


def _viewer_client() -> TestClient:
    from app.models import User
    from app.security import hash_password

    db = _db()
    email = "viewer-v08@forgesre.local"
    if db.query(User).filter_by(email=email).first() is None:
        db.add(User(email=email, name="V08 viewer", password_hash=hash_password("testpass"), role="viewer"))
        db.commit()
    db.close()
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": "testpass"}, follow_redirects=False)
    return client


