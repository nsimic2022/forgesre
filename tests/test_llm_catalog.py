"""GGUF catalog, compose interpolation, fetch-llm CLI. No live Docker / HF download."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from app.llm_catalog import (
    CATALOG,
    DEFAULT_CTX,
    DEFAULT_FILENAME,
    DEFAULT_ID,
    catalog_status,
    resolve,
    safe_gguf_name,
)

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_default_is_14b_model_gguf_ctx_8192():
    default = resolve("")
    assert default["id"] == DEFAULT_ID
    assert default["filename"] == DEFAULT_FILENAME == "model.gguf"
    assert default["ctx"] == DEFAULT_CTX == 8192
    assert default["default"] is True
    ids = [row["id"] for row in CATALOG]
    assert ids == ["qwen2.5-14b", "qwen3-1.7b", "qwen2.5-1.5b", "qwen3-4b"]
    lights = {row["id"]: row for row in CATALOG if row["kind"] == "light"}
    assert lights["qwen3-1.7b"]["filename"] == "Qwen3-1.7B-Q4_K_M.gguf"
    assert lights["qwen3-1.7b"]["ctx"] == 4096
    assert "unsloth" in lights["qwen3-1.7b"]["url"]
    assert lights["qwen2.5-1.5b"]["filename"] == "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    assert "Qwen/Qwen2.5-1.5B-Instruct-GGUF" in lights["qwen2.5-1.5b"]["url"]
    assert resolve("1.7b")["id"] == "qwen3-1.7b"
    assert resolve("1.5b")["id"] == "qwen2.5-1.5b"
    assert resolve("14b")["id"] == "qwen2.5-14b"


def test_safe_gguf_name_rejects_paths():
    assert safe_gguf_name("model.gguf") == "model.gguf"
    assert safe_gguf_name("../etc/passwd") == "model.gguf"
    assert safe_gguf_name("foo/bar.gguf") == "bar.gguf"
    assert safe_gguf_name("Qwen3-1.7B-Q4_K_M.gguf") == "Qwen3-1.7B-Q4_K_M.gguf"
    assert safe_gguf_name("not-gguf.bin") == "model.gguf"


def test_compose_gguf_and_ctx_env_defaults():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${FORGESRE_LLM_GGUF:-model.gguf}" in text
    assert "${FORGESRE_LLM_CTX:-8192}" in text
    assert "${FORGESRE_LLM_THREADS:-8}" in text
    data = yaml.safe_load(text)
    cmd = data["services"]["llm"]["command"]
    assert cmd[0] == "-m"
    assert "FORGESRE_LLM_GGUF" in cmd[1]
    assert "8088" in cmd
    assert data["services"]["llm"]["profiles"] == ["ai"]
    assert data["services"]["llm"]["image"] == "ghcr.io/ggml-org/llama.cpp:server"


def test_catalog_status_marks_active_and_present(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    light = models / "Qwen3-1.7B-Q4_K_M.gguf"
    light.write_bytes(b"x" * 500)
    dotenv = tmp_path / ".env"
    dotenv.write_text("FORGESRE_LLM_GGUF=Qwen3-1.7B-Q4_K_M.gguf\nFORGESRE_LLM_CTX=4096\n")
    status = catalog_status(models_dir=models, dotenv_path=dotenv)
    assert status["active_filename"] == "Qwen3-1.7B-Q4_K_M.gguf"
    assert status["active_id"] == "qwen3-1.7b"
    assert status["active_ctx"] == 4096
    row = next(item for item in status["models"] if item["id"] == "qwen3-1.7b")
    assert row["present"] is True
    assert row["active"] is True
    assert row["ok"] is False  # 500 bytes < min_bytes
    default = next(item for item in status["models"] if item["id"] == "qwen2.5-14b")
    assert default["present"] is False
    assert default["active"] is False


def test_fetch_llm_help_lists_catalog_and_switch():
    script_help = subprocess.check_output(
        ["bash", str(ROOT / "scripts" / "fetch-llm.sh"), "--help"], text=True
    )
    assert "Qwen2.5-14B-Instruct" in script_help
    assert "qwen3-1.7b" in script_help
    assert "qwen2.5-1.5b" in script_help
    assert "FORGESRE_LLM_GGUF" in script_help
    assert "Do not re-run ./install.sh" in script_help
    listed = subprocess.check_output(
        ["bash", str(ROOT / "scripts" / "fetch-llm.sh"), "--list"], text=True
    )
    assert "qwen3-1.7b" in listed
    assert "model.gguf" in listed
    assert "Qwen3-1.7B-Q4_K_M.gguf" in listed
    wrapper = subprocess.check_output(
        ["bash", str(ROOT / "scripts" / "forgesre"), "help", "fetch-llm"], text=True
    )
    assert "--model qwen3-1.7b" in wrapper
    assert "use qwen2.5-14b" in wrapper


def test_fetch_llm_rejects_unknown_id():
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "fetch-llm.sh"), "--model", "not-a-model", "--download-only"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unknown LLM catalog id" in result.stderr


def test_llm_guide_documents_choice_not_silent_replace():
    text = (ROOT / "docs" / "llm.md").read_text(encoding="utf-8")
    assert "FORGESRE_LLM_GGUF" in text
    assert "./forgesre fetch-llm --list" in text
    assert "./forgesre fetch-llm --model qwen3-1.7b" in text
    assert "Qwen3-1.7B-Instruct-GGUF" in text
    assert "unsloth" in text
    assert "qwen2.5-1.5b-instruct-q4_k_m.gguf" in text
    assert "8192" in text
    assert "llama.cpp" in text
    assert "Ollama is not the product default" in text or "not Ollama" in text
    continuation = (ROOT / "docs" / "continuation.md").read_text(encoding="utf-8")
    assert "qwen3-1.7b" in continuation
    assert "choice" in continuation.lower()
    assert "e44647a" in continuation or "revert-gguf-swap" in continuation


def test_health_page_and_api_list_llm_catalog():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    page = client.get("/health-ui")
    assert page.status_code == 200
    assert "Local LLM (llama.cpp)" in page.text
    assert "qwen3-1.7b" in page.text
    assert "qwen2.5-1.5b" in page.text
    assert "FORGESRE_LLM_GGUF" in page.text
    assert "./forgesre fetch-llm" in page.text
    api = client.get("/api/v1/llm/models")
    assert api.status_code == 200
    body = api.json()
    assert body["default_filename"] == "model.gguf"
    assert body["default_ctx"] == 8192
    ids = [row["id"] for row in body["models"]]
    assert "qwen2.5-14b" in ids
    assert "qwen3-1.7b" in ids
    anon = TestClient(app)
    assert anon.get("/api/v1/llm/models").status_code == 401
