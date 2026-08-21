"""Create / update / remove ForgeSRE UI users. Passwords are bcrypt hashes in Postgres."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.audit import audit
from app.models import User
from app.security import CREATABLE_ROLES, can, hash_password


def find_email(db: Session, email: str, *, exclude_id: int | None = None) -> User | None:
    q = db.query(User).filter(func.lower(User.email) == email.strip().lower())
    if exclude_id is not None:
        q = q.filter(User.id != exclude_id)
    return q.first()


def edit_blocked(actor: User, target: User) -> str:
    if not can(actor, "admin"):
        return "forbidden"
    if target.role == "super_admin" and actor.role != "super_admin":
        return "cannot edit the install super admin"
    return ""


def delete_blocked(actor: User, target: User) -> str:
    reason = edit_blocked(actor, target)
    if reason:
        return reason
    if actor.id == target.id:
        return "cannot remove yourself"
    if target.role == "super_admin":
        return "cannot remove the install super admin"
    return ""


def create_user(
    db: Session,
    actor: User,
    *,
    email: str,
    name: str,
    password: str,
    role: str,
) -> User:
    email = (email or "").strip()
    name = (name or "").strip()
    password = password or ""
    if not email or not name or not password:
        raise ValueError("email, name, and password are required")
    if role not in CREATABLE_ROLES:
        raise ValueError("invalid role")
    if find_email(db, email):
        raise ValueError("email already exists")
    row = User(email=email, name=name, password_hash=hash_password(password), role=role)
    db.add(row)
    db.flush()
    audit(db, "user.create", actor=actor.email, object_type="user", object_id=email)
    return row


def update_user(
    db: Session,
    actor: User,
    target: User,
    *,
    email: str | None = None,
    name: str | None = None,
    password: str | None = None,
    role: str | None = None,
) -> User:
    reason = edit_blocked(actor, target)
    if reason:
        raise ValueError(reason)
    if email is not None:
        email = email.strip()
        if not email:
            raise ValueError("email is required")
        if find_email(db, email, exclude_id=target.id):
            raise ValueError("email already exists")
        target.email = email
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        target.name = name
    if password:
        target.password_hash = hash_password(password)
    if role is not None:
        if target.role == "super_admin":
            if role != "super_admin":
                raise ValueError("cannot change the install super admin role")
        elif role not in CREATABLE_ROLES:
            raise ValueError("invalid role")
        else:
            target.role = role
    audit(db, "user.update", actor=actor.email, object_type="user", object_id=target.email)
    return target


def delete_user(db: Session, actor: User, target: User) -> str:
    reason = delete_blocked(actor, target)
    if reason:
        raise ValueError(reason)
    email = target.email
    db.delete(target)
    audit(db, "user.delete", actor=actor.email, object_type="user", object_id=email)
    return email
