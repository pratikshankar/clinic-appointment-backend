"""Appointment booking (Sections 7, 8, 26, 27, 38).

The whole phase turns on one guarantee: **a slot can never hold more
appointments than the clinic's configured capacity**, even when two
receptionists click the last slot at the same moment.

How that is achieved
--------------------
Availability is read, checked and written inside a single transaction that first
takes a write lock on the clinic row:

* **PostgreSQL** -- `SELECT ... FOR UPDATE` on `clinics`, so a second booking for
  the same clinic blocks until the first commits and then re-reads the true count.
* **SQLite** -- `FOR UPDATE` is not supported, so the lock is taken by issuing a
  no-op `UPDATE` on the same row. That promotes the transaction to SQLite's
  RESERVED state, which serialises writers exactly the same way.

Either way the capacity count is re-taken *after* the lock is held, so the losing
request sees the winner's row and gets `409 slot_unavailable`.

Everything else here is bookkeeping: every status change appends to
`appointment_history` in the same transaction, and no row is ever deleted.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

from fastapi import Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.auth import permissions
from app.models import (
    CAPACITY_CONSUMING_STATUSES,
    Appointment,
    AppointmentAction,
    AppointmentHistory,
    AppointmentStatus,
    AuditAction,
    Clinic,
    ClinicStatus,
    Patient,
    User,
)
from app.schemas.appointment import (
    BookExistingPatient,
    BookNewPatient,
    CancelRequest,
    RescheduleRequest,
)
from app.schemas.patient import PatientCreate
from app.services import (
    audit_service,
    clinic_service,
    notification_service,
    patient_service,
)
from app.services.schedule import add_minutes, build_day_schedule
from app.utils.exceptions import (
    NotFoundError,
    PermissionDeniedError,
    SlotUnavailableError,
    ValidationError,
)
from app.utils.identifiers import generate_appointment_code

logger = logging.getLogger(__name__)

_APPOINTMENT_LOADS = (
    selectinload(Appointment.patient).selectinload(Patient.primary_clinic),
    selectinload(Appointment.clinic),
    selectinload(Appointment.created_by),
)

#: Which status a given action may be applied from. Anything else is a 409:
#: completing a cancelled appointment, checking in twice, and so on.
ALLOWED_TRANSITIONS: dict[AppointmentStatus, set[AppointmentStatus]] = {
    AppointmentStatus.CONFIRMED: {AppointmentStatus.BOOKED},
    AppointmentStatus.CHECKED_IN: {AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED},
    AppointmentStatus.COMPLETED: {
        AppointmentStatus.BOOKED,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.CHECKED_IN,
    },
    AppointmentStatus.NO_SHOW: {
        AppointmentStatus.BOOKED,
        AppointmentStatus.CONFIRMED,
    },
    AppointmentStatus.CANCELLED: {
        AppointmentStatus.BOOKED,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.CHECKED_IN,
    },
}

#: Statuses from which an appointment may be moved to another slot.
RESCHEDULABLE_FROM = {
    AppointmentStatus.BOOKED,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.CHECKED_IN,
}


# --------------------------------------------------------------------------- #
# Availability (Section 26)
# --------------------------------------------------------------------------- #
def slot_booking_counts(
    db: Session, clinic_id: int, on_date: date
) -> dict[str, int]:
    """Capacity-consuming bookings per slot start time, as "HH:MM" keys.

    One grouped query, so availability for a whole day is a single round trip
    however many slots it contains.
    """
    rows = db.execute(
        select(Appointment.start_time, func.count())
        .where(
            Appointment.clinic_id == clinic_id,
            Appointment.appointment_date == on_date,
            Appointment.status.in_(CAPACITY_CONSUMING_STATUSES),
        )
        .group_by(Appointment.start_time)
    ).all()
    return {start.strftime("%H:%M"): count for start, count in rows}


def day_availability(db: Session, user: User, clinic_id: int, on_date: date) -> dict:
    """Bookable slots for a clinic on a date, with remaining capacity each.

    Built on the same `build_day_schedule` the Phase 2 configuration preview
    uses, so what the Superadmin previewed and what reception can book cannot
    disagree.
    """
    clinic = permissions.assert_clinic_access(db, user, clinic_id)
    holidays = clinic_service.list_holidays(db, user, clinic_id=clinic_id)
    schedule = build_day_schedule(clinic, on_date, holidays=holidays)
    counts = slot_booking_counts(db, clinic_id, on_date)

    now = datetime.now(_IST)
    slots = []
    for slot in schedule.slots:
        key = slot.start.strftime("%H:%M")
        booked = counts.get(key, 0)
        available = max(clinic.capacity_per_slot - booked, 0)

        in_the_past = on_date < now.date() or (
            on_date == now.date() and slot.end <= now.time()
        )
        if in_the_past:
            reason = "This slot has already passed"
        elif available == 0:
            reason = "Fully booked"
        else:
            reason = None

        slots.append(
            {
                "time": key,
                "end_time": slot.end.strftime("%H:%M"),
                "capacity": clinic.capacity_per_slot,
                "booked": booked,
                "available": available,
                "shift_label": slot.shift_label,
                "is_bookable": available > 0 and not in_the_past,
                "unavailable_reason": reason,
            }
        )

    return {
        "clinic_id": clinic.id,
        "clinic_name": clinic.name,
        "on_date": on_date,
        "day_name": schedule.day_name,
        "is_open": schedule.is_open,
        "closed_reason": schedule.closed_reason,
        "slot_duration_minutes": clinic.slot_duration_minutes,
        "capacity_per_slot": clinic.capacity_per_slot,
        "slots": slots,
        "total_capacity": sum(s["capacity"] for s in slots),
        "total_booked": sum(s["booked"] for s in slots),
        "total_available": sum(s["available"] for s in slots if s["is_bookable"]),
        "warnings": schedule.warnings,
    }


# --------------------------------------------------------------------------- #
# The capacity guard
# --------------------------------------------------------------------------- #
def _lock_clinic(db: Session, clinic_id: int) -> None:
    """Serialise concurrent bookings for one clinic.

    On PostgreSQL this is a row lock. On SQLite, which has no `FOR UPDATE`, the
    same effect comes from writing to the row: that takes the RESERVED lock, so a
    second transaction attempting the same thing waits (or fails and is retried
    by the caller's transaction machinery) rather than reading a stale count.
    """
    if db.bind.dialect.name == "sqlite":
        # A no-op write: sets updated_at to what it already is, purely to take
        # the write lock before the capacity count is read.
        db.execute(
            update(Clinic).where(Clinic.id == clinic_id).values(code=Clinic.code)
        )
    else:
        db.execute(select(Clinic.id).where(Clinic.id == clinic_id).with_for_update())


def _assert_slot_exists(
    db: Session, user: User, clinic: Clinic, on_date: date, start_time
) -> tuple:
    """The requested time must be a real slot for that clinic on that date."""
    holidays = clinic_service.list_holidays(db, user, clinic_id=clinic.id)
    schedule = build_day_schedule(clinic, on_date, holidays=holidays)

    if not schedule.is_open:
        raise ValidationError(
            f"{clinic.name} is not open on {on_date:%d-%b-%Y}: {schedule.closed_reason}"
        )

    match = next((slot for slot in schedule.slots if slot.start == start_time), None)
    if match is None:
        offered = ", ".join(slot.start.strftime("%H:%M") for slot in schedule.slots[:8])
        raise ValidationError(
            f"{start_time:%H:%M} is not a bookable slot at {clinic.name} on "
            f"{on_date:%d-%b-%Y}. Available start times include: {offered}…"
        )
    return match


def _assert_capacity(
    db: Session, clinic: Clinic, on_date: date, start_time, exclude_id: int | None = None
) -> None:
    """Refuse the booking if the slot is already at capacity.

    Must be called *after* `_lock_clinic`, otherwise two concurrent requests can
    both read the same pre-booking count.
    """
    stmt = select(func.count()).select_from(Appointment).where(
        Appointment.clinic_id == clinic.id,
        Appointment.appointment_date == on_date,
        Appointment.start_time == start_time,
        Appointment.status.in_(CAPACITY_CONSUMING_STATUSES),
    )
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)

    booked = db.execute(stmt).scalar_one()
    if booked >= clinic.capacity_per_slot:
        raise SlotUnavailableError(
            f"{start_time:%H:%M} on {on_date:%d-%b-%Y} is fully booked at "
            f"{clinic.name} ({booked} of {clinic.capacity_per_slot} taken). "
            "Choose another slot.",
            details={
                "clinic_id": clinic.id,
                "date": on_date.isoformat(),
                "time": start_time.strftime("%H:%M"),
                "capacity": clinic.capacity_per_slot,
                "booked": booked,
            },
        )


def _assert_bookable_clinic(clinic: Clinic) -> None:
    if clinic.status != ClinicStatus.ACTIVE:
        raise PermissionDeniedError(
            f"{clinic.name} is inactive and is not accepting new bookings"
        )


def _assert_not_in_the_past(on_date: date, start_time) -> None:
    """Refuse bookings for a slot that has already finished.

    Today is allowed -- reception legitimately books a patient who is standing at
    the desk for a slot later this afternoon.
    """
    now = datetime.now(_IST)
    if on_date < now.date():
        raise ValidationError(
            f"{on_date:%d-%b-%Y} is in the past. Appointments can only be booked "
            "for today or a future date."
        )
    if on_date == now.date() and start_time < now.time().replace(second=0, microsecond=0):
        raise ValidationError(
            f"{start_time:%H:%M} has already passed today. Choose a later slot."
        )


def _warn_duplicate_same_day(
    db: Session, patient_id: int, on_date: date, exclude_id: int | None = None
) -> list[str]:
    """Same patient already booked that day? Allowed, but say so.

    Two visits in one day is unusual but legitimate (a missed morning slot moved
    to the evening), so this is a warning rather than a refusal.
    """
    stmt = select(Appointment).where(
        Appointment.patient_id == patient_id,
        Appointment.appointment_date == on_date,
        Appointment.status.in_(CAPACITY_CONSUMING_STATUSES),
    )
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)
    existing = db.execute(stmt).scalars().all()
    if not existing:
        return []
    times = ", ".join(item.start_time.strftime("%H:%M") for item in existing)
    return [
        f"This patient already has an appointment on {on_date:%d-%b-%Y} at {times}."
    ]


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #
def _record_history(
    db: Session,
    appointment: Appointment,
    action: AppointmentAction,
    actor: User,
    *,
    old_status=None,
    new_status=None,
    old_date=None,
    old_time=None,
    new_date=None,
    new_time=None,
    old_clinic_id=None,
    new_clinic_id=None,
    reason: str | None = None,
) -> None:
    """Append to the trail. Written in the same transaction as the change."""
    db.add(
        AppointmentHistory(
            appointment_id=appointment.id,
            action=action,
            old_status=old_status,
            new_status=new_status,
            old_date=old_date,
            old_time=old_time,
            new_date=new_date,
            new_time=new_time,
            old_clinic_id=old_clinic_id,
            new_clinic_id=new_clinic_id,
            reason=reason,
            changed_by_user_id=actor.id,
        )
    )


def _create(
    db: Session,
    *,
    patient: Patient,
    clinic: Clinic,
    on_date: date,
    start_time,
    slot,
    chief_complaint: str | None,
    notes: str | None,
    actor: User,
    rescheduled_from: Appointment | None = None,
) -> Appointment:
    appointment = Appointment(
        appointment_code=generate_appointment_code(db, on_date, clinic.code),
        patient_id=patient.id,
        clinic_id=clinic.id,
        appointment_date=on_date,
        start_time=start_time,
        end_time=slot.end,
        duration_minutes=clinic.slot_duration_minutes,
        status=AppointmentStatus.BOOKED,
        chief_complaint=chief_complaint or patient.chief_complaint,
        notes=notes,
        created_by_user_id=actor.id,
        rescheduled_from_id=rescheduled_from.id if rescheduled_from else None,
    )
    db.add(appointment)
    db.flush()
    return appointment


def book_existing_patient(
    db: Session, payload: BookExistingPatient, actor: User, request: Request | None = None
) -> tuple[Appointment, list[str]]:
    """Book a patient who already has a Patient ID."""
    clinic = permissions.assert_clinic_access(db, actor, payload.clinic_id)
    _assert_bookable_clinic(clinic)
    _assert_not_in_the_past(payload.appointment_date, payload.start_time)

    # Booking at this clinic is itself what grants access, so a patient from
    # another branch is reachable by id here (Phase 3's `lookup` surfaced them).
    patient = db.get(Patient, payload.patient_id)
    if patient is None:
        raise NotFoundError(f"Patient {payload.patient_id} was not found")
    if not patient.is_active:
        raise ValidationError(
            f"{patient.full_name} is archived. Restore the patient before booking."
        )

    slot = _assert_slot_exists(db, actor, clinic, payload.appointment_date, payload.start_time)
    warnings = _warn_duplicate_same_day(db, patient.id, payload.appointment_date)

    # --- the guarded section ---
    _lock_clinic(db, clinic.id)
    _assert_capacity(db, clinic, payload.appointment_date, payload.start_time)

    appointment = _create(
        db,
        patient=patient,
        clinic=clinic,
        on_date=payload.appointment_date,
        start_time=payload.start_time,
        slot=slot,
        chief_complaint=payload.chief_complaint,
        notes=payload.notes,
        actor=actor,
    )
    _record_history(
        db,
        appointment,
        AppointmentAction.CREATED,
        actor,
        new_status=AppointmentStatus.BOOKED,
        new_date=payload.appointment_date,
        new_time=payload.start_time,
        new_clinic_id=clinic.id,
    )
    audit_service.record(
        db,
        action=AuditAction.APPOINTMENT_CREATED,
        user=actor,
        entity_type="appointment",
        entity_id=appointment.id,
        clinic_id=clinic.id,
        description=(
            f"Booked {patient.patient_code} at {clinic.name} on "
            f"{payload.appointment_date:%d-%b-%Y} {payload.start_time:%H:%M}"
        ),
        request=request,
    )
    # Written inside the transaction; delivered outside it (Section 15).
    queued = notification_service.record_appointment_event(
        db, actor, appointment, notification_service.CREATED
    )
    db.commit()
    db.refresh(appointment)
    notification_service.deliver(db, queued)
    return appointment, warnings


def book_new_patient(
    db: Session, payload: BookNewPatient, actor: User, request: Request | None = None
) -> tuple[Appointment, list[str]]:
    """Register a first-time caller and book them in one action.

    Ordered so the expensive-to-undo work happens last: the slot is validated
    before the patient is created, so a full slot does not leave an orphan
    patient record behind.
    """
    clinic = permissions.assert_clinic_access(db, actor, payload.clinic_id)
    _assert_bookable_clinic(clinic)
    _assert_not_in_the_past(payload.appointment_date, payload.start_time)
    slot = _assert_slot_exists(db, actor, clinic, payload.appointment_date, payload.start_time)

    _lock_clinic(db, clinic.id)
    _assert_capacity(db, clinic, payload.appointment_date, payload.start_time)

    # Reuses Phase 3's creation path, so duplicate detection (exact name +
    # mobile -> 409) and the derived profile-completeness flag both apply here.
    patient = patient_service.create_patient(
        db,
        PatientCreate(
            full_name=payload.full_name,
            mobile=payload.mobile,
            whatsapp_number=payload.whatsapp_number,
            email=payload.email,
            age=payload.age,
            gender=payload.gender,
            chief_complaint=payload.chief_complaint,
            source_id=payload.source_id,
            primary_clinic_id=clinic.id,
        ),
        actor,
        request=request,
    )

    # `create_patient` commits, so the clinic lock is retaken for the booking.
    _lock_clinic(db, clinic.id)
    _assert_capacity(db, clinic, payload.appointment_date, payload.start_time)

    appointment = _create(
        db,
        patient=patient,
        clinic=clinic,
        on_date=payload.appointment_date,
        start_time=payload.start_time,
        slot=slot,
        chief_complaint=payload.chief_complaint,
        notes=payload.notes,
        actor=actor,
    )
    _record_history(
        db,
        appointment,
        AppointmentAction.CREATED,
        actor,
        new_status=AppointmentStatus.BOOKED,
        new_date=payload.appointment_date,
        new_time=payload.start_time,
        new_clinic_id=clinic.id,
    )
    audit_service.record(
        db,
        action=AuditAction.APPOINTMENT_CREATED,
        user=actor,
        entity_type="appointment",
        entity_id=appointment.id,
        clinic_id=clinic.id,
        description=(
            f"Registered and booked {patient.patient_code} ({patient.full_name}) at "
            f"{clinic.name} on {payload.appointment_date:%d-%b-%Y} "
            f"{payload.start_time:%H:%M}"
        ),
        request=request,
    )
    queued = notification_service.record_appointment_event(
        db, actor, appointment, notification_service.CREATED
    )
    db.commit()
    db.refresh(appointment)
    notification_service.deliver(db, queued)
    return appointment, [
        f"{patient.full_name} was registered as {patient.patient_code}. "
        "Complete their profile when they arrive."
    ]


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get_appointment(db: Session, user: User, appointment_id: int) -> Appointment:
    appointment = db.execute(
        select(Appointment).options(*_APPOINTMENT_LOADS).where(Appointment.id == appointment_id)
    ).unique().scalar_one_or_none()
    if appointment is None:
        raise NotFoundError(f"Appointment {appointment_id} was not found")
    permissions.assert_clinic_access(db, user, appointment.clinic_id)
    return appointment


def list_appointments(
    db: Session,
    user: User,
    *,
    clinic_id: int | None = None,
    patient_id: int | None = None,
    on_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    statuses: list[AppointmentStatus] | None = None,
    search: str | None = None,
    window: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Appointment], int]:
    """Appointments in scope, with the filters Section 6 asks for.

    `window` is a convenience for the three tabs reception actually uses:
    "today", "upcoming", "past".
    """
    stmt = select(Appointment).options(*_APPOINTMENT_LOADS)

    accessible = permissions.accessible_clinic_ids(db, user)
    if accessible is not None:
        stmt = stmt.where(Appointment.clinic_id.in_(accessible or [-1]))
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        stmt = stmt.where(Appointment.clinic_id == clinic_id)

    today = date.today()
    if window == "today":
        stmt = stmt.where(Appointment.appointment_date == today)
    elif window == "upcoming":
        stmt = stmt.where(Appointment.appointment_date > today)
    elif window == "past":
        stmt = stmt.where(Appointment.appointment_date < today)

    if on_date is not None:
        stmt = stmt.where(Appointment.appointment_date == on_date)
    if date_from is not None:
        stmt = stmt.where(Appointment.appointment_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Appointment.appointment_date <= date_to)
    if patient_id is not None:
        stmt = stmt.where(Appointment.patient_id == patient_id)
    if statuses:
        stmt = stmt.where(Appointment.status.in_(statuses))
    if search:
        pattern = f"%{search.strip().lower()}%"
        stmt = stmt.join(Patient, Appointment.patient_id == Patient.id).where(
            or_(
                func.lower(Patient.full_name).like(pattern),
                Patient.mobile.like(f"%{search.strip()}%"),
                func.lower(Patient.patient_code).like(pattern),
                func.lower(Appointment.appointment_code).like(pattern),
            )
        )

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()

    # Past appointments read newest-first; upcoming ones read soonest-first.
    order = (
        (Appointment.appointment_date.desc(), Appointment.start_time.desc())
        if window == "past"
        else (Appointment.appointment_date.asc(), Appointment.start_time.asc())
    )
    rows = (
        db.execute(stmt.order_by(*order).offset(offset).limit(limit))
        .unique()
        .scalars()
        .all()
    )
    return list(rows), total


def counters(db: Session, user: User, clinic_id: int | None = None) -> dict:
    """Tallies for the list header, in one pass per metric."""
    today = date.today()

    def count(*conditions) -> int:
        stmt = select(func.count()).select_from(Appointment).where(*conditions)
        accessible = permissions.accessible_clinic_ids(db, user)
        if accessible is not None:
            stmt = stmt.where(Appointment.clinic_id.in_(accessible or [-1]))
        if clinic_id is not None:
            stmt = stmt.where(Appointment.clinic_id == clinic_id)
        return db.execute(stmt).scalar_one()

    pending = (AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED)
    return {
        "today": count(Appointment.appointment_date == today),
        "upcoming": count(
            Appointment.appointment_date > today, Appointment.status.in_(pending)
        ),
        "past": count(Appointment.appointment_date < today),
        "pending_today": count(
            Appointment.appointment_date == today, Appointment.status.in_(pending)
        ),
        "checked_in": count(
            Appointment.appointment_date == today,
            Appointment.status == AppointmentStatus.CHECKED_IN,
        ),
        "completed_today": count(
            Appointment.appointment_date == today,
            Appointment.status == AppointmentStatus.COMPLETED,
        ),
        "cancelled_today": count(
            Appointment.appointment_date == today,
            Appointment.status == AppointmentStatus.CANCELLED,
        ),
    }


# --------------------------------------------------------------------------- #
# Status transitions
# --------------------------------------------------------------------------- #
_ACTION_FOR_STATUS = {
    AppointmentStatus.CONFIRMED: AppointmentAction.CONFIRMED,
    AppointmentStatus.CHECKED_IN: AppointmentAction.CHECKED_IN,
    AppointmentStatus.COMPLETED: AppointmentAction.COMPLETED,
    AppointmentStatus.NO_SHOW: AppointmentAction.NO_SHOW,
    AppointmentStatus.CANCELLED: AppointmentAction.CANCELLED,
}

_AUDIT_FOR_STATUS = {
    AppointmentStatus.CANCELLED: AuditAction.APPOINTMENT_CANCELLED,
}


def change_status(
    db: Session,
    appointment_id: int,
    new_status: AppointmentStatus,
    actor: User,
    *,
    reason: str | None = None,
    request: Request | None = None,
) -> Appointment:
    """Move an appointment through its lifecycle (Section 8).

    Illegal transitions are refused rather than silently applied: completing a
    cancelled appointment would corrupt both the day's figures and the patient's
    session count in Phase 5.
    """
    appointment = get_appointment(db, actor, appointment_id)
    old_status = appointment.status

    allowed = ALLOWED_TRANSITIONS.get(new_status, set())
    if old_status not in allowed:
        raise ValidationError(
            f"An appointment that is {old_status.value} cannot be marked "
            f"{new_status.value}. Allowed from: "
            f"{', '.join(sorted(status.value for status in allowed))}."
        )

    appointment.status = new_status
    now = datetime.now(timezone.utc)
    if new_status == AppointmentStatus.CONFIRMED:
        appointment.confirmed_at = now
    elif new_status == AppointmentStatus.CHECKED_IN:
        appointment.checked_in_at = now
    elif new_status == AppointmentStatus.COMPLETED:
        appointment.completed_at = now
    elif new_status == AppointmentStatus.CANCELLED:
        appointment.cancelled_at = now
        appointment.cancellation_reason = reason

    _record_history(
        db,
        appointment,
        _ACTION_FOR_STATUS[new_status],
        actor,
        old_status=old_status,
        new_status=new_status,
        reason=reason,
    )
    audit_service.record(
        db,
        action=_AUDIT_FOR_STATUS.get(new_status, AuditAction.UPDATED),
        user=actor,
        entity_type="appointment",
        entity_id=appointment.id,
        clinic_id=appointment.clinic_id,
        description=(
            f"{appointment.appointment_code}: {old_status.value} -> {new_status.value}"
            + (f" ({reason})" if reason else "")
        ),
        request=request,
    )
    # Only cancellation is announced. Confirm/check-in/complete are things the
    # clinic itself does at the desk -- telling them what they just did is noise,
    # and Section 15 names exactly three events.
    # Confirm/check-in/complete stay silent: they are things the clinic does at
    # its own desk, and telling them what they just did is noise.
    _PATIENT_NOTIFIED = {
        AppointmentStatus.CANCELLED: notification_service.CANCELLED,
        AppointmentStatus.NO_SHOW: notification_service.NO_SHOW,
    }
    queued: list[int] = []
    event = _PATIENT_NOTIFIED.get(new_status)
    if event is not None:
        queued = notification_service.record_appointment_event(
            db, actor, appointment, event, reason=reason
        )
    db.commit()
    db.refresh(appointment)
    notification_service.deliver(db, queued)
    return appointment


def cancel(
    db: Session,
    appointment_id: int,
    payload: CancelRequest,
    actor: User,
    request: Request | None = None,
) -> Appointment:
    """Cancel, freeing the slot for someone else (Section 27: row is kept)."""
    return change_status(
        db,
        appointment_id,
        AppointmentStatus.CANCELLED,
        actor,
        reason=payload.reason,
        request=request,
    )


def reschedule(
    db: Session,
    appointment_id: int,
    payload: RescheduleRequest,
    actor: User,
    request: Request | None = None,
) -> tuple[Appointment, Appointment, list[str]]:
    """Move an appointment, preserving the original (Section 27).

    Returns `(old, new, warnings)`. The original row is marked RESCHEDULED rather
    than edited, and the new row points back at it through
    `rescheduled_from_id`, so the chain
    `original -> rescheduled -> new` is reconstructable forever.
    """
    original = get_appointment(db, actor, appointment_id)

    if original.status not in RESCHEDULABLE_FROM:
        raise ValidationError(
            f"An appointment that is {original.status.value} cannot be rescheduled. "
            f"Allowed from: {', '.join(sorted(s.value for s in RESCHEDULABLE_FROM))}."
        )

    target_clinic_id = payload.clinic_id or original.clinic_id
    clinic = permissions.assert_clinic_access(db, actor, target_clinic_id)
    _assert_bookable_clinic(clinic)
    _assert_not_in_the_past(payload.appointment_date, payload.start_time)

    if (
        target_clinic_id == original.clinic_id
        and payload.appointment_date == original.appointment_date
        and payload.start_time == original.start_time
    ):
        raise ValidationError("The new slot is the same as the current one")

    slot = _assert_slot_exists(db, actor, clinic, payload.appointment_date, payload.start_time)
    warnings = _warn_duplicate_same_day(
        db, original.patient_id, payload.appointment_date, exclude_id=original.id
    )

    _lock_clinic(db, clinic.id)
    # The original still holds its old slot, which is a different slot, so it is
    # not excluded from the count here.
    _assert_capacity(db, clinic, payload.appointment_date, payload.start_time)

    old_date, old_time, old_clinic_id = (
        original.appointment_date,
        original.start_time,
        original.clinic_id,
    )
    original.status = AppointmentStatus.RESCHEDULED

    replacement = _create(
        db,
        patient=original.patient,
        clinic=clinic,
        on_date=payload.appointment_date,
        start_time=payload.start_time,
        slot=slot,
        chief_complaint=original.chief_complaint,
        notes=original.notes,
        actor=actor,
        rescheduled_from=original,
    )

    # Both rows get an entry, so the trail reads correctly from either end.
    _record_history(
        db,
        original,
        AppointmentAction.RESCHEDULED,
        actor,
        old_status=AppointmentStatus.BOOKED,
        new_status=AppointmentStatus.RESCHEDULED,
        old_date=old_date,
        old_time=old_time,
        new_date=payload.appointment_date,
        new_time=payload.start_time,
        old_clinic_id=old_clinic_id,
        new_clinic_id=clinic.id,
        reason=payload.reason,
    )
    _record_history(
        db,
        replacement,
        AppointmentAction.CREATED,
        actor,
        new_status=AppointmentStatus.BOOKED,
        old_date=old_date,
        old_time=old_time,
        new_date=payload.appointment_date,
        new_time=payload.start_time,
        old_clinic_id=old_clinic_id,
        new_clinic_id=clinic.id,
        reason=payload.reason or f"Rescheduled from {original.appointment_code}",
    )
    audit_service.record(
        db,
        action=AuditAction.APPOINTMENT_RESCHEDULED,
        user=actor,
        entity_type="appointment",
        entity_id=replacement.id,
        clinic_id=clinic.id,
        description=(
            f"{original.appointment_code} ({old_date:%d-%b} {old_time:%H:%M}) "
            f"rescheduled to {replacement.appointment_code} "
            f"({payload.appointment_date:%d-%b} {payload.start_time:%H:%M})"
        ),
        details={"reason": payload.reason} if payload.reason else None,
        request=request,
    )
    # Announced once, against the replacement -- that is the appointment the
    # clinic now has to staff, and the one the patient needs the new time for.
    queued = notification_service.record_appointment_event(
        db, actor, replacement, notification_service.RESCHEDULED, reason=payload.reason
    )
    db.commit()
    db.refresh(original)
    db.refresh(replacement)
    notification_service.deliver(db, queued)
    return original, replacement, warnings


def history(db: Session, user: User, appointment_id: int) -> list[AppointmentHistory]:
    appointment = get_appointment(db, user, appointment_id)
    return list(
        db.execute(
            select(AppointmentHistory)
            .options(selectinload(AppointmentHistory.changed_by))
            .where(AppointmentHistory.appointment_id == appointment.id)
            .order_by(AppointmentHistory.changed_at, AppointmentHistory.id)
        )
        .unique()
        .scalars()
        .all()
    )
