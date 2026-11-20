"""The few server-rendered pages left: sign-in, single sign-on, the mailed acknowledge link, and
redirects from the old classic URLs into the new UI (``/app``).

Everything else is the React UI in ``web/`` talking to ``/api/v1``. Old bookmarks and links in old
mails keep working: each classic path sends the browser to the matching new page.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.models import Incident, User
from app.security import authenticate, can, revoke_sessions, set_session_cookie, user_from_session
from app.history import acknowledge
from app.settings import settings
from app.webui import web_ui_available

router = APIRouter()
templates = Jinja2Templates(directory=str(settings.frontend_dir / "templates"))
log = logging.getLogger("forgesre")


def render(request: Request, name: str, **extra):
    return templates.TemplateResponse(request, name, {"request": request, **extra})


def get_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return user_from_session(db, request.cookies.get("forgesre_session"))


class NotAuthenticated(Exception):
    """Raised by login_required; the app turns it into a redirect to /login."""


def login_required(user: User | None = Depends(get_user)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require_page(*permissions: str):
    def _inner(user: User = Depends(login_required)) -> User:
        if permissions and not any(can(user, perm) for perm in permissions):
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return _inner


def _home() -> str:
    return "/app/" if web_ui_available() else "/login"


# ---------------------------------------------------------------- acknowledge from mail


def _ack_target(db: Session, token: str) -> tuple[Incident | None, str, str]:
    """(incident, recipient, error) for a one-click acknowledge link."""
    from app.ack_links import read_token

    try:
        data = read_token(token)
    except ValueError as exc:
        return None, "", "This link has expired. Open ForgeSRE to acknowledge." if str(exc) == "expired" else "This link is not valid."
    incident = db.get(Incident, data["incident_id"])
    if incident is None:
        return None, "", "This incident no longer exists."
    if (incident.status or "").upper() in {"RESOLVED", "CLOSED"}:
        return None, "", f"{incident.number} is already resolved."
    return incident, data["recipient"], ""


@router.get("/ack/{token}", response_class=HTMLResponse)
def ack_page(token: str, request: Request, db: Session = Depends(get_db)):
    """Confirmation page for a mailed acknowledge link. A GET never acknowledges (mail scanners open links)."""
    incident, who, error = _ack_target(db, token)
    return render(request, "ack.html", incident=incident, who=who, error=error, token=token, done=False)


@router.post("/ack/{token}", response_class=HTMLResponse)
def ack_submit(token: str, request: Request, db: Session = Depends(get_db)):
    """Acknowledge as the recipient the link was mailed to; same effect as the Acknowledge button."""
    incident, who, error = _ack_target(db, token)
    if incident is not None and not incident.ack_at:
        acknowledge(incident, who or "mail link")
        audit(db, "incident.ack", actor=who or "mail link", object_type="incident", object_id=incident.number,
              ip=request.client.host if request.client else "", data={"via": "mail link"})
        db.commit()
    return render(request, "ack.html", incident=incident, who=who, error=error, token=token, done=incident is not None)


# ---------------------------------------------------------------- sign-in


def _sso_label() -> str:
    """The single sign-on button text when OIDC is set up, else ''."""
    from app.oidc_auth import ready

    return settings.oidc["button_label"] if not ready() else ""


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: User | None = Depends(get_user), sso_error: str = ""):
    """Sign-in form. A signed-in user goes to the app; without a built UI the form says so
    instead of redirecting to itself."""
    if user and web_ui_available():
        return RedirectResponse(_home(), status_code=302)
    notice = "" if web_ui_available() else "The web UI is not built on this server. Run ./forgesre update, or use ./forgesre over SSH."
    return render(request, "login.html", error=sso_error[:200] or None, sso=_sso_label(), notice=notice, signed_in=bool(user))


@router.post("/login")
def login_submit(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(...),
    password: str = Form(...),
):
    """Local password, or the directory (LDAP) when it is on. Throttled per IP and account."""
    ip = request.client.host if request.client else ""
    user, wait = authenticate(db, email, password, ip)
    if wait:
        minutes = max(1, (wait + 59) // 60)
        response = render(request, "login.html", error=f"Too many failed sign-in attempts. Try again in {minutes} min.", sso=_sso_label())
        response.status_code = 429
        response.headers["Retry-After"] = str(wait)
        return response
    if user is None:
        return render(request, "login.html", error="Invalid email or password.", sso=_sso_label())
    audit(db, "login", actor=user.email, ip=ip, commit=True)
    response = RedirectResponse(_home(), status_code=302)
    set_session_cookie(response, user)
    return response


@router.get("/auth/oidc/login")
def oidc_login():
    """Send the browser to the OIDC provider (state, nonce and PKCE verifier in a 10-minute signed cookie)."""
    from app.oidc_auth import COOKIE, ready, start

    why = ready()
    if why:
        return RedirectResponse(f"/login?{urlencode({'sso_error': 'Single sign-on is not set up: ' + why})}", status_code=302)
    try:
        url, cookie = start()
    except Exception as exc:
        log.warning("oidc start failed: %s", exc)
        return RedirectResponse(f"/login?{urlencode({'sso_error': 'The sign-on provider did not answer.'})}", status_code=302)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(COOKIE, cookie, httponly=True, samesite="lax", secure=settings.cookie_secure, max_age=600)
    return response


@router.get("/auth/oidc/callback")
def oidc_callback(request: Request, db: Session = Depends(get_db), code: str = "", state: str = "", error: str = ""):
    """Finish single sign-on: verify the provider's answer, map groups to a role, start the session."""
    from app.oidc_auth import COOKIE, finish, sign_in

    ip = request.client.host if request.client else ""
    if error or not code:
        return RedirectResponse(f"/login?{urlencode({'sso_error': 'Sign-on was cancelled or refused: ' + (error or 'no code')})}", status_code=302)
    try:
        found = finish(code, state, request.cookies.get(COOKIE, ""))
        user = sign_in(db, found)
    except Exception as exc:  # every failure is shown on the login page, never a 500
        audit(db, "login.failed", actor="oidc", ip=ip, data={"why": str(exc)[:200]}, commit=True)
        return RedirectResponse(f"/login?{urlencode({'sso_error': str(exc)[:200]})}", status_code=302)
    if user is None:
        return RedirectResponse(f"/login?{urlencode({'sso_error': 'This account is disabled or is a local account.'})}", status_code=302)
    audit(db, "login", actor=user.email, ip=ip, data={"via": "oidc"}, commit=True)
    response = RedirectResponse(_home(), status_code=302)
    response.delete_cookie(COOKIE)
    set_session_cookie(response, user)
    return response


@router.post("/logout")
@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db), user: User | None = Depends(get_user)):
    if user:
        # Kill this cookie server-side too (and any copy of it), not only in the browser.
        revoke_sessions(user)
        audit(db, "logout", actor=user.email, ip=request.client.host if request.client else "", commit=True)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("forgesre_session")
    return response


# ---------------------------------------------------------------- old classic URLs

# Old path → new page. Paths with an id keep it (incident number, asset id).
REDIRECTS = {
    "/": "/app/", "/classic": "/app/", "/dashboard": "/app/",
    "/incidents": "/app/incidents", "/history": "/app/incidents",
    "/assets": "/app/inventory", "/assets/verify": "/app/inventory", "/discovery": "/app/inventory/add",
    "/playrules": "/app/automation", "/playbooks": "/app/automation", "/escalation": "/app/automation",
    "/investigations": "/app/investigations", "/forgeai": "/app/investigations",
    "/ops": "/app/reports", "/journal": "/app/settings", "/health-ui": "/app/settings", "/admin": "/app/settings",
}


def _go(target: str, user: User | None) -> RedirectResponse:
    if user is None:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse(target if web_ui_available() else "/login", status_code=302)


for _old, _new in REDIRECTS.items():
    def _make(new: str):
        def _redirect(user: User | None = Depends(get_user)):
            return _go(new, user)
        return _redirect

    router.add_api_route(_old, _make(_new), methods=["GET"], include_in_schema=False)


@router.get("/incidents/{number}", include_in_schema=False)
@router.get("/ai/{number}", include_in_schema=False)
def old_incident(number: str, user: User | None = Depends(get_user)):
    return _go(f"/app/incidents/{quote(number, safe='')}", user)


@router.get("/assets/{asset_id}", include_in_schema=False)
def old_asset(asset_id: str, user: User | None = Depends(get_user)):
    return _go(f"/app/inventory?q={quote(asset_id, safe='')}", user)
