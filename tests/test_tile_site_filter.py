"""Dashboard asset tile follows a GUI resolve; Incidents filters by the host's Site / Customer extras."""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory import asset_tiles
from app.main import app
from app.migrate import migrate
from app.models import Asset, Incident
from app.seed import seed
from app.services import ingest_alertmanager


def _db():
    Base.metadata.create_all(bind=engine)
    migrate(engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _asset(db, **extras) -> Asset:
    token = uuid4().hex[:6]
    row = Asset(asset_id=f"tile-{token}", hostname=f"tile-{token}", type="Other", scrape_address="", extras=extras)
    db.add(row)
    db.commit()
    return row


def _fire(db, asset: Asset, severity: str = "critical") -> Incident:
    alertname = f"TileProbe{uuid4().hex[:6]}"
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": alertname, "asset": asset.asset_id, "severity": severity},
                "annotations": {"summary": f"{alertname} on {asset.asset_id}"},
            }
        ],
    }
    return ingest_alertmanager(db, payload)[0]


def _tile(db, label: str) -> int:
    db.expire_all()
    return next(tile["count"] for tile in asset_tiles(db.query(Asset).all()) if tile["label"] == label)


def test_api_close_also_refreshes_the_asset():
    db = _db()
    client = _client()
    asset = _asset(db)
    incident = _fire(db, asset, severity="warning")
    db.expire_all()
    assert db.get(Asset, asset.id).status == "warning"
    out = client.post(f"/api/v1/incidents/{incident.number}/status", json={"status": "CLOSED"})
    assert out.status_code == 200
    db.expire_all()
    assert db.get(Asset, asset.id).status == "healthy"
    db.close()


