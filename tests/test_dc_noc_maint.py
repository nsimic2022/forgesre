"""Maintenance windows: record the incident, do not start the mail ladder."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.dc_noc import in_maintenance, maint_pair_error
from app.inventory import create_manual_asset
from app.main import app
from app.models import Asset, Incident, Notification, utcnow
from app.seed import seed
from app.services import ingest_alertmanager, process_escalations

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _asset(db, token: str, **extras) -> Asset:
    row = create_manual_asset(
        db,
        hostname=f"maint-{token}",
        asset_id=f"maint-{token}",
        ip=f"10.77.{int(token[:2], 16) % 200 + 1}.{int(token[2:4], 16) % 200 + 1}",
        type="Linux Server",
        owner_email="maint-owner@dc.local",
        extras=extras or None,
        actor="tester",
    )
    return row


def _alert(asset_id: str, name: str) -> dict:
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": name, "severity": "warning", "asset": asset_id, "instance": asset_id},
                "annotations": {"summary": name, "description": "maintenance test"},
            }
        ],
    }


def _mails(db, incident: Incident) -> list[Notification]:
    return db.query(Notification).filter(Notification.incident_id == incident.id).all()


def test_maint_pair_rejects_half_window():
    assert maint_pair_error("", "") == ""
    assert "both" in maint_pair_error("2026-10-13T10:00", "").lower()
    assert "after" in maint_pair_error("2026-10-13T12:00", "2026-10-13T10:00").lower()


def test_in_window_skips_mail_and_after_window_mails():
    db = _db()
    token = uuid4().hex[:8]
    now = utcnow()
    asset = _asset(
        db,
        token,
        maint_from=(now - timedelta(hours=1)).isoformat(),
        maint_until=(now + timedelta(hours=1)).isoformat(),
    )
    db.refresh(asset)
    assert in_maintenance(asset, now)
    name = f"MaintProbe{token}"
    opened = ingest_alertmanager(db, _alert(asset.asset_id, name))
    assert len(opened) == 1
    incident = opened[0]
    db.refresh(incident)
    assert incident.status == "MAINT"
    assert _mails(db, incident) == []
    incident.started_at = now - timedelta(hours=2)
    db.commit()
    process_escalations(db)
    assert _mails(db, incident) == []

    asset.extras = {}
    db.commit()
    db.refresh(asset)
    assert not in_maintenance(asset, utcnow())
    again = ingest_alertmanager(db, _alert(asset.asset_id, name))
    assert again == []
    db.refresh(incident)
    assert incident.status == "OPEN"
    mails = _mails(db, incident)
    assert len(mails) == 1
    assert mails[0].target == "maint-owner@dc.local"
    assert mails[0].status in {"generated", "sent"}
    db.close()


def _status_cell(row_html: str) -> str:
    return row_html.split('class="col-status"', 1)[1].split("</td>", 1)[0]


def _css_block(css: str, selector: str) -> str:
    return css.split(selector, 1)[1].split("}", 1)[0]


def test_incident_status_stacks_maintenance_under_severity():
    """Maintenance sits under the status word. It does not share a row with it."""
    db = _db()
    token = uuid4().hex[:8]
    now = utcnow()
    asset = _asset(
        db,
        token,
        maint_from=(now - timedelta(minutes=5)).isoformat(),
        maint_until=(now + timedelta(hours=2)).isoformat(),
    )
    warning_name = f"MaintWarn{token}"
    critical_name = f"MaintCrit{token}"
    opened = ingest_alertmanager(db, _alert(asset.asset_id, warning_name))
    assert len(opened) == 1
    warning = opened[0]
    db.refresh(warning)
    assert warning.status == "MAINT"
    assert warning.severity == "WARNING"
    warning.ack_at = now
    warning.ack_by = "ops@forgesre.local"
    crit_alert = _alert(asset.asset_id, critical_name)
    crit_alert["alerts"][0]["labels"]["severity"] = "critical"
    crit_opened = ingest_alertmanager(db, crit_alert)
    assert len(crit_opened) == 1
    critical = crit_opened[0]
    db.refresh(critical)
    assert critical.status == "MAINT"
    assert critical.severity == "CRITICAL"
    plain = Incident(
        number=f"INC-plain-{token}",
        title=f"Plain open {token}",
        severity="WARNING",
        status="OPEN",
        fingerprint=f"plain-open:{token}",
        started_at=now,
        summary="not in maintenance",
    )
    during = Incident(
        number=f"INC-during-{token}",
        title=f"Open during window {token}",
        severity="CRITICAL",
        status="OPEN",
        fingerprint=f"open-window:{token}",
        started_at=now,
        summary="window matches, status stays OPEN",
        asset_id=asset.id,
    )
    db.add(plain)
    db.add(during)
    db.commit()
    client = _client()
    listed = client.get(f"/incidents?q={token}")
    assert listed.status_code == 200
    inc_head = listed.text.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert ">Acknowledged<" not in inc_head and "col-ack" not in inc_head
    assert 'class="col-status">Status</th>' in inc_head
    assert inc_head.index(">Status<") < inc_head.index(">L1–L4<")

    warn_row = listed.text.split(warning_name, 1)[1].split("</tr>", 1)[0]
    warn_cell = _status_cell(warn_row)
    assert '<span class="status-line"><span class="pill warn">Warning</span></span>' in warn_cell
    assert 'data-maint-badge' in warn_cell and ">Maintenance<" in warn_cell
    assert warn_cell.index(">Warning<") < warn_cell.index(">Maintenance<")
    assert "Warning Maintenance" not in warn_cell
    assert ">MAINT<" not in warn_cell
    assert warn_cell.index(">Maintenance<") < warn_cell.index(">Acknowledged<")
    assert 'class="status-ack">Acknowledged</span>' in warn_cell
    assert "col-ack" not in warn_row and "ack-dot" not in warn_row

    crit_row = listed.text.split(critical_name, 1)[1].split("</tr>", 1)[0]
    crit_cell = _status_cell(crit_row)
    assert '<span class="status-line"><span class="pill crit">Critical</span></span>' in crit_cell
    assert crit_cell.index(">Critical<") < crit_cell.index(">Maintenance<")
    assert "status-ack" not in crit_cell and ">Acknowledged<" not in crit_cell

    during_row = listed.text.split(f"Open during window {token}", 1)[1].split("</tr>", 1)[0]
    during_cell = _status_cell(during_row)
    assert '<span class="status-line"><span class="pill open">OPEN</span></span>' in during_cell
    assert during_cell.index(">OPEN<") < during_cell.index(">Maintenance<")
    assert ">Warning<" not in during_cell and ">Critical<" not in during_cell

    plain_row = listed.text.split(f"Plain open {token}", 1)[1].split("</tr>", 1)[0]
    plain_cell = _status_cell(plain_row)
    assert ">OPEN<" in plain_cell and ">Maintenance<" not in plain_cell
    assert "status-ack" not in plain_cell

    home = client.get("/")
    assert home.status_code == 200
    recent = home.text.split("<h2>Recent incidents</h2>", 1)[1].split("</table>", 1)[0]
    dash_head = recent.split("</thead>", 1)[0]
    assert ">Acknowledged<" not in dash_head and "col-ack" not in dash_head
    assert 'class="col-status">Status</th>' in dash_head
    dash_row = recent.split(warning_name, 1)[1].split("</tr>", 1)[0]
    dash_cell = _status_cell(dash_row)
    assert dash_cell.index(">Warning<") < dash_cell.index(">Maintenance<")
    assert dash_cell.index(">Maintenance<") < dash_cell.index(">Acknowledged<")
    assert "Warning Maintenance" not in dash_cell
    assert "col-ack" not in dash_row

    assets = client.get(f"/assets?q={asset.asset_id}")
    assert assets.status_code == 200
    asset_body = assets.text.split("<tbody>", 1)[1]
    asset_row = asset_body.split(asset.asset_id, 1)[1].split("</tr>", 1)[0]
    asset_cell = _status_cell(asset_row)
    assert asset_cell.index(f">{asset.status}<") < asset_cell.index(">Maintenance<")
    assert "data-maint-badge" in asset_cell
    assert f"{asset.status} Maintenance" not in asset_cell

    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    for selector in (
        ".list-scroll .incidents-table th.col-status,",
        ".dash-recent .incidents-table th.col-status,",
        ".asset-table th.col-status,",
    ):
        block = _css_block(css, selector)
        assert "white-space: nowrap" not in block
        assert "white-space: normal" in block
        assert "overflow: hidden" in block
        assert "overflow: clip" in block
    for selector in (".list-scroll .status-cell {", ".dash-recent .status-cell {"):
        block = _css_block(css, selector)
        assert "flex-direction: column" in block
        assert "inline-flex" not in block
        assert "nowrap" not in block
        assert "overflow: hidden" in block
        assert "overflow: clip" in block
    for selector in (
        ".list-scroll .status-cell .status-line {",
        ".dash-recent .status-cell .status-line {",
    ):
        block = _css_block(css, selector)
        assert "display: block" in block
        assert "white-space: normal" in block
    for selector in (".list-scroll .status-ack {", ".dash-recent .status-ack {"):
        block = _css_block(css, selector)
        assert "color: var(--ok)" in block
        assert "display: block" in block
        assert "white-space: normal" in block
        assert "nowrap" not in block
    template = (ROOT / "frontend" / "templates" / "_incident_status_cell.html").read_text(encoding="utf-8")
    assert '<span class="status-line"><span class="pill {{ primary.pill }}">{{ primary.label }}</span></span>' in template
    assert 'data-maint-badge' in template and ">Maintenance</span>" in template
    assert template.index("{{ primary.label }}") < template.index(">Maintenance</span>")
    assert template.index(">Maintenance</span>") < template.index('class="status-ack">Acknowledged</span>')
    assert "{% if item.ack_at %}" in template
    assert "col-ack" not in template
    db.close()


def test_asset_list_badge_and_form_fields():
    db = _db()
    token = uuid4().hex[:8]
    now = utcnow()
    asset = _asset(
        db,
        token,
        maint_from=(now - timedelta(minutes=5)).isoformat(),
        maint_until=(now + timedelta(hours=2)).isoformat(),
    )
    client = _client()
    page = client.get("/discovery")
    assert page.status_code == 200
    assert 'name="extra_maint_from"' in page.text
    assert 'name="extra_maint_until"' in page.text
    listed = client.get(f"/assets?q={asset.asset_id}")
    assert "data-maint-badge" in listed.text
    assert "Maintenance" in listed.text
    stale = client.get(f"/assets/{asset.asset_id}", follow_redirects=False)
    assert stale.status_code == 303
    edit = client.get(f"/assets?edit={asset.asset_id}")
    assert edit.status_code == 200
    assert 'name="extra_maint_from"' in edit.text
    assert "data-maint-badge" in listed.text
    db.close()
