"""CLI never puts a password on a process argv (ps shows argv to every local user)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from app import cli_ops

ROOT = Path(__file__).resolve().parents[1]
SECRET = "s3cr&t+pass word"

FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG.argv"
if [[ " $* " == *" password@- "* ]]; then cat >> "$FAKE_LOG.stdin"; fi
exit 0
"""

FAKE_PYTHON = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG.argv"
printf '%s' "${FORGESRE_CLI_PASSWORD:-}" >> "$FAKE_LOG.env"
exit 0
"""


def _appliance(tmp_path: Path) -> tuple[Path, dict, Path]:
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "forgesre", app / "scripts" / "forgesre")
    (app / ".env").write_text("FORGESRE_HTTP_PORT=8080\n")
    (app / "secrets").mkdir()
    (app / "secrets" / "secrets.env").write_text(
        f"FORGESRE_ADMIN_EMAIL=admin@forgesre.local\nFORGESRE_ADMIN_PASSWORD='{SECRET}'\n"
    )
    (app / "config").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("docker", "#!/usr/bin/env bash\nexit 0\n"),
        ("curl", FAKE_CURL),
        ("python3", FAKE_PYTHON),
    ):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "fake"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_LOG=str(log), FORGESRE_NOSHELL="1")
    env.pop("FORGESRE_CLI_PASSWORD", None)
    env.pop("FORGESRE_CLI_EMAIL", None)
    return app, env, log


def _run(app: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(app / "scripts" / "forgesre"), *args],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )


def test_admin_login_sends_password_on_curl_stdin(tmp_path):
    app, env, log = _appliance(tmp_path)
    result = _run(app, env, "jobs")
    assert result.returncode == 0, result.stderr
    argv = Path(f"{log}.argv").read_text()
    assert "/login" in argv
    assert SECRET not in argv and "s3cr" not in argv
    assert Path(f"{log}.stdin").read_text() == SECRET


def test_login_with_password_argument_hands_it_to_python_via_env(tmp_path):
    app, env, log = _appliance(tmp_path)
    result = _run(app, env, "login", "eng@dc.local", SECRET)
    assert result.returncode == 0, result.stderr
    argv = Path(f"{log}.argv").read_text()
    assert "login eng@dc.local" in argv
    assert "s3cr" not in argv
    assert Path(f"{log}.env").read_text() == SECRET


def test_config_prints_the_whole_file(tmp_path):
    app, env, _log = _appliance(tmp_path)
    lines = [f"# line {n}" for n in range(1, 241)]
    (app / "config" / "forgesre.yml").write_text("\n".join(lines) + "\n")
    result = _run(app, env, "config")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == lines


def test_cli_ops_login_posts_password_on_stdin(monkeypatch, tmp_path):
    seen: list[tuple[list[str], bytes | None]] = []

    def fake_run(args, check=True, input=None, stdout=None, stderr=None):
        seen.append((list(args), input))
        return subprocess.CompletedProcess(args, 0, stdout=b"302", stderr=b"")

    monkeypatch.setattr(cli_ops.subprocess, "run", fake_run)
    cli_ops._login("8080", tmp_path / "jar", "eng@dc.local", SECRET)
    args, stdin = seen[0]
    assert all("s3cr" not in part for part in args)
    assert "password@-" in args and "email=eng@dc.local" in args
    assert stdin == SECRET.encode()


def test_cli_ops_login_reads_password_from_env(monkeypatch):
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(cli_ops, "cmd_login", lambda port, email, password: calls.append((port, email, password)))
    monkeypatch.setenv("FORGESRE_CLI_PASSWORD", SECRET)
    cli_ops.main(["8080", "login", "eng@dc.local"])
    assert calls == [("8080", "eng@dc.local", SECRET)]
