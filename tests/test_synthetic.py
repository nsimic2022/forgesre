"""Synthetic checks: validation, HTTP SD, API roles, Prometheus job, rules, compose service."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.cli_ops import format_synthetic
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import SyntheticCheck
from app.synthetic import clean_target, overview, sd_targets
from test_incident_tools import _client

ROOT = Path(__file__).resolve().parents[1]
TOKEN = {"Authorization": "Bearer forgesre-dev-webhook-token"}


@pytest.fixture(autouse=True)
def _cleanup():
    Base.metadata.create_all(bind=engine)
    yield
    db = SessionLocal()
    db.query(SyntheticCheck).filter(SyntheticCheck.name.like("syn-%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_targets_are_validated_per_kind():
    assert clean_target("http", "https://erp.dc.local/health") == "https://erp.dc.local/health"
    assert clean_target("tcp", "db01:5432") == "db01:5432"
    assert clean_target("dns", "10.0.0.53") == "10.0.0.53:53"
    assert clean_target("icmp", "10.0.0.1") == "10.0.0.1"
    for kind, bad in (("http", "erp.dc.local"), ("tcp", "db01"), ("icmp", "a b"), ("smtp", "x")):
        with pytest.raises(ValueError):
            clean_target(kind, bad)


def test_engineer_adds_checks_and_prometheus_sees_them():
    eng = _client("engineer")
    created = eng.post("/api/v1/synthetic", json={"name": "syn-erp", "kind": "http", "target": "https://erp.dc.local/", "asset": "erp-01"})
    assert created.status_code == 200, created.text
    assert eng.post("/api/v1/synthetic", json={"name": "syn-erp", "kind": "tcp", "target": "db:5432"}).status_code == 400
    assert eng.post("/api/v1/synthetic", json={"name": "syn-bad", "kind": "tcp", "target": "db"}).status_code == 400
    eng.post("/api/v1/synthetic", json={"name": "syn-dns", "kind": "dns", "target": "10.0.0.53", "severity": "warning"})
    assert _client("analyst").post("/api/v1/synthetic", json={"name": "syn-x", "kind": "icmp", "target": "10.0.0.1"}).status_code == 403
    assert TestClient(app).get("/api/v1/sd/synthetic").status_code == 401
    sd = {row["labels"]["check"]: row for row in TestClient(app).get("/api/v1/sd/synthetic", headers=TOKEN).json()}
    assert sd["syn-erp"]["labels"]["module"] == "http_2xx" and sd["syn-erp"]["labels"]["asset"] == "erp-01"
    assert sd["syn-dns"]["targets"] == ["10.0.0.53:53"] and sd["syn-dns"]["labels"]["check_severity"] == "warning"
    check_id = created.json()["id"]
    changed = eng.put(f"/api/v1/synthetic/{check_id}", json={"name": "syn-erp", "kind": "http", "target": "https://erp.dc.local/", "enabled": False})
    assert changed.json()["enabled"] is False
    db = SessionLocal()
    assert "syn-erp" not in {r["labels"]["check"] for r in sd_targets(db)}
    db.close()
    assert eng.delete(f"/api/v1/synthetic/{check_id}").status_code == 200
    listed = _client("viewer").get("/api/v1/synthetic").json()
    assert listed["can_edit"] is False and [c["name"] for c in listed["checks"] if c["name"].startswith("syn-")] == ["syn-dns"]


def test_overview_reads_probe_results():
    db = SessionLocal()
    db.add(SyntheticCheck(name="syn-web", kind="http", target="https://w/"))
    db.commit()

    def fake(expr: str) -> dict:
        value = 0.0 if expr.startswith("probe_success") else 0.42
        return {"series": [{"labels": {"check": "syn-web"}, "value": value}]}

    data = overview(db, vector_fn=fake)
    row = next(c for c in data["checks"] if c["name"] == "syn-web")
    assert row["up"] == 0.0 and row["seconds"] == 0.42
    assert "FAIL" in format_synthetic(data)
    db.close()


def test_blackbox_service_job_and_rules_are_bundled():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    svc = compose["services"]["blackbox-exporter"]
    assert svc["network_mode"] == "host" and "NET_RAW" in svc["cap_add"]
    modules = yaml.safe_load((ROOT / "monitoring" / "blackbox.yml").read_text(encoding="utf-8"))["modules"]
    from app.synthetic import MODULES

    assert set(MODULES.values()) <= set(modules)
    for name in ("prometheus.yml", "prometheus.yml.tpl"):
        text = (ROOT / "monitoring" / name).read_text(encoding="utf-8")
        assert "job_name: forgesre-synthetic" in text and "/api/v1/sd/synthetic" in text and "127.0.0.1:9115" in text
    groups = {g["name"]: g for g in yaml.safe_load((ROOT / "monitoring" / "alerts.yml").read_text(encoding="utf-8"))["groups"]}
    rules = {r["alert"]: r for r in groups["forgesre-synthetic"]["rules"]}
    assert rules["SyntheticCheckFailed"]["labels"]["severity"] == "critical"
    assert rules["SyntheticCheckDegraded"]["labels"]["severity"] == "warning"
    for script in ("update.sh", "restart.sh"):
        assert "blackbox-exporter" in (ROOT / "scripts" / script).read_text(encoding="utf-8")


def test_doctor_row():
    from app.api import _blackbox_check

    assert _blackbox_check()["status"] in {"disabled", "ok", "error"}
