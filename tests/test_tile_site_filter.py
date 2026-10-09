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


def test_in_problem_tile_follows_a_gui_resolve():
    db = _db()
    client = _client()
    asset = _asset(db)
    calm = _tile(db, "In problem")
    incident = _fire(db, asset)
    db.expire_all()
    assert db.get(Asset, asset.id).status == "critical"
    assert _tile(db, "In problem") == calm + 1
    fired = client.get(f"/assets?flag=in-problem&q={asset.asset_id}").text
    assert asset.asset_id in fired.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    posted = client.post(f"/incidents/{incident.number}/status", data={"status": "RESOLVED"}, follow_redirects=False)
    assert posted.status_code == 302
    db.expire_all()
    assert db.get(Asset, asset.id).status == "healthy"
    assert _tile(db, "In problem") == calm
    cleared = client.get(f"/assets?flag=in-problem&q={asset.asset_id}").text
    assert asset.asset_id not in cleared.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    home = client.get("/").text
    infra = home.split("dash-tiles-infra", 1)[1].split("</section>", 1)[0]
    assert ">In problem<" in infra
    assert ">Assets<" in infra and ">Unreachable<" in infra
    assert ">Assets without incident<" not in home
    assert ">No open incident<" not in home
    assert "No owner email" not in infra
    db.close()


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


def test_incidents_filter_by_site_and_customer_from_asset_extras():
    db = _db()
    client = _client()
    token = uuid4().hex[:6]
    north = _asset(db, site=f"DC-North-{token}", customer=f"Acme-{token}")
    south = _asset(db, site=f"DC-South-{token}", customer=f"Acme-{token}")
    in_north = _fire(db, north)
    in_south = _fire(db, south)

    page = client.get("/incidents").text
    assert 'data-incident-filter="site"' in page and f'value="DC-North-{token}"' in page
    assert 'data-incident-filter="customer"' in page and f'value="Acme-{token}"' in page

    by_site = client.get(f"/incidents?site=dc-north-{token}").text
    assert in_north.number in by_site and in_south.number not in by_site
    assert f"site=dc-north-{token}" in by_site, "row links keep the filter for older / newer"

    by_customer = client.get(f"/incidents?customer=Acme-{token}").text
    assert in_north.number in by_customer and in_south.number in by_customer

    nowhere = client.get(f"/incidents?site=nowhere-{token}").text
    assert "No incidents match this filter." in nowhere
    db.close()
