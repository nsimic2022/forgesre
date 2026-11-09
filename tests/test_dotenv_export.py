"""scripts/dotenv_export.py must never execute dotenv values (LibreNMS id|secret)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
EXPORT_PY = SCRIPTS / "dotenv_export.py"
LOAD_SH = SCRIPTS / "dotenv_load.sh"

sys.path.insert(0, str(SCRIPTS))
import dotenv_export  # noqa: E402


PIPE_TOKEN = "2|plainUiTokenValueFromLibreNms"
WEBHOOK = "webhook-abc123"


def _write_secrets(path: Path, *, quoted: bool = True) -> None:
    token_line = f'LIBRENMS_API_TOKEN="{PIPE_TOKEN}"' if quoted else f"LIBRENMS_API_TOKEN={PIPE_TOKEN}"
    path.write_text(
        "\n".join(
            [
                "# comment",
                "ALERTMANAGER_WEBHOOK_TOKEN=" + WEBHOOK,
                token_line,
                "SECRET_KEY=not-a-default",
                "orphan-wrapped-token-line-is-not-an-assignment",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_export_quotes_pipe_token_and_skips_orphans(tmp_path):
    secrets = tmp_path / "secrets.env"
    _write_secrets(secrets, quoted=False)
    text = dotenv_export.export_bash(secrets)
    assert "export LIBRENMS_API_TOKEN=" in text
    assert PIPE_TOKEN in text
    assert "orphan-wrapped" not in text
    assert dotenv_export.assignments(secrets)["LIBRENMS_API_TOKEN"] == PIPE_TOKEN
    got = subprocess.check_output(
        [sys.executable, str(EXPORT_PY), "--get", "LIBRENMS_API_TOKEN", str(secrets)],
        text=True,
    )
    assert got == PIPE_TOKEN


def test_bash_eval_export_keeps_pipe_token(tmp_path):
    secrets = tmp_path / "secrets.env"
    _write_secrets(secrets, quoted=False)
    script = tmp_path / "load.sh"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f'source "{LOAD_SH}"',
                f'dotenv_load "{secrets}"',
                'test "$LIBRENMS_API_TOKEN" = "' + PIPE_TOKEN + '"',
                'test "$ALERTMANAGER_WEBHOOK_TOKEN" = "' + WEBHOOK + '"',
                "echo ok",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(script)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.stdout.strip() == "ok"
    assert "command not found" not in result.stderr


def test_source_unquoted_pipe_token_is_command_not_found(tmp_path):
    secrets = tmp_path / "secrets.env"
    _write_secrets(secrets, quoted=False)
    result = subprocess.run(
        ["bash", "-c", f'set +e; source "{secrets}"; echo sourced-exit:$?'],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin")},
    )
    combined = result.stdout + result.stderr
    assert "command not found" in combined


def test_scripts_do_not_source_secrets_env():
    forbidden = (
        "source secrets/secrets.env",
        "source ./secrets/secrets.env",
        'source "$ROOT/secrets/secrets.env"',
        "source $ROOT/secrets/secrets.env",
        "source ${ROOT}/secrets/secrets.env",
    )
    paths = list(SCRIPTS.glob("*.sh")) + [SCRIPTS / "forgesre", SCRIPTS / "f"]
    scanned = 0
    for path in paths:
        if not path.is_file():
            continue
        scanned += 1
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for needle in forbidden:
                assert needle not in line, f"{path.name}: {line}"
    assert scanned >= 8
    doctor = (SCRIPTS / "doctor.sh").read_text(encoding="utf-8")
    assert "dotenv_load" in doctor
    assert "dotenv_load.sh" in doctor
    forgesre = (SCRIPTS / "forgesre").read_text(encoding="utf-8")
    assert "load_secrets" in forgesre
    assert "dotenv_load.sh" in forgesre


def test_dotenv_load_and_export_bash_syntax():
    subprocess.run(["bash", "-n", str(LOAD_SH)], check=True)
    for name in (
        "doctor.sh",
        "backup.sh",
        "restore.sh",
        "remove.sh",
        "render-monitoring.sh",
        "render-snmp-auths.sh",
        "install.sh",
        "forgesre",
        "librenms-upsert-token.sh",
    ):
        subprocess.run(["bash", "-n", str(SCRIPTS / name)], check=True)


def test_handbook_does_not_teach_source_secrets():
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    install = (ROOT / "docs" / "install-config.md").read_text(encoding="utf-8")
    example = (ROOT / "secrets" / "secrets.example.env").read_text(encoding="utf-8")
    assert "source secrets/secrets.env" not in handbook
    assert "dotenv_export.py" in handbook
    assert "dotenv_export.py" in install or "never source" in install.lower()
    assert "never `source`" in example.lower() or "never source" in example.lower()
