"""Stacked L1–L4 cubes, shared list columns, and filter labels without “All”."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]


def _client() -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.close()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _order(text: str, labels: list[str]) -> None:
    pos = -1
    for label in labels:
        found = text.index(label)
        assert found > pos, label
        pos = found


def test_ladder_cubes_stack_four_high_in_html():
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    rule = css.split(".ladder-cubes {", 1)[1].split("}", 1)[0]
    assert "flex-direction: column" in rule
    assert "grid-template-columns" not in rule
    square = css.split(".ladder-cubes .reach-sq {", 1)[1].split("}", 1)[0]
    assert "1.55rem" in square and "0.82rem" in square
    markup = (ROOT / "frontend" / "templates" / "_ladder_cubes.html").read_text(encoding="utf-8")
    assert 'data-ladder-layout="stack"' in markup
    assert "ladder-stack" in markup

    client = _client()
    for path, marker in (("/assets", "asset-table"), ("/incidents", "incidents-table"), ("/", "data-dash-incident-table")):
        page = client.get(path)
        assert page.status_code == 200, path
        block = page.text.split(marker, 1)[1]
        stack = block.split('data-ladder-layout="stack"', 1)[1].split("</tr>", 1)[0]
        levels = re.findall(r'data-ladder-level="(\d)"', stack)
        assert levels == ["1", "2", "3", "4"], path
        assert ">L1<" in stack and ">L2<" in stack and ">L3<" in stack and ">L4<" in stack


def test_shared_columns_and_enum_widths():
    client = _client()
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".asset-table { table-layout: auto;" in css
    assert ".incidents-table { table-layout: auto;" in css
    assert ".found-table { table-layout: fixed;" in css
    assert "border-collapse: separate" in css
    assert "border-spacing: 0 0.5rem" in css.split(".list-scroll > table,", 1)[1].split("}", 1)[0]
    assert "border-radius: 10px 0 0 10px" in css
    clip = css.split(".enum-clip {", 1)[1].split("}", 1)[0]
    assert "max-width: 7.25rem" in clip
    assert "text-overflow: ellipsis" in clip
    assert "white-space: nowrap" in clip

    assets = client.get("/assets").text
    asset_head = assets.split('<table class="asset-table"', 1)[1].split("</thead>", 1)[0]
    _order(asset_head, [">#<", ">ID<", ">Hostname<", ">IP<", 'class="col-port"', ">Type<", ">Status<", 'class="col-escalation"', ">L1–L4<", ">Owner<", ">Contact<", ">Environment<", ">Source<", ">Notes<"])
    assert ">When<" not in asset_head
    assert 'class="enum-clip"' in assets and 'title="Linux Server"' in assets

    incidents = client.get("/incidents").text
    inc_head = incidents.split("<thead>", 1)[1].split("</thead>", 1)[0]
    _order(
        inc_head,
        [">Incident<", ">Hostname<", ">Status<", ">L1–L4<", ">When<", ">Severity<", ">Resolved by<", ">Notes<"],
    )

    recent = client.get("/").text.split("<h2>Recent incidents</h2>", 1)[1].split("</table>", 1)[0]
    recent_head = recent.split("</thead>", 1)[0]
    _order(recent_head, [">Incident<", ">Hostname<", ">Status<", ">L1–L4<", ">When<", ">Severity<", ">Resolved by<"])
    assert ">Notes<" not in recent_head
    assert "Resolved by" in recent_head.rsplit("<th", 1)[1]

    found = client.get("/discovery").text.split("<h2>Found assets</h2>", 1)[1].split("</thead>", 1)[0]
    _order(found, [">#<", ">ID<", ">Hostname<", ">IP<", 'class="col-port"', ">Type<", ">Status<", 'class="col-escalation"', ">L1–L4<", ">Owner<", ">Contact<", ">Environment<", ">Source<", ">Notes<"])


def test_filter_bars_search_first_without_all_labels():
    assets_html = (ROOT / "frontend" / "templates" / "assets.html").read_text(encoding="utf-8")
    asset_form = assets_html.split("assets-filters", 1)[1].split("</form>", 1)[0]
    assert "Verify all" not in asset_form and "Verify all" not in assets_html
    assert 'name="q"' in asset_form
    assert "Search any field" in asset_form
    assert 'href="/assets">Reset</a>' in asset_form
    assert 'data-asset-filter="' not in asset_form
    assert "<select" not in asset_form
    assert "All types" not in asset_form and "All sites" not in asset_form
    assert ">Production<" not in assets_html
    assert "Lab DEMO" not in assets_html

    incidents_html = (ROOT / "frontend" / "templates" / "incidents.html").read_text(encoding="utf-8")
    incident_form = incidents_html.split('class="list-filters incidents-filters"', 1)[1].split("</form>", 1)[0]
    assert incident_form.index('name="q"') < incident_form.index('name="status"')
    assert incident_form.index("Site") < incident_form.index('name="status"')
    assert '("all", "Status")' in incident_form and ">Severity</option>" in incident_form
    assert "All sites" not in incident_form and "All customers" not in incident_form
    assert "All rows" not in incident_form and "All racks" not in incident_form
    assert 'name="type"' not in incident_form and 'name="vlan"' not in incident_form and 'name="source"' not in incident_form

    discovery_html = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    found = discovery_html.split("<h2>Found assets</h2>", 1)[1]
    assert "list-filters" not in found
    assert 'name="type"' not in found and "All sites" not in found
    assert "Proposed role" not in found

    client = _client()
    listed = client.get("/incidents").text
    form = listed.split('class="list-filters incidents-filters"', 1)[1].split("</form>", 1)[0]
    assert form.index('name="q"') < form.index('name="status"')
    assert "All sites" not in form and ">Status</option>" in form
    assets = client.get("/assets").text
    asset_page = assets.split("assets-filters", 1)[1].split("</form>", 1)[0]
    assert 'name="q"' in asset_page
    assert "Search any field" in asset_page
    assert 'data-asset-filter="' not in asset_page
    assert "<select" not in asset_page
    assert "Verify all" not in asset_page
