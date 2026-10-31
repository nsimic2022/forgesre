"""Outbound SMTP: MIME multipart/alternative (text/plain + optional text/html)."""

from __future__ import annotations

import ssl
from email.message import EmailMessage

import aiosmtplib

from app.metrics import run_sync
from app.settings import settings

SMTP_TIMEOUT = 5.0


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


async def send_smtp_async(target: str, subject: str, body: str, html: str | None = None) -> None:
    """aiosmtplib send. STARTTLS when smtp_tls; 5s cap so a hung MTA does not freeze a worker."""
    address = resolve_smtp_address(target)
    message = build_email_message(
        from_addr=settings.smtp_from,
        to_addr=address,
        subject=subject,
        body=body,
        html=html,
    )
    kwargs: dict = {
        "hostname": settings.smtp_host,
        "port": int(settings.smtp_port or 587),
        "timeout": SMTP_TIMEOUT,
        "start_tls": bool(settings.smtp_tls),
        "use_tls": False,
    }
    if settings.smtp_tls:
        kwargs["tls_context"] = smtp_ssl_context(settings.smtp_host)
    if settings.smtp_username:
        kwargs["username"] = settings.smtp_username
        kwargs["password"] = settings.smtp_password
    await aiosmtplib.send(message, **kwargs)


def send_smtp(target: str, subject: str, body: str, html: str | None = None) -> None:
    """Sync wrapper for pytest and ``Depends(get_db)`` callers."""
    run_sync(send_smtp_async(target, subject, body, html=html))
