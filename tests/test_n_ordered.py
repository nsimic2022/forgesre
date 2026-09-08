"""N's ordered V0.7 GUI/platform simplifications."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.security import hash_password
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(email: str, password: str = "testpass") -> TestClient:
    client = TestClient(app)
    posted = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert posted.status_code in {302, 303}
    return client


def _add_user(db, email: str, role: str) -> None:
    if db.query(User).filter_by(email=email).first() is None:
        db.add(
            User(
                email=email,
                name=role,
                password_hash=hash_password("testpass"),
                role=role,
            )
        )
        db.commit()


def test_demo_html_and_api_require_admin_not_login_only():
    db = _db()
    _add_user(db, "eng-demo@forgesre.local", "engineer")
    _add_user(db, "analyst-demo@forgesre.local", "analyst")
    db.close()
    routes = [
        "/demo",
        "/demo-rca",
        "/demo-host",
        "/demo-windows",
        "/demo-network",
        "/demo-nodecpu",
        "/demo-reset",
    ]
    for email in ("eng-demo@forgesre.local", "analyst-demo@forgesre.local"):
        client = _login(email)
        for path in routes:
            posted = client.post(path, follow_redirects=False)
            assert posted.status_code == 403, path
            api = client.post(f"/api/v1{path}", follow_redirects=False)
            assert api.status_code == 403, f"/api/v1{path}"
    admin = _login("admin@forgesre.local")
    reset = admin.post("/demo-reset", follow_redirects=False)
    assert reset.status_code == 303


def test_assets_copy_separates_verify_doctor_and_test():
    db = _db()
    db.close()
    page = _login("admin@forgesre.local").get("/assets")
    assert page.status_code == 200
    text = page.text.lower()
    assert "verify" in text
    assert "./forgesre doctor" in page.text or "system health" in text
    assert "./forgesre test" in page.text


def test_grafana_only_on_health_ui_not_nav_or_incidents():
    db = _db()
    db.close()
    client = _login("admin@forgesre.local")
    home = client.get("/")
    nav = home.text.split("<aside", 1)[1].split("</aside>", 1)[0]
    assert "Grafana" not in nav
    health = client.get("/health-ui")
    assert health.status_code == 200
    assert "Open Grafana" in health.text
    assert "Prometheus" in health.text and "Alertmanager" in health.text and "Core" in health.text
    assert "one worker thread" in health.text.lower()
    assert "Celery" in health.text


def test_discovery_demo_ip_is_labeled_and_netbox_is_separate():
    db = _db()
    db.close()
    client = _login("admin@forgesre.local")
    page = client.get("/discovery")
    assert page.status_code == 200
    html = page.text
    n = 1
    while "10.20.30.41" not in html and n < 8:
        n += 1
        html = client.get(f"/discovery?page={n}").text
    assert "10.20.30.41" in html
    assert 'class="pill demo"' in html
    assert "lab seed" in html.lower()
    assert "not nmap" in page.text.lower()
    assert page.text.find("Scan now") < page.text.find("NetBox sync")
    assert "read-only" in page.text.lower()
    assert "UDP/161" in page.text
    assert "TCP/161 is skipped" in page.text
    assert "ports 22, 80, 443, 161" not in page.text


def test_discovery_template_does_not_list_tcp_161_as_a_probe_port():
    html = (ROOT / "frontend" / "templates" / "discovery.html").read_text(encoding="utf-8")
    assert "UDP/161" in html
    assert "TCP/161 is skipped" in html
    assert "ports 22, 80, 443, 161" not in html


def test_readme_does_not_claim_dashboard_doctor_or_incidents_200():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "doctor lights" not in text
    assert "recent 200" not in text
    assert "10 per page" in text
    assert "System Health" in text


def test_architecture_doc_is_proposal_not_runtime():
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "Not the V0.7 appliance runtime" in text
    handbook = (ROOT / "docs" / "operator-handbook.md").read_text(encoding="utf-8")
    assert "map `alertname`" in handbook or "maps `alertname`" in handbook
    play = (ROOT / "frontend" / "templates" / "playrules.html").read_text(encoding="utf-8")
    assert 'include "_info_tip.html"' in play
    assert "do not create Prom rules" in play
    assert "alerts.yml" in play
    assert "not a second alerting engine" in play
