"""Unit tests for ./forgesre test. Does not require a live Docker stack."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location(
        "appliance_test", ROOT / "scripts" / "appliance_test.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_appliance_test_help_does_not_need_docker():
    out = subprocess.check_output(
        ["python3", str(ROOT / "scripts" / "appliance_test.py"), "--help"],
        text=True,
    )
    assert "appliance test" in out.lower()
    assert "--out" in out
    assert "--json" in out


def test_forgesre_help_lists_test():
    overview = subprocess.check_output(
        ["bash", str(ROOT / "scripts" / "forgesre"), "help"], text=True
    )
    assert "test" in overview
    assert "Full appliance report" in overview
    detail = subprocess.check_output(
        ["bash", str(ROOT / "scripts" / "forgesre"), "help", "test"], text=True
    )
    assert "data/reports" in detail
    assert "Does not send mail" in detail
    assert "./test.sh" in detail


def test_llm_guide_and_fetch_llm_help():
    text = (ROOT / "docs" / "llm.md").read_text(encoding="utf-8")
    assert "./forgesre fetch-llm" in text
    assert "ForgeRCA" in text
    assert "ForgeAI" in text
    assert "model.gguf" in text
    assert "timeout_seconds" in text
    assert "State.Health" in text
    assert "/v1/chat/completions" in text
    assert "docker compose logs -f llm" in text
    assert "Qwen3-4B-Q4_K_M.gguf" in text
    assert "qwen2.5-1.5b-instruct-q4_k_m.gguf" in text
    assert "wget -O data/models/model.gguf" in text
    assert "/health" in text
    help_txt = subprocess.check_output(
        ["bash", str(ROOT / "scripts" / "forgesre"), "help", "fetch-llm"], text=True
    )
    assert "--download-only" in help_txt
    assert "docs/llm.md" in help_txt
    assert "Qwen3-4B" in help_txt
    assert "Qwen2.5-1.5B-Instruct" in help_txt
    script_help = subprocess.check_output(
        ["bash", str(ROOT / "scripts" / "fetch-llm.sh"), "--help"], text=True
    )
    assert "Qwen2.5-1.5B-Instruct" in script_help
    assert "Qwen3-4B" in script_help
    assert "Do not re-run ./install.sh" in script_help


def test_fetch_llm_default_url_matches_docs_and_compose():
    """Pinned Hugging Face URL/filename must match fetch-llm.sh, docs, and compose context."""
    script = (ROOT / "scripts" / "fetch-llm.sh").read_text(encoding="utf-8")
    url = None
    filename = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("DEFAULT_URL="):
            url = stripped.split("=", 1)[1].strip().strip('"')
            break
    assert url, "scripts/fetch-llm.sh must set DEFAULT_URL"
    assert filename in url
    assert url.startswith(
        "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/"
    )
    llm_doc = (ROOT / "docs" / "llm.md").read_text(encoding="utf-8")
    assert url in llm_doc
    assert filename in llm_doc
    assert "Qwen2.5-1.5B-Instruct" in llm_doc
    assert "-c 4096" in llm_doc
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    llm_block = compose.split("  llm:", 1)[1].split("\n  mailserver:", 1)[0]
    assert '- "4096"' in llm_block
    assert "/models/model.gguf" in llm_block
    assert "8088" in llm_block
    example = (ROOT / "config" / "forgesre.example.yml").read_text(encoding="utf-8")
    assert "timeout_seconds: 90" in example
    assert "enabled: false" in example
    install_help = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "Qwen2.5-1.5B" in install_help
    assert "Qwen2.5-14B" not in install_help
    admin = (ROOT / "frontend" / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "1.5B GGUF" in admin
    assert "14B GGUF" not in admin


def test_root_test_sh_and_scripts_test_sh_exist():
    assert (ROOT / "test.sh").is_file()
    assert (ROOT / "scripts" / "test.sh").is_file()
    assert (ROOT / "scripts" / "appliance_test.py").is_file()


def test_yaml_ai_reads_example_indent():
    at = _mod()
    example = (ROOT / "config" / "forgesre.example.yml").read_text(encoding="utf-8")
    parsed = at._yaml_ai(example)
    assert parsed["enabled"] == "false"
    assert parsed["mode"] == "disabled"
    assert "8088" in parsed["url"]
    assert parsed["timeout_seconds"] == "90"


def test_yaml_email_reads_example_indent():
    at = _mod()
    example = (ROOT / "config" / "forgesre.example.yml").read_text(encoding="utf-8")
    parsed = at._yaml_email(example)
    assert parsed["enabled"] == "false"
    assert parsed["host"] == "smtp.local"
    assert parsed["port"] == "587"
    assert parsed["tls"] == "true"
    assert parsed["from"] == "forgesre@example.local"


def test_parse_compose_ps_array_and_ndjson():
    at = _mod()
    array = at.parse_compose_ps(
        '[{"Service":"core","State":"running"},{"Service":"prometheus","State":"running"}]'
    )
    assert [row["Service"] for row in array] == ["core", "prometheus"]
    ndjson = at.parse_compose_ps(
        '{"Name":"forgesre-core-1","Service":"core","State":"running"}\n'
        '{"Service":"grafana","State":"running"}\n'
    )
    assert [row["Service"] for row in ndjson] == ["core", "grafana"]
    assert at.parse_compose_ps("") == []


def test_report_markdown_table_and_fail_exit():
    at = _mod()
    checks = [
        at.Check("host.python3", "pass", "Python 3.12", "python3 --version"),
        at.Check("compose.core", "fail", "core not running", "docker compose ps core", "docker compose up -d core"),
        at.Check("http.llm", "skip", "profile empty"),
        at.Check("logs.core_errors", "warn", "2 hits", "docker compose logs core"),
    ]
    runner = SimpleNamespace(
        checks=checks,
        root=ROOT,
        base="http://127.0.0.1:8080",
        env={"FORGESRE_VERSION": "0.7", "COMPOSE_PROFILES": ""},
        port="8080",
    )
    md = at.render_markdown(runner)
    assert "# ForgeSRE appliance test report" in md
    assert "| FAIL | `compose.core` |" in md
    assert "| SKIP | `http.llm` |" in md
    assert "docker compose up -d core" in md
    tally = at.counts(checks)
    assert tally == {"pass": 1, "warn": 1, "fail": 1, "skip": 1}
