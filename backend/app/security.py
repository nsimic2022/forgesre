from __future__ import annotations

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.models import User
from app.settings import settings

ROLES = ["super_admin", "admin", "engineer", "analyst", "viewer"]
CREATABLE_ROLES = ["analyst", "engineer", "admin", "viewer"]

ROLE_LABELS = {
    "super_admin": "Super admin",
    "admin": "System admin",
    "system_admin": "System admin",
    "analyst": "Analyst",
    "engineer": "Engineer",
    "viewer": "Viewer",
}

PERMISSIONS = {
    "viewer": {"read_dashboard", "read_assets", "read_incidents"},
    "analyst": {
        "read_dashboard",
        "read_assets",
        "read_incidents",
        "read_ai",
        "ack_incidents",
        "write_incidents",
        "read_play",
        "write_play",
        "write_assets",
    },
    "engineer": {
        "read_dashboard",
        "read_assets",
        "read_incidents",
        "read_ai",
        "ack_incidents",
        "read_play",
        "read_evidence",
        "investigate",
        "write_incidents",
        "write_assets",
    },
    "admin": {
        "read_dashboard",
        "read_assets",
        "read_incidents",
        "read_ai",
        "ack_incidents",
        "read_play",
        "read_evidence",
        "investigate",
        "write_incidents",
        "write_play",
        "write_assets",
        "admin",
    },
    "super_admin": {
        "read_dashboard",
        "read_assets",
        "read_incidents",
        "read_ai",
        "ack_incidents",
        "read_play",
        "read_evidence",
        "investigate",
        "write_incidents",
        "write_play",
        "write_assets",
        "admin",
        "super_admin",
    },
}

serializer = URLSafeTimedSerializer(settings.secret_key, salt="forgesre-session")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def make_session_token(user_id: int) -> str:
    return serializer.dumps({"uid": user_id})


def parse_session_token(token: str) -> int | None:
    try:
        data = serializer.loads(token, max_age=60 * 60 * 12)
    except (BadSignature, SignatureExpired):
        return None
    return int(data.get("uid")) if data and data.get("uid") is not None else None


def user_from_session(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    user_id = parse_session_token(token)
    if not user_id:
        return None
    user = db.get(User, user_id)
    if not user or not user.is_active:
        return None
    return user


def role_label(role: str) -> str:
    if role in ROLE_LABELS:
        return ROLE_LABELS[role]
    pretty = (role or "").replace("_", " ").strip()
    return pretty.title() if pretty else "Unknown"


def distinct_who_name(name: str | None, role: str | None) -> str:
    """Return a display name only when it is not the same phrase as the role."""
    label = role_label(role or "")
    text = (name or "").strip()
    if not text or text.casefold() == label.casefold():
        return ""
    return text


def can(user: User | None, permission: str) -> bool:
    if user is None:
        return False
    return permission in PERMISSIONS.get(user.role, set())
