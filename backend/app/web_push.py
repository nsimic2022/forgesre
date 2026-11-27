"""Web push to phones and browsers, also when ForgeSRE is closed (opt-in).

With ``notifications.push.enabled`` a user can subscribe a browser or the installed phone app
(Settings › Appearance). A new open CRITICAL incident that is not suppressed under a parent
queues job ``push_send``; it encrypts a short message (number, severity, host, title) for each
subscription (RFC 8291, aes128gcm) and posts it to the browser vendor's push service (Google,
Mozilla, Apple) with a VAPID signature (RFC 8292). The push service only sees ciphertext, but
this is the one notification path that goes through a third party, so it is off by default.

The VAPID key pair is made on first use and kept in the database (``push_keys``); only the
public key ever leaves Core. A subscription that the push service reports gone (404 / 410) is
deleted. Every POST has a 10 s timeout and runs in the jobs worker, never in a web request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.orm import Session

from app.models import Incident, PushKey, PushSubscription, utcnow
from app.settings import settings

JOB_KIND = "push_send"
TIMEOUT = 10.0
TTL_SECONDS = 3600
RECORD_SIZE = 4096


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _cfg() -> dict[str, Any]:
    raw = ((settings.yaml or {}).get("notifications") or {}).get("push") or {}
    return raw if isinstance(raw, dict) else {}


def enabled() -> bool:
    return bool(_cfg().get("enabled"))


def _public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid_key(db: Session) -> ec.EllipticCurvePrivateKey:
    """The appliance's VAPID key, made once and kept in push_keys."""
    row = db.query(PushKey).order_by(PushKey.id).first()
    if row is None:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        row = PushKey(private_pem=pem.decode(), public_b64=b64u(_public_bytes(key)))
        db.add(row)
        db.commit()
        return key
    return serialization.load_pem_private_key(row.private_pem.encode(), password=None)  # type: ignore[return-value]


def public_key(db: Session) -> str:
    """Base64url VAPID public key for pushManager.subscribe (applicationServerKey)."""
    return b64u(_public_bytes(vapid_key(db)))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            sender: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """RFC 8291 message encryption (aes128gcm, one record). Returns the request body."""
    ua_public = b64u_decode(p256dh)
    auth_secret = b64u_decode(auth)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(sender)
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = sender.exchange(ec.ECDH(), receiver)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise ValueError("push payload too long")
    cipher = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # 0x02 = last record
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + cipher


def vapid_headers(db: Session, endpoint: str) -> dict[str, str]:
    """Authorization header for the push service of this endpoint (RFC 8292)."""
    parts = urlsplit(endpoint)
    contact = str(((settings.yaml or {}).get("notifications") or {}).get("email", {}).get("from") or "")
    sub = f"mailto:{contact}" if "@" in contact else (str((settings.yaml or {}).get("system", {}).get("public_url") or "") or "mailto:noc@forgesre.local")
    key = vapid_key(db)
    token = jwt.encode({"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": sub}, key, algorithm="ES256")
    return {"Authorization": f"vapid t={token}, k={b64u(_public_bytes(key))}"}


def message(incident: Incident) -> dict[str, str]:
    host = incident.asset.hostname if incident.asset is not None else ""
    return {
        "title": f"{incident.severity} {incident.number.split('_')[0]}",
        "body": " · ".join(x for x in (host, incident.title) if x)[:180],
        "url": f"/app/incidents/{incident.number}",
        "tag": incident.number,
    }


def send(db: Session, sub: PushSubscription, data: dict[str, str], client: httpx.Client) -> str:
    """Post one encrypted message. Returns ok | gone | error:<status>."""
    body = encrypt(json.dumps(data).encode(), sub.p256dh, sub.auth)
    headers = {**vapid_headers(db, sub.endpoint), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
               "TTL": str(TTL_SECONDS), "Urgency": "high"}
    response = client.post(sub.endpoint, content=body, headers=headers)
    if response.status_code in {404, 410}:
        return "gone"
    if response.status_code >= 300:
        return f"error:{response.status_code}"
    return "ok"


def deliver(db: Session, data: dict[str, str], only_email: str = "", client: httpx.Client | None = None) -> dict[str, int]:
    """Send to every subscription (or one user's). Gone ones are deleted; failures counted."""
    query = db.query(PushSubscription)
    if only_email:
        query = query.filter(PushSubscription.email == only_email)
    counts = {"sent": 0, "gone": 0, "failed": 0}
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        for sub in query.all():
            try:
                result = send(db, sub, data, client)
            except (httpx.HTTPError, ValueError) as exc:
                result = f"error:{type(exc).__name__}"
            if result == "ok":
                sub.last_ok_at, sub.fails = utcnow(), 0
                counts["sent"] += 1
            elif result == "gone":
                db.delete(sub)
                counts["gone"] += 1
            else:
                sub.fails = (sub.fails or 0) + 1
                sub.last_error = result[:120]
                counts["failed"] += 1
    finally:
        if own:
            client.close()
    db.commit()
    from app.exchanges import record

    if any(counts.values()):
        record("notifications", "out", "web push", status="ok" if not counts["failed"] else "error",
               summary=f"sent {counts['sent']}, failed {counts['failed']}, gone {counts['gone']}" + (f" (to {only_email.split('@')[0][:1]}…)" if only_email else ""))
    return counts


def queue_new_critical(db: Session, incident: Incident) -> None:
    """After intake committed: queue a push for a new open CRITICAL that is not under a parent."""
    if not enabled() or str(incident.severity).upper() != "CRITICAL" or incident.status != "OPEN" or incident.caused_by_id:
        return
    if not db.query(PushSubscription.id).first():
        return
    from app.jobs import enqueue

    enqueue(db, JOB_KIND, incident.number, object_type="incident", payload={"incident": incident.number})


def run_job(db: Session, payload: dict[str, Any]) -> dict[str, int]:
    """Jobs worker: the incident's message to every subscription, or a test to one user."""
    if payload.get("test_email"):
        return deliver(db, {"title": "ForgeSRE test", "body": "Push works on this device.", "url": "/app/", "tag": "forgesre-test"},
                       only_email=str(payload["test_email"]))
    incident = db.query(Incident).filter_by(number=str(payload.get("incident") or "")).first()
    if incident is None:
        raise RuntimeError("incident not found")
    return deliver(db, message(incident))


def save_subscription(db: Session, email: str, endpoint: str, p256dh: str, auth: str) -> PushSubscription:
    """Store (or move to this user) one browser subscription; keys are checked for shape."""
    if not endpoint.startswith("https://") or len(endpoint) > 1000:
        raise ValueError("endpoint must be an https:// push service URL")
    if len(b64u_decode(p256dh)) != 65 or len(b64u_decode(auth)) != 16:
        raise ValueError("bad subscription keys")
    row = db.query(PushSubscription).filter_by(endpoint=endpoint).first() or PushSubscription(endpoint=endpoint)
    row.email, row.p256dh, row.auth, row.fails = email, p256dh, auth, 0
    db.add(row)
    db.commit()
    return row
