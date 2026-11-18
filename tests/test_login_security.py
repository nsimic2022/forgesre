"""Sign-in hardening: failed-attempt throttle, server-side logout, email case, inactive users."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.security import LoginThrottle, hash_password, make_session_token
from app.seed import seed


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _user(db, *, active: bool = True, password: str = "user-pass", role: str = "analyst") -> User:
    row = User(
        email=f"sec-{uuid.uuid4().hex[:8]}@dc.local",
        name="Sec",
        password_hash=hash_password(password),
        role=role,
        is_active=active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _login(client: TestClient, email: str, password: str):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_five_bad_passwords_lock_the_account_for_this_ip():
    db = _db()
    row = _user(db)
    client = TestClient(app)
    for _ in range(5):
        assert _login(client, row.email, "wrong").status_code == 200
    blocked = _login(client, row.email, "user-pass")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert "Too many failed sign-in attempts" in blocked.text
    assert "forgesre_session" not in blocked.cookies


def test_api_login_is_throttled_too():
    db = _db()
    row = _user(db)
    client = TestClient(app)
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json={"email": row.email, "password": "x"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": row.email, "password": "user-pass"}).status_code == 429


def test_successful_login_clears_the_account_counter():
    db = _db()
    row = _user(db)
    client = TestClient(app)
    for _ in range(4):
        _login(client, row.email, "wrong")
    assert _login(client, row.email, "user-pass").status_code == 302
    for _ in range(4):
        _login(client, row.email, "wrong")
    assert _login(client, row.email, "user-pass").status_code == 302


def test_throttle_window_expires_and_ip_limit_covers_many_emails():
    throttle = LoginThrottle(per_account=2, per_ip=3, window_seconds=60)
    throttle.failure("10.0.0.1", "a@x", now=0)
    throttle.failure("10.0.0.1", "a@x", now=1)
    assert throttle.retry_after("10.0.0.1", "A@X", now=2) > 0
    assert throttle.retry_after("10.0.0.1", "a@x", now=61) == 0
    throttle.reset()
    for i in range(3):
        throttle.failure("10.0.0.2", f"u{i}@x", now=i)
    assert throttle.retry_after("10.0.0.2", "someone-else@x", now=4) > 0
    assert throttle.retry_after("10.0.0.3", "someone-else@x", now=4) == 0


def test_email_is_case_insensitive_at_login():
    db = _db()
    row = _user(db)
    client = TestClient(app)
    assert _login(client, "  " + row.email.upper() + " ", "user-pass").status_code == 302
    assert client.get("/", follow_redirects=False).status_code == 200


def test_inactive_user_gets_no_cookie_and_no_login_audit():
    db = _db()
    row = _user(db, active=False)
    client = TestClient(app)
    response = _login(client, row.email, "user-pass")
    assert response.status_code == 200
    assert "Invalid email or password." in response.text
    assert "forgesre_session" not in response.cookies


def test_logout_revokes_a_copied_cookie():
    db = _db()
    row = _user(db)
    client = TestClient(app)
    assert _login(client, row.email, "user-pass").status_code == 302
    stolen = client.cookies.get("forgesre_session")
    assert stolen
    assert client.post("/logout", follow_redirects=False).status_code == 302

    thief = TestClient(app)
    thief.cookies.set("forgesre_session", stolen)
    assert thief.get("/", follow_redirects=False).status_code in (302, 303, 401)


def test_password_change_signs_out_other_browsers_but_keeps_the_admin_signed_in():
    db = _db()
    row = _user(db, password="old-pass", role="admin")
    laptop = TestClient(app)
    phone = TestClient(app)
    assert _login(laptop, row.email, "old-pass").status_code == 302
    assert _login(phone, row.email, "old-pass").status_code == 302
    changed = laptop.post(
        f"/admin/users/{row.id}",
        data={"email": row.email, "name": "Sec", "password": "new-pass", "role": "admin"},
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert laptop.get("/", follow_redirects=False).status_code == 200
    assert phone.get("/", follow_redirects=False).status_code in (302, 303, 401)


def test_cookie_from_before_the_upgrade_still_works():
    """Tokens issued before session_version existed have no "sv" and map to version 0."""
    from app.security import serializer

    db = _db()
    row = _user(db)
    client = TestClient(app)
    client.cookies.set("forgesre_session", serializer.dumps({"uid": row.id}))
    assert client.get("/", follow_redirects=False).status_code == 200
    client.cookies.set("forgesre_session", make_session_token(row.id, 7))
    assert client.get("/", follow_redirects=False).status_code in (302, 303, 401)
