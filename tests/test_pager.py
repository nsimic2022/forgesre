"""GUI list pager: 10 rows per page, clamp, ?page=, required operator lists."""

from datetime import datetime, timezone
from uuid import uuid4

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
    assert first["start"] == 1
    assert first["end"] == 10
    assert first["size"] == 10
    page_two, mid = paginate(rows, "2")
    assert page_two == list(range(10, 20))
    assert mid["page"] == 2
    last, end = paginate(rows, "3")
    assert last == list(range(20, 25))
    assert end["page"] == 3
    assert len(last) == 5
    assert end["start"] == 21
    assert end["end"] == 25
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


