"""Incident detail Who did what list chrome (own pager + Rows), and the Assets Type / Source / Site / Customer filters."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.asset_types import ASSET_TYPE_CHOICES
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
    start = html.index('<section id="audit">')
    return html[start : html.index("</section>", start)]


def test_who_did_what_has_its_own_pager_and_rows_select():
    number = _incident_with_audit(14)
    client = _client()
    audit = _audit(client.get(f"/incidents/{number}").text)
    assert re.search(r'<div class="table-scroll list-scroll" data-list-scroll>\s*<table>', audit)
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


def test_assets_page_type_filter(seeded):
    page = _get(_client(), "type=Switch")
    assert _ids(page) == {"iaf-sw1", "iaf-sw2"}
    select = page.split('data-asset-filter="type">', 1)[1].split("</select>", 1)[0]
    assert '<option value="Switch" selected>' in select
    assert '<option value="">All types</option>' in select
    for name in ASSET_TYPE_CHOICES:
        assert f'<option value="{name}"' in select, name
    assert '<optgroup label="Custom">' in select and '<option value="UPS (APC)"' in select


def test_assets_page_source_filter(seeded):
    client = _client()
    assert _ids(_get(client, "source=zabbix")) == {"iaf-sw2"}
    assert _ids(_get(client, "source=netbox")) == {"iaf-sw1", "iaf-fw"}
    assert _ids(_get(client, "source=forge")) == {"iaf-lin", "iaf-ups"}
    select = _get(client, "source=zabbix").split('data-asset-filter="source">', 1)[1].split("</select>", 1)[0]
    labels = re.findall(r'<option value="([^"]*)"[^>]*>([^<]+)</option>', select)
    assert labels == [("", "All sources"), ("forge", "Forge"), ("netbox", "NetBox"), ("zabbix", "Zabbix")]
    assert '<option value="zabbix" selected>' in select


def test_assets_page_site_and_customer_filters(seeded):
    client = _client()
    assert _ids(_get(client, "site=DC-East")) == {"iaf-sw1", "iaf-lin"}
    assert _ids(_get(client, "customer=Globex")) == {"iaf-sw2", "iaf-lin"}
    assert _ids(_get(client, "site=")) >= {s[0] for s in SEEDED}
    page = _get(client, "site=DC-West&customer=Acme")
    assert _ids(page) == {"iaf-ups"}
    site = page.split('data-asset-filter="site">', 1)[1].split("</select>", 1)[0]
    assert '<option value="">All sites</option>' in site
    assert '<option value="DC-West" selected>' in site and '<option value="DC-East">' in site
    customer = page.split('data-asset-filter="customer">', 1)[1].split("</select>", 1)[0]
    assert '<option value="Acme" selected>' in customer and '<option value="Globex">' in customer


def test_search_combines_with_filters_and_reset_clears(seeded):
    client = _client()
    assert _ids(_get(client, "q=iaf-sw")) == {"iaf-sw1", "iaf-sw2"}
    page = _get(client, "q=iaf-sw&source=zabbix")
    assert _ids(page) == {"iaf-sw2"}
    assert _ids(_get(client, "q=host&type=Switch&customer=Acme")) == {"iaf-sw1"}
    assert _ids(_get(client, "q=nomatch-xyz&type=Switch")) == set()
    form = page.split('<form method="get" action="/assets"', 1)[1].split("</form>", 1)[0]
    assert form.count('<input name="q"') == 1
    assert form.count("<select") == 5
    assert 'type="hidden" name="source"' not in form
    assert '<a class="muted list-reset" href="/assets">Reset</a>' in form
    def filter_form(query: str) -> str:
        return _get(client, query).split('<form method="get" action="/assets"', 1)[1].split("</form>", 1)[0]

    assert "list-reset" not in filter_form("")
    for query in ("type=Switch", "source=forge", "site=DC-East", "customer=Acme"):
        assert "list-reset" in filter_form(query), query


def test_edit_link_keeps_filters(seeded):
    page = _get(_client(), "type=Switch&site=DC-East")
    assert "/assets?edit=iaf-sw1&amp;type=Switch&amp;site=DC-East" in page
    form = page.split('<form method="get" action="/assets"', 1)[1].split("</form>", 1)[0]
    filters = form.split('<input name="q"', 1)[1]
    assert "Ping" not in filters


def test_v08_15_cache_bump_and_compact_select_css():
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v08-18" in base and "app.js?v=v08-18" in base
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".list-filters select.filter-select { width: auto;" in css
