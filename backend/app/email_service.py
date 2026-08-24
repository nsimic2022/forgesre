"""Outbound SMTP: MIME multipart/alternative (text/plain + optional text/html)."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from app.settings import settings


def smtp_ssl_context(host: str):
    """STARTTLS context. Local docker-mailserver uses a self-signed cert."""
    ctx = ssl.create_default_context()
    if (host or "").lower() in {"127.0.0.1", "localhost", "::1"}:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def resolve_smtp_address(target: str) -> str:
    return target.strip() if "@" in (target or "") else f"{target}@forgesre.local"


def build_email_message(
    *,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    html: str | None = None,
) -> EmailMessage:
    """stdlib EmailMessage. HTML last so clients prefer it (RFC 2046)."""
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = to_addr
    message["Subject"] = subject
    message.set_content(body or "")
    if html:
        message.add_alternative(html, subtype="html")
    return message


def compose_email_message(
    *,
    sender: str,
    to: str,
    subject: str,
    body: str,
    html_body: str | None = None,
) -> EmailMessage:
    """Alias used by tests: sender/to/html_body names."""
    return build_email_message(
        from_addr=sender,
        to_addr=to,
        subject=subject,
        body=body,
        html=html_body,
    )


def send_smtp(target: str, subject: str, body: str, html: str | None = None) -> None:
    address = resolve_smtp_address(target)
    message = build_email_message(
        from_addr=settings.smtp_from,
        to_addr=address,
        subject=subject,
        body=body,
        html=html,
    )
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        if settings.smtp_tls:
            client.starttls(context=smtp_ssl_context(settings.smtp_host))
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)
