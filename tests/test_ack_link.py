"""One-click acknowledge from mail: signed link, GET only shows a page, POST acknowledges."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app import ack_links
from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Incident, utcnow
from app.notifications import build_escalation_body, build_escalation_html
from app.settings import settings


def _incident(status="OPEN"):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tag = uuid4().hex[:6]
    asset = Asset(asset_id=f"ack-{tag}", hostname=f"ack-{tag}", ip="10.99.0.1", type="Linux Server")
    db.add(asset)
    db.flush()
    row = Incident(number=f"INC-A{tag}", title="CPU 96%", severity="HIGH", status=status, fingerprint=f"ack:{tag}",
                   asset_id=asset.id, started_at=utcnow())
    db.add(row)
    db.commit()
    db.refresh(row)
    return db, row


def test_links_need_a_public_url_and_go_into_both_mail_parts(monkeypatch):
    old = settings.yaml
    try:
        settings.yaml = {**(old or {}), "system": {"public_url": ""}}
        assert ack_links.ack_url(1, "ana@dc.local") == ""
        settings.yaml = {**(old or {}), "system": {"public_url": "https://forgesre.dc.local/"}}
        url = ack_links.ack_url(7, "ana@dc.local")
        assert url.startswith("https://forgesre.dc.local/ack/")
        token = url.rsplit("/", 1)[1]
        assert ack_links.read_token(token) == {"incident_id": 7, "recipient": "ana@dc.local"}
    finally:
        settings.yaml = old
    db, inc = _incident()
    text = build_escalation_body(inc, "immediate", "team", "https://x/ack/abc")
    html = build_escalation_html(inc, "immediate", "team", "https://x/ack/abc")
    assert text.startswith("Acknowledge (stops further escalation): https://x/ack/abc")
    assert 'href="https://x/ack/abc"' in html
    assert "Acknowledge" not in build_escalation_body(inc, "immediate", "team")
    db.close()


def test_get_never_acks_post_does_once_and_bad_links_are_refused():
    db, inc = _incident()
    token = ack_links.make_token(inc.id, "ana@dc.local")
    number, inc_id = inc.number, inc.id
    db.close()
    client = TestClient(create_app())
    page = client.get(f"/ack/{token}")
    assert page.status_code == 200 and "Acknowledge" in page.text and number in page.text
    db = SessionLocal()
    assert db.get(Incident, inc_id).ack_at is None  # a mail scanner opening the link changes nothing
    db.close()
    done = client.post(f"/ack/{token}")
    assert done.status_code == 200 and "Escalation has stopped" in done.text
    db = SessionLocal()
    row = db.get(Incident, inc_id)
    assert row.ack_at is not None and row.ack_by == "ana@dc.local" and row.status == "INVESTIGATING"
    db.close()
    assert "Already acknowledged" in client.get(f"/ack/{token}").text
    assert "not valid" in client.get("/ack/garbage").text
    expired = ack_links._serializer.dumps({"i": inc_id, "r": "x"})
    try:
        ack_links.read_token(expired, max_age=-1)
        raise AssertionError("expected expiry")
    except ValueError as exc:
        assert str(exc) == "expired"
    db2, resolved = _incident(status="RESOLVED")
    assert "already resolved" in client.get(f"/ack/{ack_links.make_token(resolved.id, 'x')}").text
    db2.close()
