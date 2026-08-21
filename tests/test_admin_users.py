from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.security import verify_password
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(client: TestClient, email: str = "admin@forgesre.local", password: str = "testpass") -> None:
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_admin_lists_users_and_selects_one():
    db = _db()
    client = TestClient(app)
    _login(client)
    created = client.post(
        "/admin/users",
        data={"email": "ops@dc.local", "name": "Ops", "password": "ops-pass", "role": "analyst"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    row = db.query(User).filter_by(email="ops@dc.local").one()
    assert row.password_hash.startswith("$2")
    assert row.password_hash != "ops-pass"
    assert verify_password("ops-pass", row.password_hash)
    page = client.get(f"/admin?selected={row.id}")
    assert page.status_code == 200
    assert "Edit user" in page.text
    assert "Remove user" in page.text
    assert f'action="/admin/users/{row.id}"' in page.text
    assert "ops@dc.local" in page.text
    db.close()


def test_admin_edit_user_name_role_and_password():
    db = _db()
    client = TestClient(app)
    _login(client)
    client.post(
        "/admin/users",
        data={"email": "eng@dc.local", "name": "Eng", "password": "old-pass", "role": "engineer"},
        follow_redirects=False,
    )
    row = db.query(User).filter_by(email="eng@dc.local").one()
    saved = client.post(
        f"/admin/users/{row.id}",
        data={"email": "eng@dc.local", "name": "Engineer One", "password": "new-pass", "role": "analyst"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    db.refresh(row)
    assert row.name == "Engineer One"
    assert row.role == "analyst"
    assert verify_password("new-pass", row.password_hash)
    db.close()


def test_admin_blank_password_keeps_hash():
    db = _db()
    client = TestClient(app)
    _login(client)
    client.post(
        "/admin/users",
        data={"email": "keep@dc.local", "name": "Keep", "password": "keep-pass", "role": "viewer"},
        follow_redirects=False,
    )
    row = db.query(User).filter_by(email="keep@dc.local").one()
    old = row.password_hash
    client.post(
        f"/admin/users/{row.id}",
        data={"email": "keep@dc.local", "name": "Keep", "password": "", "role": "viewer"},
        follow_redirects=False,
    )
    db.refresh(row)
    assert row.password_hash == old
    db.close()


def test_admin_cannot_remove_self_or_super_admin():
    db = _db()
    client = TestClient(app)
    _login(client)
    admin = db.query(User).filter_by(email="admin@forgesre.local").one()
    denied = client.post(f"/admin/users/{admin.id}/delete", follow_redirects=False)
    assert denied.status_code == 400
    assert db.get(User, admin.id) is not None
    page = client.get(f"/admin?selected={admin.id}")
    assert "cannot be removed" in page.text.lower() or "cannot remove" in page.text.lower()
    db.close()


def test_admin_remove_other_user():
    db = _db()
    client = TestClient(app)
    _login(client)
    client.post(
        "/admin/users",
        data={"email": "gone@dc.local", "name": "Gone", "password": "x", "role": "viewer"},
        follow_redirects=False,
    )
    row = db.query(User).filter_by(email="gone@dc.local").one()
    deleted = client.post(f"/admin/users/{row.id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/admin"
    assert db.query(User).filter_by(email="gone@dc.local").first() is None
    db.close()


def test_duplicate_email_rejected():
    db = _db()
    client = TestClient(app)
    _login(client)
    first = client.post(
        "/admin/users",
        data={"email": "dup@dc.local", "name": "A", "password": "x", "role": "viewer"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    second = client.post(
        "/admin/users",
        data={"email": "DUP@dc.local", "name": "B", "password": "y", "role": "viewer"},
        follow_redirects=False,
    )
    assert second.status_code == 400
    db.close()


def test_api_update_and_delete_user():
    db = _db()
    client = TestClient(app)
    _login(client)
    made = client.post(
        "/api/v1/users",
        json={"email": "api@dc.local", "name": "Api", "password": "p1", "role": "engineer"},
    )
    assert made.status_code == 200
    user_id = made.json()["id"]
    patched = client.post(f"/api/v1/users/{user_id}", json={"name": "Api Two", "role": "analyst"})
    assert patched.status_code == 200
    assert patched.json()["role"] == "analyst"
    row = db.get(User, user_id)
    assert row.name == "Api Two"
    assert verify_password("p1", row.password_hash)
    gone = client.post(f"/api/v1/users/{user_id}/delete")
    assert gone.status_code == 200
    db.expire_all()
    assert db.query(User).filter_by(id=user_id).first() is None
    db.close()
