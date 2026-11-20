"""Ported from the classic pages: inventory bulk delete and CSV export, backup download and restore, automation export."""

from __future__ import annotations

import csv
import io
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import Asset, Job, User
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


def _letters() -> str:
    return "".join(chr(97 + int(c, 16) % 26) for c in uuid4().hex[:6])


def test_inventory_csv_round_trips_and_bulk_delete_reports_each_asset():
    from app.asset_import import COLUMNS, preview

    engineer = _client("engineer")
    tag = _letters()
    db = SessionLocal()
    db.add_all([Asset(asset_id=f"bulk-{tag}-a", hostname=f"bulk-{tag}-a", ip="", type="Linux Server", extras={"site": "DC9", "rack": "R1"}),
                Asset(asset_id=f"bulk-{tag}-b", hostname=f"bulk-{tag}-b", ip="", type="Switch")])
    db.commit()
    text = engineer.get("/api/v1/inventory.csv", params={"q": f"bulk-{tag}"}).text
    assert text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    assert [r["hostname"] for r in rows] == [f"bulk-{tag}-a", f"bulk-{tag}-b"] and list(rows[0]) == list(COLUMNS)
    assert rows[0]["site"] == "DC9" and rows[0]["rack"] == "R1"
    # the exported file goes back into Import CSV; every row is recognised as already there
    assert {r["action"] for r in preview(db, text)["rows"]} == {"exists"}
    viewer = _client("viewer")
    assert viewer.post("/api/v1/inventory/bulk-delete", json={"ids": [f"bulk-{tag}-a"]}).status_code == 403
    out = engineer.post("/api/v1/inventory/bulk-delete", json={"ids": [f"bulk-{tag}-a", f"bulk-{tag}-b", "nope"]}).json()
    assert out["removed"] == [f"bulk-{tag}-a", f"bulk-{tag}-b"] and out["refused"] == [{"asset_id": "nope", "reason": "not found"}]
    db.expire_all()
    assert db.query(Asset).filter(Asset.asset_id.like(f"bulk-{tag}%")).count() == 0
    db.close()


def test_backup_download_and_confirmed_restore_in_a_job(tmp_path, monkeypatch):
    from app import backup
    from app.jobs import run_pending_jobs

    monkeypatch.setenv("FORGESRE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("FORGESRE_RESTORE_STOP_CORE", "0")
    monkeypatch.setenv("FORGESRE_FILES_WRITABLE", "0")
    admin = _client("admin")
    made = backup.create_backup(include_secrets=False)
    name = backup.list_archives()[0]["name"]
    down = admin.get(f"/api/v1/backups/{name}/download")
    assert down.status_code == 200 and down.content[:2] == b"\x1f\x8b"
    assert admin.get("/api/v1/backups/..%2Fsecrets.env/download").status_code == 404
    assert _client("engineer").get(f"/api/v1/backups/{name}/download").status_code == 403
    refused = admin.post(f"/api/v1/backups/{name}/restore", json={"confirm": "yes", "acknowledged": True})
    assert refused.status_code == 400 and "RESTORE" in refused.json()["detail"]
    assert admin.post(f"/api/v1/backups/{name}/restore", json={"confirm": "RESTORE", "acknowledged": False}).status_code == 400
    restored: list[str] = []
    monkeypatch.setattr(backup, "restore_archive", lambda path, confirm, stop_core: restored.append(path.name) or {"notes": ["database restored"]})
    job = admin.post(f"/api/v1/backups/{name}/restore", json={"confirm": "RESTORE", "acknowledged": True}).json()["job"]
    assert admin.get("/api/v1/backups").json()["restoring"] is True
    db = SessionLocal()
    run_pending_jobs(db, limit=20)
    assert restored == ["forgesre.tar.gz"] and made.name
    db.query(Job).filter_by(id=job).delete()
    db.commit()
    db.close()
    last = admin.get("/api/v1/backups").json()["last_restore"]
    assert last["status"] == "ok" and "Restored" in last["summary"]


def test_automation_export_is_one_json_file():
    analyst = _client("analyst")
    r = analyst.get("/api/v1/automation/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = json.loads(r.text)
    for key in ("routing_rules", "playbooks", "escalation_policies", "noise_filters", "oncall", "slos", "forgeai_skills"):
        assert isinstance(data[key], list), key
    assert data["forgeai_skills"] and "instructions" in data["forgeai_skills"][0]
    assert _client("viewer").get("/api/v1/automation/export").status_code == 403
