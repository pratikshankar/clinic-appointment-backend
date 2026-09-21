"""Clinic notification schemas (Section 15)."""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.enums import NotificationType
from app.schemas.common import ORMModel


class NotificationRead(ORMModel):
    id: int
    notification_type: NotificationType
    title: str
    message: str
    clinic_id: int | None = None
    clinic_name: str | None = None
    appointment_id: int | None = None
    patient_id: int | None = None
    #: Structured detail the card renders: patient, time, clinic, reason.
    payload: dict | None = None
    is_acknowledged: bool = False
    acknowledged_at: datetime | None = None
    #: True until someone acknowledges it; drives the one-time sound.
    requires_sound_alert: bool = True
    created_at: datetime
    #: Who acknowledged it, for the "Acknowledged by Priya" line.
    acknowledged_by: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if not hasattr(value, "notification_type"):
            return value
        return {
            "id": value.id,
            "notification_type": value.notification_type,
            "title": value.title,
            "message": value.message,
            "clinic_id": value.clinic_id,
            "clinic_name": value.clinic.name if value.clinic else None,
            "appointment_id": value.appointment_id,
            "patient_id": value.patient_id,
            "payload": value.payload,
            "is_acknowledged": value.is_acknowledged,
            "acknowledged_at": value.acknowledged_at,
            "requires_sound_alert": value.requires_sound_alert,
            "created_at": value.created_at,
            "acknowledged_by": [
                ack.user.full_name for ack in value.acknowledgements if ack.user
            ],
        }


class LatestNotification(BaseModel):
    """The newest unacknowledged item, flattened for the floating card.

    Carried on the counters response rather than fetched separately: the card
    polls every 15 seconds and a second round trip for one row would double the
    traffic for no benefit.
    """

    id: int
    notification_type: NotificationType
    title: str
    patient_name: str | None = None
    clinic_name: str | None = None
    date: str | None = None
    time: str | None = None
    created_at: datetime


class NotificationCounters(BaseModel):
    """Polled every few seconds, so it stays deliberately small."""

    unacknowledged: int = 0
    total: int = 0
    #: Newest unacknowledged item, or None when there is nothing outstanding.
    latest: LatestNotification | None = None
    #: Highest notification id the caller can see. The client compares this
    #: against what it last saw to decide whether anything is genuinely new,
    #: which is cheaper and more reliable than diffing the list.
    latest_id: int | None = None
    #: False for Admin/Superadmin: they get a chain-wide feed for oversight, but
    #: no badge and no sound. An alert that fires for twelve clinics at once is
    #: not an alert anyone can act on.
    alerts_enabled: bool = True
