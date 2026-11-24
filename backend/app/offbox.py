"""Off-box copies of ForgeSRE backups: a mounted directory (NFS, SMB) or S3-compatible storage.

Runs on the host with the standard library only (``./forgesre backup`` uses the host's
python3, which has no boto3). Configured in ``.env`` and ``secrets/secrets.env``:

    FORGESRE_OFFBOX=dir | s3           (empty: off)
    FORGESRE_OFFBOX_PATH=/mnt/nfs/forgesre          dir
    FORGESRE_OFFBOX_S3_URL=https://s3.dc.local:9000 s3 endpoint (MinIO, Ceph, AWS)
    FORGESRE_OFFBOX_S3_BUCKET / _PREFIX / _REGION   s3 (region default us-east-1)
    FORGESRE_OFFBOX_KEEP=14            copies kept there, oldest removed
    FORGESRE_OFFBOX_STRIP=yes          leave secrets.env and .env out of the copy
    BACKUP_S3_ACCESS_KEY / BACKUP_S3_SECRET_KEY     secrets (./forgesre settings set-secret)

The archive holds the database and, unless stripped, every password: point it only at storage
you control. Each push writes ``.offbox-status.json`` next to the backups; doctor reads it.
Network calls have a timeout; nothing here runs inside Core's web requests.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import io
import json
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

TIMEOUT = 120.0
STATUS_FILE = ".offbox-status.json"
_STRIP_NAMES = {"secrets.env", "dotenv", ".env"}  # .env is stored as "dotenv" in the archive


def config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Off-box settings from the environment (backup.sh exports .env and secrets.env)."""
    e = os.environ if env is None else env
    kind = str(e.get("FORGESRE_OFFBOX") or "").strip().lower()
    try:
        keep = max(1, min(365, int(e.get("FORGESRE_OFFBOX_KEEP") or 14)))
    except ValueError:
        keep = 14
    return {
        "kind": kind if kind in {"dir", "s3"} else "",
        "path": str(e.get("FORGESRE_OFFBOX_PATH") or "").strip(),
        "url": str(e.get("FORGESRE_OFFBOX_S3_URL") or "").strip().rstrip("/"),
        "bucket": str(e.get("FORGESRE_OFFBOX_S3_BUCKET") or "").strip(),
        "prefix": str(e.get("FORGESRE_OFFBOX_S3_PREFIX") or "forgesre").strip().strip("/"),
        "region": str(e.get("FORGESRE_OFFBOX_S3_REGION") or "us-east-1").strip(),
        "access": str(e.get("BACKUP_S3_ACCESS_KEY") or ""),
        "secret": str(e.get("BACKUP_S3_SECRET_KEY") or ""),
        "keep": keep,
        "strip": str(e.get("FORGESRE_OFFBOX_STRIP") or "").strip().lower() in {"1", "yes", "true", "on"},
    }


def problem(cfg: dict[str, Any]) -> str:
    """Why off-box cannot run ("" when it can, "off" when it is not configured)."""
    if not cfg["kind"]:
        return "off"
    if cfg["kind"] == "dir":
        if not cfg["path"]:
            return "FORGESRE_OFFBOX_PATH is empty"
        if not Path(cfg["path"]).is_dir():
            return f"{cfg['path']} is not a directory (is the NFS share mounted?)"
        return ""
    if not cfg["url"].startswith(("http://", "https://")) or not cfg["bucket"]:
        return "set FORGESRE_OFFBOX_S3_URL (https://…) and FORGESRE_OFFBOX_S3_BUCKET"
    if not cfg["access"] or not cfg["secret"]:
        return "set BACKUP_S3_ACCESS_KEY and BACKUP_S3_SECRET_KEY (./forgesre settings set-secret)"
    return ""


def stripped_copy(archive: Path, workdir: Path) -> Path:
    """The archive without secrets.env and .env (for FORGESRE_OFFBOX_STRIP=yes)."""
    out = workdir / archive.name
    with tarfile.open(archive, "r:gz") as src, tarfile.open(out, "w:gz") as dst:
        for member in src.getmembers():
            if Path(member.name).name in _STRIP_NAMES and member.name.count("/") == 1:
                continue
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
        note = b"secrets.env and .env were left out of this off-box copy (FORGESRE_OFFBOX_STRIP=yes).\n"
        info = tarfile.TarInfo(f"{src.getmembers()[0].name.split('/')[0]}/OFFBOX-STRIPPED.txt")
        info.size = len(note)
        dst.addfile(info, io.BytesIO(note))
    return out


# --- S3 (Signature V4, path-style, so MinIO and Ceph work without DNS buckets) ---------------

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def s3_request(cfg: dict[str, Any], method: str, key: str = "", *, query: dict[str, str] | None = None,
               body: Any = None, length: int = 0, now: dt.datetime | None = None) -> tuple[int, bytes]:
    """One signed S3 call. ``body`` may be bytes or an open file of ``length`` bytes."""
    parsed = urllib.parse.urlsplit(cfg["url"])
    host = parsed.netloc
    path = "/" + "/".join(urllib.parse.quote(p, safe="") for p in [cfg["bucket"], *[x for x in key.split("/") if x]])
    q = sorted((query or {}).items())
    canonical_query = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}" for k, v in q)
    when = now or dt.datetime.now(dt.timezone.utc)
    amz_date, day = when.strftime("%Y%m%dT%H%M%SZ"), when.strftime("%Y%m%d")
    payload_hash = "UNSIGNED-PAYLOAD"
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, path, canonical_query, "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, payload_hash])
    scope = f"{day}/{cfg['region']}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _sign(("AWS4" + cfg["secret"]).encode(), day)
    for part in (cfg["region"], "s3", "aws4_request"):
        k = _sign(k, part)
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    req_headers = {"x-amz-content-sha256": payload_hash, "x-amz-date": amz_date,
                   "Authorization": f"AWS4-HMAC-SHA256 Credential={cfg['access']}/{scope}, SignedHeaders={signed}, Signature={signature}"}
    if body is not None:
        req_headers["Content-Length"] = str(length if length else len(body))
    url = f"{parsed.scheme}://{host}{path}" + (f"?{canonical_query}" if canonical_query else "")
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 — URL comes from the admin's .env
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _s3_keys(cfg: dict[str, Any]) -> list[str]:
    status, body = s3_request(cfg, "GET", query={"list-type": "2", "prefix": cfg["prefix"] + "/"})
    if status != 200:
        raise RuntimeError(f"S3 list failed: HTTP {status} {body[:200]!r}")
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    root = ET.fromstring(body)
    return sorted(e.text or "" for e in root.findall(".//s3:Contents/s3:Key", ns))


# --- push / prune / test --------------------------------------------------------------------

def _push_dir(cfg: dict[str, Any], archive: Path, name: str) -> str:
    target = Path(cfg["path"]) / name
    target.mkdir(parents=True, exist_ok=True)
    tmp = target / (archive.name + ".part")
    shutil.copyfile(archive, tmp)
    os.replace(tmp, target / archive.name)  # a half-written copy never looks complete
    copies = sorted(p for p in Path(cfg["path"]).iterdir() if p.is_dir() and p.name.startswith("backup_"))
    for old in copies[: max(0, len(copies) - cfg["keep"])]:
        shutil.rmtree(old, ignore_errors=True)
    return str(target / archive.name)


def _push_s3(cfg: dict[str, Any], archive: Path, name: str) -> str:
    key = f"{cfg['prefix']}/{name}/{archive.name}"
    with archive.open("rb") as fh:
        status, body = s3_request(cfg, "PUT", key, body=fh, length=archive.stat().st_size)
    if status not in {200, 201}:
        raise RuntimeError(f"S3 upload failed: HTTP {status} {body[:200]!r}")
    runs = sorted({k.split("/")[1] for k in _s3_keys(cfg) if k.count("/") >= 2 and k.split("/")[1].startswith("backup_")})
    for old in runs[: max(0, len(runs) - cfg["keep"])]:
        for k in [k for k in _s3_keys(cfg) if k.startswith(f"{cfg['prefix']}/{old}/")]:
            s3_request(cfg, "DELETE", k)
    return f"s3://{cfg['bucket']}/{key}"


def push(archive: Path, run_name: str, backup_dir: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Copy one backup off the box and prune old copies there; records the result for doctor."""
    cfg = config(env)
    why = problem(cfg)
    if why == "off":
        return {"ok": True, "skipped": True, "detail": "off-box copy is off (FORGESRE_OFFBOX)"}
    result: dict[str, Any] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "kind": cfg["kind"], "backup": run_name}
    try:
        if why:
            raise RuntimeError(why)
        with tempfile.TemporaryDirectory() as tmp:
            source = stripped_copy(archive, Path(tmp)) if cfg["strip"] else archive
            where = _push_dir(cfg, source, run_name) if cfg["kind"] == "dir" else _push_s3(cfg, source, run_name)
        result.update(ok=True, detail=f"copied to {where}" + (" (without secrets.env and .env)" if cfg["strip"] else ""))
    except Exception as exc:  # recorded for doctor; the local backup is still fine
        result.update(ok=False, detail=f"{type(exc).__name__}: {exc}"[:400])
    try:
        (backup_dir / STATUS_FILE).write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result


def test(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Write, read back and delete a small file on the target."""
    cfg = config(env)
    why = problem(cfg)
    if why:
        return {"ok": False, "detail": why}
    probe = b"forgesre off-box test\n"
    try:
        if cfg["kind"] == "dir":
            path = Path(cfg["path"]) / ".forgesre-offbox-test"
            path.write_bytes(probe)
            ok = path.read_bytes() == probe
            path.unlink()
        else:
            key = f"{cfg['prefix']}/.forgesre-offbox-test"
            status, body = s3_request(cfg, "PUT", key, body=probe)
            if status not in {200, 201}:
                return {"ok": False, "detail": f"S3 PUT HTTP {status} {body[:200]!r}"}
            status, got = s3_request(cfg, "GET", key)
            ok = status == 200 and got == probe
            s3_request(cfg, "DELETE", key)
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}
    target = cfg["path"] if cfg["kind"] == "dir" else f"{cfg['url']}/{cfg['bucket']}/{cfg['prefix']}"
    return {"ok": bool(ok), "detail": f"wrote, read and deleted a test file in {target}" if ok else "the test file read back wrong"}


def status(backup_dir: Path) -> dict[str, Any] | None:
    """The last push result, or None when none was recorded."""
    try:
        return json.loads((backup_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
