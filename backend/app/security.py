from __future__ import annotations

import threading
import time
from collections import deque

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import User
from app.settings import settings

# Four roles on one ladder: each has every permission of the role before it (AGENTS.md §3).
ROLES = ["viewer", "analyst", "engineer", "admin"]
CREATABLE_ROLES = list(ROLES)
# Roles from before V1.0 that now mean "admin". migrate() rewrites them in the database;
# role_key() also maps them so an un-migrated row never loses access.
LEGACY_ADMIN_ROLES = {"super_admin", "system_admin"}

ROLE_LABELS = {
    "viewer": "Viewer",
    "analyst": "Analyst",
    "engineer": "Engineer",
    "admin": "Admin",
}

ROLE_HELP = {
    "viewer": "Viewer — read only (wall screen, management)",
    "analyst": "Analyst — NOC operator: acknowledge, notes, checklists, local ForgeAI",
    "engineer": "Engineer — changes inventory, rules, playbooks, escalation for every team",
    "admin": "Admin — also settings, integrations, users, backup",
}

_VIEWER = {"read_dashboard", "read_assets", "read_incidents"}
_ANALYST = _VIEWER | {"read_ai", "ack_incidents", "write_incidents", "read_play", "read_evidence", "investigate"}
_ENGINEER = _ANALYST | {"write_assets", "write_play", "ai_cloud", "run_checks"}
_ADMIN = _ENGINEER | {"admin"}

PERMISSIONS = {"viewer": _VIEWER, "analyst": _ANALYST, "engineer": _ENGINEER, "admin": _ADMIN}


def role_key(role: str | None) -> str:
    """Normalise a stored role: legacy admin roles become "admin", unknown roles get nothing."""
    value = (role or "").strip().lower()
    return "admin" if value in LEGACY_ADMIN_ROLES else value


serializer = URLSafeTimedSerializer(settings.secret_key, salt="forgesre-session")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


SESSION_MAX_AGE = 60 * 60 * 12


def make_session_token(user_id: int, session_version: int = 0) -> str:
    """Signed cookie value. ``sv`` must match ``User.session_version`` to stay valid."""
    return serializer.dumps({"uid": user_id, "sv": int(session_version or 0)})


def _load_session(token: str) -> tuple[int, int] | None:
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or data.get("uid") is None:
        return None
    try:
        # Tokens issued before session_version existed carry no "sv": treat as 0.
        return int(data["uid"]), int(data.get("sv") or 0)
    except (TypeError, ValueError):
        return None


def parse_session_token(token: str) -> int | None:
    loaded = _load_session(token)
    return loaded[0] if loaded else None


def user_from_session(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    loaded = _load_session(token)
    if not loaded:
        return None
    user_id, version = loaded
    user = db.get(User, user_id)
    if not user or not user.is_active:
        return None
    if int(user.session_version or 0) != version:
        return None
    return user


def revoke_sessions(user: User) -> None:
    """Invalidate every cookie already issued to this user (logout, password change)."""
    user.session_version = int(user.session_version or 0) + 1


class LoginThrottle:
    """Count failed sign-ins in a sliding window, in memory (Core runs one uvicorn process).

    Two keys are tracked: (client IP, email) and client IP alone. A key is blocked once it
    reaches its limit inside the window; a successful sign-in clears the (IP, email) key.
    """

    def __init__(self, *, per_account: int = 5, per_ip: int = 30, window_seconds: int = 15 * 60) -> None:
        self.per_account = per_account
        self.per_ip = per_ip
        self.window = window_seconds
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _keys(ip: str, email: str) -> tuple[str, str]:
        return f"acct:{ip}|{(email or '').strip().lower()}", f"ip:{ip}"

    def _recent(self, key: str, now: float) -> deque[float]:
        bucket = self._fails.get(key)
        if bucket is None:
            return deque()
        while bucket and now - bucket[0] >= self.window:
            bucket.popleft()
        if not bucket:
            self._fails.pop(key, None)
        return bucket

    def retry_after(self, ip: str, email: str, now: float | None = None) -> int:
        """Seconds until another attempt is allowed; 0 when not blocked."""
        now = time.monotonic() if now is None else now
        acct, ip_key = self._keys(ip, email)
        wait = 0.0
        with self._lock:
            for key, limit in ((acct, self.per_account), (ip_key, self.per_ip)):
                bucket = self._recent(key, now)
                if len(bucket) >= limit:
                    wait = max(wait, self.window - (now - bucket[0]))
        return int(wait) + 1 if wait > 0 else 0

    def failure(self, ip: str, email: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            for key in self._keys(ip, email):
                self._recent(key, now)
                self._fails.setdefault(key, deque()).append(now)

    def success(self, ip: str, email: str) -> None:
        acct, _ip_key = self._keys(ip, email)
        with self._lock:
            self._fails.pop(acct, None)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()


login_throttle = LoginThrottle()

_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("forgesre-not-a-real-password")
    return _DUMMY_HASH


def authenticate(db: Session, email: str, password: str, ip: str) -> tuple[User | None, int]:
    """Check a sign-in. Returns (user, 0) on success, (None, 0) on bad credentials,
    (None, retry_after_seconds) when this IP / account is temporarily blocked."""
    wait = login_throttle.retry_after(ip, email)
    if wait:
        return None, wait
    clean = (email or "").strip().lower()
    user = db.query(User).filter(func.lower(User.email) == clean).first() if clean else None
    if user is None:
        # Spend the same bcrypt time as a real check so unknown emails are not told apart.
        verify_password(password, _dummy_hash())
        login_throttle.failure(ip, email)
        return None, 0
    if not verify_password(password, user.password_hash) or not user.is_active:
        login_throttle.failure(ip, email)
        return None, 0
    login_throttle.success(ip, email)
    return user, 0


def role_label(role: str) -> str:
    key = role_key(role)
    if key in ROLE_LABELS:
        return ROLE_LABELS[key]
    pretty = (role or "").replace("_", " ").strip()
    return pretty.title() if pretty else "Unknown"


def distinct_who_name(name: str | None, role: str | None, surname: str | None = "") -> str:
    """Return first + last name when that phrase is not just the role label."""
    label = role_label(role or "")
    first = " ".join(str(name or "").split())
    last = " ".join(str(surname or "").split())
    text = " ".join(part for part in (first, last) if part)
    if not text or text.casefold() == label.casefold():
        return ""
    return text


def can(user: User | None, permission: str) -> bool:
    """Server-side permission check used by every route; the UI only hides what this denies."""
    if user is None:
        return False
    return permission in PERMISSIONS.get(role_key(user.role), set())


def can_send_ops(user: User | None) -> bool:
    return can(user, "write_play") or can(user, "write_incidents") or can(user, "admin")
