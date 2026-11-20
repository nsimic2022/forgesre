"""Vendor documents for ForgeAI (RAG): upload, cut into chunks, embed locally, search.

Nothing leaves the appliance. An Engineer uploads a PDF, text, Markdown or HTML file; the
``doc_index`` job extracts the text, cuts it into chunks of a few paragraphs, and asks the local
embedding server (llama.cpp ``--embedding``, nomic-embed-text by default, see ``embeddings``) for
one vector per chunk. ForgeAI then gets the 3 chunks closest to the incident (or the operator's
question) as ``vendor_docs`` D1…D3 and may cite them.

Vectors are stored as JSON on every database. On Postgres with the pgvector extension they are
also stored in ``doc_chunks.embedding_vec`` with an HNSW index, and the search runs in SQL;
otherwise (SQLite in tests, an external Postgres without pgvector) it runs in Python.
"""

from __future__ import annotations

import html
import io
import logging
import math
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import DocChunk, DocFile

log = logging.getLogger("forgesre.docs")

MAX_BYTES = 30 * 1024 * 1024
MAX_PAGES = 2000
CHUNK_CHARS = 1200
OVERLAP_CHARS = 200
BATCH = 16
SUFFIXES = (".pdf", ".txt", ".md", ".html", ".htm")
DIMENSIONS = 768  # nomic-embed-text-v1.5


# ------------------------------------------------------------------ pgvector


def ensure_vector(engine: Engine) -> bool:
    """Postgres: create the pgvector extension, the vector column and its index. False when unavailable.

    Runs in its own transaction: a failed CREATE EXTENSION must not abort the other migrations.
    """
    if engine.dialect.name != "postgresql":
        return False
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text(f"ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS embedding_vec vector({DIMENSIONS})"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_doc_chunks_vec ON doc_chunks USING hnsw (embedding_vec vector_cosine_ops)"))
        return True
    except Exception as exc:  # noqa: BLE001 — the extension is optional; the Python search still works
        log.warning("pgvector not available, document search runs in Python: %s", str(exc).splitlines()[0][:200])
        return False


def vector_ready(db: Session) -> bool:
    """True when this database has the pgvector column (checked per call; cheap)."""
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return False
    try:
        return bool(db.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'doc_chunks' AND column_name = 'embedding_vec'"
        )).first())
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ text


def _clean(value: str) -> str:
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", value.replace("\r", ""))).strip()


def extract_pages(name: str, data: bytes) -> list[tuple[int, str]]:
    """(page number, text) from a PDF, or one "page" for text, Markdown and HTML. Raises ValueError."""
    lower = name.lower()
    if lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            pages = []
            for i, page in enumerate(reader.pages[:MAX_PAGES], 1):
                body = _clean(page.extract_text() or "")
                if body:
                    pages.append((i, body))
        except Exception as exc:  # noqa: BLE001 — pypdf raises many types on broken files
            raise ValueError(f"could not read the PDF: {str(exc)[:200]}") from exc
        if not pages:
            raise ValueError("the PDF has no text layer (a scan?); ForgeSRE does not run OCR")
        return pages
    body = data.decode("utf-8", errors="replace")
    if lower.endswith((".html", ".htm")):
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
        body = html.unescape(re.sub(r"(?s)<[^>]+>", "\n", body))
    body = _clean(body)
    if not body:
        raise ValueError("the file has no text")
    return [(1, body)]


def chunk_pages(pages: list[tuple[int, str]], size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[tuple[int, str]]:
    """Cut each page into chunks of about ``size`` characters on paragraph or sentence ends, overlapping a little."""
    out: list[tuple[int, str]] = []
    for page, body in pages:
        start = 0
        while start < len(body):
            end = min(len(body), start + size)
            if end < len(body):
                cut = max(body.rfind("\n\n", start + size // 2, end), body.rfind(". ", start + size // 2, end))
                if cut > start:
                    end = cut + 1
            piece = body[start:end].strip()
            if len(piece) > 40:
                out.append((page, piece))
            if end >= len(body):
                break
            start = max(end - overlap, start + 1)
    return out


# ------------------------------------------------------------------ index and search


def index_doc(db: Session, doc_id: int) -> dict[str, Any]:
    """The doc_index job: extract, chunk, embed in batches, store. Raises on failure (the job records it)."""
    from app import embeddings

    doc = db.get(DocFile, doc_id)
    if doc is None:
        raise LookupError(f"document {doc_id} is gone")
    doc.status, doc.error = "indexing", ""
    db.commit()
    try:
        if doc.raw is None:
            raise ValueError("the upload is no longer there; upload the file again")
        pages = extract_pages(doc.name, bytes(doc.raw))
        chunks = chunk_pages(pages)
        db.query(DocChunk).filter_by(doc_id=doc.id).delete(synchronize_session=False)
        use_vec = vector_ready(db)
        for start in range(0, len(chunks), BATCH):
            batch = chunks[start:start + BATCH]
            vectors = embeddings.embed([body for _page, body in batch], kind="document")
            added = [DocChunk(doc_id=doc.id, n=start + i, page=page, text=body, embedding=vec)
                     for i, ((page, body), vec) in enumerate(zip(batch, vectors))]
            db.add_all(added)
            db.flush()
            if use_vec:
                for row in added:
                    if len(row.embedding or []) == DIMENSIONS:
                        db.execute(text("UPDATE doc_chunks SET embedding_vec = CAST(:v AS vector) WHERE id = :id"),
                                   {"v": "[" + ",".join(f"{x:.6f}" for x in row.embedding) + "]", "id": row.id})
        doc.pages, doc.chunks, doc.status, doc.raw, doc.model = len(pages), len(chunks), "ready", None, embeddings.model_name()
        db.commit()
        return {"pages": len(pages), "chunks": len(chunks)}
    except Exception as exc:
        db.rollback()
        doc = db.get(DocFile, doc_id)
        if doc is not None:
            doc.status, doc.error = "error", str(exc)[:500]
            db.commit()
        raise


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def search(db: Session, query: str, k: int = 3, min_score: float = 0.35) -> list[dict[str, Any]]:
    """The ``k`` chunks closest to ``query`` (cosine), best first. [] when there are no documents or no embedder."""
    from app import embeddings

    query = " ".join((query or "").split())[:1000]
    if not query or not db.query(DocChunk.id).first():
        return []
    vec = embeddings.embed([query], kind="query")[0]
    names = {d.id: d.title or d.name for d in db.query(DocFile).all()}
    if vector_ready(db) and len(vec) == DIMENSIONS:
        rows = db.execute(text(
            "SELECT id, doc_id, page, text, 1 - (embedding_vec <=> CAST(:v AS vector)) AS score FROM doc_chunks "
            "WHERE embedding_vec IS NOT NULL ORDER BY embedding_vec <=> CAST(:v AS vector) LIMIT :k"
        ), {"v": "[" + ",".join(f"{x:.6f}" for x in vec) + "]", "k": k}).all()
        hits = [{"chunk": r.id, "doc": names.get(r.doc_id, ""), "page": r.page, "text": r.text, "score": round(float(r.score), 3)} for r in rows]
    else:
        scored = [(_cosine(vec, c.embedding or []), c) for c in db.query(DocChunk).all() if c.embedding]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        hits = [{"chunk": c.id, "doc": names.get(c.doc_id, ""), "page": c.page, "text": c.text, "score": round(s, 3)} for s, c in scored[:k]]
    return [h for h in hits if h["score"] >= min_score]


def for_prompt(db: Session, query: str, k: int = 3) -> list[dict[str, Any]]:
    """Search results shaped for a ForgeAI prompt: D1…Dk with document, page and a short excerpt.

    Never raises: no embedder or a slow one only means the prompt goes without documents.
    """
    try:
        hits = search(db, query, k=k)
    except Exception as exc:  # noqa: BLE001
        log.info("document search skipped: %s", str(exc)[:200])
        return []
    return [{"id": f"D{i}", "doc": h["doc"], "page": h["page"], "text": h["text"][:700]} for i, h in enumerate(hits, 1)]


def doc_as_dict(doc: DocFile) -> dict[str, Any]:
    return {"id": doc.id, "name": doc.name, "title": doc.title or doc.name, "size": doc.size, "pages": doc.pages,
            "chunks": doc.chunks, "status": doc.status, "error": doc.error, "model": doc.model,
            "uploaded_by": doc.uploaded_by, "at": doc.created_at.isoformat() if doc.created_at else None}
