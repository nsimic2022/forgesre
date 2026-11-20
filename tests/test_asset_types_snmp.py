"""Add-asset Type list, empty default scrape for non-servers, per-asset SNMP port, Custom support,
address-book email picks, runbook wording."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.asset_extras import support_status
from app.asset_extras import extras_from_form
from app.asset_types import snmp_family, snmp_poll_on, snmp_port_for, snmp_target
from app.db import Base, SessionLocal, engine
from app.inventory import default_scrape_address
from app.main import app
from app.models import Asset, MailContact
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
NEW_TYPES = ("Storage", "QNAP/NAS", "Switch", "Router", "Firewall", "Printer", "Hypervisor", "Other")
KEPT_TYPES = ("Auto (detect exporter)", "Linux Server", "Windows Server", "Network device", "Web/appliance")


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
    db = SessionLocal()
    for asset in db.query(Asset).filter(Asset.asset_id.like("ats-%")).all():
        db.delete(asset)
    db.query(MailContact).filter(MailContact.email.like("ats-%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def _ip() -> str:
    raw = uuid4().int
    return f"10.{170 + raw % 20}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


def _post(client: TestClient, asset_id: str, type_: str, ip: str | None = None, **extra):
    data = {"asset_id": asset_id, "hostname": asset_id, "ip": ip or _ip(), "type": type_, **extra}
    return client.post("/assets", data=data, follow_redirects=False)


def _form_type_select(page: str) -> str:
    return page.split("data-detect-type>", 1)[1].split("</select>", 1)[0]


def _row(listing: str, asset_id: str) -> str:
    return listing.split(f">{asset_id}<", 1)[1].split("</tr>", 1)[0]


def test_default_scrape_only_for_linux_and_windows():
    ip = "10.9.9.9"
    assert default_scrape_address("Linux Server", ip) == f"{ip}:9100"
    assert default_scrape_address("Windows Server", ip) == f"{ip}:9182"
    for name in NEW_TYPES + ("Network device", "Web/appliance", "Auto (detect exporter)"):
        assert default_scrape_address(name, ip) == "", name


def test_snmp_family_and_effective_port():
    for name in ("Network device", "Switch", "Router", "Firewall", "Storage", "QNAP/NAS", "Printer"):
        assert snmp_family(name), name
    for name in ("Linux Server", "Windows Server", "Hypervisor", "Web/appliance", "Other", "Auto (detect exporter)"):
        assert not snmp_family(name), name
    assert not snmp_family("Hypervisor", "network-switch")
    assert snmp_port_for(Asset(type="Switch", ip="1.1.1.1")) == 161
    assert snmp_port_for(Asset(type="Linux Server", ip="1.1.1.1")) == 0
    assert snmp_port_for(Asset(type="Linux Server", ip="1.1.1.1", snmp_port=1161)) == 1161
    assert snmp_port_for(Asset(type="Other", ip="1.1.1.1", extras={"snmp_poll": "1"})) == 161
    assert snmp_port_for(Asset(type="Hypervisor", ip="1.1.1.1", extras={"snmp_poll": "1"})) == 161
    assert snmp_port_for(Asset(type="Other", ip="1.1.1.1")) == 0
    assert snmp_poll_on({"snmp_poll": "1"})
    assert not snmp_poll_on({})
    assert extras_from_form("1", snmp_poll="1")["snmp_poll"] == "1"
    assert "snmp_poll" not in extras_from_form("1", site="DC1")
    assert snmp_target("1.1.1.1", 161) == "1.1.1.1"
    assert snmp_target("1.1.1.1", 1161) == "1.1.1.1:1161"


def test_support_custom_dates_still_expire():
    row = Asset(extras={"support": "custom", "support_custom": "NBD", "support_to": (date.today() - timedelta(days=1)).isoformat()})
    assert support_status(row)["state"] == "expired"
    soon = Asset(extras={"support": "custom", "support_custom": "NBD", "support_to": (date.today() + timedelta(days=3)).isoformat()})
    assert support_status(soon)["state"] == "expiring"


