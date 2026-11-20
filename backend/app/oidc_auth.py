"""Single sign-on with OpenID Connect (Entra ID, Keycloak, Okta, Google Workspace, Authentik ...).

Authorization code flow with PKCE. ``/auth/oidc/login`` sends the browser to the provider with a
random state, nonce and code verifier kept in a short-lived signed cookie (not in process memory,
so any Core worker can finish the sign-in). ``/auth/oidc/callback`` exchanges the code (5 s
timeout), verifies the ID token against the provider's keys (issuer, audience, expiry, nonce),
maps the groups claim to a ForgeSRE role and signs the person in. Users are created on first
sign-in with password hash ``!oidc``, so they can never sign in with a local password.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.settings import settings

EXTERNAL_HASH = "!oidc"
COOKIE = "forgesre_oidc"
TIMEOUT = 5.0
ROLE_ORDER = ("admin", "engineer", "analyst", "viewer")
_meta_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="forgesre-oidc")


def redirect_uri() -> str:
    from app.ack_links import public_url

    base = public_url()
    return f"{base}/auth/oidc/callback" if base else ""


def ready(cfg: dict[str, Any] | None = None) -> str:
    """'' when OIDC can be used, else what is missing."""
    cfg = cfg or settings.oidc
    if not cfg["enabled"]:
        return "OIDC sign-in is off"
    missing = [n for n, v in (("issuer", cfg["issuer"]), ("client id", cfg["client_id"]), ("client secret", cfg["client_secret"]),
                               ("public address (Settings › General)", redirect_uri())) if not v]
    return f"missing: {', '.join(missing)}" if missing else ""


def metadata(issuer: str) -> dict[str, Any]:
    """The provider's discovery document, cached 10 minutes per process (a cache, not shared state)."""
    hit = _meta_cache.get(issuer)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    r = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    _meta_cache[issuer] = (time.monotonic(), data)
    return data


def start() -> tuple[str, str]:
    """(provider URL to redirect to, signed cookie value with state, nonce and PKCE verifier)."""
    cfg = settings.oidc
    meta = metadata(cfg["issuer"])
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urlencode({"response_type": "code", "client_id": cfg["client_id"], "redirect_uri": redirect_uri(),
                       "scope": cfg["scopes"], "state": state, "nonce": nonce,
                       "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{meta['authorization_endpoint']}?{query}", _serializer().dumps({"s": state, "n": nonce, "v": verifier})


def role_from_claims(claims: dict[str, Any], cfg: dict[str, Any]) -> str:
    raw = claims.get(cfg["groups_claim"]) or []
    have = {str(g).strip().lower() for g in (raw if isinstance(raw, list) else [raw])}
    for role in ROLE_ORDER:
        if any(g.lower() in have for g in cfg["groups"].get(role) or []):
            return role
    return cfg["default_role"] if cfg["default_role"] in ROLE_ORDER else ""


def finish(code: str, state: str, cookie: str, *, verify_token=None) -> dict[str, Any]:
    """Check state, exchange the code, verify the ID token. {email, name, role}; raises ValueError with the reason."""
    import jwt

    cfg = settings.oidc
    try:
        saved = _serializer().loads(cookie or "", max_age=600)
    except BadSignature as exc:
        raise ValueError("the sign-in took too long or did not start here; try again") from exc
    if not state or not secrets.compare_digest(str(saved.get("s")), state):
        raise ValueError("state does not match; try again")
    meta = metadata(cfg["issuer"])
    r = httpx.post(meta["token_endpoint"], timeout=TIMEOUT, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(), "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"], "code_verifier": saved["v"]})
    if r.status_code >= 400:
        raise ValueError(f"the provider refused the code: HTTP {r.status_code}")
    token = r.json().get("id_token") or ""
    if verify_token is not None:
        claims = verify_token(token)
    else:
        key = jwt.PyJWKClient(meta["jwks_uri"], timeout=int(TIMEOUT)).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"],
                            audience=cfg["client_id"], issuer=meta.get("issuer") or cfg["issuer"], leeway=60)
    if claims.get("nonce") != saved.get("n"):
        raise ValueError("nonce does not match; try again")
    email = str(claims.get("email") or claims.get("preferred_username") or claims.get("upn") or "").strip().lower()
    if "@" not in email:
        raise ValueError("the provider sent no email address; add the email scope or claim")
    role = role_from_claims(claims, cfg)
    if not role:
        raise ValueError(f"{email} is in none of the groups mapped to a ForgeSRE role")
    return {"email": email, "name": str(claims.get("name") or email), "role": role}


def sign_in(db, found: dict[str, Any]):
    """The local User for a verified person, created or updated; None for a disabled or local account."""
    from app.audit import audit
    from app.models import User

    user = db.query(User).filter(User.email == found["email"]).first()
    if user is not None and user.password_hash != EXTERNAL_HASH:
        return None  # a local account with this email signs in with its password only
    if user is None:
        user = User(email=found["email"], name=found["name"][:255], password_hash=EXTERNAL_HASH, role=found["role"], is_active=True)
        db.add(user)
        audit(db, "user.create", actor="oidc", object_type="user", object_id=found["email"], data={"role": found["role"], "source": "oidc"})
    elif user.role != found["role"]:
        audit(db, "user.update", actor="oidc", object_type="user", object_id=found["email"], data={"role": found["role"], "was": user.role})
        user.role = found["role"]
    db.commit()
    db.refresh(user)
    return user if user.is_active else None


def check() -> dict[str, Any]:
    """Settings › Test connection: read the provider's discovery document."""
    why = ready()
    if why == "OIDC sign-in is off":
        return {"status": "disabled", "detail": why}
    if why:
        return {"status": "error", "detail": why}
    try:
        meta = metadata(settings.oidc["issuer"])
        return {"status": "ok", "detail": f"Provider answered; redirect URI to register: {redirect_uri()}",
                "authorization_endpoint": meta.get("authorization_endpoint")}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:300]}
