from investigation import DISCLAIMER, investigate


def test_heuristic_cpu_rca():
    result = investigate(
        {
            "incident": {"title": "High CPU", "asset": "forge-demo-01"},
            "asset": {"hostname": "forge-demo-01"},
            "alert": {"alertname": "HighCPU"},
            "metrics": {"cpu_percent": 94, "disk_percent": 20},
            "logs": ["cpu raised"],
            "history": [],
        }
    )
    assert result["disclaimer"] == DISCLAIMER
    assert result["confidence"] >= 70
    assert "forge-demo-01" in result["summary"]
    assert result["provider"] == "builtin-analyst"


def test_playrule_match_and_incident(monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import Base, engine, SessionLocal
    from app.main import app
    from app.seed import seed
    from app.services import close_open_incidents, ingest_alertmanager, match_playrule

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    rule = match_playrule(db, "HighCPU", {})
    assert rule is not None
    assert rule.name == "high-cpu"
    close_open_incidents(db, "HighCPU:forge-demo-01")

    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighCPU", "severity": "warning", "asset": "forge-demo-01"},
                "annotations": {"summary": "High CPU", "description": "CPU 94%"},
                "fingerprint": "test-highcpu",
            }
        ],
    }
    created = ingest_alertmanager(db, payload)
    assert created
    incident = created[0]
    assert incident.number.startswith("INC-")
    assert incident.playbook is not None
    assert incident.investigations

    client = TestClient(app)
    login = client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    assert login.status_code in {302, 303}
    home = client.get("/")
    assert home.status_code == 200
    assert b"forge-demo-01" in client.get("/assets").content or b"Assets" in client.get("/assets").content

    hook = client.post(
        "/api/v1/webhooks/alertmanager",
        json=payload,
        headers={"Authorization": "Bearer forgesre-dev-webhook-token"},
    )
    assert hook.status_code == 200
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert b"forgesre_demo_cpu_percent" in metrics.content
    db.close()
