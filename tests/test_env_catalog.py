"""Settings › Server: .env catalogue, change requests and the host apply helper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app import env_catalog
from test_incident_tools import _client

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "apply-env.sh"


def test_tsv_for_the_helper_matches_the_catalogue():
    assert (ROOT / "scripts" / "env-keys.tsv").read_text(encoding="utf-8") == env_catalog.tsv()


def test_every_compose_and_example_key_is_described():
    import re

    compose = set(re.findall(r"\$\{([A-Z_]+)", (ROOT / "docker-compose.yml").read_text(encoding="utf-8")))
    example = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M))
    internal = {"FORGESRE_DATABASE_URL"}  # built by Core from Postgres settings, never set by hand
    missing = (compose | example) - set(env_catalog.BY_KEY) - internal
    assert not missing, missing


@pytest.mark.parametrize("key,value", [("FORGESRE_LLM_THREADS", "0"), ("FORGESRE_LLM_THREADS", "8; rm -rf /"), ("POSTGRES_PASSWORD", "x"),
                                       ("FORGESRE_HTTP_PORT", "9090"), ("FORGESRE_OFFBOX", "ftp"), ("FORGESRE_OFFBOX_PATH", "mnt/x"),
                                       ("FORGESRE_TIMEZONE", "Mars/Base")])
def test_validate_refuses(key, value):
    with pytest.raises(ValueError):
        env_catalog.validate(key, value)


def test_describe_hides_passwords_and_reads_values(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FORGESRE_LLM_THREADS=6\nPOSTGRES_PASSWORD=hunter2\nMY_OWN=1\n# FORGESRE_EMBED_THREADS=2\n", encoding="utf-8")
    monkeypatch.setenv("FORGESRE_DOTENV", str(env))
    monkeypatch.setenv("FORGESRE_APPLY_DIR", str(tmp_path / "apply"))
    out = env_catalog.describe_env()
    rows = {r["key"]: r for r in out["rows"]}
    assert rows["FORGESRE_LLM_THREADS"]["value"] == "6" and rows["FORGESRE_LLM_THREADS"]["restart"] == ["llm", "llm-gpu"]
    assert rows["POSTGRES_PASSWORD"] == {**rows["POSTGRES_PASSWORD"], "set": True} and "value" not in rows["POSTGRES_PASSWORD"]
    assert "hunter2" not in str(out)
    assert not rows["FORGESRE_EMBED_THREADS"]["in_file"] and out["unlisted"] == ["MY_OWN"]
    assert out["helper"]["installed"] is False


def _helper(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    fake = tmp_path / "fake-docker"
    fake.write_text('#!/bin/sh\necho "$@" >> "$FAKE_LOG"\ncase "$*" in *"config --services"*) printf "core\\nllm\\nembed\\n" ;; esac\n', encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "FORGESRE_ENV_FILE": str(tmp_path / ".env"), "FORGESRE_APPLY_DIR": str(tmp_path / "apply"),
           "FORGESRE_DOCKER_CMD": str(fake), "FAKE_LOG": str(tmp_path / "docker.log"), "FORGESRE_SYSTEMD_DIR": str(tmp_path / "units")}
    return subprocess.run(["bash", str(HELPER), *args], env=env, capture_output=True, text=True, timeout=30)


def test_helper_applies_a_valid_request_and_restarts_only_active_services(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("FORGESRE_LLM_THREADS=8\nOTHER=keep\n", encoding="utf-8")
    monkeypatch.setenv("FORGESRE_APPLY_DIR", str(tmp_path / "apply"))
    req = env_catalog.request_change({"FORGESRE_LLM_THREADS": "12", "FORGESRE_EMBED_THREADS": "3"}, "admin@dc.local")
    assert req["restarts"] == ["embed", "llm", "llm-gpu"]
    assert env_catalog.read_result(req["id"])["status"] == "pending"
    run = _helper(tmp_path, "run")
    assert run.returncode == 0, run.stderr
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "FORGESRE_LLM_THREADS=12" in text and "FORGESRE_EMBED_THREADS=3" in text and "OTHER=keep" in text
    res = env_catalog.read_result(req["id"])
    assert res["status"] == "ok" and res["restarted"] == "embed llm" and res["actor"] == "admin@dc.local"
    # llm-gpu is not in the active profiles, so it is not named (that would start it)
    assert "up -d --no-deps embed llm" in (tmp_path / "docker.log").read_text(encoding="utf-8")


def test_helper_refuses_a_forged_request_and_leaves_env_alone(tmp_path):
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=old\n", encoding="utf-8")
    reqs = tmp_path / "apply" / "requests"
    reqs.mkdir(parents=True)
    (reqs / "20261125100000-abcdef.req").write_text("#id=x\n#actor=evil\nPOSTGRES_PASSWORD=new\n", encoding="utf-8")
    (reqs / "20261125100001-abcdef.req").write_text("FORGESRE_LLM_THREADS=8|reboot\n", encoding="utf-8")
    assert _helper(tmp_path, "run").returncode == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "POSTGRES_PASSWORD=old\n"
    results = sorted((tmp_path / "apply" / "results").glob("*.result"))
    assert [("status=error" in r.read_text()) for r in results] == [True, True]
    assert not list(reqs.glob("*.req"))


def test_helper_on_writes_the_path_unit(tmp_path):
    (tmp_path / ".env").write_text("", encoding="utf-8")
    out = _helper(tmp_path, "on")
    assert out.returncode == 0, out.stderr
    unit = (tmp_path / "units" / "forgesre-apply.path").read_text(encoding="utf-8")
    assert f"PathExistsGlob={tmp_path / 'apply'}/requests/*.req" in unit
    assert "apply-env.sh run" in (tmp_path / "units" / "forgesre-apply.service").read_text(encoding="utf-8")
    assert (tmp_path / "apply" / "installed").is_file()
    assert "Apply helper: on" in _helper(tmp_path, "status").stdout


def test_api_admin_only_and_needs_the_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGESRE_APPLY_DIR", str(tmp_path / "apply"))
    assert _client("engineer").get("/api/v1/server/env").status_code == 403
    admin = _client("admin")
    body = admin.get("/api/v1/server/env").json()
    assert body["helper"]["installed"] is False and any(s["key"] == "GLPI_APP_TOKEN" for s in body["secrets"])
    assert any(a["task"] == "chat" and a["base"] == 800 for a in body["answer_lengths"])
    assert admin.post("/api/v1/server/env", json={"changes": {"FORGESRE_LLM_THREADS": "12"}}).status_code == 409
    (tmp_path / "apply").mkdir()
    (tmp_path / "apply" / "installed").write_text("x", encoding="utf-8")
    assert admin.post("/api/v1/server/env", json={"changes": {"POSTGRES_PASSWORD": "x"}}).status_code == 400
    ok = admin.post("/api/v1/server/env", json={"changes": {"FORGESRE_LLM_THREADS": "12"}}).json()
    assert ok["restarts"] == ["llm", "llm-gpu"] and (tmp_path / "apply" / "requests" / f"{ok['id']}.req").is_file()
    assert admin.get(f"/api/v1/server/env/requests/{ok['id']}").json()["status"] == "pending"


def test_answer_length_percent(monkeypatch):
    from app import forgeai
    from app.settings import settings

    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "ai": {**(settings.yaml.get("ai") or {}), "llm": {"answer_percent": 150, "think_tokens": 2048}}})
    assert forgeai.answer_tokens("chat") == 1200 and settings.llm_think_tokens == 2048


def test_doctor_row(tmp_path, monkeypatch):
    from app import api

    monkeypatch.setenv("FORGESRE_APPLY_DIR", str(tmp_path / "apply"))
    assert api._apply_helper_check()["status"] == "disabled"
    (tmp_path / "apply" / "requests").mkdir(parents=True)
    (tmp_path / "apply" / "installed").write_text("x", encoding="utf-8")
    assert api._apply_helper_check()["status"] == "ok"
    old = tmp_path / "apply" / "requests" / "20261125100000-abcdef.req"
    old.write_text("FORGESRE_LLM_THREADS=8\n", encoding="utf-8")
    os.utime(old, (1, 1))
    assert api._apply_helper_check()["status"] == "warn"
