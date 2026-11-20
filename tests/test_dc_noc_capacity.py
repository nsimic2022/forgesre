"""30-day capacity: the query window is 30 days, and one sample is not a forecast."""

from __future__ import annotations



from app.db import Base, SessionLocal, engine
from app.dc_noc import forecast_days_to_full
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def test_forecast_needs_two_climbing_points():
    assert forecast_days_to_full([1.0], [40.0]) is None
    assert forecast_days_to_full([0.0, 86400.0], [40.0, 40.0]) is None
    assert forecast_days_to_full([0.0, 30 * 86400.0], [50.0, 80.0]) == 20


