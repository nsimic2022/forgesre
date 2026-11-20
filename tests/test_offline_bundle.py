"""Offline bundle: load-bundle.sh checks the checksums, loads the images and unpacks the code."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tgz(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _bundle(tmp: Path, *, damage: bool = False) -> Path:
    work = tmp / "work"
    work.mkdir()
    (work / "forgesre-src.tar.gz").write_bytes(_tgz({"forgesre/docker-compose.yml": "services: {}\n", "forgesre/README.md": "hi\n"}))
    (work / "images.tar.gz").write_bytes(_tgz({"manifest.json": "[]"}))
    (work / "load-bundle.sh").write_bytes((ROOT / "scripts" / "load-bundle.sh").read_bytes())
    (work / "MANIFEST.txt").write_text("ForgeSRE bundle 2.0.0\nproject: buildbox\ncore_inputs: abc123\nimages:\n  postgres:16-alpine\n")
    sums = "".join(f"{hashlib.sha256((work / n).read_bytes()).hexdigest()}  ./{n}\n" if n.endswith(".gz") else
                   f"{hashlib.sha256((work / n).read_bytes()).hexdigest()}  {n}\n"
                   for n in ("forgesre-src.tar.gz", "images.tar.gz", "load-bundle.sh", "MANIFEST.txt"))
    (work / "SHA256SUMS").write_text(sums)
    if damage:
        (work / "images.tar.gz").write_bytes(b"broken")
    out = tmp / "forgesre-bundle-2.0.0-abc.tar"
    with tarfile.open(out, "w") as tar:
        for f in work.iterdir():
            tar.add(f, arcname=f.name)
    return out


def _fake_docker(tmp: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    log = tmp / "docker.log"
    (bin_dir / "docker").write_text(f"#!/usr/bin/env bash\necho \"$@\" >> {log}\ncat >/dev/null || true\nexit 0\n")
    (bin_dir / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, log


def test_load_bundle_unpacks_code_loads_images_and_marks_offline(tmp_path):
    bundle = _bundle(tmp_path)
    env, log = _fake_docker(tmp_path)
    target = tmp_path / "forgesre"
    run = subprocess.run(["bash", str(ROOT / "scripts" / "load-bundle.sh"), str(bundle), str(target)],
                         capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert (target / "docker-compose.yml").exists() and (target / "README.md").read_text() == "hi\n"
    assert (target / "data" / ".core-image.stamp").read_text().strip() == "abc123"
    assert (target / "data" / ".offline-bundle").exists()
    calls = log.read_text()
    assert "load" in calls
    assert "./install.sh --offline" in run.stdout


def test_damaged_bundle_is_refused(tmp_path):
    bundle = _bundle(tmp_path, damage=True)
    env, log = _fake_docker(tmp_path)
    run = subprocess.run(["bash", str(ROOT / "scripts" / "load-bundle.sh"), str(bundle), str(tmp_path / "t")],
                         capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60)
    assert run.returncode == 1 and "damaged" in run.stdout
    assert "load" not in (log.read_text() if log.exists() else "")


def test_bundle_script_saves_every_profile_and_offline_install_skips_the_build():
    text = (ROOT / "scripts" / "bundle.sh").read_text()
    assert "config --profiles" in text and "docker" in text and "save" in text and "git archive" in text
    install = (ROOT / "scripts" / "install.sh").read_text()
    assert ".offline-bundle" in install and ".offline-bundle" in (ROOT / "scripts" / "update.sh").read_text()
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / "bundle.sh")]).returncode == 0
