from __future__ import annotations

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.models import User
from app.settings import settings

ROLES = ["super_admin", "admin", "engineer", "analyst", "viewer"]
CREATABLE_ROLES = ["analyst", "engineer", "admin", "viewer"]

ROLE_LABELS = {
    "super_admin": "Super admin (system)",
    "admin": "System admin",
    "analyst": "Analyst (incidents, playrules, playbooks)",
    "engineer": "Engineer (detailed RCA)",
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
    return ROLE_LABELS.get(role, role)


def can(user: User | None, permission: str) -> bool:
    if user is None:
        return False
    return permission in PERMISSIONS.get(user.role, set())
