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
    assert "wget -O data/models/model.gguf" in text
    assert "/health" in text
    help_txt = subprocess.check_output(
        ["bash", str(ROOT / "scripts" / "forgesre"), "help", "fetch-llm"], text=True
    )
    assert "--download-only" in help_txt
    assert "docs/llm.md" in help_txt
    assert "Qwen3-4B" in help_txt
    script_help = subprocess.check_output(
        ["bash", str(ROOT / "scripts" / "fetch-llm.sh"), "--help"], text=True
    )
    assert "Qwen2.5-14B-Instruct" in script_help
    assert "Qwen3-4B" in script_help
    assert "Do not re-run ./install.sh" in script_help


def test_root_test_sh_and_scripts_test_sh_exist():
    assert (ROOT / "test.sh").is_file()
    assert (ROOT / "scripts" / "test.sh").is_file()
    assert (ROOT / "scripts" / "appliance_test.py").is_file()


def test_yaml_ai_reads_example_indent():
    at = _mod()
    example = (ROOT / "config" / "forgesre.example.yml").read_text(encoding="utf-8")
    parsed = at._yaml_ai(example)
    assert parsed["enabled"] == "true"
    assert parsed["mode"] == "bundled"
    assert "8088" in parsed["url"]
    assert parsed["timeout_seconds"] == "600"


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
