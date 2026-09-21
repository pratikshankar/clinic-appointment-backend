"""Health and metadata endpoints (no authentication required)."""

from fastapi import APIRouter
from sqlalchemy import text

from app.auth.dependencies import DbSession
from app.config import settings

router = APIRouter(tags=["Meta"])


@router.get("/health", summary="Liveness and database connectivity check")
def health(db: DbSession):
    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:  # pragma: no cover - reported, not raised
        database_ok = False

    return {
        "status": "ok" if database_ok else "degraded",
        "app": settings.APP_NAME,
        "environment": settings.APP_ENV,
        "database": {
            "connected": database_ok,
            "dialect": "sqlite" if settings.is_sqlite else "postgresql",
        },
    }


@router.get("/meta", summary="Enum values and provider configuration for the frontend")
def meta():
    from app.models.enums import (
        AppointmentStatus,
        ClinicStatus,
        Gender,
        NotificationType,
        PaymentMethod,
        PaymentStatus,
        RoleName,
    )

    return {
        "app_name": settings.APP_NAME,
        "environment": settings.APP_ENV,
        "phase": "Phase 1 - Foundation",
        "roles": [role.value for role in RoleName],
        "clinic_statuses": [status.value for status in ClinicStatus],
        "appointment_statuses": [status.value for status in AppointmentStatus],
        "genders": [gender.value for gender in Gender],
        "payment_methods": [method.value for method in PaymentMethod],
        "payment_statuses": [status.value for status in PaymentStatus],
        "notification_types": [item.value for item in NotificationType],
        "providers": {
            "email": settings.EMAIL_PROVIDER,
            "whatsapp": settings.WHATSAPP_PROVIDER,
        },
        "defaults": {
            "slot_duration_minutes": settings.DEFAULT_SLOT_DURATION_MINUTES,
            "capacity_per_slot": settings.DEFAULT_CAPACITY_PER_SLOT,
        },
    }
