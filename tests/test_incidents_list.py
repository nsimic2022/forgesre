"""Incidents list GUI: stacked cell, short #N, filters and pager unchanged."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.history import PAGE_SIZE
from app.main import app
from app.models import Incident
from app.seed import seed
from app.services import incident_seq, next_incident_number

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(client: TestClient) -> None:
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )


def test_incidents_list_columns_short_id_and_full_href():
    db = _db()
    now = datetime.now(timezone.utc)
    number = next_incident_number(db, now)
    seq = incident_seq(number)
    row = Incident(
        number=number,
        title="Very long disk-full title that should still link by full id",
        severity="CRITICAL",
        status="OPEN",
        fingerprint=f"list-gui:{uuid4().hex}",
        started_at=now,
        summary="list gui",
    )
    db.add(row)
    db.commit()
    client = TestClient(app)
    _login(client)
    listed = client.get("/incidents")
    assert listed.status_code == 200
    headers = listed.text.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert ">Incident<" in headers
    assert ">Severity<" in headers
    assert ">Status<" in headers
    assert ">When<" in headers
    assert "Reported to" not in headers
    assert ">Asset<" not in headers
    assert ">Started<" not in headers
    assert f'href="/incidents/{number}"' in listed.text
    assert f'title="{number}"' in listed.text
    assert f">#{seq}<" in listed.text
    assert "pill demo" not in listed.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert 'class="pill crit"' in listed.text
    assert 'class="pill open"' in listed.text
    assert "incidents-table" in listed.text
    assert "inc-cell" in listed.text
    assert 'class="list-filters incidents-filters"' in listed.text
    assert 'class="muted list-reset"' in listed.text
    home = client.get("/")
    assert home.status_code == 200
    recent = home.text.split("Recent incidents", 1)[1]
    assert f'href="/incidents/{number}"' in recent
    assert f">#{seq}<" in recent
    history = client.get("/history")
    assert history.status_code == 200
    assert f'href="/incidents/{number}"' in history.text
    assert f">#{seq}<" in history.text
    db.close()


def test_incidents_list_does_not_500_and_keeps_filters():
    db = _db()
    client = TestClient(app)
    _login(client)
    listed = client.get("/incidents")
    assert listed.status_code == 200
    closed = client.get("/incidents?status=closed")
    assert closed.status_code == 200
    tbody = closed.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert "OPEN" not in tbody
    opened = client.get("/incidents?status=open")
    assert opened.status_code == 200
    open_body = opened.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert "CLOSED" not in open_body
    assert "RESOLVED" not in open_body
    empty = client.get("/incidents?status=open&days=1")
    assert empty.status_code == 200
    if "No incidents" in empty.text:
        assert "All systems operational" not in empty.text
    db.close()


def test_incidents_list_pager_still_ten_and_showing_copy():
    db = _db()
    token = uuid4().hex[:8]
    for i in range(12):
        db.add(
            Incident(
                number=next_incident_number(db),
                title=f"ListPager {token} {i:02d}",
                severity="WARNING",
                status="OPEN",
                fingerprint=f"list-pager:{token}:{i}",
                started_at=datetime.now(timezone.utc),
                summary="pager",
            )
        )
        db.flush()
    db.commit()
    client = TestClient(app)
    _login(client)
    first = client.get("/incidents")
    assert first.status_code == 200
    assert PAGE_SIZE == 10
    ids = [
        chunk.split('href="/incidents/', 1)[1].split('"', 1)[0]
        for chunk in first.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0].split("<tr")
        if 'href="/incidents/' in chunk
    ]
    assert len(ids) == 10
    assert "Showing " in first.text
    assert " of " in first.text
    second = client.get("/incidents?status=all&page=2")
    assert second.status_code == 200
    ids_two = [
        chunk.split('href="/incidents/', 1)[1].split('"', 1)[0]
        for chunk in second.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0].split("<tr")
        if 'href="/incidents/' in chunk
    ]
    assert set(ids).isdisjoint(ids_two)
    assert "status=all" in second.text
    db.close()


def test_resolved_row_keeps_severity_pill_not_green():
    db = _db()
    now = datetime.now(timezone.utc)
    number = next_incident_number(db, now - timedelta(days=2))
    db.add(
        Incident(
            number=number,
            title="Resolved critical still shows heat as status",
            severity="CRITICAL",
            status="RESOLVED",
            fingerprint=f"list-resolved:{uuid4().hex}",
            started_at=now - timedelta(days=2),
            summary="resolved",
        )
    )
    db.commit()
    client = TestClient(app)
    _login(client)
    listed = client.get("/incidents?status=closed")
    assert listed.status_code == 200
    tbody = listed.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert f'href="/incidents/{number}"' in tbody
    assert "inc-row-done" in tbody
    assert 'class="pill crit"' in tbody
    assert 'class="pill resolved"' in tbody
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".incidents-table tr.inc-ok td:first-child" in css
    assert ".inc-demo" in css
    html = (ROOT / "frontend" / "templates" / "incidents.html").read_text(encoding="utf-8")
    assert "ACTIVE" not in html
    assert "All systems operational" not in html
    db.close()
