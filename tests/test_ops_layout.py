"""Dashboard / incident / admin chrome: ticker, Save As, report preview, equal heights."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.main import app
from app.models import Incident
from app.seed import seed
from app.services import alarm_ticker, next_incident_number

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


def test_ticker_api_lists_fresh_production_incidents():
    db = _db()
    token = uuid4().hex[:6]
    asset = create_manual_asset(db, hostname=f"tick-{token}", ip="10.44.8.2", actor="tester", asset_id=f"tick-{token}")
    number = next_incident_number(db)
    db.add(
        Incident(
            number=number,
            title=f"HighCPU {token}",
            severity="CRITICAL",
            status="OPEN",
            fingerprint=f"tick:{token}",
            started_at=datetime.now(timezone.utc),
            asset_id=asset.id,
        )
    )
    db.commit()
    payload = alarm_ticker(db, limit=200)
    db.close()
    assert payload["fresh"] is True
    assert any(item["number"] == number for item in payload["items"])
    ours = next(item for item in payload["items"] if item["number"] == number)
    assert ours["severity"] == "CRITICAL"

    client = _client()
    data = client.get("/api/v1/ticker").json()
    assert "items" in data and "heat" in data and "fresh" in data
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "data-alarm-ticker" in base
    assert "app.css?v=v09-44" in base and "app.js?v=v09-G" in base
    js = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    ticker = js.split("(function bindAlarmTicker() {", 1)[1].split("\n})();", 1)[0]
    assert "forgesre-ticker-seen" in ticker
    assert "sessionStorage" in ticker
    assert "ticker-blink" in ticker
    assert 'path === "/"' in ticker


def test_incident_report_preview_edit_export_and_sent_to():
    db = _db()
    token = uuid4().hex[:6]
    asset = create_manual_asset(
        db,
        hostname=f"rep-{token}",
        ip="10.44.8.3",
        actor="tester",
        asset_id=f"rep-{token}",
        owner="NOC",
        contact_name="Ana",
    )
    number = next_incident_number(db)
    db.add(
        Incident(
            number=number,
            title=f"Disk {token}",
            severity="WARNING",
            status="OPEN",
            fingerprint=f"rep:{token}",
            started_at=datetime.now(timezone.utc),
            asset_id=asset.id,
            summary="disk is high",
        )
    )
    db.commit()
    db.close()

    client = _client()
    page = client.get(f"/incidents/{number}")
    assert page.status_code == 200
    assert 'name="subject"' in page.text
    assert 'data-report-body' in page.text
    assert "ForgeSRE incident report" in page.text
    assert "Sent to" not in page.text
    assert "Not sent yet." not in page.text
    assert "Outbox:" not in page.text
    assert 'class="incident-mail-toolbar"' in page.text
    assert 'form="incident-send-report"' in page.text
    assert 'action="/incidents/%s/report/export"' % number in page.text
    assert page.text.index("<h2>What happened") < page.text.index("<h2>Who to call")
    assert page.text.index('id="notes"') < page.text.index('id="send-report"')

    exported = client.post(
        f"/incidents/{number}/report/export",
        data={"format": "csv", "filename": f"rep-{token}"},
    )
    assert exported.status_code == 200
    assert exported.content.startswith(b"\xef\xbb\xbf")
    assert f'filename="rep-{token}.csv"' in exported.headers["content-disposition"]
    assert b"disk is high" in exported.content

    named = client.post("/incidents/export", data={"selected": number, "format": "csv", "filename": f"ops-{token}"})
    assert named.status_code == 200
    assert f'filename="ops-{token}.csv"' in named.headers["content-disposition"]

    mailed = client.post(
        f"/incidents/{number}/mail",
        data={"target": "ops-preview@example.local", "subject": "Edited subject", "body": "Edited body line"},
        follow_redirects=False,
    )
    assert mailed.status_code == 303
    after = client.get(f"/incidents/{number}")
    assert "ops-preview@example.local" in after.text
    assert "Edited subject" in after.text or "generated" in after.text


def test_admin_split_and_cli_example():
    html = _client().get("/admin").text
    assert "admin-mid" in html
    assert "admin-audit-pane" in html
    assert "admin-backup-pane" in html
    assert html.index("<h2>Audit log</h2>") < html.index("<h2>History backup")
    assert html.index("<h2>History backup") < html.index("<h2>Create new backup")
    assert html.index("<h2>Create new backup") < html.index("Import / restore")
    assert html.index("admin-restore-pane") < html.index("admin-cli-pane")
    assert "cli-example" not in html
    assert "ssh admin@forgesre" not in html
    assert "admin-backup-left" in html
    assert "admin-user-col" in html
    assert html.index("<h2>Create user</h2>") < html.index('class="form-grid person-grid"')
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert "grid-template-columns: 1fr 1fr" in css.split(".admin-user-split {", 1)[1].split("}", 1)[0]
    assert "minmax(16rem, 1fr) minmax(22rem, 2fr)" in css
    assert ".admin-mid" in css
    assert "grid-template-areas: \"inc ai app\" \"infra ai app\"" in css.replace("\n", " ") or 'grid-template-areas: "inc ai app" "infra ai app"' in css


def test_dashboard_forgeai_cube_and_on_duty():
    html = _client().get("/").text
    top = html.split('class="dash-top"', 1)[1].split('class="dash-band"', 1)[0]
    assert "dash-ai-cube" in top
    assert "data-appliance-card" in top
    assert "appliance-oncall" in top
    assert "On duty" in top
    assert "data-forgeai-insights" in top
    assert html.index("data-forgeai-insights") < html.index("<h2>Recent incidents</h2>")
