"""Audit logging (Section 28).

`record()` adds to the caller's session without committing, so an audit entry is
written in the same transaction as the action it describes -- an action can
never be committed without its log entry, or vice versa.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import AuditAction, AuditLog, User


def record(
    db: Session,
    *,
    action: AuditAction,
    user: User | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    clinic_id: int | None = None,
    description: str | None = None,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
    username: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user.id if user else None,
        username=username or (user.username if user else None),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        clinic_id=clinic_id,
        description=description,
        details=details,
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent")[:255] if request else None),
    )
    db.add(entry)
    return entry


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    # Respect the first hop in X-Forwarded-For when running behind a proxy.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host[:45] if request.client else None


# --------------------------------------------------------------------------- #
# Reading the log (Phase 9)
# --------------------------------------------------------------------------- #
def list_logs(
    db: Session,
    user: User,
    *,
    action: AuditAction | None = None,
    entity_type: str | None = None,
    clinic_id: int | None = None,
    username: str | None = None,
    date_from=None,
    date_to=None,
    search: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[AuditLog], int]:
    """Browse the audit trail, newest first.

    Superadmin only, enforced by the router: the log records who did what across
    every clinic, including password resets, so it is not clinic-floor reading.
    Entries are never filtered *out* by scope here -- a partial audit log is
    worse than none, because it looks complete.
    """
    from datetime import datetime, time

    from sqlalchemy import func, or_, select
    from sqlalchemy.orm import selectinload

    stmt = select(AuditLog).options(selectinload(AuditLog.user))

    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if clinic_id is not None:
        stmt = stmt.where(AuditLog.clinic_id == clinic_id)
    if username:
        stmt = stmt.where(func.lower(AuditLog.username) == username.strip().lower())
    if date_from is not None:
        stmt = stmt.where(AuditLog.created_at >= datetime.combine(date_from, time.min))
    if date_to is not None:
        stmt = stmt.where(AuditLog.created_at <= datetime.combine(date_to, time.max))
    if search:
        pattern = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(AuditLog.description).like(pattern),
                func.lower(AuditLog.username).like(pattern),
                func.lower(AuditLog.entity_type).like(pattern),
            )
        )

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .unique()
        .scalars()
        .all()
    )
    return list(rows), total
