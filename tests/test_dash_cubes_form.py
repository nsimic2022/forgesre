"""Dashboard squares, recent-incident columns, Escalation labels, asset Notes, form row order."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Asset, Incident
from app.seed import seed
from app.services import next_incident_number

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


def test_dashboard_recent_drops_notes_and_ack_column():
    _db().close()
    home = _client().get("/")
    assert home.status_code == 200
    recent = home.text.split("<h2>Recent incidents</h2>", 1)[1].split("</table>", 1)[0]
    head = recent.split("</thead>", 1)[0]
    assert ">Notes<" not in head and "col-notes" not in recent
    assert ">Acknowledged<" not in head and "col-ack" not in head
    assert head.index(">L1–L4<") < head.index(">When<") < head.index(">Resolved by<")
    assert ">Ack<" not in head and "info-tip" not in head
    assert 'class="col-when">When</th>' in head
    assert 'class="col-status">Status</th>' in head
    last = head.rsplit("<th", 1)[1]
    assert "Resolved by" in last and "Acknowledged" not in last and "When" not in last
    if "<tbody>" in recent and 'class="inc-when"' in recent:
        row = recent.split('class="inc-when"', 1)[1].split("</tr>", 1)[0]
        assert "col-ack" not in row and "ack-dot" not in row
        assert 'class="col-resolved"' in row
        assert "col-notes" not in row
    incidents = _client().get("/incidents")
    inc_head = incidents.text.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert ">Acknowledged<" not in inc_head and "col-ack" not in inc_head
    assert 'class="col-status">Status</th>' in inc_head
    assert inc_head.index(">When<") < inc_head.index(">Resolved by<") < inc_head.index(">Notes<")
    assert ">Ladder<" not in inc_head and ">L1–L4<" in inc_head


def test_dashboard_status_cell_shows_acknowledged_in_green():
    db = _db()
    now = datetime.now(timezone.utc)
    token = uuid4().hex[:8]

    def add(status: str, title: str, *, ack: bool, resolved_by: str = "") -> None:
        db.add(
            Incident(
                number=next_incident_number(db),
                title=title,
                severity="CRITICAL" if status != "ESCALATED" else "WARNING",
                status=status,
                fingerprint=f"dash-ack:{token}:{status}",
                started_at=now,
                ack_at=now if ack else None,
                ack_by="ops@forgesre.local" if ack else "",
                resolved_by=resolved_by,
                summary="dash ack",
            )
        )
        db.flush()

    add("CLOSED", f"Acked closed {token}", ack=True, resolved_by=f"closer-{token}@forgesre.local")
    add("ESCALATED", f"Acked escalated {token}", ack=True)
    add("OPEN", f"Open unacked {token}", ack=False)
    db.commit()
    try:
        home = _client().get("/")
        assert home.status_code == 200
        recent = home.text.split("<h2>Recent incidents</h2>", 1)[1].split("</table>", 1)[0]
        head = recent.split("</thead>", 1)[0]
        assert ">Acknowledged<" not in head
        assert "<th" in head and "Acknowledged" not in head
        assert "Resolved by" in head.rsplit("<th", 1)[1]

        def status_cell(title: str) -> str:
            row = recent.split(title, 1)[1].split("</tr>", 1)[0]
            assert "col-ack" not in row and "ack-dot" not in row
            return row.split('class="col-status"', 1)[1].split("</td>", 1)[0]

        closed = status_cell(f"Acked closed {token}")
        assert 'class="pill closed"' in closed and ">CLOSED<" in closed
        assert 'class="status-ack">Acknowledged</span>' in closed
        escalated = status_cell(f"Acked escalated {token}")
        assert 'class="pill escalated"' in escalated and ">ESCALATED<" in escalated
        assert 'class="status-ack">Acknowledged</span>' in escalated
        opened = status_cell(f"Open unacked {token}")
        assert 'class="pill open"' in opened and ">OPEN<" in opened
        assert "Acknowledged" not in opened and "status-ack" not in opened
        closed_row = recent.split(f"Acked closed {token}", 1)[1].split("</tr>", 1)[0]
        assert f"closer-{token}@forgesre.local" in closed_row.split('class="col-resolved"', 1)[1]

        css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
        ack_css = css.split(".dash-recent .status-ack {", 1)[1].split("}", 1)[0]
        assert "color: var(--ok)" in ack_css
        resolved = css.split(".dash-recent .incidents-table th.col-resolved,", 1)[1].split("}", 1)[0]
        assert "white-space: normal" in resolved
        assert "nowrap" not in resolved
        assert "max-width: none" in resolved
        assert "min-width: 11rem" in resolved
        assert "width: 26%" in resolved

        listed = _client().get(f"/incidents?q={token}")
        assert listed.status_code == 200
        inc_head = listed.text.split("<thead>", 1)[1].split("</thead>", 1)[0]
        assert ">Acknowledged<" not in inc_head and "col-ack" not in inc_head
        assert 'class="col-status">Status</th>' in inc_head
        assert inc_head.index(">L1–L4<") < inc_head.index(">When<") < inc_head.index(">Resolved by<") < inc_head.index(">Notes<")
        inc_row = listed.text.split(f"Acked closed {token}", 1)[1].split("</tr>", 1)[0]
        assert "ack-dot" not in inc_row and "col-ack" not in inc_row
        inc_status = inc_row.split('class="col-status"', 1)[1].split("</td>", 1)[0]
        assert 'class="pill closed"' in inc_status and ">CLOSED<" in inc_status
        assert 'class="status-ack">Acknowledged</span>' in inc_status
        open_row = listed.text.split(f"Open unacked {token}", 1)[1].split("</tr>", 1)[0]
        open_status = open_row.split('class="col-status"', 1)[1].split("</td>", 1)[0]
        assert ">OPEN<" in open_status and "status-ack" not in open_status and "Acknowledged" not in open_status
        assert f"closer-{token}@forgesre.local" in inc_row
        inc_css = css.split(".list-scroll .status-ack {", 1)[1].split("}", 1)[0]
        assert "color: var(--ok)" in inc_css
        inc_resolved = css.split(".list-scroll .incidents-table th.col-resolved,", 1)[1].split("}", 1)[0]
        assert "white-space: normal" in inc_resolved
        assert "max-width: none" in inc_resolved
        assert "min-width: 11rem" in inc_resolved
        assert "width: 26%" in inc_resolved
    finally:
        db.query(Incident).filter(Incident.fingerprint.like(f"dash-ack:{token}:%")).delete(synchronize_session=False)
        db.commit()
        db.close()


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
    assert head.index(">Source<") < head.index(">Notes<")
    assert "<th" not in head.split(">Notes<", 1)[1].split("</tr>", 1)[0]
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
    page = _client().get("/discovery")
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
    contacts = row1.split("data-asset-contacts", 1)[1].split("data-asset-support", 1)[0]
    support = row1.split("data-asset-support", 1)[1]
    for label in ("Support hours", "Timezone", "License / contract / SLA"):
        assert label not in contacts
        assert label in support
    for name in ("extra_support_hours", "extra_timezone", "extra_contract"):
        assert contacts.count(f'name="{name}"') == 0
        assert support.count(f'name="{name}"') == 1
        assert form.count(f'name="{name}"') == 1
    for name in ("owner", "contact_name", "owner_email", "owner_phone", "extra_backup_name", "extra_backup_phone", "extra_backup_email"):
        assert f'name="{name}"' in contacts
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    rules = css.split(".asset-form-band + .asset-form-band", 1)[1].split("}", 1)[0]
    assert "border-top: 1px solid var(--line);" in rules
    assert "margin-top: 1.75rem;" in rules
    assert "padding-top: 1.75rem;" in rules
    row1_css = css.split(".asset-form-row-1", 1)[1].split("}", 1)[0]
    assert "repeat(4, minmax(0, 1fr))" in row1_css
