"""Info tips are gone. Operator pages keep titles only."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.seed import seed

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"


def test_no_template_includes_info_tip():
    css = (ROOT / "frontend" / "static" / "app.css").read_text(encoding="utf-8")
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v09-56" in base
    assert "font-size: 0.82rem" in css.split("html, body {", 1)[1].split("}", 1)[0]
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert 'include "_info_tip.html"' not in text, path.name
        assert 'class="info-tip"' not in text, path.name
        assert 'class="page-lede"' not in text, path.name


def test_operator_pages_render_without_info_tips():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    db.close()
    client = TestClient(app)
    posted = client.post(
        "/login",
        data={"email": "admin@forgesre.local", "password": "testpass"},
        follow_redirects=False,
    )
    assert posted.status_code in {302, 303}
    for path in ("/", "/assets", "/discovery", "/playrules", "/playbooks", "/escalation", "/health-ui", "/ops"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert 'class="info-tip"' not in page.text, path
        assert 'class="page-lede"' not in page.text, path
    home = client.get("/")
    home_h1 = home.text.split("<h1>", 1)[1].split("</h1>", 1)[0]
    assert home_h1.strip() == "Dashboard"
    assert client.get("/playbooks").text.split("<h1>", 1)[1].split("</h1>", 1)[0].strip() == "Playbooks"
    assert client.get("/playrules").text.split("<h1>", 1)[1].split("</h1>", 1)[0].strip() == "Rules"
    assert "Full stack diagnostics live on System Health" not in home.text
