from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.metrics import set_demo_cpu
from app.models import Asset, Incident, Playbook, Playrule, User
from app.security import can, hash_password, user_from_session, verify_password
from app.seed import seed
from app.services import ingest_alertmanager, run_demo, run_investigation
from app.settings import settings

log = logging.getLogger("forgesre")
router = APIRouter(prefix="/api/v1")


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return user_from_session(db, request.cookies.get("forgesre_session"))


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require(permission: str):
    def _inner(user: User = Depends(require_user)) -> User:
        if not can(user, permission):
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return _inner


class LoginBody(BaseModel):
    email: str
    password: str


class UserBody(BaseModel):
    email: str
    name: str
    password: str
    role: str = "analyst"


class PlayruleBody(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    severity: str = "warning"
    condition: dict[str, Any] = Field(default_factory=dict)
    playbook_id: int | None = None


class PlaybookBody(BaseModel):
    name: str
    slug: str
    description: str = ""
    steps: list[Any] = Field(default_factory=list)


class StatusBody(BaseModel):
    status: str
    note: str = ""


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    db.query(Asset).first()
    return {"status": "ready"}


@router.post("/auth/login")
def api_login(body: LoginBody, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.query(User).filter_by(email=body.email).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    audit(db, "login", actor=user.email, ip=request.client.host if request.client else "", commit=True)
    return {"email": user.email, "role": user.role, "name": user.name}


@router.get("/me")
def me(user: User = Depends(require_user)) -> dict[str, Any]:
    return {"email": user.email, "role": user.role, "name": user.name}


@router.get("/assets")
def list_assets(db: Session = Depends(get_db), user: User = Depends(require("read_assets"))) -> list[dict]:
    return [_asset(item) for item in db.query(Asset).order_by(Asset.hostname).all()]


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, db: Session = Depends(get_db), user: User = Depends(require("read_assets"))) -> dict:
    item = db.query(Asset).filter_by(asset_id=asset_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return _asset(item)


@router.get("/incidents")
def list_incidents(db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> list[dict]:
    rows = db.query(Incident).order_by(Incident.id.desc()).all()
    return [_incident(item, include_evidence=can(user, "read_evidence")) for item in rows]


@router.get("/incidents/{number}")
def get_incident(number: str, db: Session = Depends(get_db), user: User = Depends(require("read_incidents"))) -> dict:
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return _incident(item, include_evidence=can(user, "read_evidence"))


@router.post("/incidents/{number}/status")
def set_status(
    number: str,
    body: StatusBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_incidents")),
) -> dict:
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    allowed = {"OPEN", "INVESTIGATING", "ESCALATED", "RESOLVED", "CLOSED"}
    status = body.status.upper()
    if status not in allowed:
        raise HTTPException(status_code=400, detail="invalid status")
    item.status = status
    audit(db, "incident.status", actor=user.email, object_type="incident", object_id=number, data={"status": status})
    db.commit()
    return _incident(item, include_evidence=True)


@router.post("/incidents/{number}/investigate")
def investigate_incident(
    number: str,
    db: Session = Depends(get_db),
    user: User = Depends(require("investigate")),
) -> dict:
    item = db.query(Incident).filter_by(number=number).first()
    if item is None:
        raise HTTPException(status_code=404, detail="incident not found")
    run_investigation(db, item)
    db.refresh(item)
    return _incident(item, include_evidence=True)


@router.get("/playrules")
def list_playrules(db: Session = Depends(get_db), user: User = Depends(require("read_play"))) -> list[dict]:
    return [_playrule(item) for item in db.query(Playrule).order_by(Playrule.name).all()]


@router.post("/playrules")
def create_playrule(
    body: PlayruleBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_play")),
) -> dict:
    row = Playrule(**body.model_dump())
    db.add(row)
    audit(db, "playrule.create", actor=user.email, object_type="playrule", object_id=body.name)
    db.commit()
    db.refresh(row)
    return _playrule(row)


@router.get("/playbooks")
def list_playbooks(db: Session = Depends(get_db), user: User = Depends(require("read_play"))) -> list[dict]:
    return [_playbook(item) for item in db.query(Playbook).order_by(Playbook.name).all()]


@router.post("/playbooks")
def create_playbook(
    body: PlaybookBody,
    db: Session = Depends(get_db),
    user: User = Depends(require("write_play")),
) -> dict:
    row = Playbook(**body.model_dump())
    db.add(row)
    audit(db, "playbook.create", actor=user.email, object_type="playbook", object_id=body.slug)
    db.commit()
    db.refresh(row)
    return _playbook(row)


@router.post("/webhooks/alertmanager")
async def alertmanager_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    auth = request.headers.get("authorization") or ""
    token = auth.replace("Bearer ", "").strip()
    if token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="invalid webhook token")
    payload = await request.json()
    created = ingest_alertmanager(db, payload)
    return {"accepted": True, "incidents": [item.number for item in created]}


@router.post("/demo")
def demo(db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    seed(db)
    set_demo_cpu(94)
    incident = run_demo(db)
    return {"ok": True, "incident": incident.number if incident else None}


@router.get("/system/status")
def system_status(db: Session = Depends(get_db), user: User = Depends(require("read_dashboard"))) -> dict:
    assets = db.query(Asset).all()
    incidents = db.query(Incident).all()
    return {
        "assets": {
            "total": len(assets),
            "healthy": sum(item.status == "healthy" for item in assets),
            "warning": sum(item.status == "warning" for item in assets),
            "critical": sum(item.status == "critical" for item in assets),
            "offline": sum(item.status == "offline" for item in assets),
        },
        "incidents": {
            "open": sum(item.status == "OPEN" for item in incidents),
            "critical": sum(item.severity.upper() == "CRITICAL" for item in incidents if item.status != "CLOSED"),
            "investigating": sum(item.status == "INVESTIGATING" for item in incidents),
            "resolved": sum(item.status == "RESOLVED" for item in incidents),
        },
        "monitoring": doctor_payload()["components"],
    }


@router.get("/system/doctor")
def doctor() -> dict:
    return doctor_payload()


@router.post("/users")
def create_user(body: UserBody, db: Session = Depends(get_db), user: User = Depends(require("admin"))) -> dict:
    if body.role not in {"admin", "engineer", "analyst", "viewer"}:
        raise HTTPException(status_code=400, detail="invalid role")
    row = User(
        email=body.email,
        name=body.name,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(row)
    audit(db, "user.create", actor=user.email, object_type="user", object_id=body.email)
    db.commit()
    return {"email": row.email, "role": row.role}


def doctor_payload() -> dict[str, Any]:
    components = {
        "core": _ok("ok"),
        "postgres": _probe_sql(),
        "prometheus": _http(f"{settings.prometheus_url}/-/ready", "GET"),
        "alertmanager": _http(f"{settings.alertmanager_url}/-/ready", "GET"),
        "loki": _http(f"{settings.loki_url}/loki/api/v1/status/buildinfo", "GET") if settings.loki_enabled else _ok("disabled"),
        "grafana": _http("http://127.0.0.1:3000/api/health", "GET") if settings.grafana_enabled else _ok("disabled"),
        "llm": _http((settings.llm_url or "").rstrip("/") + "/models", "GET") if settings.llm_url else _ok("disabled"),
    }
    failed = [name for name, item in components.items() if item["status"] not in {"ok", "disabled"}]
    return {
        "overall": "HEALTHY" if not failed else "DEGRADED",
        "components": components,
        "failed": failed,
    }


def _ok(status: str) -> dict[str, str]:
    return {"status": status}


def _probe_sql() -> dict[str, str]:
    from app.db import SessionLocal

    try:
        db = SessionLocal()
        db.execute(Asset.__table__.select().limit(1))
        db.close()
        return _ok("ok")
    except Exception as exc:
        return {"status": "error", "why": str(exc), "fix": "Check PostgreSQL container and DATABASE_URL"}


def _http(url: str, method: str) -> dict[str, str]:
    try:
        with httpx.Client(timeout=4.0) as client:
            response = client.request(method, url)
        if response.status_code < 400:
            return _ok("ok")
        return {
            "status": "error",
            "why": f"{url} returned {response.status_code}",
            "test": f"curl -fsS {url}",
            "fix": "Check the container logs and config/forgesre.yml",
        }
    except Exception as exc:
        return {
            "status": "error",
            "why": str(exc),
            "test": f"curl -fsS {url}",
            "fix": "Confirm the service is running: ./doctor.sh",
        }


def _asset(item: Asset) -> dict[str, Any]:
    return {
        "asset_id": item.asset_id,
        "hostname": item.hostname,
        "ip": item.ip,
        "type": item.type,
        "environment": item.environment,
        "status": item.status,
        "monitoring_profile": item.monitoring_profile,
        "owner": item.owner,
    }


def _incident(item: Incident, include_evidence: bool) -> dict[str, Any]:
    latest = item.investigations[-1] if item.investigations else None
    data: dict[str, Any] = {
        "number": item.number,
        "title": item.title,
        "severity": item.severity,
        "status": item.status,
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "asset": _asset(item.asset) if item.asset else None,
        "playrule": item.playrule.name if item.playrule else None,
        "playbook": item.playbook.name if item.playbook else None,
        "timeline": item.timeline or [],
        "summary": item.summary,
        "investigation": None,
    }
    if latest:
        data["investigation"] = {
            "summary": latest.summary,
            "likely_cause": latest.likely_cause,
            "confidence": latest.confidence,
            "evidence": latest.evidence,
            "recommended_action": latest.recommended_action,
            "provider": latest.provider,
            "disclaimer": latest.disclaimer,
        }
    if include_evidence:
        data["evidence"] = [
            {"kind": ev.kind, "title": ev.title, "payload": ev.payload, "captured_at": ev.captured_at.isoformat()}
            for ev in item.evidence
        ]
        data["alert"] = item.alert_payload
    return data


def _playrule(item: Playrule) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "enabled": item.enabled,
        "severity": item.severity,
        "condition": item.condition,
        "playbook": item.playbook.name if item.playbook else None,
    }


def _playbook(item: Playbook) -> dict[str, Any]:
    return {
        "id": item.id,
        "slug": item.slug,
        "name": item.name,
        "description": item.description,
        "steps": item.steps,
    }
