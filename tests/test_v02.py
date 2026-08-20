from discovery import classify, hosts_from_cidrs

from app.inventory import DEMO_CANDIDATE_IP, approve_candidate, ignore_candidate, sd_targets, seed_demo_candidate
from app.models import Asset, DiscoveryCandidate


def test_classify_roles():
    assert classify([22, 9100]) == "Possible Linux server"
    assert classify([161]) == "Possible network device"
    assert classify([443]) == "Possible web/appliance"
    assert classify([]) == "No open ports"


def test_hosts_from_cidrs_skips_loopback_and_caps():
    assert hosts_from_cidrs(["127.0.0.0/8"]) == []
    assert hosts_from_cidrs(["not-a-cidr", "10.20.30.41/32"]) == ["10.20.30.41"]
    hosts = hosts_from_cidrs(["10.0.0.0/16"])
    assert len(hosts) == 256
    assert "10.0.0.1" in hosts


def test_approve_ignore_and_http_sd():
    from fastapi.testclient import TestClient

    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.seed import seed

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    row = seed_demo_candidate(db)
    assert row.ip == DEMO_CANDIDATE_IP
    assert row.status in {"new", "approved"}

    client = TestClient(app)
    login = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}

    page = client.get("/discovery")
    assert page.status_code == 200
    assert b"Discovery" in page.content
    if row.status == "new":
        assert b"NEW DEVICE DETECTED" in page.content
        assert DEMO_CANDIDATE_IP.encode() in page.content

    denied = client.get("/api/v1/sd/prometheus")
    assert denied.status_code == 401

    db.refresh(row)
    if row.status == "new":
        asset = approve_candidate(db, row, actor="tester@forgesre.local")
    else:
        asset = db.query(Asset).filter_by(ip=DEMO_CANDIDATE_IP).one()
    assert asset.asset_id.startswith("disc-")
    assert asset.ip == DEMO_CANDIDATE_IP
    assert asset.source == "discovery"
    assert asset.scrape_address == f"{DEMO_CANDIDATE_IP}:9100"
    targets = sd_targets(db)
    assert any(DEMO_CANDIDATE_IP in item["targets"][0] for item in targets)
    assert all("127.0.0.1" not in item["targets"][0] for item in targets)

    other = db.query(DiscoveryCandidate).filter_by(ip="10.20.30.99").first()
    if other is None:
        other = DiscoveryCandidate(ip="10.20.30.99", proposed_role="Possible web/appliance", open_ports=[80], status="new")
        db.add(other)
        db.commit()
        db.refresh(other)
    ignore_candidate(db, other, actor="tester@forgesre.local")
    db.refresh(other)
    assert other.status == "ignored"
    assert db.query(Asset).filter_by(ip="10.20.30.99").first() is None

    sd = client.get("/api/v1/sd/prometheus", headers={"Authorization": "Bearer forgesre-dev-webhook-token"})
    assert sd.status_code == 200
    body = sd.json()
    assert any(DEMO_CANDIDATE_IP in target["targets"][0] for target in body)
    db.close()
