"""Dashboard squares, recent-incident columns, Escalation labels, asset Notes, form row order."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Asset
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


def _ip() -> str:
    raw = uuid4().int
    return f"10.{180 + raw % 50}.{(raw >> 8) % 250 + 1}.{(raw >> 16) % 250 + 1}"


def test_dashboard_tiles_are_square_and_share_one_group_width():
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    band = css.split(".dash-tiles-incidents,", 1)[1].split("}", 1)[0]
    assert "justify-self: start" in band
    assert "width: min(100%, var(--dash-tile-band))" in band
    assert "--dash-tile-band: 42.5rem" in css
    tile = css.split(".stat-row-big .stat {", 1)[1].split("}", 1)[0]
    assert "aspect-ratio: 1" in tile
    assert "width: 100%" in tile
    assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in css
    assert "grid-template-columns: repeat(3, calc((100% - 4 * var(--dash-tile-gap)) / 5));" in css
    tiles = css.split("/* Dashboard layout pass:", 1)[1].split("/* Incidents tile only:", 1)[0]
    assert "repeat(3, minmax(0, 1fr))" not in tiles
    clock = css.split(".appliance-clock {", 1)[1].split("}", 1)[0]
    assert "padding:" in clock
    assert "clamp(1.85rem, 14cqi, 2.55rem)" in clock
    assert "5.4rem" not in clock


def test_dashboard_recent_drops_notes_and_keeps_when_apart_from_acknowledged():
    _db().close()
    home = _client().get("/")
    assert home.status_code == 200
    recent = home.text.split("<h2>Recent incidents</h2>", 1)[1].split("</table>", 1)[0]
    head = recent.split("</thead>", 1)[0]
    assert ">Notes<" not in head and "col-notes" not in recent
    assert head.index(">Escalation<") < head.index(">When<") < head.index(">Acknowledged<") < head.index(">Resolved by<")
    assert ">Ack<" not in head and "info-tip" not in head
    assert 'class="col-when">When</th>' in head
    assert 'class="col-ack">Acknowledged</th>' in head
    last = head.rsplit("<th", 1)[1]
    assert "Resolved by" in last and "Acknowledged" not in last and "When" not in last
    if "<tbody>" in recent and 'class="inc-when"' in recent:
        row = recent.split('class="inc-when"', 1)[1].split("</tr>", 1)[0]
        assert 'class="col-ack"' in row
        assert row.index('class="col-ack"') < row.index('class="col-resolved"')
        assert "col-notes" not in row
    incidents = _client().get("/incidents")
    inc_head = incidents.text.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert inc_head.index(">When<") < inc_head.index(">Acknowledged<") < inc_head.index(">Resolved by<") < inc_head.index(">Notes<")
    assert ">Ladder<" not in inc_head and ">Escalation<" in inc_head


def test_templates_and_handbook_say_escalation_not_ladder():
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "Ladder" not in text, path.name
        assert "default ladder" not in text.lower(), path.name
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert "ladder" not in handbook.lower()
    assert "L1" in handbook and "L4" in handbook


def test_assets_notes_column_is_the_form_runbook():
    db = _db()
    client = _client()
    token = uuid4().hex[:6]
    filled = f"note-{token}"
    blank = f"blank-{token}"
    filled_ip = _ip()
    blank_ip = _ip()
    runbook = f"Call the night desk {token}"
    created = client.post(
        "/assets",
        data={
            "asset_id": filled,
            "hostname": filled,
            "ip": filled_ip,
            "type": "Other",
            "notes": "incident-style asset notes must not show",
            "extras_present": "1",
            "extra_runbook_note": runbook,
        },
        follow_redirects=False,
    )
    assert created.status_code in {302, 303}
    empty = client.post(
        "/assets",
        data={
            "asset_id": blank,
            "hostname": blank,
            "ip": blank_ip,
            "type": "Other",
            "notes": "also hidden",
        },
        follow_redirects=False,
    )
    assert empty.status_code in {302, 303}
    page = client.get(f"/assets?q={token}&per_page=100")
    assert page.status_code == 200
    head = page.text.split('<table class="asset-table"', 1)[1].split("</thead>", 1)[0]
    assert head.index(">Source<") < head.index(">Notes ")
    assert "<th" not in head.split(">Notes ", 1)[1].split("</tr>", 1)[0]
    filled_row = page.text.split(f"/assets/{filled}", 1)[1].split("</tr>", 1)[0]
    filled_cell = filled_row.split('class="col-asset-notes"', 1)[1].split("</td>", 1)[0]
    assert runbook in filled_cell
    assert "incident-style" not in filled_cell
    blank_row = page.text.split(f"/assets/{blank}", 1)[1].split("</tr>", 1)[0]
    blank_cell = blank_row.split('class="col-asset-notes"', 1)[1].split("</td>", 1)[0]
    assert "—" in blank_cell
    assert "also hidden" not in blank_cell
    kept = client.post(
        f"/assets/{filled}/update",
        data={"hostname": filled, "ip": filled_ip, "type": "Other"},
        follow_redirects=False,
    )
    assert kept.status_code in {302, 303}
    db.expire_all()
    row = db.query(Asset).filter_by(asset_id=filled).one()
    assert row.notes == "incident-style asset notes must not show"
    assert row.extras["runbook_note"] == runbook
    db.close()


def test_asset_form_has_one_notes_and_row1_order():
    _db().close()
    page = _client().get("/assets")
    assert page.status_code == 200
    form = page.text.split('id="asset-form"', 1)[1].split("</form>", 1)[0]
    assert form.count(">Notes ") == 1
    assert 'name="notes"' not in form
    assert 'name="extra_runbook_note"' in form
    assert "Anything else the next operator should know" not in form
    row1 = form.split("asset-form-row-1", 1)[1].split("asset-form-row-2", 1)[0]
    order = [
        row1.index(">Identity<"),
        row1.index(">Ports "),
        row1.index(">Contacts "),
        row1.index(">Support "),
    ]
    assert order == sorted(order)
    assert "L1 NOC" in form and "L4 Manager" in form
    assert row1.index("asset-form-left") < row1.index("asset-form-middle") < row1.index("data-asset-contacts") < row1.index("data-asset-support")
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    rules = css.split(".asset-form-band + .asset-form-band", 1)[1].split("}", 1)[0]
    assert "border-top: 1px solid var(--line);" in rules
    assert "margin-top: 1.75rem;" in rules
    assert "padding-top: 1.75rem;" in rules
    row1_css = css.split(".asset-form-row-1", 1)[1].split("}", 1)[0]
    assert "repeat(4, minmax(0, 1fr))" in row1_css
