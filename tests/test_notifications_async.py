"""Async SMTP (aiosmtplib) and webhook POST. Escalation stays email-only."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.email_service import SMTP_TIMEOUT, send_smtp
from app.notifications import send_email, send_webhook

ROOT = Path(__file__).resolve().parents[1]


def test_email_service_uses_aiosmtplib_not_smtplib():
    src = (ROOT / "backend" / "app" / "email_service.py").read_text(encoding="utf-8")
    assert "aiosmtplib.send" in src
    assert "smtplib.SMTP" not in src
    assert SMTP_TIMEOUT == 5.0
    notes = (ROOT / "backend" / "app" / "notifications.py").read_text(encoding="utf-8")
    assert "send_smtp_async" in notes
    assert "NotificationManager" not in notes


def test_send_email_calls_aiosmtplib(monkeypatch):
    seen: list[dict] = []

    async def fake_send(message, **kwargs):
        seen.append({"message": message, **kwargs})

    monkeypatch.setattr("app.email_service.aiosmtplib.send", fake_send)

    async def _run():
        return await send_email("ops@example.test", "Alarm", "<p>hi</p>", body="hi")

    assert asyncio.run(_run()) is True
    assert seen
    assert seen[0]["timeout"] == 5.0
    assert seen[0]["hostname"]
    assert seen[0]["message"]["To"] == "ops@example.test"


def test_send_email_returns_false_on_error(monkeypatch):
    async def boom(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("smtp down")

    monkeypatch.setattr("app.email_service.aiosmtplib.send", boom)

    async def _run():
        return await send_email("ops@example.test", "Alarm", "x")

    assert asyncio.run(_run()) is False


def test_send_smtp_sync_wrapper(monkeypatch):
    seen: list[str] = []

    async def fake_send(message, **kwargs):
        del kwargs
        seen.append(str(message["To"]))

    monkeypatch.setattr("app.email_service.aiosmtplib.send", fake_send)
    send_smtp("ops@example.test", "Subj", "body")
    assert seen == ["ops@example.test"]


def test_send_webhook_posts_json(monkeypatch):
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.notifications.httpx.AsyncClient", fake_client)
    assert asyncio.run(send_webhook("http://hooks.example/noc", {"text": "down"})) is True
    assert seen and b"down" in seen[0]


def test_send_webhook_rejects_non_http():
    async def _run():
        return await send_webhook("ftp://hooks.example/noc", {"text": "x"})

    assert asyncio.run(_run()) is False
