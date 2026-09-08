"""GUI list pager: 10 rows per page, clamp, ?page=, required operator lists."""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.history import PAGE_SIZE, page_numbers, paginate, parse_page
from app.inventory import create_manual_asset
from app.journal import report
from app.main import app
from app.models import Asset, DiscoveryCandidate, Incident, JournalEntry, Notification, ScheduledReport
from app.seed import seed
from app.services import next_incident_number


def test_paginate_page_two_of_twenty_five_and_last_remainder():
    assert PAGE_SIZE == 10
    rows = list(range(25))
    page_one, first = paginate(rows, "1")
    assert page_one == list(range(10))
    assert first["page"] == 1
    assert first["pages"] == 3
    assert first["total"] == 25
    page_two, mid = paginate(rows, "2")
    assert page_two == list(range(10, 20))
    assert mid["page"] == 2
    last, end = paginate(rows, "3")
    assert last == list(range(20, 25))
    assert end["page"] == 3
    assert len(last) == 5
    clamped, past = paginate(rows, "99")
    assert clamped == last
    assert past["page"] == 3
    garbage, bad = paginate(rows, "nope")
    assert garbage == page_one
    assert bad["page"] == 1
    empty, empty_pager = paginate([], "4")
    assert empty == []
    assert empty_pager["page"] == 1
    assert empty_pager["pages"] == 1


def test_parse_page_and_numbers():
    page, pages, offset = parse_page("2", total=25)
    assert (page, pages, offset) == (2, 3, 10)
    assert page_numbers(1, 3) == [1, 2, 3]
    nums = page_numbers(1, 20)
    assert nums[0] == 1
    assert None in nums
    assert nums[-1] == 20


def _login():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    token = uuid4().hex[:8]
    for i in range(25):
        db.add(
            Incident(
                number=next_incident_number(db),
                title=f"PageTest {i:02d}",
                severity="WARNING",
                status="OPEN",
                fingerprint=f"pagetest:{token}:{i}",
                started_at=datetime.now(timezone.utc),
                summary="pager fixture",
            )
        )
        db.flush()
    db.commit()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return db, client


def _tbody(html: str) -> str:
    return html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]


def _tbody_incidents(html: str) -> list[str]:
    return [chunk.split('href="/incidents/', 1)[1].split('"', 1)[0] for chunk in _tbody(html).split("<tr") if 'href="/incidents/' in chunk]


def _row_count(html: str) -> int:
    return _tbody(html).count("<tr")


def _assert_bottom_pager(html: str, *, page_param: str = "page", page: int = 1):
    assert 'class="pager"' in html
    assert "Previous" in html
    assert "Next" in html
    assert 'aria-current="page"' in html
    assert f"{page_param}=" in html
    if page == 1:
        assert "pager-disabled" in html
        assert f"{page_param}=2" in html
    else:
        assert f"{page_param}={page - 1}" in html


def test_incidents_page_two_is_ten_rows():
    db, client = _login()
    first = client.get("/incidents")
    assert first.status_code == 200
    ids_one = _tbody_incidents(first.text)
    assert len(ids_one) == 10
    _assert_bottom_pager(first.text)
    second = client.get("/incidents?page=2")
    assert second.status_code == 200
    ids_two = _tbody_incidents(second.text)
    assert len(ids_two) == 10
    assert set(ids_one).isdisjoint(ids_two)
    third = client.get("/incidents?page=3")
    assert third.status_code == 200
    ids_three = _tbody_incidents(third.text)
    assert 1 <= len(ids_three) <= 10
    assert set(ids_two).isdisjoint(ids_three)
    assert "PageTest" in first.text
    filtered = client.get("/incidents?open=1")
    assert "name=\"page\"" not in filtered.text.split("<form", 1)[1].split("</form>", 1)[0]
    kept = client.get("/incidents?status=all&page=2")
    assert "status=all" in kept.text
    db.close()


def test_history_aligns_to_ten_per_page():
    db, client = _login()
    page = client.get("/history?days=90")
    assert page.status_code == 200
    ids = _tbody_incidents(page.text)
    assert len(ids) == 10
    _assert_bottom_pager(page.text)
    assert "10 per page" in page.text
    two = client.get("/history?days=90&page=2")
    assert two.status_code == 200
    assert "days=90" in two.text
    assert "name=\"page\"" not in two.text.split("<form", 1)[1].split("</form>", 1)[0]
    db.close()


def test_dashboard_recent_incidents_pages_ten():
    db, client = _login()
    home = client.get("/")
    assert home.status_code == 200
    section = home.text.split("Recent incidents", 1)[1]
    ids_one = _tbody_incidents(section)
    assert len(ids_one) == 10
    _assert_bottom_pager(section)
    two = client.get("/?page=2")
    assert two.status_code == 200
    ids_two = _tbody_incidents(two.text.split("Recent incidents", 1)[1])
    assert 1 <= len(ids_two) <= 10
    assert set(ids_one).isdisjoint(ids_two)
    preview = home.text.split("Recent journal reports", 1)[1].split("Recent incidents", 1)[0]
    assert 'class="pager"' not in preview
    db.close()


def test_journal_pages_ten_and_keeps_filters():
    db, client = _login()
    token = uuid4().hex[:8]
    for i in range(25):
        report(db, "jobs", "page-test", "ok", summary=f"Pager journal {token} {i:02d}")
    first = client.get("/journal")
    assert first.status_code == 200
    assert _row_count(first.text) == 10
    _assert_bottom_pager(first.text)
    filtered = client.get(f"/journal?status=ok&q={token}&page=2")
    assert filtered.status_code == 200
    assert 1 <= _row_count(filtered.text) <= 10
    assert "status=ok" in filtered.text
    assert token in filtered.text
    form = filtered.text.split("<form", 1)[1].split("</form>", 1)[0]
    assert 'name="page"' not in form
    assert db.query(JournalEntry).filter(JournalEntry.summary.like(f"%{token}%")).count() >= 25
    db.close()


def test_assets_and_discovery_page_ten():
    db, client = _login()
    have = db.query(Asset).count()
    for i in range(max(0, 12 - have)):
        create_manual_asset(
            db,
            hostname=f"page-asset-{uuid4().hex[:8]}",
            ip=f"10.88.1.{20 + i}",
            actor="tester",
        )
    listed = client.get("/assets")
    assert listed.status_code == 200
    table = listed.text.split('<table class="asset-table"', 1)[1]
    assert _row_count(table) == 10
    _assert_bottom_pager(table)
    searched = client.get("/assets?q=forge-demo-01")
    assert searched.status_code == 200
    found = searched.text.split('<table class="asset-table"', 1)[1]
    assert _row_count(found) == 1
    assert "forge-demo-01" in found
    form = searched.text.split("<form", 1)[1].split("</form>", 1)[0]
    assert 'name="page"' not in form
    two = client.get("/assets?page=2")
    assert two.status_code == 200
    assert 1 <= _row_count(two.text.split('<table class="asset-table"', 1)[1]) <= 10

    existing = {row.ip for row in db.query(DiscoveryCandidate).all()}
    n = 0
    octet = 1
    while db.query(DiscoveryCandidate).count() < 12:
        ip = f"10.77.8.{octet}"
        octet += 1
        if ip in existing:
            continue
        db.add(DiscoveryCandidate(ip=ip, proposed_role="Unknown device", status="new", source="scan"))
        existing.add(ip)
        n += 1
        if n > 20:
            break
    db.commit()
    discovery = client.get("/discovery")
    assert discovery.status_code == 200
    assert _row_count(discovery.text) == 10
    _assert_bottom_pager(discovery.text)
    db.close()


def test_ops_mail_and_reports_page_ten():
    db, client = _login()
    token = uuid4().hex[:8]
    while db.query(Notification).count() < 12:
        i = db.query(Notification).count()
        db.add(
            Notification(
                target=f"page-{token}-{i}@example.local",
                subject=f"Pager mail {token} {i}",
                body="pager",
                status="generated",
                step_key="pager-test",
            )
        )
        db.flush()
    while db.query(ScheduledReport).count() < 12:
        i = db.query(ScheduledReport).count()
        db.add(
            ScheduledReport(
                name=f"pager-report-{token}-{i}",
                to_email="ops@example.local",
                interval_hours=6,
            )
        )
        db.flush()
    db.commit()
    ops = client.get("/ops")
    assert ops.status_code == 200
    mail = ops.text.split('id="mail"', 1)[1].split('id="reports"', 1)[0]
    reports = ops.text.split('id="reports"', 1)[1]
    assert _row_count(mail) == 10
    assert _row_count(reports) == 10
    _assert_bottom_pager(mail)
    _assert_bottom_pager(reports, page_param="reports_page")
    assert "#mail" in mail
    assert "#reports" in reports
    mail_two = client.get("/ops?page=2")
    mail_body = mail_two.text.split('id="mail"', 1)[1].split('id="reports"', 1)[0]
    assert 1 <= _row_count(mail_body) <= 10
    reports_two = client.get("/ops?reports_page=2")
    reports_body = reports_two.text.split('id="reports"', 1)[1]
    assert 1 <= _row_count(reports_body) <= 10
    db.close()
