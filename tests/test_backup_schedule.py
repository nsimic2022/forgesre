"""Nightly backup timer, backup verify and pruning."""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from app import backup_ops

ROOT = Path(__file__).resolve().parents[1]


def _archive(folder: Path, name: str, db: dict | None, *, secrets: bool = True) -> Path:
    run = folder / name
    run.mkdir(parents=True)
    path = run / "forgesre.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        files = {"config/forgesre.yml": b"a: 1", "dotenv": b"X=1"}
        if secrets:
            files["secrets.env"] = b"Y=2"
        if db is not None:
            files["db.json"] = json.dumps(db).encode()
        for member, body in files.items():
            info = tarfile.TarInfo(f"forgesre-x/{member}")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


def test_verify_good_and_bad_archives(tmp_path: Path):
    good = _archive(tmp_path, "backup_20261124T013000Z", {"users": [{"id": 1}], "incidents": [], "assets": [{"id": 1}]})
    got = backup_ops.verify(good)
    assert got["ok"] and got["has_secrets"] and got["has_env"] and got["tables"] == 3
    assert "OK:" in backup_ops.format_verify(got) and "users 1" in backup_ops.format_verify(got)
    no_db = _archive(tmp_path, "backup_20261124T013100Z", None, secrets=False)
    bad = backup_ops.verify(no_db)
    assert not bad["ok"] and "no db.json" in bad["problems"][0] and "restore needs them" in backup_ops.format_verify(bad)
    empty_users = _archive(tmp_path, "backup_20261124T013200Z", {"users": [], "incidents": [], "assets": []})
    assert "table users is empty" in backup_ops.verify(empty_users)["problems"]
    cut = tmp_path / "cut.tar.gz"
    cut.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    assert backup_ops.verify(cut)["problems"][0].startswith("archive cannot be read")
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(gzip.compress(b"not a tar"))
    assert not backup_ops.verify(junk)["ok"]


def test_prune_keeps_newest(tmp_path: Path):
    for day in range(1, 6):
        (tmp_path / f"backup_2026112{day}T013000Z").mkdir()
    (tmp_path / "other").mkdir()
    removed = backup_ops.prune(tmp_path, 2)
    assert removed == ["backup_20261121T013000Z", "backup_20261122T013000Z", "backup_20261123T013000Z"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backup_20261124T013000Z", "backup_20261125T013000Z", "other"]
    assert backup_ops.keep_count({"FORGESRE_BACKUP_KEEP": "x"}) == 14 and backup_ops.keep_count({"FORGESRE_BACKUP_KEEP": "3"}) == 3


def test_schedule_writes_systemd_units(tmp_path: Path):
    clone = tmp_path / "forgesre"
    (clone / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "backup-schedule.sh", clone / "scripts" / "backup-schedule.sh")
    units = tmp_path / "units"
    units.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(f"#!/bin/sh\necho systemctl \"$@\" >> {tmp_path}/calls.log\n")
    (bin_dir / "systemctl").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FORGESRE_SYSTEMD_DIR": str(units)}
    run = lambda *a: subprocess.run(["bash", str(clone / "scripts" / "backup-schedule.sh"), *a], env=env, capture_output=True, text=True, timeout=30)  # noqa: E731
    assert run("on", "25:00").returncode == 1
    done = run("on", "03:15")
    assert done.returncode == 0, done.stderr
    timer = (units / "forgesre-backup.timer").read_text()
    service = (units / "forgesre-backup.service").read_text()
    assert "OnCalendar=*-*-* 03:15:00" in timer and "Persistent=true" in timer
    assert f"ExecStart={clone}/forgesre backup --scheduled" in service and "TimeoutStartSec=2h" in service
    assert "enable --now forgesre-backup.timer" in (tmp_path / "calls.log").read_text()
    assert "Nightly at 03:15" in run("status").stdout
    assert run("off").returncode == 0 and not (units / "forgesre-backup.timer").exists()


def test_doctor_row_and_cli(monkeypatch, tmp_path: Path):
    from app.api import _backup_verify_check

    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(tmp_path))
    assert _backup_verify_check()["status"] == "disabled"
    backup_ops.record(tmp_path, {"ok": False, "problems": ["no db.json in the archive"], "archive": "x"})
    assert _backup_verify_check()["status"] == "error"
    good = _archive(tmp_path, "backup_20261124T013000Z", {"users": [{"id": 1}], "incidents": [], "assets": []})
    backup_ops.record(tmp_path, backup_ops.verify(good))
    assert _backup_verify_check()["status"] == "ok"
    cli = (ROOT / "scripts" / "forgesre").read_text(encoding="utf-8")
    assert 'exec "$ROOT/scripts/backup-schedule.sh"' in cli
    assert "--backup-schedule) BACKUP_SCHEDULE" in (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
