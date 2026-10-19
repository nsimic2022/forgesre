"""I'm on duty now, and the Dashboard ForgeAI digest (SQL facts, DEMO excluded)."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.dc_noc import add_oncall_slot, current_oncall, take_duty
from app.insights import insight_facts, insight_sentences, insights_csv, insights_text
from app.inventory import create_manual_asset
from app.main import app
from app.models import Asset, Incident, Notification, OncallSlot, User, utcnow
from app.seed import seed
from app.security import hash_password
from app.services import next_incident_number
from app.settings import settings


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client(email: str = "admin@forgesre.local") -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": email, "password": "testpass"}, follow_redirects=False)
    return client


def _token() -> str:
    return "".join("abcdef"[int(ch, 16) % 6] for ch in uuid4().hex[:8])


def _user(db, role: str) -> User:
    email = f"{role}-{_token()}@forgesre.local"
    row = User(email=email, name=role.title(), password_hash=hash_password("testpass"), role=role)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _incident(db, asset, *, title, status="OPEN", started=None, ack=False, demo=False, alertname=""):
    number = next_incident_number(db)
    labels = {"alertname": alertname or title}
    if demo:
        labels["asset"] = "forge-demo-01"
    row = Incident(
        number=number,
        title=title,
        severity="WARNING",
        status=status,
        fingerprint=f"ins:{number}",
        started_at=started or utcnow(),
        asset_id=asset.id if asset is not None else None,
        alert_payload={"labels": labels},
        ack_at=utcnow() if ack else None,
        ack_by="ana" if ack else "",
    )
    db.add(row)
    db.flush()
    return row


def test_later_take_duty_wins_and_dashboard_shows_the_button():
    db = _db()
    zone = __import__("zoneinfo", fromlist=["ZoneInfo"]).ZoneInfo(settings.timezone)
    now = utcnow().astimezone(zone)
    if now.hour == 23 and now.minute >= 59:
        db.close()
        return
    ana_mail = f"ana-{_token()}@dc.local"
    add_oncall_slot(db, weekday=now.weekday(), start="00:00", end="23:59", name="Ana", email=ana_mail)
    db.commit()
    admin = db.query(User).filter_by(email="admin@forgesre.local").one()
    slot = take_duty(db, admin, at=now)
    db.commit()
    person = current_oncall(db, now)
    assert person is not None
    assert person["email"] == admin.email
    assert person["name"] == "Admin"
    assert person["person"] == "Admin"
    assert person["name"] != "Super Admin"
    assert slot["email"] == admin.email
    earlier = db.query(OncallSlot).filter_by(email=ana_mail).one()
    assert int(earlier.end_minute) < now.hour * 60 + now.minute
    db.close()

    client = _client()
    home = client.get("/")
    assert home.status_code == 200
    assert "I'm on duty now" in home.text
    assert 'action="/escalation/oncall/take"' in home.text
    assert admin.email in home.text or "On call" in home.text
    took = client.post("/escalation/oncall/take", data={"next": "/"}, follow_redirects=False)
    assert took.status_code == 303
    assert took.headers["location"] == "/"
    again = client.get("/")
    assert admin.email in again.text

    viewer = _user(_db(), "viewer")
    denied = _client(viewer.email)
    assert denied.post("/escalation/oncall/take", data={"next": "/"}, follow_redirects=False).status_code == 403

    # Shared test DB: do not leave a later-start admin window for other on-call tests.
    tidy = _db()
    tidy.query(OncallSlot).filter(OncallSlot.email.in_([admin.email, ana_mail])).delete(synchronize_session=False)
    tidy.commit()
    tidy.close()


def test_insights_exclude_demo_and_flag_repeats_parent_and_aging():
    db = _db()
    token = _token()
    parent = create_manual_asset(db, hostname=f"parent-{token}", ip="10.40.1.2", actor="tester", asset_id=f"parent-{token}")
    child = create_manual_asset(db, hostname=f"child-{token}", ip="10.40.1.3", actor="tester", asset_id=f"child-{token}")
    child.parent_id = parent.id
    noisy = create_manual_asset(
        db,
        hostname=f"noisy-{token}",
        ip="10.40.1.4",
        actor="tester",
        asset_id=f"noisy-{token}",
    )
    noisy.extras = {"site": f"dc-{token}", "row": "A", "rack": "R1"}
    db.commit()
    now = utcnow()
    for offset in (1, 10, 20):
        _incident(
            db,
            noisy,
            title="HighCPU",
            alertname="HighCPU",
            started=now - timedelta(hours=offset),
        )
    _incident(db, parent, title="ParentDown", alertname="NodeExporterDown", status="OPEN")
    _incident(db, child, title="ChildDisk", alertname="NodeFilesystemUsageHigh", status="OPEN")
    old = _incident(
        db,
        noisy,
        title="OldOpen",
        alertname="NodeMemoryHigh",
        status="OPEN",
        started=now - timedelta(hours=8),
    )
    demo = db.query(Asset).filter_by(asset_id="forge-demo-01").first()
    if demo is not None:
        _incident(db, demo, title="DemoCPU", alertname="HighCPU", demo=True)
    db.commit()

    facts = insight_facts(db, now=now)
    repeat_hosts = [item["hostname"] for item in facts["repeats"]]
    assert f"noisy-{token}" in repeat_hosts
    assert all("forge-demo" not in item["hostname"] for item in facts["repeats"])
    assert any(item["name"] == f"dc-{token}" or item["name"] == "R1" for item in facts["hot_groups"]) or facts["hot_groups"]
    pairs = [f"{item['child']}|{item['parent']}" for item in facts["parent_child"]]
    assert any(f"child-{token}" in pair and f"parent-{token}" in pair for pair in pairs)
    aging_nums = [item["number"] for item in facts["aging"]]
    assert old.number in aging_nums
    lines = insight_sentences(facts)
    assert lines
    assert any("noisy" in line or "HighCPU" in line or "repeated" in line.lower() for line in lines)
    panel = {"facts": facts, "sentences": lines, "source": "facts"}
    text = "\n".join(insights_text(panel))
    assert "ForgeSRE ForgeAI digest" in text
    headers, rows = insights_csv(panel)
    assert headers[0] == "Kind"
    assert rows
    db.close()


def test_dashboard_forgeai_strip_and_report_export():
    db = _db()
    db.close()
    client = _client()
    html = client.get("/").text
    assert "data-forgeai-insights" in html
    assert 'id="forgeai"' in html
    assert ">Repeats<" in html
    assert ">Hot groups<" in html
    assert "Parent–child" in html or "Parent-child" in html
    assert ">Aging open<" in html
    assert 'action="/insights/export"' in html
    assert 'action="/insights/refresh"' in html
    exported = client.post("/insights/export", data={"format": "txt"})
    assert exported.status_code == 200
    assert 'filename="forgeai-digest.txt"' in exported.headers["content-disposition"]
    csv_file = client.post("/insights/export", data={"format": "csv"})
    assert csv_file.status_code == 200
    assert csv_file.content.startswith(b"\xef\xbb\xbf")
    assert 'filename="forgeai-digest.csv"' in csv_file.headers["content-disposition"]
    mailed = client.post("/insights/mail", data={"target": "ops-digest@example.local"}, follow_redirects=False)
    assert mailed.status_code == 303
    db = _db()
    row = (
        db.query(Notification)
        .filter_by(target="ops-digest@example.local", step_key="report")
        .order_by(Notification.id.desc())
        .first()
    )
    assert row is not None
    assert "ForgeAI" in (row.subject or "")
    db.close()
