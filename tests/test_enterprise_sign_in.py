"""LDAP / Active Directory and OIDC single sign-on: roles from groups, local accounts untouched."""

from __future__ import annotations

import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ldap3 import MOCK_SYNC, OFFLINE_AD_2012_R2, Connection, Server

from app.web import _home as web_home
from app import ldap_auth, oidc_auth
from app.db import Base, SessionLocal, engine
from app.main import create_app
from app.models import User
from app.security import authenticate
from app.seed import seed

GROUPS = {"admin": "CN=NOC-Admins,OU=Groups,DC=dc,DC=local", "engineer": "CN=NOC-Engineers,OU=Groups,DC=dc,DC=local",
          "analyst": "CN=NOC-Operators,OU=Groups,DC=dc,DC=local", "viewer": ""}


@pytest.fixture
def directory(monkeypatch):
    server = Server("fake_dc", get_info=OFFLINE_AD_2012_R2)
    tag = uuid4().hex[:6]
    seed_conn = Connection(server, user="CN=svc,DC=dc,DC=local", password="svcpw", client_strategy=MOCK_SYNC)
    seed_conn.strategy.add_entry("CN=svc,DC=dc,DC=local", {"userPassword": "svcpw", "sn": "svc"})
    seed_conn.strategy.add_entry(f"CN=Ana {tag},OU=Staff,DC=dc,DC=local", {
        "userPassword": "anapw", "sn": "Ana", "sAMAccountName": f"ana{tag}", "mail": f"ana.{tag}@dc.local",
        "displayName": f"Ana {tag}", "memberOf": [GROUPS["engineer"], "CN=Everyone,DC=dc,DC=local"], "objectClass": "person"})
    seed_conn.strategy.add_entry(f"CN=Bob {tag},OU=Staff,DC=dc,DC=local", {
        "userPassword": "bobpw", "sn": "Bob", "sAMAccountName": f"bob{tag}", "mail": f"bob.{tag}@dc.local",
        "memberOf": ["CN=Everyone,DC=dc,DC=local"], "objectClass": "person"})

    def connection(cfg, user=None, password=None):
        # every connection sees the same in-memory directory
        conn = Connection(server, user=user, password=password, client_strategy=MOCK_SYNC)
        conn.strategy.entries = seed_conn.strategy.entries
        return conn

    cfg = {"enabled": True, "url": "ldaps://fake_dc", "bind_dn": "CN=svc,DC=dc,DC=local", "bind_password": "svcpw",
           "base_dn": "OU=Staff,DC=dc,DC=local", "user_filter": "(|(mail={login})(sAMAccountName={login}))",
           "groups": GROUPS, "default_role": "", "timeout": 2.0}
    monkeypatch.setattr(ldap_auth, "_connection", connection)
    monkeypatch.setattr("app.settings.Settings.ldap", property(lambda self: cfg))
    return tag, cfg


def test_role_from_groups_takes_the_highest():
    assert ldap_auth.role_from_groups([GROUPS["analyst"].lower(), GROUPS["admin"]], GROUPS) == "admin"
    assert ldap_auth.role_from_groups(["CN=Other"], GROUPS) == ""
    assert ldap_auth.role_from_groups(["CN=Other"], GROUPS, "viewer") == "viewer"


def test_ldap_sign_in_creates_the_user_with_the_mapped_role(directory):
    tag, cfg = directory
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    user, wait = authenticate(db, f"ana{tag}", "anapw", "10.0.0.1")
    assert wait == 0 and user is not None and user.email == f"ana.{tag}@dc.local" and user.role == "engineer"
    assert user.password_hash == "!ldap"
    assert authenticate(db, f"ana{tag}", "wrong", "10.0.0.1")[0] is None
    assert authenticate(db, f"bob{tag}", "bobpw", "10.0.0.1")[0] is None  # in no mapped group
    cfg["default_role"] = "viewer"
    assert authenticate(db, f"bob{tag}", "bobpw", "10.0.0.1")[0].role == "viewer"
    # the installer's local admin never goes to the directory
    assert authenticate(db, "admin@forgesre.local", "testpass", "10.0.0.1")[0] is not None
    db.close()


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def test_oidc_code_flow_signs_in_with_the_group_role(monkeypatch):
    key, public = _keypair()
    tag = uuid4().hex[:6]
    cfg = {"enabled": True, "issuer": "https://idp.test/realms/dc", "client_id": "forgesre", "client_secret": "s3cret",
           "scopes": "openid email profile groups", "groups_claim": "groups",
           "groups": {"admin": [], "engineer": [], "analyst": ["noc-operators"], "viewer": []},
           "default_role": "", "button_label": "Sign in with Keycloak"}
    monkeypatch.setattr("app.settings.Settings.oidc", property(lambda self: cfg))
    monkeypatch.setattr("app.ack_links.public_url", lambda: "https://forgesre.dc.local")
    meta = {"issuer": cfg["issuer"], "authorization_endpoint": "https://idp.test/auth", "token_endpoint": "https://idp.test/token",
            "jwks_uri": "https://idp.test/certs"}
    oidc_auth._meta_cache.clear()
    monkeypatch.setattr(oidc_auth.httpx, "get", lambda url, timeout: SimpleNamespace(raise_for_status=lambda: None, json=lambda: meta))
    nonce = {}

    def post(url, timeout, data):
        claims = {"iss": cfg["issuer"], "aud": "forgesre", "exp": int(time.time()) + 300, "iat": int(time.time()),
                  "email": f"mila.{tag}@dc.local", "name": "Mila", "groups": ["noc-operators"], "nonce": nonce["n"]}
        assert data["code_verifier"] and data["client_secret"] == "s3cret"
        return SimpleNamespace(status_code=200, json=lambda: {"id_token": jwt.encode(claims, key, algorithm="RS256")})

    monkeypatch.setattr(oidc_auth.httpx, "post", post)
    monkeypatch.setattr(jwt, "PyJWKClient", lambda uri, timeout=5: SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=public)))
    Base.metadata.create_all(bind=engine)
    client = TestClient(create_app())
    assert "Sign in with Keycloak" in client.get("/login").text
    start = client.get("/auth/oidc/login", follow_redirects=False)
    params = parse_qs(urlsplit(start.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"] and params["redirect_uri"] == ["https://forgesre.dc.local/auth/oidc/callback"]
    nonce["n"] = params["nonce"][0]
    bad = client.get("/auth/oidc/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert "sso_error" in bad.headers["location"] and "state" in bad.headers["location"]
    done = client.get("/auth/oidc/callback", params={"code": "c", "state": params["state"][0]}, follow_redirects=False)
    assert done.headers["location"] == web_home() and "forgesre_session" in done.headers.get("set-cookie", "")
    db = SessionLocal()
    user = db.query(User).filter_by(email=f"mila.{tag}@dc.local").one()
    assert user.role == "analyst" and user.password_hash == "!oidc"
    db.close()


def test_oidc_without_settings_says_what_is_missing(monkeypatch):
    monkeypatch.setattr("app.ack_links.public_url", lambda: "")
    why = oidc_auth.ready({"enabled": True, "issuer": "", "client_id": "x", "client_secret": ""})
    assert "issuer" in why and "client secret" in why and "public address" in why
    client = TestClient(create_app())
    r = client.get("/auth/oidc/login", follow_redirects=False)
    assert "sso_error" in r.headers["location"]
