"""HTTPS front: compose service, Caddyfile, ./forgesre https on|only|off|cert, doctor row."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_proxy_service_and_core_flags():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    proxy = compose["services"]["proxy"]
    assert proxy["profiles"] == ["https"] and proxy["network_mode"] == "host"
    assert proxy["image"].startswith("caddy:2.")
    assert any("Caddyfile" in v for v in proxy["volumes"]) and any("/tls:/certs" in v for v in proxy["volumes"])
    command = compose["services"]["core"]["command"]
    assert "--host ${FORGESRE_BIND:-0.0.0.0}" in command and "--proxy-headers" in command and "--forwarded-allow-ips 127.0.0.1" in command
    caddy = (ROOT / "config" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "tls /certs/forgesre.crt /certs/forgesre.key" in caddy and "reverse_proxy 127.0.0.1:{$FORGESRE_HTTP_PORT:8080}" in caddy


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    (tmp_path / "scripts").mkdir()
    for name in ("https.sh", "lib-profiles.sh"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / ".env").write_text("FORGESRE_DATA=./data\nFORGESRE_HTTP_PORT=8080\nCOMPOSE_PROFILES=ai\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {tmp_path}/docker.log\nexit 0\n")
    fake.chmod(0o755)
    return tmp_path


def _run(clone: Path, *args: str) -> str:
    env = {**os.environ, "PATH": f"{clone / 'bin'}:{os.environ['PATH']}"}
    done = subprocess.run(["bash", str(clone / "scripts" / "https.sh"), *args], cwd=clone, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr + done.stdout
    return done.stdout


def _env(clone: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (clone / ".env").read_text().splitlines() if "=" in line)


def test_https_on_only_off(clone: Path):
    out = _run(clone, "on")
    env = _env(clone)
    assert env["COMPOSE_PROFILES"] == "ai,https" and env["FORGESRE_HTTPS"] == "on" and env["FORGESRE_HTTPS_PORT"] == "443"
    assert "FORGESRE_BIND" not in env
    crt = clone / "data" / "tls" / "forgesre.crt"
    assert crt.is_file() and oct((clone / "data" / "tls" / "forgesre.key").stat().st_mode)[-3:] == "600"
    assert "IP:127.0.0.1" in out or "127.0.0.1" in subprocess.run(["openssl", "x509", "-in", str(crt), "-noout", "-ext", "subjectAltName"],
                                                                   capture_output=True, text=True).stdout
    assert "up -d proxy" in (clone / "docker.log").read_text()
    _run(clone, "only")
    env = _env(clone)
    assert env["FORGESRE_BIND"] == "127.0.0.1" and env["FORGESRE_COOKIE_SECURE"] == "true" and env["COMPOSE_PROFILES"] == "ai,https"
    status = _run(clone, "status")
    assert "HTTPS: on, port 443" in status and "Mode: only" in status and "subject" in status
    _run(clone, "cert", "noc.dc.local", "10.1.2.3")
    san = subprocess.run(["openssl", "x509", "-in", str(crt), "-noout", "-ext", "subjectAltName"], capture_output=True, text=True).stdout
    assert "DNS:noc.dc.local" in san and "IP Address:10.1.2.3" in san
    _run(clone, "off")
    env = _env(clone)
    assert env["COMPOSE_PROFILES"] == "ai" and env["FORGESRE_HTTPS"] == "off" and "FORGESRE_BIND" not in env and "FORGESRE_COOKIE_SECURE" not in env


def test_doctor_row_off_by_default(monkeypatch):
    from app.api import _https_check

    monkeypatch.delenv("FORGESRE_HTTPS", raising=False)
    assert _https_check()["status"] == "disabled"
    monkeypatch.setenv("FORGESRE_HTTPS", "on")
    monkeypatch.setenv("FORGESRE_HTTPS_PORT", "1")
    assert _https_check()["status"] == "error"


def test_cli_install_and_update_know_https():
    assert 'exec "$ROOT/scripts/https.sh"' in (ROOT / "scripts" / "forgesre").read_text(encoding="utf-8")
    install = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "--https) ENABLE_HTTPS" in install and 'scripts/https.sh" on' in install
    assert "up -d proxy" in (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
