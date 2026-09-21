"""Validation of clinic configuration.

Phase 2's substance is not the CRUD, it is making invalid configuration
impossible to save -- because Phase 4's slot engine reads this configuration and
a bad shift or a misplaced break turns into unbookable or overbookable slots.

Two categories, deliberately distinguished:

* **Errors** raise `ValidationError` (HTTP 400) and block the save. These are
  configurations that are self-contradictory, e.g. overlapping shifts on the
  same day.
* **Warnings** are returned to the caller and shown in the UI, but save fine.
  These are legitimate-but-lossy choices, e.g. a slot duration that leaves a
  remainder, or lowering capacity below what is already booked.
"""

from collections import defaultdict
from datetime import date, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Appointment,
    CAPACITY_CONSUMING_STATUSES,
    Clinic,
    ClinicHoliday,
)
from app.schemas.clinic import BreakBase, WorkingHourBase
from app.services.schedule import DAY_NAMES, minutes_between, intervals_overlap
from app.utils.exceptions import ValidationError


def validate_shifts(shifts: list[WorkingHourBase]) -> None:
    """Reject overlapping or backwards shifts.

    Two shifts touching exactly (13:00-13:00) are fine; genuine overlap is not,
    because a slot would then belong to two shifts and be generated twice.
    """
    by_day: dict[int, list[WorkingHourBase]] = defaultdict(list)
    for shift in shifts:
        if shift.is_closed:
            continue
        if shift.open_time >= shift.close_time:
            raise ValidationError(
                f"{DAY_NAMES[shift.day_of_week]}: closing time must be later than "
                f"opening time ({shift.open_time:%H:%M}-{shift.close_time:%H:%M})"
            )
        by_day[shift.day_of_week].append(shift)

    for day_of_week, day_shifts in by_day.items():
        ordered = sorted(day_shifts, key=lambda item: item.open_time)
        for earlier, later in zip(ordered, ordered[1:]):
            if intervals_overlap(
                earlier.open_time, earlier.close_time, later.open_time, later.close_time
            ):
                raise ValidationError(
                    f"{DAY_NAMES[day_of_week]}: shifts overlap "
                    f"({earlier.open_time:%H:%M}-{earlier.close_time:%H:%M} and "
                    f"{later.open_time:%H:%M}-{later.close_time:%H:%M})"
                )


def validate_break_within_hours(
    clinic_break: BreakBase, shifts: list[WorkingHourBase]
) -> None:
    """A break must sit inside a working shift.

    An every-day break (``day_of_week is None``) must fit on every day that has
    working hours -- otherwise it silently does nothing on some days, which is
    the kind of quiet misconfiguration that is painful to debug later.
    """
    open_shifts = [shift for shift in shifts if not shift.is_closed]
    if not open_shifts:
        raise ValidationError(
            "Configure working hours before adding breaks -- a break must fall inside a shift"
        )

    if clinic_break.start_time >= clinic_break.end_time:
        raise ValidationError("Break end time must be later than its start time")

    target_days = (
        sorted({shift.day_of_week for shift in open_shifts})
        if clinic_break.day_of_week is None
        else [clinic_break.day_of_week]
    )

    for day_of_week in target_days:
        day_shifts = [shift for shift in open_shifts if shift.day_of_week == day_of_week]
        if not day_shifts:
            raise ValidationError(
                f"{DAY_NAMES[day_of_week]} has no working hours, so a break cannot apply to it"
            )
        fits = any(
            shift.open_time <= clinic_break.start_time
            and clinic_break.end_time <= shift.close_time
            for shift in day_shifts
        )
        if not fits:
            windows = ", ".join(
                f"{shift.open_time:%H:%M}-{shift.close_time:%H:%M}" for shift in day_shifts
            )
            scope = (
                "applies to every working day, but "
                if clinic_break.day_of_week is None
                else ""
            )
            raise ValidationError(
                f"Break {clinic_break.start_time:%H:%M}-{clinic_break.end_time:%H:%M} "
                f"{scope}does not fit inside {DAY_NAMES[day_of_week]}'s working hours ({windows})"
            )


def slot_duration_warnings(shifts: list[WorkingHourBase], slot_duration: int) -> list[str]:
    """Report shifts that the slot duration does not divide evenly.

    Not an error: a trailing 15 minutes is simply not bookable, which is often
    an acceptable trade-off. But it should be visible rather than silent.
    """
    warnings: list[str] = []
    for shift in shifts:
        if shift.is_closed:
            continue
        span = minutes_between(shift.open_time, shift.close_time)
        if span < slot_duration:
            warnings.append(
                f"{DAY_NAMES[shift.day_of_week]} {shift.open_time:%H:%M}-"
                f"{shift.close_time:%H:%M} is shorter than one {slot_duration}-minute slot, "
                "so it produces no bookable time"
            )
        elif span % slot_duration:
            warnings.append(
                f"{DAY_NAMES[shift.day_of_week]} {shift.open_time:%H:%M}-"
                f"{shift.close_time:%H:%M}: {span % slot_duration} minute(s) unusable after "
                f"{span // slot_duration} slot(s) of {slot_duration} minutes"
            )
    return warnings


def overbooked_slots(
    db: Session, clinic_id: int, new_capacity: int, from_date: date | None = None
) -> list[dict]:
    """Future slots that already hold more bookings than `new_capacity`.

    Returned as warnings, not errors: an appointment a patient was already
    promised is never invalidated by a configuration change. Those slots simply
    accept no new bookings until they fall below the new capacity.
    """
    from_date = from_date or date.today()
    rows = db.execute(
        select(
            Appointment.appointment_date,
            Appointment.start_time,
            func.count().label("booked"),
        )
        .where(
            Appointment.clinic_id == clinic_id,
            Appointment.appointment_date >= from_date,
            Appointment.status.in_(CAPACITY_CONSUMING_STATUSES),
        )
        .group_by(Appointment.appointment_date, Appointment.start_time)
        .having(func.count() > new_capacity)
        .order_by(Appointment.appointment_date, Appointment.start_time)
    ).all()

    return [
        {
            "date": on_date.isoformat() if hasattr(on_date, "isoformat") else str(on_date),
            "time": start.strftime("%H:%M") if hasattr(start, "strftime") else str(start),
            "booked": booked,
            "capacity": new_capacity,
        }
        for on_date, start, booked in rows
    ]


def future_appointment_count(db: Session, clinic_id: int, from_date: date | None = None) -> int:
    """Appointments still ahead of us at a clinic, used when deactivating it."""
    from_date = from_date or date.today()
    return db.execute(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.clinic_id == clinic_id,
            Appointment.appointment_date >= from_date,
            Appointment.status.in_(CAPACITY_CONSUMING_STATUSES),
        )
    ).scalar_one()


def assert_holiday_not_duplicated(
    db: Session, holiday_date: date, clinic_id: int | None
) -> None:
    """Guard the UNIQUE(clinic_id, holiday_date) constraint with a clear message."""
    existing = db.execute(
        select(ClinicHoliday).where(
            ClinicHoliday.holiday_date == holiday_date,
            ClinicHoliday.clinic_id.is_(None) if clinic_id is None else ClinicHoliday.clinic_id == clinic_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        scope = "chain-wide" if clinic_id is None else "this clinic"
        raise ValidationError(
            f"A {scope} holiday already exists on {holiday_date:%d-%b-%Y}"
        )


def shifts_from_models(clinic: Clinic) -> list[WorkingHourBase]:
    """Adapt stored working hours to the schema the validators accept."""
    return [
        WorkingHourBase(
            day_of_week=hour.day_of_week,
            open_time=hour.open_time,
            close_time=hour.close_time,
            # Coerced because a column default is not applied until flush.
            is_closed=bool(hour.is_closed),
            label=hour.label,
        )
        for hour in clinic.working_hours
    ]
