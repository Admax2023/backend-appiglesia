from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, User


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, (uuid.UUID,)):
        return str(v)
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    return v


def record(
    db: Session,
    *,
    user: User | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user.id if user else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=_jsonable(details or {}),
        ip_address=ip,
    )
    db.add(entry)
    return entry


def list_logs(
    db: Session,
    *,
    offset: int,
    limit: int,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[list[AuditLog], int]:
    conds = []
    if action:
        conds.append(AuditLog.action == action)
    if entity_type:
        conds.append(AuditLog.entity_type == entity_type)
    if entity_id:
        conds.append(AuditLog.entity_id == entity_id)
    if user_id:
        conds.append(AuditLog.user_id == user_id)
    if start:
        conds.append(AuditLog.created_at >= start)
    if end:
        conds.append(AuditLog.created_at < end)
    total = db.scalar(select(func.count()).select_from(AuditLog).where(*conds)) or 0
    rows = db.scalars(
        select(AuditLog).where(*conds).order_by(AuditLog.created_at.desc(), AuditLog.id).offset(offset).limit(limit)
    ).all()
    return list(rows), total
