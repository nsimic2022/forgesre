import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
[[ "$1" == "info" ]] && exit 0
[[ "$1" == "compose" ]] || exit 0
shift
case "$1" in
  ps)
    printf '%s\\n' $FAKE_SERVICES
    ;;
  restart)
    for f in $FAKE_FAIL; do [[ "$2" == "$f" ]] && exit 1; done
    ;;
  exec)
    ;;
  *)
    echo "unexpected compose $*" >> "$FAKE_LOG.unexpected"
    ;;
esac
exit 0
"""

SENTINEL = """#!/usr/bin/env bash
echo "$0 $*" >> "$FAKE_LOG.forbidden"
exit 0
"""

DEFAULT_SERVICES = (
    "postgres core prometheus alertmanager snmp-exporter loki alloy grafana "
    "netbox-redis netbox-db-init netbox"
)


def _appliance(tmp_path: Path) -> tuple[Path, dict]:
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    for name in ("forgesre", "restart.sh", "forgesre-completion.bash"):
        shutil.copy2(ROOT / "scripts" / name, app / "scripts" / name)
    for name in ("install.sh", "update.sh", "render-monitoring.sh", "ensure-netbox-secrets.sh", "backup.sh"):
        p = app / "scripts" / name
        p.write_text(SENTINEL)
        p.chmod(0o755)
    (app / "install.sh").write_text(SENTINEL)
    (app / "install.sh").chmod(0o755)
    (app / ".env").write_text("FORGESRE_HTTP_PORT=8080\nFORGESRE_VERSION=0.8.0\n")
    (app / "secrets").mkdir()
    (app / "secrets" / "secrets.env").write_text("SECRET_KEY=keep-me\nPOSTGRES_PASSWORD=pw\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("git", SENTINEL), ("curl", "#!/usr/bin/env bash\nexit 0\n")):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)

    log = tmp_path / "docker.log"
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        FAKE_LOG=str(log),
        FAKE_SERVICES=DEFAULT_SERVICES,
        FAKE_FAIL="",
        FORGESRE_NOSHELL="1",
    )
    return app, env


def _run(app: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(app / "scripts" / "forgesre"), "restart"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _restarted(log: Path) -> list[str]:
    return [
        line.split()[2]
        for line in log.read_text().splitlines()
        if line.startswith("compose restart ")
    ]


def test_help_and_completion_list_restart():
    overview = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help"], text=True)
    assert "./forgesre restart" in overview
    assert any(line.strip().startswith("restart ") for line in overview.splitlines())
    detail = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help", "restart"], text=True)
    assert "install.sh" in detail
    assert "secrets/" in detail
    assert "git pull" in detail
    comp = subprocess.check_output(
        [
            "bash",
            "-c",
            f"source '{ROOT / 'scripts/forgesre-completion.bash'}'; "
            "COMP_WORDS=(./forgesre re); COMP_CWORD=1; _forgesre_complete; printf '%s\\n' \"${COMPREPLY[@]}\"",
        ],
        text=True,
    )
    assert "restart" in comp.split()


def test_restart_bounces_stack_without_install_or_secret_writes(tmp_path):
    app, env = _appliance(tmp_path)
    log = Path(env["FAKE_LOG"])
    env_before = (app / ".env").read_bytes()
    secrets_before = (app / "secrets" / "secrets.env").read_bytes()
    secrets_listing = sorted(p.name for p in (app / "secrets").iterdir())

    result = _run(app, env)
    assert result.returncode == 0, result.stdout + result.stderr

    order = _restarted(log)
    assert order[0] == "postgres"
    assert order[-1] == "core"
    assert {"prometheus", "alertmanager", "snmp-exporter", "netbox", "grafana"} <= set(order)
    assert "netbox-db-init" not in order
    assert "llm" not in order
    assert "Skipping LLM (llama.cpp) (llm): no container" in result.stdout
    assert "Restarting Core (core)..." in result.stdout
    assert "Restarting Postgres (postgres)..." in result.stdout
    assert "Core is up." in result.stdout

    assert not Path(str(log) + ".forbidden").exists()
    assert not Path(str(log) + ".unexpected").exists()
    calls = log.read_text()
    for verb in ("compose up", "compose pull", "compose down", "compose build"):
        assert verb not in calls
    assert (app / ".env").read_bytes() == env_before
    assert (app / "secrets" / "secrets.env").read_bytes() == secrets_before
    assert sorted(p.name for p in (app / "secrets").iterdir()) == secrets_listing


def test_restart_optional_failure_warns_but_core_failure_exits_nonzero(tmp_path):
    app, env = _appliance(tmp_path)
    env["FAKE_FAIL"] = "grafana"
    soft = _run(app, env)
    assert soft.returncode == 0, soft.stdout + soft.stderr
    assert "WARN: could not restart Grafana" in soft.stdout
    assert _restarted(Path(env["FAKE_LOG"]))[-1] == "core"

    env["FAKE_FAIL"] = "core"
    hard = _run(app, env)
    assert hard.returncode == 1
    assert "FAIL: could not restart Core" in hard.stdout


def test_restart_refuses_when_stack_was_never_brought_up(tmp_path):
    app, env = _appliance(tmp_path)
    env["FAKE_SERVICES"] = "postgres"
    result = _run(app, env)
    assert result.returncode == 1
    assert "./forgesre update" in result.stdout
    assert _restarted(Path(env["FAKE_LOG"])) == []


def test_restart_script_is_host_only_shell():
    text = (ROOT / "scripts" / "restart.sh").read_text(encoding="utf-8")
    assert "sqlalchemy" not in text
    assert "python" not in text
    assert "docker info" in text
