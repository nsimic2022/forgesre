from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog


def audit(
    db: Session,
    action: str,
    actor: str = "system",
    object_type: str = "",
    object_id: str = "",
    ip: str = "",
    data: dict | None = None,
    commit: bool = False,
) -> None:
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            object_type=object_type,
            object_id=str(object_id or ""),
            ip=ip or "",
            data=data or {},
        )
    )
    if commit:
        db.commit()
