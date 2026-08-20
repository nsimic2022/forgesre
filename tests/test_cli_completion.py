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


def test_tab_after_logs_completes_snmp_exporter():
    out = _complete(["./forgesre", "logs", "sn"], 2)
    assert "snmp-exporter" in out.split()


def test_tab_after_journal_and_help():
    journal = _complete(["./forgesre", "journal", "sn"], 2)
    assert "snmp" in journal.split()
    help_txt = _complete(["./forgesre", "help", "his"], 2)
    assert "history" in help_txt.split()


def test_tab_inside_prompt_first_word():
    out = _complete(["inc"], 0)
    assert "incidents" in out.split()


def test_cli_help_documents_completion_and_tab():
    overview = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help"], text=True)
    assert "completion" in overview
    shell = subprocess.check_output(["bash", str(ROOT / "scripts/forgesre"), "help", "shell"], text=True)
    assert "TAB" in shell
    assert "snmp-exporter" in shell
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
