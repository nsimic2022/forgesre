"""Web push: RFC 8291 encryption, VAPID signature, subscribe API, queue and delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import web_push
from app.db import Base, SessionLocal, engine
from app.models import Incident, Job, PushSubscription, utcnow
from app.settings import settings
from test_incident_tools import _client


def _receiver():
    key = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    return key, web_push.b64u(web_push._public_bytes(key)), web_push.b64u(auth), auth


def _decrypt(body: bytes, key: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """Receiver side of RFC 8291, written from the RFC to check the sender."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public = body[21:21 + idlen]
    ua_public = web_push._public_bytes(key)
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))

    def hkdf(salt_: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
        prk = hmac.new(salt_, ikm, hashlib.sha256).digest()
        return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:n]

    ikm = hkdf(auth, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    assert rs == 4096
    plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


@pytest.fixture
def push_on(monkeypatch):
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "notifications": {**(settings.yaml.get("notifications") or {}), "push": {"enabled": True}}})
    yield
    db = SessionLocal()
    db.query(PushSubscription).delete()
    db.query(Job).filter(Job.kind == web_push.JOB_KIND).delete()
    for inc in db.query(Incident).filter(Incident.title.like("wp-%")).all():
        db.delete(inc)
    db.commit()
    db.close()


def test_encrypt_round_trip():
    key, p256dh, auth_b64, auth = _receiver()
    body = web_push.encrypt(b'{"title":"CRITICAL INC-1"}', p256dh, auth_b64)
    assert _decrypt(body, key, auth) == b'{"title":"CRITICAL INC-1"}'
    with pytest.raises(ValueError):
        web_push.encrypt(b"x" * 5000, p256dh, auth_b64)


def test_vapid_header_is_a_valid_es256_token(push_on):
    db = SessionLocal()
    header = web_push.vapid_headers(db, "https://fcm.googleapis.com/fcm/send/abc")["Authorization"]
    token = header.split("t=")[1].split(",")[0]
    k = header.split("k=")[1]
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), web_push.b64u_decode(k))
    claims = jwt.decode(token, public, algorithms=["ES256"], audience="https://fcm.googleapis.com")
    assert claims["sub"].startswith(("mailto:", "http"))
    assert web_push.public_key(db) == k  # the key is kept, not made again
    db.close()


def test_api_subscribe_test_and_off(push_on, monkeypatch):
    _key, p256dh, auth_b64, _auth = _receiver()
    client = _client("viewer")
    info = client.get("/api/v1/push").json()
    assert info["enabled"] and len(web_push.b64u_decode(info["public_key"])) == 65
    endpoint = f"https://push.example/{uuid4().hex}"
    assert client.post("/api/v1/push/subscribe", json={"endpoint": "http://x", "keys": {"p256dh": p256dh, "auth": auth_b64}}).status_code == 400
    assert client.post("/api/v1/push/subscribe", json={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth_b64}}).status_code == 200
    assert client.get("/api/v1/push").json()["devices"] == 1
    assert client.post("/api/v1/push/test").json()["job"]
    assert client.post("/api/v1/push/unsubscribe", json={"endpoint": endpoint, "keys": {}}).json()["deleted"] == 1
    monkeypatch.setattr(settings, "yaml", {**settings.yaml, "notifications": {"push": {"enabled": False}}})
    assert client.get("/api/v1/push").json() == {"enabled": False, "public_key": "", "devices": 0}


def test_new_critical_is_queued_and_delivered(push_on):
    db = SessionLocal()
    key, p256dh, auth_b64, auth = _receiver()
    web_push.save_subscription(db, "a@dc.local", "https://push.example/ok", p256dh, auth_b64)
    web_push.save_subscription(db, "b@dc.local", "https://push.example/gone", p256dh, auth_b64)
    inc = Incident(number=f"INC-WP-{uuid4().hex[:6]}", title="wp-disk full", severity="CRITICAL", status="OPEN",
                   fingerprint=f"wp:{uuid4().hex}", started_at=utcnow())
    quiet = Incident(number=f"INC-WP-{uuid4().hex[:6]}", title="wp-child", severity="CRITICAL", status="SUPPRESSED",
                     fingerprint=f"wp:{uuid4().hex}", started_at=utcnow())
    db.add_all([inc, quiet])
    db.commit()
    web_push.queue_new_critical(db, inc)
    web_push.queue_new_critical(db, quiet)
    assert [j.object_id for j in db.query(Job).filter_by(kind=web_push.JOB_KIND).all()] == [inc.number]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(_decrypt(request.content, key, auth)))
        assert request.headers["Content-Encoding"] == "aes128gcm" and request.headers["Authorization"].startswith("vapid t=")
        return httpx.Response(410 if request.url.path == "/gone" else 201)

    counts = web_push.deliver(db, web_push.message(inc), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert counts == {"sent": 1, "gone": 1, "failed": 0}
    assert seen[0]["title"].startswith("CRITICAL INC-WP") and seen[0]["url"].startswith("/app/incidents/")
    assert [s.endpoint for s in db.query(PushSubscription).all()] == ["https://push.example/ok"]
    db.close()
