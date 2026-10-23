"""Incident detail Who did what list chrome (own pager + Rows), and the Assets search box."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import asset_filter_options, assets_matching
from app.main import app
from app.models import Asset, AuditLog, Incident
from app.seed import seed
from app.services import next_incident_number

ROOT = Path(__file__).resolve().parents[1]
TAG = "iaf:"
PREFIX = "iaf-"


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
    numbers = [n for (n,) in db.query(Incident.number).filter(Incident.fingerprint.like(f"{TAG}%"))]
    if numbers:
        db.query(AuditLog).filter(AuditLog.object_type == "incident", AuditLog.object_id.in_(numbers)).delete(
            synchronize_session=False
        )
        db.query(Incident).filter(Incident.number.in_(numbers)).delete(synchronize_session=False)
    for asset in db.query(Asset).filter(Asset.asset_id.like(f"{PREFIX}%")).all():
        db.delete(asset)
    db.commit()
    db.close()


def _incident_with_audit(n: int) -> str:
    db = _db()
    row = Incident(
        number=next_incident_number(db),
        title=f"Audit pager {uuid4().hex[:4]}",
        severity="WARNING",
        status="OPEN",
        fingerprint=f"{TAG}{uuid4().hex}",
        started_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    number = row.number
    for i in range(n):
        db.add(AuditLog(actor="admin@forgesre.local", action=f"iaf.step{i:02d}", object_type="incident", object_id=number))
    db.commit()
    db.close()
    return number


def _audit(html: str) -> str:
    start = html.index('id="audit"')
    start = html.rindex("<section", 0, start)
    return html[start : html.index("</section>", start)]


def test_who_did_what_has_its_own_pager_and_rows_select():
    number = _incident_with_audit(14)
    client = _client()
    audit = _audit(client.get(f"/incidents/{number}").text)
    assert re.search(r'<div class="table-scroll list-scroll" data-list-scroll>\s*<table class="scan-list audit-table">', audit)
    assert ">Body<" in audit
    assert "data-text-open" in audit or "—" in audit
    assert "Outbox:" not in audit
    assert 'class="incident-audit-body"' in audit
    assert audit.count("iaf.step") == 10
    assert 'class="pager-bar"' in audit and '<div class="pager-left">' in audit
    assert "Showing 1–10 of 14" in audit
    assert f"/incidents/{number}?audit_page=2#audit" in audit
    assert 'name="audit_per_page"' in audit and "data-pager-size" in audit
    options = re.findall(r'<option value="(\d+)"', audit.split('name="audit_per_page"', 1)[1].split("</select>", 1)[0])
    assert options == ["10", "20", "50", "100"]
    assert 'action="/incidents/' + number + '#audit"' in audit

    second = _audit(client.get(f"/incidents/{number}?audit_page=2").text)
    assert second.count("iaf.step") == 4 and "iaf.step13" in second
    more = _audit(client.get(f"/incidents/{number}?audit_per_page=20").text)
    assert more.count("iaf.step") == 14


def test_short_who_did_what_shows_rows_without_pager():
    number = _incident_with_audit(3)
    audit = _audit(_client().get(f"/incidents/{number}").text)
    assert audit.count("iaf.step") == 3
    assert "Previous" not in audit
    assert f"/incidents/{number}/audit/export" in audit


def _asset(asset_id: str, type_: str, source: str, site: str = "", customer: str = "", **extra) -> Asset:
    raw = uuid4().int
    return Asset(
        asset_id=asset_id,
        hostname=f"{asset_id}-host",
        ip=f"10.{200 + raw % 20}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}",
        type=type_,
        source=source,
        monitoring_profile="",
        scrape_address="",
        extras={k: v for k, v in (("site", site), ("customer", customer)) if v},
        **extra,
    )


SEEDED = (
    ("iaf-sw1", "Switch", "netbox", "DC-East", "Acme"),
    ("iaf-sw2", "Switch", "zabbix", "DC-West", "Globex"),
    ("iaf-lin", "Linux Server", "manual", "DC-East", "Globex"),
    ("iaf-ups", "UPS (APC)", "discovery", "DC-West", "Acme"),
    ("iaf-fw", "Firewall", "netbox", "", ""),
)


@pytest.fixture
def seeded():
    db = _db()
    for row in SEEDED:
        db.add(_asset(*row))
    db.commit()
    db.close()


def _ids(page: str) -> set[str]:
    return {m for m in re.findall(r'data-asset-reach="([^"]+)"', page) if m.startswith(PREFIX)}


def _get(client: TestClient, query: str) -> str:
    response = client.get(f"/assets?per_page=100&{query}")
    assert response.status_code == 200
    return response.text


def test_assets_matching_type_source_site_customer_unit():
    rows = [_asset(*row) for row in SEEDED]
    rows.append(_asset("iaf-zl", "Linux Server", "manual", "DC-East", "Acme", zabbix_hostid="10500"))
    ids = lambda out: {a.asset_id for a in out}  # noqa: E731
    assert ids(assets_matching(rows, type="Switch")) == {"iaf-sw1", "iaf-sw2"}
    assert ids(assets_matching(rows, type="switch")) == {"iaf-sw1", "iaf-sw2"}
    assert ids(assets_matching(rows, source="zabbix")) == {"iaf-sw2", "iaf-zl"}
    assert ids(assets_matching(rows, source="netbox")) == {"iaf-sw1", "iaf-fw"}
    assert ids(assets_matching(rows, source="forge")) == {"iaf-lin", "iaf-ups", "iaf-zl"}
    assert ids(assets_matching(rows, site="dc-east")) == {"iaf-sw1", "iaf-lin", "iaf-zl"}
    assert ids(assets_matching(rows, customer="Acme")) == {"iaf-sw1", "iaf-ups", "iaf-zl"}
    assert ids(assets_matching(rows, type="Switch", site="DC-West", customer="Globex")) == {"iaf-sw2"}
    assert ids(assets_matching(rows, q="iaf-sw", customer="Acme")) == {"iaf-sw1"}
    assert ids(assets_matching(rows, type="UPS (APC)")) == {"iaf-ups"}

    options = asset_filter_options(rows)
    assert options["custom_types"] == ["UPS (APC)"]
    assert options["sites"] == ["DC-East", "DC-West"]
    assert options["customers"] == ["Acme", "Globex"]


def test_assets_page_search_covers_type_source_site_customer(seeded):
    client = _client()
    page = _get(client, "q=Switch")
    assert _ids(page) == {"iaf-sw1", "iaf-sw2"}
    form = page.split('<form method="get" action="/assets"', 1)[1].split("</form>", 1)[0]
    assert 'data-asset-filter="' not in form
    assert "<select" not in form
    assert 'placeholder="Search any field"' in form
    assert _ids(_get(client, "q=zabbix")) == {"iaf-sw2"}
    assert _ids(_get(client, "q=netbox")) == {"iaf-sw1", "iaf-fw"}
    assert _ids(_get(client, "q=DC-East")) == {"iaf-sw1", "iaf-lin"}
    assert _ids(_get(client, "q=Globex")) == {"iaf-sw2", "iaf-lin"}
    assert _ids(_get(client, "q=DC-West")) >= {"iaf-sw2", "iaf-ups"}


def test_search_and_reset_clears(seeded):
    client = _client()
    assert _ids(_get(client, "q=iaf-sw")) == {"iaf-sw1", "iaf-sw2"}
    page = _get(client, "q=iaf-sw")
    assert _ids(page) == {"iaf-sw1", "iaf-sw2"}
    assert _ids(_get(client, "q=Acme")) == {"iaf-sw1", "iaf-ups"}
    assert _ids(_get(client, "q=nomatch-xyz")) == set()
    form = page.split('<form method="get" action="/assets"', 1)[1].split("</form>", 1)[0]
    assert form.count('<input name="q"') == 1
    assert form.count("<select") == 0
    assert 'data-asset-filter="' not in form
    assert 'href="/assets">Reset</a>' in form
    assert "list-reset" in form
    empty = _get(client, "").split('<form method="get" action="/assets"', 1)[1].split("</form>", 1)[0]
    assert 'href="/assets">Reset</a>' in empty


def test_edit_link_keeps_search(seeded):
    client = _client()
    page = _get(client, "q=DC-East")
    assert 'name="stay" value="/assets?per_page=100&amp;q=DC-East"' in page
    opened = client.post(
        "/assets/open-edit",
        data={"selected": "iaf-sw1", "stay": "/assets?per_page=100&q=DC-East"},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    assert "edit=iaf-sw1" in opened.headers["location"]
    assert "q=DC-East" in opened.headers["location"]
    form = page.split('<form method="get" action="/assets"', 1)[1].split("</form>", 1)[0]
    assert "Ping" not in form
    assert "<select" not in form


def test_v08_15_cache_bump_and_compact_select_css():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v09-47" in base and "app.js?v=v09-I" in base
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".list-filters select.filter-select { width: auto;" in css
    assert ".assets-filters input[name=\"q\"]" in css
