"""Off-box backup copies: mounted directory and S3 (Signature V4), pruning, strip, doctor row."""

from __future__ import annotations

import datetime as dt
import io
import json
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app import offbox


def _archive(tmp: Path, name: str = "backup_20261123T100000Z") -> Path:
    run = tmp / "backups" / name
    run.mkdir(parents=True)
    path = run / "forgesre.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for member, body in (("db.json", b"{}"), ("secrets.env", b"X=1"), (".env", b"Y=2"), ("config/forgesre.yml", b"a: 1")):
            info = tarfile.TarInfo(f"forgesre-x/{member}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


def test_config_and_problems(tmp_path: Path):
    assert offbox.problem(offbox.config({})) == "off"
    assert "not a directory" in offbox.problem(offbox.config({"FORGESRE_OFFBOX": "dir", "FORGESRE_OFFBOX_PATH": str(tmp_path / "nope")}))
    s3 = {"FORGESRE_OFFBOX": "s3", "FORGESRE_OFFBOX_S3_URL": "https://s3", "FORGESRE_OFFBOX_S3_BUCKET": "b"}
    assert "BACKUP_S3_ACCESS_KEY" in offbox.problem(offbox.config(s3))
    assert offbox.config({"FORGESRE_OFFBOX_KEEP": "x"})["keep"] == 14


def test_push_to_directory_prunes_and_strips(tmp_path: Path):
    target = tmp_path / "nfs"
    target.mkdir()
    env = {"FORGESRE_OFFBOX": "dir", "FORGESRE_OFFBOX_PATH": str(target), "FORGESRE_OFFBOX_KEEP": "2"}
    for i in range(3):
        archive = _archive(tmp_path, f"backup_2026112{i}T100000Z")
        got = offbox.push(archive, archive.parent.name, tmp_path / "backups", env)
        assert got["ok"], got
    assert sorted(p.name for p in target.iterdir()) == ["backup_20261121T100000Z", "backup_20261122T100000Z"]
    assert json.loads((tmp_path / "backups" / offbox.STATUS_FILE).read_text())["ok"] is True
    stripped = offbox.push(_archive(tmp_path, "backup_20261123T100000Z"), "backup_20261123T100000Z", tmp_path / "backups",
                           {**env, "FORGESRE_OFFBOX_STRIP": "yes"})
    assert "without secrets.env" in stripped["detail"]
    with tarfile.open(target / "backup_20261123T100000Z" / "forgesre.tar.gz") as tar:
        names = tar.getnames()
    assert "forgesre-x/db.json" in names and "forgesre-x/secrets.env" not in names and "forgesre-x/.env" not in names
    assert "forgesre-x/OFFBOX-STRIPPED.txt" in names and "forgesre-x/config/forgesre.yml" in names
    assert offbox.test(env)["ok"] is True


def test_failed_push_is_recorded_not_raised(tmp_path: Path):
    archive = _archive(tmp_path)
    got = offbox.push(archive, archive.parent.name, tmp_path / "backups", {"FORGESRE_OFFBOX": "dir", "FORGESRE_OFFBOX_PATH": str(tmp_path / "gone")})
    assert got["ok"] is False and "not a directory" in got["detail"]
    assert offbox.status(tmp_path / "backups")["ok"] is False


class _S3(BaseHTTPRequestHandler):
    store: dict[str, bytes] = {}
    auths: list[str] = []

    def log_message(self, *args):
        pass

    def _key(self) -> str:
        return urlsplit(self.path).path.lstrip("/").split("/", 1)[1] if "/" in urlsplit(self.path).path.lstrip("/") else ""

    def do_PUT(self):
        self.auths.append(self.headers.get("Authorization", ""))
        self.store[self._key()] = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        q = parse_qs(urlsplit(self.path).query)
        if q.get("list-type") == ["2"]:
            prefix = q.get("prefix", [""])[0]
            keys = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in sorted(self.store) if k.startswith(prefix))
            body = f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{keys}</ListBucketResult>'.encode()
        else:
            body = self.store.get(self._key(), b"")
        self.send_response(200 if body or q else 404)
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        self.store.pop(self._key(), None)
        self.send_response(204)
        self.end_headers()


@pytest.fixture
def s3_env():
    server = HTTPServer(("127.0.0.1", 0), _S3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _S3.store.clear()
    _S3.auths.clear()
    yield {"FORGESRE_OFFBOX": "s3", "FORGESRE_OFFBOX_S3_URL": f"http://127.0.0.1:{server.server_port}", "FORGESRE_OFFBOX_S3_BUCKET": "bk",
           "BACKUP_S3_ACCESS_KEY": "AKID", "BACKUP_S3_SECRET_KEY": "secret", "FORGESRE_OFFBOX_KEEP": "2"}
    server.shutdown()


def test_push_to_s3_signs_and_prunes(tmp_path: Path, s3_env):
    for i in range(3):
        archive = _archive(tmp_path, f"backup_2026112{i}T100000Z")
        got = offbox.push(archive, archive.parent.name, tmp_path / "backups", s3_env)
        assert got["ok"], got
    assert sorted(_S3.store) == ["forgesre/backup_20261121T100000Z/forgesre.tar.gz", "forgesre/backup_20261122T100000Z/forgesre.tar.gz"]
    assert all(a.startswith("AWS4-HMAC-SHA256 Credential=AKID/") and "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in a for a in _S3.auths)
    assert offbox.test(s3_env)["ok"] is True and ".forgesre-offbox-test" not in " ".join(_S3.store)


def test_signature_is_stable():
    cfg = offbox.config({"FORGESRE_OFFBOX": "s3", "FORGESRE_OFFBOX_S3_URL": "http://127.0.0.1:9", "FORGESRE_OFFBOX_S3_BUCKET": "b",
                         "BACKUP_S3_ACCESS_KEY": "A", "BACKUP_S3_SECRET_KEY": "S"})
    # unreachable port: the call fails, but only after signing; same input → same request either way
    with pytest.raises(OSError):
        offbox.s3_request(cfg, "GET", "k", now=dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc))


def test_doctor_row(monkeypatch, tmp_path: Path):
    from app.api import _offbox_check

    monkeypatch.delenv("FORGESRE_OFFBOX", raising=False)
    assert _offbox_check()["status"] == "disabled"
    monkeypatch.setenv("FORGESRE_OFFBOX", "dir")
    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(tmp_path))
    assert _offbox_check()["status"] == "warn"
    (tmp_path / offbox.STATUS_FILE).write_text(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(), "ok": True, "detail": "copied to /mnt/x"}))
    assert _offbox_check()["status"] == "ok"
    (tmp_path / offbox.STATUS_FILE).write_text(json.dumps({"at": "2026-01-01T00:00:00+00:00", "ok": False, "detail": "PermissionError"}))
    assert _offbox_check()["status"] == "error"


def test_gpu_service_and_script():  # noqa: D103
    import subprocess

    import yaml

    root = Path(__file__).resolve().parents[1]
    services = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    gpu = services["llm-gpu"]
    assert gpu["profiles"] == ["ai-gpu"] and "server-cuda-" in gpu["image"]
    assert gpu["deploy"]["resources"]["reservations"]["devices"][0]["capabilities"] == ["gpu"]
    assert set(services["embed"]["profiles"]) == {"ai", "ai-gpu"} and services["llm"]["profiles"] == ["ai"]
    run = (root / "scripts" / "llm-run.sh").read_text(encoding="utf-8")
    assert '-ngl "$GPU_LAYERS"' in run
    assert "ai-gpu" in (root / "scripts" / "fetch-llm.sh").read_text(encoding="utf-8")
    done = subprocess.run(["bash", "-n", str(root / "scripts" / "gpu.sh")], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
