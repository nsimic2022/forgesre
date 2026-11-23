"""NetBox and LibreNMS as optional Compose profiles: compose, update migration, ./forgesre optional."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_bundled_netbox_and_librenms_are_profiles():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    for name in ("netbox-redis", "netbox-db-init", "netbox"):
        assert services[name]["profiles"] == ["netbox"]
    for name in ("librenms-db", "librenms-redis", "librenms", "librenms-dispatcher"):
        assert services[name]["profiles"] == ["librenms"]
    for name in ("postgres", "core", "prometheus", "snmp-exporter", "blackbox-exporter"):
        assert "profiles" not in services[name]


def test_update_keeps_both_on_old_installs_and_starts_only_enabled_parts():
    update = (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    migrate = update.index("FORGESRE_OPTIONAL_PROFILES")
    assert migrate < update.index('"${DC[@]}" pull') and "profile_add netbox" in update and "profile_add librenms" in update
    assert '[[ "$NETBOX_ON" -eq 1 ]] && extras+=(netbox-redis netbox)' in update
    assert 'if [[ "$LIBRENMS_ON" -eq 1 ]]; then\n  echo "Waiting for LibreNMS' in update
    install = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "--netbox) BUNDLED_NETBOX" in install and "--librenms) BUNDLED_LIBRENMS" in install
    assert "FORGESRE_OPTIONAL_PROFILES" in install and "enabled: ${librenms_enabled}" in install


def _lib(env_file: Path, script: str) -> str:
    done = subprocess.run(["bash", "-c", f'set -euo pipefail; ENV_FILE="{env_file}"; source "{ROOT}/scripts/lib-profiles.sh"; {script}'],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_profile_helpers(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("COMPOSE_PROFILES= ai , mailbox\nOTHER=1\n")
    _lib(env, "profile_add netbox; profile_add netbox; profile_add librenms; profile_remove mailbox")
    assert "COMPOSE_PROFILES=ai,netbox,librenms" in env.read_text()
    assert _lib(env, "profile_on ai && echo yes || echo no").strip() == "yes"
    assert _lib(env, "profile_on mail && echo yes || echo no").strip() == "no"
    _lib(env, "env_unset OTHER; env_set NEW 'a b'")
    assert "OTHER=" not in env.read_text() and "NEW=a b" in env.read_text()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    for name in ("optional.sh", "lib-profiles.sh"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    for name in ("ensure-netbox-secrets.sh", "ensure-librenms-secrets.sh"):
        (tmp_path / "scripts" / name).write_text(f"#!/bin/sh\necho {name} >> {tmp_path}/calls.log\n")
        (tmp_path / "scripts" / name).chmod(0o755)
    cli = tmp_path / "forgesre"
    cli.write_text(f"#!/bin/sh\necho forgesre \"$@\" >> {tmp_path}/calls.log\n")
    cli.chmod(0o755)
    (tmp_path / ".env").write_text("COMPOSE_PROFILES=ai,netbox,librenms\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(f"#!/bin/sh\necho docker \"$@\" >> {tmp_path}/calls.log\nexit 0\n")
    (bin_dir / "docker").chmod(0o755)
    return tmp_path


def _run(clone: Path, *args: str) -> str:
    env = {**os.environ, "PATH": f"{clone / 'bin'}:{os.environ['PATH']}"}
    done = subprocess.run(["bash", str(clone / "scripts" / "optional.sh"), *args], cwd=clone, env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr + done.stdout
    return done.stdout


def test_optional_disable_and_enable(clone: Path):
    listing = _run(clone, "list")
    assert "netbox    on" in listing and "https     off" in listing
    _run(clone, "disable", "librenms")
    text = (clone / ".env").read_text()
    assert "COMPOSE_PROFILES=ai,netbox\n" in text and "FORGESRE_OPTIONAL_PROFILES=1" in text
    calls = (clone / "calls.log").read_text()
    assert "forgesre settings set inventory.librenms.enabled false" in calls
    assert "compose stop librenms-db librenms-redis librenms librenms-dispatcher" in calls
    _run(clone, "enable", "librenms")
    assert "COMPOSE_PROFILES=ai,netbox,librenms" in (clone / ".env").read_text()
    calls = (clone / "calls.log").read_text()
    assert "ensure-librenms-secrets.sh" in calls and "compose up -d librenms-db librenms-redis librenms librenms-dispatcher" in calls
    assert "inventory.librenms.enabled true" in calls
    env = {**os.environ, "PATH": f"{clone / 'bin'}:{os.environ['PATH']}"}
    bad = subprocess.run(["bash", str(clone / "scripts" / "optional.sh"), "enable", "grafana"], cwd=clone, env=env, capture_output=True, text=True)
    assert bad.returncode == 1
