"""On-box mailbox profile: SMTP TLS helper, CLI, compose (no Docker required)."""

from __future__ import annotations

import ssl
import subprocess
from pathlib import Path

from app.services import smtp_ssl_context
from app.web import smtp_provider_id

ROOT = Path(__file__).resolve().parents[1]


def test_smtp_provider_id_is_off_when_yaml_email_disabled():
    assert smtp_provider_id() == "off"


def test_smtp_ssl_skips_verify_only_for_loopback():
    local = smtp_ssl_context("127.0.0.1")
    assert local.verify_mode == ssl.CERT_NONE
    assert local.check_hostname is False
    named = smtp_ssl_context("localhost")
    assert named.verify_mode == ssl.CERT_NONE
    remote = smtp_ssl_context("smtp.gmail.com")
    assert remote.verify_mode == ssl.CERT_REQUIRED
    assert remote.check_hostname is True


def test_compose_mailbox_profile_is_opt_in():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "profiles: [\"mailbox\"]" in text
    assert "docker-mailserver" in text
    assert "roundcubemail" in text
    core_idx = text.index("  core:")
    prom_idx = text.index("  prometheus:")
    mail_idx = text.index("  mailserver:")
    assert core_idx < mail_idx
    core_block = text[core_idx:prom_idx]
    assert "profiles:" not in core_block
    assert "127.0.0.1:587:587" in text
    assert "network_mode: host" not in text[mail_idx:]


def test_forgesre_help_documents_mailbox():
    overview = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help"], text=True)
    assert "mailbox" in overview
    detail = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help", "mailbox"], text=True)
    assert "Roundcube" in detail
    assert "Gmail" in detail
    assert "Outlook" in detail
    assert "--bind-core" in detail
    assert "Mailpit" in detail
    script = (ROOT / "scripts/mailbox.sh").read_text()
    assert "docker-mailserver" in script
    assert "BIND_CORE" in script
    assert "MAILBOX_PASSWORD" in script
    assert script.splitlines()[0].startswith("#!/")
