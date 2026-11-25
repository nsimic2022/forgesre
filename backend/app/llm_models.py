"""ForgeAI models from Settings: download from Hugging Face, import a file, pick the active one.

Files live in data/models (Core mounts it at FORGESRE_MODELS_DIR, read-write; llama.cpp mounts it
read-only). ``active`` names the model llama.cpp should run; scripts/llm-run.sh inside the llm
container reads it every 5 seconds and restarts llama-server on a change, so switching needs no
shell. Split GGUF files (``-00001-of-00003.gguf``) are kept together; llama.cpp loads the first part.

Downloads run as ``llm_download`` jobs on the LLM lane (they can take an hour), write ``.partial``
files and rename them only when complete. Only https://huggingface.co (or hf.co) URLs are fetched.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

JOB_KIND = "llm_download"
ACTIVE_FILE = "active"
HF = "https://huggingface.co"
SPLIT = re.compile(r"^(?P<base>.+)-(?P<part>\d{5})-of-(?P<total>\d{5})\.gguf$", re.I)
NAME_OK = re.compile(r"^[A-Za-z0-9._-]+\.gguf$")

# Checked on huggingface.co (Nov 2026). ram_gb: what the VM should have free for model + context.
CATALOG: list[dict[str, Any]] = [
    {"id": "qwen3-4b", "name": "Qwen3 4B (Q4_K_M)", "size_gb": 2.5, "ram_gb": 6, "timeout_s": 90,
     "note": "Fast on CPU (30–90 s per answer). The best start for most appliances.",
     "files": [f"{HF}/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf"]},
    {"id": "qwen3-8b", "name": "Qwen3 8B (Q4_K_M)", "size_gb": 5.0, "ram_gb": 10, "timeout_s": 180,
     "note": "Better answers, about twice as slow as 4B.",
     "files": [f"{HF}/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf"]},
    {"id": "llama3.1-8b", "name": "Llama 3.1 8B Instruct (Q4_K_M)", "size_gb": 4.9, "ram_gb": 10, "timeout_s": 180,
     "note": "Meta's model, community GGUF (bartowski). An alternative to Qwen3 8B.",
     "files": [f"{HF}/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"]},
    {"id": "qwen2.5-7b", "name": "Qwen2.5 7B Instruct (Q4_K_M)", "size_gb": 4.7, "ram_gb": 10, "timeout_s": 180,
     "note": "Older Qwen, very steady JSON. Two files.",
     "files": [f"{HF}/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m-0000{i}-of-00002.gguf" for i in (1, 2)]},
    {"id": "qwen2.5-14b", "name": "Qwen2.5 14B Instruct (Q4_K_M)", "size_gb": 9.0, "ram_gb": 16, "timeout_s": 600,
     "note": "Best answers, slow on CPU (several minutes). Three files.",
     "files": [f"{HF}/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q4_k_m-0000{i}-of-00003.gguf" for i in (1, 2, 3)]},
    # OpsLLM (Qwen2 base) is tuned for software operations: QA and root cause analysis. Smaller to
    # larger: more bits, slightly better answers, more RAM. Not a thinking model, so thinking goes off.
    {"id": "opsllm-7b-q4ks", "family": "OpsLLM 7B", "name": "OpsLLM 7B (i1-Q4_K_S)", "size_gb": 4.5, "ram_gb": 8, "timeout_s": 180, "thinking": False,
     "note": "Operations model, smallest of the useful sizes.", "files": [f"{HF}/mradermacher/OpsLLM-7B-i1-GGUF/resolve/main/OpsLLM-7B.i1-Q4_K_S.gguf"]},
    {"id": "opsllm-7b-q4km", "family": "OpsLLM 7B", "name": "OpsLLM 7B (i1-Q4_K_M)", "size_gb": 4.7, "ram_gb": 9, "timeout_s": 180, "thinking": False,
     "note": "Operations model, the recommended size.", "files": [f"{HF}/mradermacher/OpsLLM-7B-i1-GGUF/resolve/main/OpsLLM-7B.i1-Q4_K_M.gguf"]},
    {"id": "opsllm-7b-q5km", "family": "OpsLLM 7B", "name": "OpsLLM 7B (i1-Q5_K_M)", "size_gb": 5.4, "ram_gb": 10, "timeout_s": 200, "thinking": False,
     "note": "Operations model, a little better, a little slower.", "files": [f"{HF}/mradermacher/OpsLLM-7B-i1-GGUF/resolve/main/OpsLLM-7B.i1-Q5_K_M.gguf"]},
    {"id": "opsllm-7b-q6k", "family": "OpsLLM 7B", "name": "OpsLLM 7B (i1-Q6_K)", "size_gb": 6.3, "ram_gb": 11, "timeout_s": 240, "thinking": False,
     "note": "Operations model, close to full quality.", "files": [f"{HF}/mradermacher/OpsLLM-7B-i1-GGUF/resolve/main/OpsLLM-7B.i1-Q6_K.gguf"]},
]


def models_dir() -> Path:
    return Path(os.environ.get("FORGESRE_MODELS_DIR") or "data/models")


def _first_part(name: str) -> bool:
    m = SPLIT.match(name)
    return m is None or m.group("part") == "00001"


def _parts(directory: Path, name: str) -> list[Path]:
    m = SPLIT.match(name)
    if m is None:
        return [directory / name]
    total = int(m.group("total"))
    return [directory / f"{m.group('base')}-{i:05d}-of-{total:05d}.gguf" for i in range(1, total + 1)]


def _is_gguf(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"GGUF"
    except OSError:
        return False


def active_name(directory: Path | None = None) -> str:
    """The model llama.cpp should run: the ``active`` file, else model.gguf, else the first one."""
    d = directory or models_dir()
    try:
        name = (d / ACTIVE_FILE).read_text(encoding="utf-8").strip().splitlines()[0]
        if name and (d / name).is_file():
            return name
    except (OSError, IndexError):
        pass
    if (d / "model.gguf").is_file():
        return "model.gguf"
    names = sorted(p.name for p in d.glob("*.gguf") if _first_part(p.name))
    return names[0] if names else ""


def installed(directory: Path | None = None) -> list[dict[str, Any]]:
    d = directory or models_dir()
    active = active_name(d)
    out = []
    for path in sorted(d.glob("*.gguf")):
        if not _first_part(path.name):
            continue
        parts = _parts(d, path.name)
        complete = all(p.is_file() for p in parts)
        size = sum(p.stat().st_size for p in parts if p.is_file())
        catalog = next((c for c in CATALOG if Path(urlparse(c["files"][0]).path).name == path.name), None)
        out.append({"name": path.name, "size_gb": round(size / 1e9, 2), "parts": len(parts), "complete": complete,
                    "valid": complete and _is_gguf(path), "active": path.name == active, "catalog": catalog["id"] if catalog else ""})
    return out


def ram_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1e6, 1)
    except OSError:
        pass
    return 0.0


def overview(db: Session) -> dict[str, Any]:
    """Settings › ForgeAI models: what is installed and active, what can be fetched, running downloads."""
    from app.models import Job

    d = models_dir()
    writable = d.is_dir() and os.access(d, os.W_OK)
    free = round(shutil.disk_usage(d).free / 1e9, 1) if d.is_dir() else 0.0
    jobs = db.query(Job).filter(Job.kind == JOB_KIND).order_by(Job.id.desc()).limit(5).all()
    return {
        "dir": str(d), "writable": writable, "disk_free_gb": free, "ram_gb": ram_gb(), "active": active_name(d) if d.is_dir() else "",
        "installed": installed(d) if d.is_dir() else [], "catalog": CATALOG,
        "downloads": [{"id": j.id, "status": j.status, "error": j.error or "", **{k: (j.payload or {}).get(k) for k in ("label", "done_bytes", "total_bytes", "file", "cancel")}} for j in jobs],
    }


def check_url(url: str) -> list[str]:
    """Files to fetch for a Hugging Face .gguf URL; a split model's first part brings all parts."""
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"huggingface.co", "hf.co"} or "/resolve/" not in parsed.path:
        raise ValueError("use a https://huggingface.co/<org>/<repo>/resolve/<branch>/<file>.gguf link")
    name = Path(parsed.path).name
    if not NAME_OK.match(name):
        raise ValueError("the link must end in a .gguf file name")
    m = SPLIT.match(name)
    if m is None:
        return [url.strip()]
    total = int(m.group("total"))
    base_url = url.strip().rsplit("/", 1)[0]
    return [f"{base_url}/{m.group('base')}-{i:05d}-of-{total:05d}.gguf" for i in range(1, total + 1)]


def queue_download(db: Session, *, catalog_id: str = "", url: str = "", actor: str) -> int:
    from app.jobs import enqueue

    if catalog_id:
        entry = next((c for c in CATALOG if c["id"] == catalog_id), None)
        if entry is None:
            raise ValueError("unknown model")
        files, label = list(entry["files"]), entry["name"]
    else:
        files = check_url(url)
        label = Path(urlparse(files[0]).path).name
    first = Path(urlparse(files[0]).path).name
    job = enqueue(db, JOB_KIND, first, object_type="model", payload={"files": files, "label": label, "actor": actor})
    db.commit()
    return job.id


def run_download(db: Session, job: Any, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """Jobs worker: fetch each file to ``.partial`` (resuming), then rename. Progress is saved on the job
    every 2 s; setting ``cancel`` on the job stops it. Refuses when the disk would be left with < 2 GB."""
    d = models_dir()
    files: list[str] = list((job.payload or {}).get("files") or [])
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0), headers={"User-Agent": "forgesre-models"})
    try:
        sizes = []
        for url in files:
            head = client.head(url)
            head.raise_for_status()
            sizes.append(int(head.headers.get("content-length") or 0))
        total = sum(sizes)
        if total and shutil.disk_usage(d).free < total + 2e9:
            raise RuntimeError(f"not enough disk: needs {total / 1e9:.1f} GB plus 2 GB, {shutil.disk_usage(d).free / 1e9:.1f} GB free")
        done = 0
        last = 0.0
        for url, size in zip(files, sizes):
            name = Path(urlparse(url).path).name
            target, partial = d / name, d / f"{name}.partial"
            if target.is_file() and (not size or target.stat().st_size == size):
                done += target.stat().st_size
                continue
            have = partial.stat().st_size if partial.is_file() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                mode = "ab" if have and resp.status_code == 206 else "wb"
                done += have if mode == "ab" else 0
                with partial.open(mode) as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        fh.write(chunk)
                        done += len(chunk)
                        if time.monotonic() - last > 2:
                            last = time.monotonic()
                            db.refresh(job)
                            if (job.payload or {}).get("cancel"):
                                raise RuntimeError("cancelled")
                            job.payload = {**(job.payload or {}), "done_bytes": done, "total_bytes": total, "file": name}
                            db.commit()
            if size and partial.stat().st_size != size:
                raise RuntimeError(f"{name}: got {partial.stat().st_size} of {size} bytes; Download again resumes it")
            partial.rename(target)
        first = d / Path(urlparse(files[0]).path).name
        if not _is_gguf(first):
            raise RuntimeError(f"{first.name} is not a GGUF file")
        job.payload = {**(job.payload or {}), "done_bytes": total or done, "total_bytes": total or done, "file": ""}
        return {"model": first.name, "bytes": total or done}
    finally:
        if own:
            client.close()


def activate(db: Session, name: str, actor: str) -> dict[str, Any]:
    """Make ``name`` the model llama.cpp runs (it restarts within ~5 s) and set the catalog's timeout."""
    from app.system_settings import set_setting

    d = models_dir()
    if not NAME_OK.match(name) or not (d / name).is_file() or not _first_part(name):
        raise LookupError("model not found")
    if not all(p.is_file() for p in _parts(d, name)) or not _is_gguf(d / name):
        raise ValueError("the model is incomplete or not a GGUF file")
    tmp = d / f".{ACTIVE_FILE}.tmp"
    tmp.write_text(name + "\n", encoding="utf-8")
    tmp.replace(d / ACTIVE_FILE)
    entry = next((c for c in CATALOG if Path(urlparse(c["files"][0]).path).name == name), None)
    timeout = entry["timeout_s"] if entry else None
    if timeout:
        set_setting(db, "ai.llm.timeout_seconds", timeout, actor)
    # a model that cannot think (OpsLLM, Qwen2.5) would only waste the thinking budget
    if entry and entry.get("thinking") is False:
        set_setting(db, "ai.llm.thinking", False, actor)
    return {"active": name, "timeout_s": timeout}


def label(name: str) -> str:
    """Catalogue name for a downloaded file, else the file name without .gguf (an imported model)."""
    entry = next((c for c in CATALOG if Path(urlparse(c["files"][0]).path).name == name), None)
    return entry["name"] if entry else re.sub(r"(-0+1-of-\d+)?\.gguf$", "", name)


def switchable(directory: Path | None = None) -> list[dict[str, Any]]:
    """Downloaded and imported models that are complete and can be run, for the chat's model picker."""
    return [{"name": m["name"], "label": label(m["name"]), "size_gb": m["size_gb"], "active": m["active"],
             "source": "catalog" if m["catalog"] else "imported"} for m in installed(directory) if m["valid"]]


def remove(name: str) -> list[str]:
    d = models_dir()
    if not NAME_OK.match(name) or not _first_part(name):
        raise LookupError("model not found")
    if name == active_name(d):
        raise ValueError("this model is in use; activate another one first")
    gone = []
    for p in _parts(d, name):
        for path in (p, p.with_name(p.name + ".partial")):
            if path.is_file():
                path.unlink()
                gone.append(path.name)
    if not gone:
        raise LookupError("model not found")
    return gone


async def save_upload(upload: Any, *, size_hint: int = 0) -> dict[str, Any]:
    """Store an uploaded .gguf in chunks (``.partial`` then rename). Checks the GGUF header and free disk."""
    d = models_dir()
    name = Path(upload.filename or "").name
    if not NAME_OK.match(name):
        raise ValueError("the file must be a .gguf with a plain name (letters, digits, . _ -)")
    if size_hint and shutil.disk_usage(d).free < size_hint + 2e9:
        raise ValueError("not enough disk for this file plus 2 GB")
    partial = d / f"{name}.partial"
    first = True
    total = 0
    try:
        with partial.open("wb") as fh:
            while chunk := await upload.read(1 << 20):
                if first and not chunk.startswith(b"GGUF"):
                    raise ValueError("this is not a GGUF model file")
                first = False
                fh.write(chunk)
                total += len(chunk)
        partial.rename(d / name)
    finally:
        if partial.exists():
            partial.unlink()
    return {"name": name, "size_gb": round(total / 1e9, 2)}
