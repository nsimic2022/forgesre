"""GUI list pager: 10 rows per page, clamp, incidents ?page=."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.history import PAGE_SIZE, page_numbers, paginate, parse_page
from app.main import app
from app.models import Incident
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
    for i in range(25):
        db.add(
            Incident(
                number=next_incident_number(db),
                title=f"PageTest {i:02d}",
                severity="WARNING",
                status="OPEN",
                fingerprint=f"pagetest:{i}",
                started_at=datetime.now(timezone.utc),
                summary="pager fixture",
            )
        )
        db.flush()
    db.commit()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return db, client


def _tbody_incidents(html: str) -> list[str]:
    table = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    return [chunk.split('href="/incidents/', 1)[1].split('"', 1)[0] for chunk in table.split("<tr") if 'href="/incidents/' in chunk]


def test_incidents_page_two_is_ten_rows():
    db, client = _login()
    first = client.get("/incidents")
    assert first.status_code == 200
    ids_one = _tbody_incidents(first.text)
    assert len(ids_one) == 10
    assert 'class="pager"' in first.text
    assert "page=2" in first.text
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
    db.close()


def test_history_aligns_to_ten_per_page():
    db, client = _login()
    page = client.get("/history?days=90")
    assert page.status_code == 200
    ids = _tbody_incidents(page.text)
    assert len(ids) <= 10
    assert "10 per page" in page.text
    db.close()
