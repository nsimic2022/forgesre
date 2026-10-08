"""Startup seed journal line names only the DEMO rows that are really there."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.migrate import migrate
from app.models import Asset, AuditLog
from app.seed import DEMO_ASSET, DEMO_SW_ASSET, DEMO_WIN_ASSET, seed, seed_summary


def _session(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    Base.metadata.create_all(bind=eng)
    migrate(eng)
    return eng, sessionmaker(bind=eng)()


def _remove(db, asset_id: str) -> None:
    db.query(Asset).filter_by(asset_id=asset_id).delete(synchronize_session=False)
    db.add(AuditLog(actor="admin", action="asset.delete", object_type="asset", object_id=asset_id))
    db.commit()


def test_seed_summary_full_lab(tmp_path):
    eng, db = _session(tmp_path)
    seed(db)
    assert seed_summary(db) == "DEMO assets (Linux, Windows, network), playrules, and closed HighCPU history are ready"
    db.close()
    eng.dispose()


def test_seed_summary_after_operator_removed_demo_rows(tmp_path):
    eng, db = _session(tmp_path)
    seed(db)
    _remove(db, DEMO_WIN_ASSET)
    seed(db)
    assert seed_summary(db) == (
        "DEMO assets (Linux, network), playrules, and closed HighCPU history are ready; "
        f"not re-seeded (removed by an operator): {DEMO_WIN_ASSET}"
    )
    _remove(db, DEMO_ASSET)
    _remove(db, DEMO_SW_ASSET)
    seed(db)
    summary = seed_summary(db)
    assert summary.startswith("Playrules are ready; not re-seeded")
    assert "DEMO assets" not in summary and "HighCPU history" not in summary
    db.close()
    eng.dispose()
