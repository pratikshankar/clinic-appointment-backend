"""Clinic notification endpoints (Section 15)."""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.schemas.common import Message, Page
from app.schemas.notification import NotificationCounters, NotificationRead
from app.services import notification_service

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", response_model=Page[NotificationRead], summary="Notifications you can see")
def list_notifications(
    db: DbSession,
    current_user: ClinicStaffUser,
    unacknowledged_only: bool = False,
    clinic_id: int | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
):
    """Unacknowledged first, then newest first — what needs action is at the top.

    Clinic Users see their own clinics; Admin and Superadmin see the chain."""
    items, total = notification_service.list_notifications(
        db,
        current_user,
        unacknowledged_only=unacknowledged_only,
        clinic_id=clinic_id,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[NotificationRead](
        items=[NotificationRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/counters",
    response_model=NotificationCounters,
    summary="Unread count for the badge (polled)",
)
def notification_counters(db: DbSession, current_user: ClinicStaffUser):
    """Deliberately small: the clinic dashboard polls this every few seconds."""
    return NotificationCounters(**notification_service.counters(db, current_user))


@router.post(
    "/acknowledge-all",
    response_model=Message,
    summary="Acknowledge everything currently unread",
)
def acknowledge_all(request: Request, db: DbSession, current_user: ClinicStaffUser):
    count = notification_service.acknowledge_all(db, current_user, request=request)
    return Message(message=f"Acknowledged {count} notification(s)")


@router.post(
    "/{notification_id}/acknowledge",
    response_model=NotificationRead,
    summary="Acknowledge a notification",
)
def acknowledge(
    notification_id: int, request: Request, db: DbSession, current_user: ClinicStaffUser
):
    """Idempotent — acknowledging twice is a no-op, not an error."""
    notification = notification_service.acknowledge(
        db, current_user, notification_id, request=request
    )
    return NotificationRead.model_validate(notification)
