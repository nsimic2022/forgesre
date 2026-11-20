"""Vendor documents for ForgeAI: chunking, indexing with a local embedder, search, and the prompt."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import doc_search, embeddings, forgeai
from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, DocChunk, DocFile, Incident, Job, User, utcnow
from app.security import hash_password
from app.seed import seed


def fake_embed(texts, *, kind="document", timeout=None):
    """Bag of words hashed into 64 dims: enough for "the text about RAID is closest to a RAID question"."""
    out = []
    for body in texts:
        vec = [0.0] * 64
        for word in body.lower().replace(":", " ").split():
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        out.append([x / norm for x in vec])
    return out


MANUAL = (
    "Chapter 4. RAID controller.\n\nWhen the RAID controller reports a degraded virtual disk, replace the failed "
    "physical disk and let the rebuild finish. The rebuild of a degraded RAID array takes several hours.\n\n"
    "Chapter 5. Power supplies.\n\nA redundant power supply alarm means one PSU lost input power. Check the "
    "power cable and the PDU outlet before replacing the PSU."
)


@pytest.fixture()
def db(monkeypatch):
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    seed(session)
    monkeypatch.setattr(embeddings, "embed", fake_embed)
    yield session
    session.close()


def _client(role: str) -> TestClient:
    db = SessionLocal()
    email = f"{role}-{uuid4().hex[:6]}@dc.local"
    db.add(User(email=email, name=role, password_hash=hash_password("pw"), role=role))
    db.commit()
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": email, "password": "pw"})
    return client


def _cleanup(db):
    db.query(DocChunk).delete()
    db.query(DocFile).delete()
    db.commit()


def test_chunks_cut_on_paragraphs_and_html_is_stripped():
    pages = doc_search.extract_pages("x.html", b"<html><style>p{}</style><p>Hello &amp; welcome</p><script>x()</script></html>")
    assert pages == [(1, "Hello & welcome")]
    chunks = doc_search.chunk_pages([(3, ("Sentence number one is here. " * 120).strip())], size=400, overlap=50)
    assert len(chunks) > 5 and all(page == 3 and len(body) <= 400 for page, body in chunks)
    with pytest.raises(ValueError):
        doc_search.extract_pages("scan.pdf", b"%PDF-1.4 not really")


def test_upload_index_search_and_rights(db):
    _cleanup(db)
    analyst, engineer = _client("analyst"), _client("engineer")
    files = {"file": ("dell-r750.md", MANUAL.encode(), "text/markdown")}
    assert analyst.post("/api/v1/forgeai/docs", files=files).status_code == 403
    assert engineer.post("/api/v1/forgeai/docs", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400
    up = engineer.post("/api/v1/forgeai/docs?title=Dell R750 manual", files=files)
    assert up.status_code == 200 and up.json()["status"] == "pending"
    job = db.query(Job).filter_by(kind="doc_index", object_id=str(up.json()["id"])).one()
    from app.jobs import run_pending_jobs

    others = db.query(Job).filter(Job.status == "pending", Job.id != job.id).all()
    for row in others:
        row.status = "parked"
    db.commit()
    try:
        run_pending_jobs(db, limit=2)
    finally:
        for row in others:
            row.status = "pending"
        db.commit()
    db.expire_all()
    doc = db.get(DocFile, up.json()["id"])
    assert doc.status == "ready", doc.error
    assert doc.raw is None and doc.chunks >= 1
    listed = analyst.get("/api/v1/forgeai/docs").json()
    assert listed["docs"][0]["title"] == "Dell R750 manual" and listed["can_edit"] is False
    hits = doc_search.search(db, "RAID virtual disk degraded rebuild", k=2, min_score=0.0)
    assert "RAID" in hits[0]["text"] and hits[0]["doc"] == "Dell R750 manual"
    assert engineer.delete(f"/api/v1/forgeai/docs/{doc.id}").json() == {"deleted": doc.id}
    assert db.query(DocChunk).count() == 0


def test_forgeai_prompt_gets_vendor_docs_and_may_cite_them(db, monkeypatch):
    _cleanup(db)
    doc = DocFile(name="psu.txt", title="Server manual", raw=MANUAL.encode())
    db.add(doc)
    db.commit()
    doc_search.index_doc(db, doc.id)
    host = f"srv-{uuid4().hex[:6]}"
    asset = Asset(asset_id=host, hostname=host, ip="10.30.0.7", type="Linux Server")
    db.add(asset)
    db.flush()
    incident = Incident(number=f"INC-D{uuid4().hex[:6]}", title="Redundant power supply lost input power", severity="HIGH",
                        status="OPEN", fingerprint=f"PsuRedundancy:{host}", asset_id=asset.id, started_at=utcnow() - timedelta(minutes=3))
    db.add(incident)
    db.commit()
    skill = forgeai.pick_skill(db, "incident", incident)
    _system, user = forgeai.build_prompts(db, "incident", skill, incident)
    docs = json.loads(user)["vendor_docs"]
    assert docs[0]["id"] == "D1" and "power" in docs[0]["text"].lower() and docs[0]["doc"] == "Server manual"
    assert "[D1]" in _system
    skill.include = {**(skill.include or {}), "docs": False}
    _system, user = forgeai.build_prompts(db, "incident", skill, incident)
    assert "vendor_docs" not in json.loads(user)
    db.rollback()
    _cleanup(db)


def test_no_embedder_means_no_docs_not_an_error(db, monkeypatch):
    _cleanup(db)
    db.add(DocChunk(doc_id=db.query(DocFile.id).scalar() or 0, text="x", embedding=[1.0]))

    def down(*a, **k):
        raise embeddings.EmbeddingError("embedding server at http://127.0.0.1:8089/v1: refused")

    monkeypatch.setattr(embeddings, "embed", down)
    assert doc_search.for_prompt(db, "anything") == []
    db.rollback()
