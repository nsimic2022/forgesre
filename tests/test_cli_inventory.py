"""Every ./forgesre subcommand is in help, TAB completion, the Administration list, and docs/cli.md."""

import ast
import re
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "forgesre"
COMP = ROOT / "scripts" / "forgesre-completion.bash"

NEW_COMMANDS = ("restart", "snmp-auths")


def _dispatcher_commands() -> set[str]:
    text = CLI.read_text(encoding="utf-8")
    block = text[text.index('\ncase "$cmd" in\n') :]
    block = block[: block.index("\nesac")]
    names: set[str] = set()
    for match in re.finditer(r"^  ([a-z][a-z|-]*)\)", block, re.M):
        names.update(match.group(1).split("|"))
    return names


def _overview_names() -> set[str]:
    text = subprocess.check_output(["bash", str(CLI), "help"], text=True)
    section = text[text.index("Commands:\n") : text.index("\nExamples:\n")]
    names: set[str] = set()
    for line in section.splitlines()[1:]:
        left = line.strip().split("  ", 1)[0]
        if not left:
            continue
        for part in left.split(" / "):
            names.add(part.split()[0])
    return names


def _completion_words() -> set[str]:
    out = subprocess.check_output(["bash", "-c", f"source '{COMP}'; printf '%s' \"$_forgesre_cmds\""], text=True)
    return set(out.split())


def _complete(words: list[str], cword: int) -> list[str]:
    joined = " ".join(f"'{w}'" for w in words)
    script = f"""
source '{COMP}'
COMP_WORDS=({joined})
COMP_CWORD={cword}
COMPREPLY=()
_forgesre_complete
printf '%s\\n' "${{COMPREPLY[@]}}"
"""
    return subprocess.check_output(["bash", "-c", script], text=True).split()


def _admin_cheatsheet(text: str) -> set[str]:
    start = text.index("cli-cheatsheet\"")
    chunk = text[start : text.index("</table>", start)]
    return {part.split("</code>", 1)[0].split()[0] for part in chunk.split("<code>")[1:]}


def test_dispatcher_has_the_known_commands():
    cmds = _dispatcher_commands()
    for name in NEW_COMMANDS + ("help", "update", "doctor", "fetch-llm", "backup", "test", "jobs", "ping", "version", "render-monitoring"):
        assert name in cmds, name
    assert len(cmds) >= 40


def test_every_command_is_in_help_overview():
    missing = _dispatcher_commands() - _overview_names()
    assert not missing, f"missing from ./forgesre help: {sorted(missing)}"


def test_every_command_has_detailed_help():
    for name in sorted(_dispatcher_commands()):
        result = subprocess.run(["bash", str(CLI), "help", name], capture_output=True, text=True)
        assert result.returncode == 0, name
        assert "No detailed help" not in result.stdout, name
        assert name.split("-")[0] in result.stdout, name


def test_every_command_is_in_tab_completion():
    missing = _dispatcher_commands() - _completion_words()
    assert not missing, f"missing from forgesre-completion.bash: {sorted(missing)}"
    extra = _completion_words() - _dispatcher_commands()
    assert not extra, f"completion offers commands the CLI does not have: {sorted(extra)}"


def test_tab_completes_restart_and_snmp_auths():
    assert "restart" in _complete(["./forgesre", "res"], 1)
    assert "snmp-auths" in _complete(["./forgesre", "snmp-"], 1)
    assert "snmp-auths" in _complete(["./f", "sn"], 1)
    assert "snmp-auths" in _complete(["./forgesre", "help", "snmp-"], 2)
    assert "restart" in _complete(["./forgesre", "help", "rest"], 2)
    assert "snmp-auths" in _complete(["snmp-"], 0)


def test_help_lists_restart_and_snmp_auths():
    overview = subprocess.check_output(["bash", str(CLI), "help"], text=True)
    for name in NEW_COMMANDS:
        assert any(line.strip().startswith(name + " ") for line in overview.splitlines()), name
    snmp = subprocess.check_output(["bash", str(CLI), "help", "snmp"], text=True)
    assert "snmp-auths" in snmp
    assert "v3" in snmp
    assert "if_mib" in snmp
    assert "SnmpDeviceUnreachable" in snmp
    auths = subprocess.check_output(["bash", str(CLI), "help", "snmp-auths"], text=True)
    assert "v3" in auths and "forgesre_<asset id>" in auths
    restart = subprocess.check_output(["bash", str(CLI), "help", "restart"], text=True)
    assert "./forgesre update" in restart


def test_admin_page_lists_every_cli_command():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.close()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    page = client.get("/admin")
    assert page.status_code == 200
    shown = _admin_cheatsheet(page.text)
    for name in NEW_COMMANDS:
        assert name in shown, name
    missing = _dispatcher_commands() - shown
    assert not missing, f"missing from Administration CLI list: {sorted(missing)}"
    assert shown <= _dispatcher_commands(), sorted(shown - _dispatcher_commands())
    assert "./forgesre help &lt;command&gt;" in page.text


def test_docs_cli_md_lists_every_command():
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    table = text[text.index("### All commands") : text.index("### Update vs restart")]
    listed: set[str] = set()
    for row in table.splitlines():
        if row.startswith("| `"):
            listed.update(code.split()[0] for code in re.findall(r"`([^`]+)`", row.split("|")[1]))
    missing = _dispatcher_commands() - listed
    assert not missing, f"missing from docs/cli.md All commands: {sorted(missing)}"
    for name in NEW_COMMANDS:
        assert f"./forgesre {name}" in text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in NEW_COMMANDS:
        assert f"./forgesre {name}" in readme


def test_snmp_auths_renderer_is_stdlib_only():
    tree = ast.parse((ROOT / "scripts" / "render_snmp_auths.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("sqlalchemy"), alias.name
                assert not alias.name.startswith("app"), alias.name
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("sqlalchemy", "app")), node.module
    for script in ("render-snmp-auths.sh", "restart.sh"):
        assert "sqlalchemy" not in (ROOT / "scripts" / script).read_text(encoding="utf-8")
