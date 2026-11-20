"""Inventory › Import CSV: preview checks every row and writes nothing; the job adds the accepted rows."""

from __future__ import annotations

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


def _drop(*ids: str) -> None:
    db = SessionLocal()
    for asset_id in ids:
        db.query(Asset).filter_by(asset_id=asset_id).delete()
    db.commit()
    db.close()


def test_preview_checks_rows_and_writes_nothing():
    from app.asset_import import preview

    db = SessionLocal()
    seed(db)
    demo = db.query(Asset).filter_by(asset_id="forge-demo-01").one()
    text = "﻿Host;IP;Type;Site;Rack;Parent\n" \
        "csv-sw-core;10.77.0.1;switch;DC1;A01;\n" \
        "csv-web-a;10.77.0.11;Linux Server;DC1;A07;csv-sw-core\n" \
        f"{demo.hostname};{demo.ip};Linux Server;;;\n" \
        "csv-bad;10.77.0.12:9100;Linux Server;;;\n" \
        "csv-twin;10.77.0.11;Linux Server;;;\n" \
        "csv-odd;10.77.0.13;Toaster;;;\n" \
        "csv-orphan;10.77.0.14;Linux Server;;;nowhere\n" \
        ";;;;;\n"
    before = db.query(Asset).count()
    result = preview(db, text)
    assert db.query(Asset).count() == before
    db.close()
    actions = {r["hostname"]: (r["action"], r["reason"]) for r in result["rows"]}
    assert actions["csv-sw-core"][0] == "create" and result["rows"][0]["type"] == "Switch"
    assert actions["csv-web-a"][0] == "create"
    assert actions[demo.hostname][0] == "exists"
    assert actions["csv-bad"][0] == "error" and "no :port" in actions["csv-bad"][1]
    assert actions["csv-twin"] == ("error", "same IP as line 3")
    assert "unknown type" in actions["csv-odd"][1]
    assert "parent 'nowhere'" in actions["csv-orphan"][1]
    assert result["counts"] == {"create": 2, "exists": 1, "error": 4}


def test_unreadable_files_are_refused():
    from app.asset_import import preview

    db = SessionLocal()
    for text, words in (("", "empty"), ("ip,type\n10.0.0.1,Switch\n", "hostname"), ("hostname\n" + "x\n" * 2001, "2000")):
        try:
            preview(db, text)
        except ValueError as exc:
            assert words in str(exc)
        else:
            raise AssertionError(text[:20])
    db.close()


def test_import_job_adds_rows_with_parent_and_engineer_only(monkeypatch):
    from app.jobs import run_pending_jobs

    client = _client("engineer")
    text = "hostname,ip,type,site,rack,parent,notes\n" \
        "csv-core-b,10.78.0.1,Router,DC2,B01,,\n" \
        "csv-app-b,10.78.0.21,Other,DC2,B04,csv-core-b,imported\n"
    shown = client.post("/api/v1/assets/import/preview", json={"csv": text}).json()
    assert shown["counts"]["create"] == 2 and "args" not in shown["rows"][0]
    started = client.post("/api/v1/assets/import", json={"csv": text}).json()
    assert started["counts"]["create"] == 2
    db = SessionLocal()
    run_pending_jobs(db, limit=10)
    job = db.get(Job, started["job"])
    assert job.status == "done" and "rows" not in job.payload
    assert job.payload["result"]["created"] == ["csv-core-b", "csv-app-b"] and job.payload["result"]["failed"] == []
    app_b = db.query(Asset).filter_by(asset_id="csv-app-b").one()
    core = db.query(Asset).filter_by(asset_id="csv-core-b").one()
    assert app_b.parent_id == core.id and app_b.extras.get("rack") == "B04" and app_b.notes == "imported"
    db.close()
    polled = client.get(f"/api/v1/onboard/jobs/{started['job']}").json()
    assert polled["result"]["created"] == ["csv-core-b", "csv-app-b"]
    again = client.post("/api/v1/assets/import", json={"csv": text})
    assert again.status_code == 400 and "Nothing to add" in again.json()["detail"]
    assert client.get("/api/v1/assets/import/template").text.startswith("hostname,ip,type,")
    analyst = _client("analyst")
    assert analyst.post("/api/v1/assets/import/preview", json={"csv": text}).status_code == 403
    assert analyst.post("/api/v1/assets/import", json={"csv": text}).status_code == 403
    _drop("csv-app-b", "csv-core-b")
