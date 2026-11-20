"""Settings › ForgeAI models: catalog, Hugging Face downloads, import, the active model, llm-run.sh."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import llm_models
from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Job, User
from app.security import hash_password
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
GGUF = b"GGUF" + b"\0" * 60


@pytest.fixture()
def models(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGESRE_MODELS_DIR", str(tmp_path))
    return tmp_path


def _client(role="admin") -> TestClient:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def test_links_must_be_hugging_face_gguf_and_split_models_bring_all_parts():
    with pytest.raises(ValueError):
        llm_models.check_url("http://huggingface.co/a/b/resolve/main/x.gguf")
    with pytest.raises(ValueError):
        llm_models.check_url("https://evil.example/a/b/resolve/main/x.gguf")
    with pytest.raises(ValueError):
        llm_models.check_url("https://huggingface.co/a/b/resolve/main/x.bin")
    parts = llm_models.check_url("https://huggingface.co/Qwen/R/resolve/main/m-q4-00001-of-00003.gguf")
    assert [Path(p).name for p in parts] == [f"m-q4-0000{i}-of-00003.gguf" for i in (1, 2, 3)]


def test_installed_groups_split_parts_and_knows_the_active_one(models):
    (models / "a-00001-of-00002.gguf").write_bytes(GGUF)
    (models / "a-00002-of-00002.gguf").write_bytes(b"x" * 10)
    (models / "Qwen3-4B-Q4_K_M.gguf").write_bytes(GGUF)
    (models / "active").write_text("Qwen3-4B-Q4_K_M.gguf\n")
    rows = {r["name"]: r for r in llm_models.installed(models)}
    assert set(rows) == {"a-00001-of-00002.gguf", "Qwen3-4B-Q4_K_M.gguf"}
    assert rows["a-00001-of-00002.gguf"]["parts"] == 2 and rows["a-00001-of-00002.gguf"]["valid"]
    assert rows["Qwen3-4B-Q4_K_M.gguf"]["active"] and rows["Qwen3-4B-Q4_K_M.gguf"]["catalog"] == "qwen3-4b"


def test_download_job_fetches_parts_resumes_and_reports_progress(models):
    body = GGUF + b"y" * 1000
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("range")))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(body))})
        start = int(request.headers["range"].split("=")[1].rstrip("-")) if request.headers.get("range") else 0
        return httpx.Response(206 if start else 200, content=body[start:])

    (models / "m-00002-of-00002.gguf.partial").write_bytes(body[:100])
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    job = Job(kind="llm_download", object_id="m", status="running",
              payload={"files": [f"https://huggingface.co/o/r/resolve/main/m-0000{i}-of-00002.gguf" for i in (1, 2)], "label": "m"})
    db.add(job)
    db.commit()
    result = llm_models.run_download(db, job, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert result["model"] == "m-00001-of-00002.gguf"
    assert (models / "m-00001-of-00002.gguf").read_bytes() == body and (models / "m-00002-of-00002.gguf").read_bytes() == body
    assert ("GET", "/o/r/resolve/main/m-00002-of-00002.gguf", "bytes=100-") in seen
    assert not list(models.glob("*.partial")) and job.payload["done_bytes"] == job.payload["total_bytes"]
    db.delete(job)
    db.commit()
    db.close()


def test_api_activate_sets_timeout_remove_protects_active_upload_checks_header(models, monkeypatch):
    from app import system_settings

    calls = []
    monkeypatch.setattr(system_settings, "set_setting", lambda db, key, raw, actor: calls.append((key, raw)))
    (models / "Qwen3-8B-Q4_K_M.gguf").write_bytes(GGUF)
    (models / "model.gguf").write_bytes(GGUF)
    admin = _client()
    assert _client("engineer").get("/api/v1/llm/models").status_code == 403
    data = admin.get("/api/v1/llm/models").json()
    assert data["active"] == "model.gguf" and data["writable"] and {c["id"] for c in data["catalog"]} >= {"qwen3-4b", "qwen2.5-14b"}
    assert admin.delete("/api/v1/llm/models/model.gguf").status_code == 400
    assert admin.post("/api/v1/llm/models/Qwen3-8B-Q4_K_M.gguf/activate").json() == {"active": "Qwen3-8B-Q4_K_M.gguf", "timeout_s": 180}
    assert (models / "active").read_text().strip() == "Qwen3-8B-Q4_K_M.gguf" and calls == [("ai.llm.timeout_seconds", 180)]
    assert admin.delete("/api/v1/llm/models/model.gguf").json() == {"removed": ["model.gguf"]}
    bad = admin.post("/api/v1/llm/models/upload", files={"file": ("x.gguf", b"not a model", "application/octet-stream")})
    assert bad.status_code == 400 and not (models / "x.gguf").exists()
    good = admin.post("/api/v1/llm/models/upload", files={"file": ("mine.gguf", GGUF, "application/octet-stream")})
    assert good.json()["name"] == "mine.gguf" and (models / "mine.gguf").read_bytes() == GGUF
    db = SessionLocal()
    for job in db.query(Job).filter(Job.status == "pending").all():
        db.delete(job)
    db.commit()
    queued = admin.post("/api/v1/llm/models/download", json={"catalog_id": "qwen3-4b"}).json()
    job = db.get(Job, queued["job"])
    assert job.kind == "llm_download" and job.payload["files"][0].endswith("Qwen3-4B-Q4_K_M.gguf")
    assert admin.post(f"/api/v1/llm/downloads/{job.id}/cancel").json() == {"cancelled": True}
    db.refresh(job)
    assert job.status == "error" and job.payload["cancel"]
    assert admin.post("/api/v1/llm/models/download", json={"url": "https://example.com/x.gguf"}).status_code == 400
    db.close()


def test_llm_run_sh_picks_active_then_model_gguf_then_first_part(tmp_path):
    def pick():
        env = {**os.environ, "LLM_MODELS_DIR": str(tmp_path), "LLM_PICK_ONLY": "1"}
        return subprocess.check_output(["sh", str(ROOT / "scripts" / "llm-run.sh")], env=env, text=True).strip()

    assert pick() == ""
    (tmp_path / "b-00002-of-00002.gguf").write_bytes(b"x")
    (tmp_path / "b-00001-of-00002.gguf").write_bytes(b"x")
    assert pick() == "b-00001-of-00002.gguf"
    (tmp_path / "model.gguf").write_bytes(b"x")
    assert pick() == "model.gguf"
    (tmp_path / "active").write_text("b-00001-of-00002.gguf\n")
    assert pick() == "b-00001-of-00002.gguf"
    (tmp_path / "active").write_text("gone.gguf\n")
    assert pick() == "model.gguf"
