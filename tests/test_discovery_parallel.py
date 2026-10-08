"""Discovery scan probes hosts in a bounded pool; candidate writes stay on the scan thread."""

from __future__ import annotations

import random
import threading
import time

import app.inventory as inventory
from app.db import Base, SessionLocal, engine
from app.inventory import DISCOVERY_PROBE_WORKERS, run_scan
from app.migrate import migrate
from app.models import DiscoveryCandidate
from app.seed import seed

PROBE_DELAY = 0.3


def test_run_scan_probes_in_parallel_and_still_writes_candidates(monkeypatch):
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    prefix = f"10.{random.randint(150, 250)}.{random.randint(1, 250)}"
    cidr = f"{prefix}.0/26"
    lock = threading.Lock()
    state = {"live": 0, "peak": 0, "calls": 0}
    writer_threads: set[int] = set()

    def slow_probe(ip, *args, **kwargs):
        with lock:
            state["live"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["live"])
        try:
            time.sleep(PROBE_DELAY)
        finally:
            with lock:
                state["live"] -= 1
        last = int(ip.rsplit(".", 1)[1])
        if last == 13:
            raise OSError("probe blew up")
        alive = last % 10 == 0
        return {
            "ip": ip,
            "open_ports": [22, 9100] if alive else [],
            "snmp_ok": False,
            "proposed_role": "Possible Linux server",
            "alive": alive,
            "exporter_kind": "linux" if alive else "",
            "detect_message": "",
        }

    real_upsert = inventory.upsert_candidate

    def recording_upsert(*args, **kwargs):
        writer_threads.add(threading.get_ident())
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr("discovery.probe_host", slow_probe)
    monkeypatch.setattr(inventory, "upsert_candidate", recording_upsert)

    started = time.monotonic()
    result = run_scan(db, cidrs=[cidr])
    elapsed = time.monotonic() - started

    assert state["calls"] == 62
    assert 1 < state["peak"] <= DISCOVERY_PROBE_WORKERS
    assert elapsed < 62 * PROBE_DELAY / 4, elapsed
    expected = {f"{prefix}.{n}" for n in range(10, 63, 10)}
    assert result["found"] == len(expected)
    rows = db.query(DiscoveryCandidate).filter(DiscoveryCandidate.ip.like(f"{prefix}.%")).all()
    assert {row.ip for row in rows} == expected
    assert all(row.node_exporter for row in rows)
    assert writer_threads == {threading.get_ident()}
    db.query(DiscoveryCandidate).filter(DiscoveryCandidate.ip.like(f"{prefix}.%")).delete(synchronize_session=False)
    db.commit()
    db.close()
