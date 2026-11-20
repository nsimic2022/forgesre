"""Verify all: page first, one SD/targets/Alertmanager fetch per request, bounded parallel probes."""

from __future__ import annotations

import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient

import app.api as api_mod
from app.asset_probe import AssetProbe, CheckResult
from app.db import Base, SessionLocal, engine
from app.inventory import create_manual_asset
from app.main import app
from app.seed import seed

PROBE_DELAY = 0.4


class _Calls:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.probes: list[str] = []
        self.targets = 0
        self.am = 0
        self.sd = 0
        self.live = 0
        self.peak = 0

    def bump_live(self, step: int) -> None:
        with self.lock:
            self.live += step
            self.peak = max(self.peak, self.live)


def _patch(monkeypatch) -> _Calls:
    calls = _Calls()
    real_sd = api_mod._sd_membership

    def fake_probe(item, timeout=2.0, **kwargs):
        calls.bump_live(1)
        try:
            time.sleep(PROBE_DELAY)
        finally:
            calls.bump_live(-1)
        with calls.lock:
            calls.probes.append(str(item["asset_id"]))
        return AssetProbe(
            asset_id=item["asset_id"],
            hostname=item.get("hostname") or "",
            ip=item.get("ip") or "",
            kind="linux",
            type=item.get("type") or "",
            scrape=item.get("scrape_address") or "",
            port=9100,
            icmp=CheckResult("icmp", True, "reachable"),
            metrics=CheckResult("metrics", True, "node_exporter :9100/metrics"),
        )

    def fake_targets(url):
        calls.targets += 1
        return {"targets": []}

    def fake_am(url):
        calls.am += 1
        return {"ok": True, "detail": "Alertmanager reachable (/-/ready)"}

    def counted_sd(db):
        calls.sd += 1
        return real_sd(db)

    monkeypatch.setattr(api_mod, "probe_target", fake_probe)
    monkeypatch.setattr(api_mod, "urllib_prom_targets", fake_targets)
    monkeypatch.setattr(api_mod, "urllib_am_health", fake_am)
    monkeypatch.setattr(api_mod, "_sd_membership", counted_sd)
    return calls


def _setup(count: int) -> tuple[TestClient, list[str]]:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    tag = uuid4().hex[:6]
    ids = []
    for n in range(count):
        asset = create_manual_asset(
            db,
            hostname=f"va-{tag}-{n:02d}",
            ip=f"192.0.2.{n + 1}",
            type="Linux Server",
            scrape_address=f"192.0.2.{n + 1}:9100",
            actor="tester",
        )
        ids.append(asset.asset_id)
    db.close()
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client, ids


def test_verify_api_shares_targets_and_alertmanager_across_assets(monkeypatch):
    client, ids = _setup(4)
    calls = _patch(monkeypatch)
    response = client.get("/api/v1/verify", params={"selector": "", "timeout": 1})
    assert response.status_code == 200
    results = response.json()["results"]
    assert {row["asset_id"] for row in results} >= set(ids)
    assert len(calls.probes) == len(results)
    assert calls.targets == 1 and calls.am == 1 and calls.sd == 1


