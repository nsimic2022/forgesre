"""Sign in with LDAP / Active Directory.

Flow: find the person with the service account (or anonymously), bind as them with the password
they typed, read their groups and map them to a ForgeSRE role (the highest mapped group wins).
The local user row is created or updated on every sign-in, so the directory stays the source of
roles; its password hash is ``!ldap`` and can never match a local password. Every call has a
timeout; an unreachable directory means a failed sign-in, never a hang, and local accounts
(the installer's admin) keep working.
"""

from __future__ import annotations

from typing import Any

from app.settings import settings

EXTERNAL_HASH = "!ldap"
ROLE_ORDER = ("admin", "engineer", "analyst", "viewer")


def _server(cfg: dict[str, Any]):
    from ldap3 import Server

    return Server(cfg["url"], connect_timeout=cfg["timeout"], get_info=None)


def _connection(cfg: dict[str, Any], user: str | None = None, password: str | None = None):
    from ldap3 import Connection

    conn = Connection(_server(cfg), user=user, password=password, receive_timeout=cfg["timeout"], auto_bind=False, raise_exceptions=False)
    if cfg["url"].lower().startswith("ldap://"):
        conn.open()
        conn.start_tls()
    return conn


def role_from_groups(groups: list[str], mapping: dict[str, str], default: str = "") -> str:
    """Highest role whose group DN is in ``groups`` (case-insensitive), else ``default``."""
    have = {g.strip().lower() for g in groups}
    for role in ROLE_ORDER:
        dn = (mapping.get(role) or "").strip().lower()
        if dn and dn in have:
            return role
    return default if default in ROLE_ORDER else ""


def lookup(login: str, password: str, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """{email, name, role} when the directory accepts ``login`` / ``password`` and a role applies, else None."""
    from ldap3.utils.conv import escape_filter_chars

    cfg = cfg or settings.ldap
    if not (cfg["enabled"] and cfg["url"] and cfg["base_dn"] and login and password):
        return None
    search = _connection(cfg, cfg["bind_dn"] or None, cfg["bind_password"] or None)
    try:
        if not search.bind():
            return None
        flt = cfg["user_filter"].replace("{login}", escape_filter_chars(login.strip()))
        search.search(cfg["base_dn"], flt, attributes=["mail", "userPrincipalName", "displayName", "cn", "memberOf"], size_limit=2)
        if len(search.entries) != 1:
            return None  # nobody, or ambiguous: never guess between two people
        entry = search.entries[0]
        dn = entry.entry_dn
        attrs = entry.entry_attributes_as_dict
    finally:
        search.unbind()
    person = _connection(cfg, dn, password)
    try:
        if not person.bind():
            return None
    finally:
        person.unbind()
    role = role_from_groups([str(g) for g in attrs.get("memberOf") or []], cfg["groups"], cfg["default_role"])
    if not role:
        return None
    first = lambda key: str((attrs.get(key) or [""])[0] or "")  # noqa: E731
    email = (first("mail") or first("userPrincipalName") or login).strip().lower()
    return {"email": email, "name": first("displayName") or first("cn") or email, "role": role}


def sign_in(db, login: str, password: str):
    """Directory sign-in: the matching local User (created or updated), or None. Never raises."""
    from app.audit import audit
    from app.models import User

    try:
        found = lookup(login, password)
    except Exception:
        return None
    if found is None:
        return None
    user = db.query(User).filter(User.email == found["email"]).first()
    if user is not None and user.password_hash != EXTERNAL_HASH:
        return None  # a local account with the same email keeps its own password
    if user is None:
        user = User(email=found["email"], name=found["name"][:255], password_hash=EXTERNAL_HASH, role=found["role"], is_active=True)
        db.add(user)
        audit(db, "user.create", actor="ldap", object_type="user", object_id=found["email"], data={"role": found["role"], "source": "ldap"})
    elif user.role != found["role"] or user.name != found["name"]:
        audit(db, "user.update", actor="ldap", object_type="user", object_id=found["email"], data={"role": found["role"], "was": user.role})
        user.role, user.name = found["role"], found["name"][:255]
    if not user.is_active:
        db.commit()
        return None
    db.commit()
    db.refresh(user)
    return user


def check() -> dict[str, Any]:
    """Settings › Test connection: bind with the service account and search the base. Read-only."""
    cfg = settings.ldap
    if not cfg["enabled"]:
        return {"status": "disabled", "detail": "LDAP sign-in is off"}
    if not (cfg["url"] and cfg["base_dn"]):
        return {"status": "error", "detail": "Set the LDAP server and the search base"}
    try:
        conn = _connection(cfg, cfg["bind_dn"] or None, cfg["bind_password"] or None)
        if not conn.bind():
            return {"status": "error", "detail": f"bind failed: {conn.result.get('description') or conn.result}"}
        conn.search(cfg["base_dn"], "(objectClass=*)", attributes=[], size_limit=1)
        conn.unbind()
        return {"status": "ok", "detail": f"Connected to {cfg['url']} and read {cfg['base_dn']}"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:300]}
