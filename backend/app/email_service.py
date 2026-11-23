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


def check(send: bool = True) -> dict:
    """Settings › Notifications › Test and ``./forgesre settings test smtp``.

    Signs in to the SMTP server (STARTTLS when on) and, with ``send``, mails one short test message
    to the From address, so the whole path is proven. Never raises; the 5 s timeout applies. The
    password is never part of the answer.
    """
    if not settings.email_enabled:
        return {"status": "disabled", "why": "notifications.email.enabled is false"}
    if not settings.smtp_host:
        return {"status": "error", "why": "no SMTP server (notifications.email.host)", "fix": "Settings › Notifications › SMTP server"}

    async def _run() -> None:
        kwargs: dict = {"hostname": settings.smtp_host, "port": int(settings.smtp_port or 587), "timeout": SMTP_TIMEOUT,
                        "start_tls": bool(settings.smtp_tls), "use_tls": False}
        if settings.smtp_tls:
            kwargs["tls_context"] = smtp_ssl_context(settings.smtp_host)
        client = aiosmtplib.SMTP(**kwargs)
        await client.connect()
        try:
            if settings.smtp_username:
                await client.login(settings.smtp_username, settings.smtp_password)
            if send:
                message = build_email_message(from_addr=settings.smtp_from, to_addr=settings.smtp_from,
                                              subject="ForgeSRE test mail", body="This is a test from ForgeSRE Settings. Mail works.")
                await client.send_message(message)
        finally:
            try:
                await client.quit()
            except Exception:  # noqa: BLE001 — the server may already have closed the line
                pass

    where = f"{settings.smtp_host}:{settings.smtp_port}"
    try:
        run_sync(_run())
    except aiosmtplib.SMTPAuthenticationError as exc:
        hint = ("Gmail: SMTP_USERNAME = the full address, SMTP_PASSWORD = an app password "
                "(myaccount.google.com/apppasswords), not the account password") if "gmail" in settings.smtp_host else \
               "check SMTP_USERNAME and SMTP_PASSWORD (./forgesre settings set-secret …)"
        return {"status": "error", "why": f"{where} refused the sign-in ({exc.code})", "fix": hint}
    except Exception as exc:  # noqa: BLE001 — a check reports, it never raises
        return {"status": "error", "why": f"{where}: {type(exc).__name__}: {str(exc)[:160]}",
                "fix": "check host, port (587 STARTTLS) and that outbound SMTP is open"}
    return {"status": "ok", "why": f"signed in to {where}" + (f" and sent a test mail to {settings.smtp_from}" if send else "")}
