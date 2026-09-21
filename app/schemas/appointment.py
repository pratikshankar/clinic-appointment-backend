"""Appointment schemas (Sections 7, 8, 26, 27).

Two shapes matter most here:

* `SlotAvailability` -- exactly the payload Section 26 specifies
  (`time`, `capacity`, `booked`, `available`).
* `BookNewPatient` -- one request that registers a first-time caller *and* books
  them, because reception is on the phone and a two-step wizard means holding
  the line.
"""

from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.enums import AppointmentAction, AppointmentStatus, Gender
from app.schemas.common import ORMModel, clean_text, normalize_mobile
from app.schemas.patient import PatientCard


# --------------------------------------------------------------------------- #
# Availability (Section 26)
# --------------------------------------------------------------------------- #
class SlotAvailability(BaseModel):
    """One bookable slot and how much room is left in it."""

    time: str = Field(description="Slot start, HH:MM")
    end_time: str
    capacity: int
    booked: int
    available: int
    shift_label: str | None = None
    #: False when the slot is in the past, so the UI can grey it out rather than
    #: hiding it -- staff still want to see the shape of the day.
    is_bookable: bool = True
    unavailable_reason: str | None = None


class DayAvailability(BaseModel):
    clinic_id: int
    clinic_name: str
    on_date: date
    day_name: str
    is_open: bool
    closed_reason: str | None = None
    slot_duration_minutes: int
    capacity_per_slot: int
    slots: list[SlotAvailability] = Field(default_factory=list)
    total_capacity: int = 0
    total_booked: int = 0
    total_available: int = 0
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #
class AppointmentSlotRequest(BaseModel):
    """The when-and-where of a booking, shared by both booking paths."""

    clinic_id: int
    appointment_date: date
    start_time: time
    chief_complaint: str | None = None
    notes: str | None = None

    @field_validator("chief_complaint", "notes")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)


class BookExistingPatient(AppointmentSlotRequest):
    """Book a patient who already has a Patient ID."""

    patient_id: int


class BookNewPatient(AppointmentSlotRequest):
    """Register a first-time caller and book them in one request.

    Only the fields reception can realistically get on a phone call. The patient
    profile is created incomplete on purpose (Section 11): the clinic finishes it
    when the patient arrives.
    """

    full_name: str = Field(min_length=2, max_length=150)
    mobile: str = Field(min_length=10, max_length=20)
    whatsapp_number: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    age: int | None = Field(default=None, ge=0, le=130)
    gender: Gender | None = None
    source_id: int | None = None

    @field_validator("mobile")
    @classmethod
    def _check_mobile(cls, value):
        normalized = normalize_mobile(value)
        if not normalized:
            raise ValueError("A mobile number is required")
        return normalized

    @field_validator("whatsapp_number")
    @classmethod
    def _check_whatsapp(cls, value):
        return normalize_mobile(value)

    @field_validator("full_name")
    @classmethod
    def _clean_name(cls, value):
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("Patient name is required")
        return " ".join(cleaned.split())


class RescheduleRequest(BaseModel):
    """Move an appointment. The original row is kept (Section 27)."""

    appointment_date: date
    start_time: time
    #: Omit to keep the same clinic.
    clinic_id: int | None = None
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)


class CancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)


class StatusChangeRequest(BaseModel):
    """Optional note attached to a confirm / check-in / complete / no-show."""

    note: str | None = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
class AppointmentHistoryEntry(ORMModel):
    id: int
    action: AppointmentAction
    old_date: date | None = None
    old_time: time | None = None
    new_date: date | None = None
    new_time: time | None = None
    old_status: AppointmentStatus | None = None
    new_status: AppointmentStatus | None = None
    reason: str | None = None
    changed_by: str | None = None
    changed_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if hasattr(value, "action"):
            return {
                "id": value.id,
                "action": value.action,
                "old_date": value.old_date,
                "old_time": value.old_time,
                "new_date": value.new_date,
                "new_time": value.new_time,
                "old_status": value.old_status,
                "new_status": value.new_status,
                "reason": value.reason,
                "changed_by": value.changed_by.full_name if value.changed_by else None,
                "changed_at": value.changed_at,
            }
        return value


class AppointmentRead(ORMModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    appointment_code: str
    clinic_id: int
    clinic_name: str | None = None
    appointment_date: date
    start_time: time
    end_time: time
    duration_minutes: int
    status: AppointmentStatus
    chief_complaint: str | None = None
    notes: str | None = None
    cancellation_reason: str | None = None
    confirmed_at: datetime | None = None
    checked_in_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    created_by: str | None = None
    #: Set on the appointment created *by* a reschedule.
    rescheduled_from_id: int | None = None
    patient: PatientCard

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if not hasattr(value, "appointment_code"):
            return value
        return {
            "id": value.id,
            "appointment_code": value.appointment_code,
            "clinic_id": value.clinic_id,
            "clinic_name": value.clinic.name if value.clinic else None,
            "appointment_date": value.appointment_date,
            "start_time": value.start_time,
            "end_time": value.end_time,
            "duration_minutes": value.duration_minutes,
            "status": value.status,
            "chief_complaint": value.chief_complaint,
            "notes": value.notes,
            "cancellation_reason": value.cancellation_reason,
            "confirmed_at": value.confirmed_at,
            "checked_in_at": value.checked_in_at,
            "completed_at": value.completed_at,
            "cancelled_at": value.cancelled_at,
            "created_at": value.created_at,
            "created_by": value.created_by.full_name if value.created_by else None,
            "rescheduled_from_id": value.rescheduled_from_id,
            "patient": value.patient,
        }


class AppointmentDetail(AppointmentRead):
    """One appointment plus its full audit trail (Section 27)."""

    history: list[AppointmentHistoryEntry] = Field(default_factory=list)


class BookingResult(BaseModel):
    """An appointment, plus anything the operator should be told about it."""

    appointment: AppointmentRead
    #: Populated when the booking also created the patient record.
    patient_created: bool = False
    warnings: list[str] = Field(default_factory=list)


class AppointmentCounters(BaseModel):
    """Tallies for the appointment list header."""

    today: int = 0
    upcoming: int = 0
    past: int = 0
    pending_today: int = 0
    checked_in: int = 0
    completed_today: int = 0
    cancelled_today: int = 0
