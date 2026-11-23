"""SMTP test: sign-in and one mail to the From address, with a plain hint on a refused sign-in."""

from __future__ import annotations

import aiosmtplib

from app import email_service
from app.settings import settings


class FakeSMTP:
    sent: list = []
    fail_login = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def connect(self):
        return None

    async def login(self, user, password):
        if FakeSMTP.fail_login:
            raise aiosmtplib.SMTPAuthenticationError(535, "5.7.8 Username and Password not accepted")

    async def send_message(self, message):
        FakeSMTP.sent.append(message)

    async def quit(self):
        return None


def _email(monkeypatch, **email):
    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "notifications": {"email": {"enabled": True, "host": "smtp.gmail.com",
                                                                                          "port": 587, "tls": True, "from": "noc@gmail.com", **email}}})
    monkeypatch.setattr(type(settings), "smtp_username", property(lambda self: "noc@gmail.com"), raising=False)
    monkeypatch.setattr(type(settings), "smtp_password", property(lambda self: "app-password"), raising=False)
    monkeypatch.setattr(aiosmtplib, "SMTP", FakeSMTP)


def test_smtp_check_signs_in_and_mails_the_from_address(monkeypatch):
    _email(monkeypatch)
    FakeSMTP.sent, FakeSMTP.fail_login = [], False
    out = email_service.check()
    assert out["status"] == "ok" and "noc@gmail.com" in out["why"]
    assert FakeSMTP.sent[0]["To"] == "noc@gmail.com" and FakeSMTP.sent[0]["Subject"] == "ForgeSRE test mail"


def test_gmail_refused_sign_in_explains_the_app_password(monkeypatch):
    _email(monkeypatch)
    FakeSMTP.sent, FakeSMTP.fail_login = [], True
    out = email_service.check()
    assert out["status"] == "error" and "535" in out["why"] and "app password" in out["fix"]
    assert "app-password" not in str(out)


def test_smtp_is_a_known_settings_test(monkeypatch):
    from app.system_settings import REGISTRY, test_connection

    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "notifications": {"email": {"enabled": False}}})
    assert test_connection("smtp")["status"] == "disabled"
    assert any(r.key == "notifications.email.enabled" and r.test == "smtp" for r in REGISTRY)
