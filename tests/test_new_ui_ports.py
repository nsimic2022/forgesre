"""What moved from the classic pages to the new UI: inventory CSV and bulk delete, automation export,
backup download and restore."""

from __future__ import annotations

import csv
import io
import time
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, JournalEntry, User
from app.security import hash_password
from app.seed import seed


def _client(role: str) -> TestClient:
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


def _tag() -> str:
    return "".join(chr(97 + int(c, 16) % 26) for c in uuid4().hex[:6])


def test_inventory_csv_uses_import_columns_and_bulk_delete_reports_each():
    eng = _client("engineer")
    tag = _tag()
    db = SessionLocal()
    db.add_all([Asset(asset_id=f"exa-{tag}", hostname=f"exa-{tag}", ip="", type="Switch", extras={"site": "DC9", "service": "ERP"}),
                Asset(asset_id=f"exb-{tag}", hostname=f"exb-{tag}", ip="", type="Linux Server")])
    db.commit()
    db.close()
    text = eng.get(f"/api/v1/inventory.csv?q=ex&type=Switch").text.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text)))
    mine = [r for r in rows if r["hostname"] == f"exa-{tag}"]
    assert mine and mine[0]["site"] == "DC9" and mine[0]["service"] == "ERP" and mine[0]["type"] == "Switch"
    assert not any(r["hostname"] == f"exb-{tag}" for r in rows)  # the type filter applies
    viewer = _client("viewer")
    assert viewer.post("/api/v1/assets/bulk-delete", json={"asset_ids": [f"exa-{tag}"]}).status_code == 403
    out = eng.post("/api/v1/assets/bulk-delete", json={"asset_ids": [f"exa-{tag}", f"exb-{tag}", "nope-zz"]}).json()
    assert sorted(out["removed"]) == [f"exa-{tag}", f"exb-{tag}"] and out["kept"] == [{"asset_id": "nope-zz", "reason": "not found"}]
    assert eng.post("/api/v1/assets/bulk-delete", json={"asset_ids": []}).status_code == 400


def test_automation_export_is_one_json_file():
    analyst = _client("analyst")
    r = analyst.get("/api/v1/automation/export")
    assert r.status_code == 200 and "forgesre-automation.json" in r.headers["content-disposition"]
    data = r.json()
    assert {"routing_rules", "playbooks", "escalation_policies", "noise_filters", "forgeai_skills"} <= set(data)
    assert any(s["slug"] == "incident-generic" for s in data["forgeai_skills"])
    assert _client("viewer").get("/api/v1/automation/export").status_code == 403


def test_backup_download_and_typed_restore(monkeypatch, tmp_path: Path):
    from app import backup

    archive = tmp_path / "forgesre.tar.gz"
    archive.write_bytes(b"not really a backup")
    monkeypatch.setattr(backup, "resolve_archive", lambda name: archive if name == "backup_x" else (_ for _ in ()).throw(FileNotFoundError(name)))
    monkeypatch.setattr(backup, "backup_ident", lambda path: "backup_x")
    restored: list[Path] = []
    monkeypatch.setattr(backup, "restore_archive", lambda path, **kw: restored.append(path) or {"notes": ["database restored"]})
    admin = _client("admin")
    got = admin.get("/api/v1/backups/backup_x/download")
    assert got.status_code == 200 and got.content == b"not really a backup"
    assert admin.get("/api/v1/backups/other/download").status_code == 404
    assert admin.post("/api/v1/backups/backup_x/restore", json={"confirm": "yes"}).status_code == 400
    assert _client("engineer").post("/api/v1/backups/backup_x/restore", json={"confirm": "RESTORE"}).status_code == 403
    assert admin.post("/api/v1/backups/backup_x/restore", json={"confirm": "RESTORE"}).json() == {"started": "backup_x"}
    for _ in range(50):
        state = admin.get("/api/v1/backups").json()["restore"]
        if state and state["status"] != "running":
            break
        time.sleep(0.1)
    assert restored == [archive] and state["status"] == "ok" and state["summary"] == "Restored backup_x"
    db = SessionLocal()
    assert db.query(JournalEntry).filter_by(module="backup", action="restore", status="ok").count() >= 1
    db.close()
