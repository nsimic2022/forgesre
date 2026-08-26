import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "scripts" / "forgesre-completion.bash"


def _complete(words: list[str], cword: int) -> str:
    joined = " ".join(f"'{w}'" for w in words)
    script = f"""
source '{COMP}'
COMP_WORDS=({joined})
COMP_CWORD={cword}
COMPREPLY=()
_forgesre_complete
printf '%s\\n' "${{COMPREPLY[@]}}"
"""
    return subprocess.check_output(["bash", "-c", script], text=True)


def test_tab_completes_all_cli_commands_from_prefix():
    out = _complete(["./forgesre", "in"], 1)
    assert "incidents" in out.split()
    assert "install" in out.split()
    hist = _complete(["./forgesre", "hi"], 1)
    assert "history" in hist.split()
    logn = _complete(["./forgesre", "log"], 1)
    assert "login" in logn.split()
    assert "logout" in logn.split()
    assert "logs" in logn.split()
    sn = _complete(["./forgesre", "sn"], 1)
    assert "snmp" in sn.split()
    assert "snmp-exporter" not in sn.split()
    mail = _complete(["./forgesre", "ma"], 1)
    assert "mailbox" in mail.split()
    te = _complete(["./forgesre", "te"], 1)
    assert "test" in te.split()
    pi = _complete(["./forgesre", "pi"], 1)
    assert "ping" in pi.split()
    pr = _complete(["./forgesre", "pr"], 1)
    assert "probe" in pr.split()
    ve = _complete(["./forgesre", "ve"], 1)
    assert "verify" in ve.split()
    im = _complete(["./forgesre", "im"], 1)
    assert "import" in im.split()


def test_tab_after_verify_and_assets_completes_asset_hook():
    text = COMP.read_text()
    assert "_forgesre_asset_refs" in text
    assert "asset-refs" in text
    ping = _complete(["./forgesre", "ping", "--"], 2)
    assert "--timeout" in ping.split()
    assert "--demo" in ping.split()
    verify = _complete(["./forgesre", "verify", "--"], 2)
    assert "--timeout" in verify.split()

    out = _complete(["./forgesre", "logs", "sn"], 2)
    assert "snmp-exporter" in out.split()


def test_tab_after_journal_and_help():
    journal = _complete(["./forgesre", "journal", "sn"], 2)
    assert "snmp" in journal.split()
    help_txt = _complete(["./forgesre", "help", "his"], 2)
    assert "history" in help_txt.split()
    help_te = _complete(["./forgesre", "help", "te"], 2)
    assert "test" in help_te.split()
    help_pi = _complete(["./forgesre", "help", "pi"], 2)
    assert "ping" in help_pi.split()
    help_q = _complete(["./forgesre", "help", "qu"], 2)
    assert "quit" in help_q.split()


def test_tab_inside_prompt_completes_quit():
    out = _complete(["qu"], 0)
    assert "quit" in out.split()


def test_tab_inside_prompt_first_word():
    out = _complete(["inc"], 0)
    assert "incidents" in out.split()


def test_cli_help_documents_completion_and_tab():
    overview = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help"], text=True)
    assert "test" in overview
    assert "ping" in overview
    assert "probe" in overview
    assert "verify" in overview
    ping_help = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help", "ping"], text=True)
    assert "9182" in ping_help
    assert "9100" in ping_help
    assert "ICMP" in ping_help
    probe_help = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help", "probe"], text=True)
    assert "ping" in probe_help
    verify_help = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help", "verify"], text=True)
    assert "not ./forgesre test" in verify_help.lower()
    assert "9100" in verify_help
    assert "9182" in verify_help
    assert "SKIP" in verify_help
    help_ve = _complete(["./forgesre", "help", "ve"], 2)
    assert "verify" in help_ve.split()
    assert "quit" in overview
    assert "completion" in overview
    shell = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help", "shell"], text=True)
    assert "TAB" in shell
    assert "snmp-exporter" in shell
    assert "quit" in shell
    quit_help = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help", "quit"], text=True)
    assert "Ctrl-D" in quit_help
    assert "exit" in quit_help
    assert "forgesre>" in quit_help
    exit_help = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help", "exit"], text=True)
    assert "quit" in exit_help
    printed = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "completion"], text=True)
    assert "forgesre-completion.bash" in printed


def test_completion_file_drops_colon_wordbreak():
    text = COMP.read_text()
    assert "complete -F _forgesre_complete forgesre f ./forgesre ./f" in text
    script = f"""
source '{COMP}'
printf '%s' "$COMP_WORDBREAKS"
"""
    out = subprocess.check_output(["bash", "-c", script], text=True)
    assert ":" not in out
