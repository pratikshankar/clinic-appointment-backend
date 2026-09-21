"""Audit log viewer (Section 28, Phase 9).

Superadmin only. The log records password resets, user disablement and every
financial action across every clinic; that is oversight material, not clinic-floor
reading.

Read-only by construction — there is no endpoint that edits or deletes an entry,
because an audit trail that can be edited is not one.
"""

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, model_validator

from app.auth.dependencies import DbSession, SuperadminUser
from app.models.enums import AuditAction
from app.schemas.common import ORMModel, Page
from app.services import audit_service

router = APIRouter(prefix="/audit-logs", tags=["Audit"])


class AuditLogRead(ORMModel):
    id: int
    action: AuditAction
    username: str | None = None
    full_name: str | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    clinic_id: int | None = None
    description: str | None = None
    details: dict | None = None
    ip_address: str | None = None
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if not hasattr(value, "action"):
            return value
        return {
            "id": value.id,
            "action": value.action,
            "username": value.username,
            "full_name": value.user.full_name if value.user else None,
            "entity_type": value.entity_type,
            "entity_id": value.entity_id,
            "clinic_id": value.clinic_id,
            "description": value.description,
            "details": value.details,
            "ip_address": value.ip_address,
            "created_at": value.created_at,
        }


class AuditActionList(BaseModel):
    """The action values actually present, so the filter offers real options."""

    actions: list[str]


@router.get("", response_model=Page[AuditLogRead], summary="Browse the audit trail")
def list_audit_logs(
    db: DbSession,
    current_user: SuperadminUser,
    action: AuditAction | None = None,
    entity_type: str | None = None,
    clinic_id: int | None = None,
    username: str | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """Newest first. Append-only: nothing here can change or remove an entry."""
    rows, total = audit_service.list_logs(
        db,
        current_user,
        action=action,
        entity_type=entity_type,
        clinic_id=clinic_id,
        username=username,
        date_from=date_from,
        date_to=date_to,
        search=search,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[AuditLogRead](
        items=[AuditLogRead.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/actions", response_model=AuditActionList, summary="Known action types")
def audit_actions(db: DbSession, current_user: SuperadminUser):
    return AuditActionList(actions=[item.value for item in AuditAction])
