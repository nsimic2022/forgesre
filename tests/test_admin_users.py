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


def test_last_admin_cannot_be_demoted_or_removed():
    from app.users import delete_blocked, is_last_admin, update_user

    db = _db()
    for row in db.query(User).filter(User.role.in_(["admin", "super_admin"])).all():
        if row.email != "admin@forgesre.local":
            row.role = "viewer"
    db.commit()
    admin = db.query(User).filter_by(email="admin@forgesre.local").one()
    other = User(email="ops-last@dc.local", name="Ops", password_hash="x", role="admin")
    db.add(other)
    db.commit()
    assert not is_last_admin(db, admin)
    assert delete_blocked(admin, other, db) == ""
    other.role = "engineer"
    db.commit()
    assert is_last_admin(db, admin)
    try:
        update_user(db, admin, admin, role="engineer")
    except ValueError as exc:
        assert "last admin" in str(exc)
    else:
        raise AssertionError("demoting the last admin must fail")
    db.delete(other)
    db.commit()
    db.close()


def test_migrate_merges_super_admin_into_admin():
    from sqlalchemy import text

    from app.migrate import migrate

    db = _db()
    db.add(User(email="legacy-root@dc.local", name="Super Admin", password_hash="x", role="super_admin"))
    db.add(User(email="legacy-sys@dc.local", name="Sys", password_hash="x", role="system_admin"))
    db.commit()
    migrate(engine)
    db.expire_all()
    rows = {u.email: u for u in db.query(User).filter(User.email.like("legacy-%")).all()}
    assert rows["legacy-root@dc.local"].role == "admin"
    assert rows["legacy-root@dc.local"].name == "Admin"
    assert rows["legacy-sys@dc.local"].role == "admin"
    assert db.execute(text("SELECT COUNT(*) FROM users WHERE role IN ('super_admin', 'system_admin')")).scalar() == 0
    for row in rows.values():
        db.delete(row)
    db.commit()
    db.close()


def _create(client: TestClient, **fields) -> int:
    made = client.post("/api/v1/users", json=fields)
    assert made.status_code == 200, made.text
    return made.json()["id"]


def test_api_create_hashes_password_and_keeps_phones():
    db = _db()
    client = TestClient(app)
    _login(client)
    user_id = _create(
        client, email="noc@dc.local", email2="noc-alt@dc.local", name="Noc", surname="One",
        password="noc-pass", role="analyst", phone="+381-11-555-0100", phone2="+381-11-555-0101",
    )
    row = db.get(User, user_id)
    assert row.password_hash.startswith("$2") and verify_password("noc-pass", row.password_hash)
    assert (row.surname, row.email2, row.phone, row.phone2) == ("One", "noc-alt@dc.local", "+381-11-555-0100", "+381-11-555-0101")
    listed = client.get("/api/v1/users-page").json()
    assert "noc@dc.local" in str(listed)
    db.close()


def test_api_edit_changes_password_and_blank_keeps_hash():
    db = _db()
    client = TestClient(app)
    _login(client)
    user_id = _create(client, email="eng@dc.local", name="Eng", password="old-pass", role="engineer")
    assert client.post(f"/api/v1/users/{user_id}", json={"name": "Engineer One", "password": "new-pass", "role": "analyst"}).status_code == 200
    row = db.get(User, user_id)
    db.refresh(row)
    assert (row.name, row.role) == ("Engineer One", "analyst")
    assert verify_password("new-pass", row.password_hash)
    old = row.password_hash
    assert client.post(f"/api/v1/users/{user_id}", json={"password": "", "phone": "+381-11-555-0200"}).status_code == 200
    db.refresh(row)
    assert row.password_hash == old and row.phone == "+381-11-555-0200"
    db.close()


def test_api_cannot_remove_self_and_rejects_duplicate_email():
    db = _db()
    client = TestClient(app)
    _login(client)
    admin = db.query(User).filter_by(email="admin@forgesre.local").one()
    denied = client.post(f"/api/v1/users/{admin.id}/delete")
    assert denied.status_code == 400
    assert db.get(User, admin.id) is not None
    _create(client, email="dup@dc.local", name="A", password="x", role="viewer")
    assert client.post("/api/v1/users", json={"email": "DUP@dc.local", "name": "B", "password": "y", "role": "viewer"}).status_code == 400
    db.close()
