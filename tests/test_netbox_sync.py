"""Core NetBox sync must not 403, and the journal must stay short."""

from __future__ import annotations

import httpx
import pytest

from app.db import Base, SessionLocal, engine
from app.inventory import sync_netbox
from app.journal import list_entries, report
from app.models import JournalEntry
from app.netbox import format_client_error, list_devices
from app.seed import seed

MDN = "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403"


def _httpx_403() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://127.0.0.1:8001/api/dcim/devices/?limit=200")
    response = httpx.Response(403, request=request)
    return httpx.HTTPStatusError(
        f"Client error '403 Forbidden' for url '{request.url}'\nFor more information check: {MDN}",
        request=request,
        response=response,
    )


def test_format_client_error_drops_mdn_on_403():
    msg = format_client_error(_httpx_403())
    assert "403" in msg
    assert "mozilla" not in msg.lower()
    assert "For more information check" not in msg
    assert "devices" in msg


def test_format_client_error_strips_mdn_from_plain_string():
    raw = (
        "Client error '403 Forbidden' for url 'http://127.0.0.1:8001/api/dcim/devices/?limit=200'\n"
        f"For more information check: {MDN}"
    )
    msg = format_client_error(RuntimeError(raw))
    assert "403" in msg
    assert "mozilla" not in msg.lower()
    assert "For more information check" not in msg


def test_list_devices_empty_token_does_not_call_netbox():
    with pytest.raises(RuntimeError, match="NETBOX_API_TOKEN is empty"):
        list_devices("http://127.0.0.1:8001", "")


def test_list_devices_403_is_short(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    real = httpx.Client

    def wrapped(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", wrapped)
    with pytest.raises(RuntimeError) as caught:
        list_devices("http://127.0.0.1:8001", "a" * 40)
    msg = str(caught.value)
    assert "403" in msg
    assert "mozilla" not in msg.lower()
    assert "For more information check" not in msg


def test_list_devices_reads_results(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 7,
                        "name": "sw-lab-01",
                        "primary_ip": {"address": "10.1.2.3/24"},
                        "device_type": {"model": "switch"},
                        "status": {"value": "active"},
                    }
                ],
                "next": None,
            },
            request=request,
        )

    real = httpx.Client

    def wrapped(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", wrapped)
    rows = list_devices("http://127.0.0.1:8001", "a" * 40)
    assert rows == [
        {
            "netbox_id": "7",
            "name": "sw-lab-01",
            "ip": "10.1.2.3",
            "type": "switch",
            "status": "active",
        }
    ]


def test_sync_netbox_403_journal_is_short_and_deduped(monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.query(JournalEntry).filter_by(module="netbox").delete()
    db.commit()
    monkeypatch.setattr("app.settings.Settings.netbox_enabled", True)
    monkeypatch.setattr("app.settings.Settings.netbox_url", "http://127.0.0.1:8001")
    monkeypatch.setattr("app.settings.Settings.netbox_token", "a" * 40)

    def boom(*_a, **_k):
        raise RuntimeError(format_client_error(_httpx_403()))

    monkeypatch.setattr("app.netbox.list_devices", boom)
    first = sync_netbox(db)
    second = sync_netbox(db)
    db.close()
    assert first["synced"] == 0
    assert "403" in first["error"]
    assert "mozilla" not in first["error"].lower()
    assert second["error"] == first["error"]

    db = SessionLocal()
    rows = [row for row in list_entries(db, module="netbox") if row.action == "sync" and row.status == "error"]
    db.close()
    assert len(rows) == 1
    assert rows[0].summary == "NetBox sync failed: HTTP 403"
    assert "mozilla" not in rows[0].detail.lower()
    assert "For more information check" not in rows[0].detail


def test_journal_report_strips_mdn_and_dedupes_netbox_403():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.query(JournalEntry).filter_by(module="netbox").delete()
    db.commit()
    detail = (
        "Client error '403 Forbidden' for url 'http://127.0.0.1:8001/api/dcim/devices/?limit=200'\n"
        f"For more information check: {MDN}"
    )
    first = report(db, "netbox", "sync", "error", summary="NetBox sync failed: HTTP 403", detail=detail)
    second = report(db, "netbox", "sync", "error", summary="NetBox sync failed: HTTP 403", detail=detail)
    rows = [row for row in list_entries(db, module="netbox") if row.action == "sync" and row.status == "error"]
    db.close()
    assert first is not None and second is not None
    assert first.id == second.id
    assert len(rows) == 1
    assert "mozilla" not in rows[0].detail.lower()
    assert "For more information check" not in rows[0].detail
    assert "403" in rows[0].detail
