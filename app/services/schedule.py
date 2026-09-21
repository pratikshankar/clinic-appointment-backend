"""Schedule computation from clinic configuration.

Pure, side-effect-free functions that turn a clinic's working hours, breaks,
holidays and slot duration into a list of slot times. Phase 2 uses this to let
the Superadmin *see* what their configuration produces before any patient is
booked into it; Phase 4 layers booking counts and capacity on top of the same
generator, so the two can never disagree about what a slot is.

Nothing here touches capacity or existing appointments -- that is deliberate.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.models import Clinic, ClinicBreak, ClinicHoliday, ClinicWorkingHour

DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


@dataclass(frozen=True, slots=True)
class Slot:
    start: time
    end: time
    shift_label: str | None = None


@dataclass(frozen=True, slots=True)
class DaySchedule:
    """What a clinic's configuration produces for one calendar day."""

    on_date: date
    day_of_week: int
    is_open: bool
    closed_reason: str | None
    slots: list[Slot]
    #: Configuration issues that do not prevent saving but lose bookable time,
    #: e.g. a 45-minute slot in a 4-hour shift leaves 15 unusable minutes.
    warnings: list[str]

    @property
    def day_name(self) -> str:
        return DAY_NAMES[self.day_of_week]


def minutes_between(start: time, end: time) -> int:
    reference = date(2000, 1, 1)
    return int(
        (datetime.combine(reference, end) - datetime.combine(reference, start)).total_seconds()
        // 60
    )


def add_minutes(value: time, minutes: int) -> time:
    reference = date(2000, 1, 1)
    return (datetime.combine(reference, value) + timedelta(minutes=minutes)).time()


def intervals_overlap(start_a: time, end_a: time, start_b: time, end_b: time) -> bool:
    """True when two half-open intervals intersect. Touching edges do not."""
    return start_a < end_b and start_b < end_a


def shifts_for_day(clinic: Clinic, day_of_week: int) -> list[ClinicWorkingHour]:
    return sorted(
        (
            hour
            for hour in clinic.working_hours
            if hour.day_of_week == day_of_week and not hour.is_closed
        ),
        key=lambda hour: hour.open_time,
    )


def breaks_for_day(clinic: Clinic, day_of_week: int) -> list[ClinicBreak]:
    """Breaks applying to a weekday: day-specific ones plus every-day ones."""
    return sorted(
        (
            item
            for item in clinic.breaks
            if item.day_of_week is None or item.day_of_week == day_of_week
        ),
        key=lambda item: item.start_time,
    )


def holiday_for_date(holidays: list[ClinicHoliday], on_date: date) -> ClinicHoliday | None:
    """Closure applying on a date.

    A chain-wide holiday (``clinic_id is None``) closes every clinic
    unconditionally, so it is checked first.
    """
    matches = [holiday for holiday in holidays if holiday.holiday_date == on_date]
    for holiday in matches:
        if holiday.clinic_id is None:
            return holiday
    return matches[0] if matches else None


def build_day_schedule(
    clinic: Clinic,
    on_date: date,
    holidays: list[ClinicHoliday] | None = None,
) -> DaySchedule:
    """Slots the configuration yields for `on_date`."""
    day_of_week = on_date.weekday()
    warnings: list[str] = []

    holiday = holiday_for_date(holidays or [], on_date)
    if holiday is not None:
        scope = "chain-wide holiday" if holiday.clinic_id is None else "clinic holiday"
        reason = holiday.reason or "Closed"
        return DaySchedule(
            on_date=on_date,
            day_of_week=day_of_week,
            is_open=False,
            closed_reason=f"{reason} ({scope})",
            slots=[],
            warnings=warnings,
        )

    shifts = shifts_for_day(clinic, day_of_week)
    if not shifts:
        return DaySchedule(
            on_date=on_date,
            day_of_week=day_of_week,
            is_open=False,
            closed_reason=f"No working hours configured for {DAY_NAMES[day_of_week]}",
            slots=[],
            warnings=warnings,
        )

    duration = clinic.slot_duration_minutes
    day_breaks = breaks_for_day(clinic, day_of_week)
    slots: list[Slot] = []

    for shift in shifts:
        span = minutes_between(shift.open_time, shift.close_time)
        remainder = span % duration
        if remainder:
            warnings.append(
                f"{DAY_NAMES[day_of_week]} {shift.open_time:%H:%M}-{shift.close_time:%H:%M}: "
                f"{remainder} minute(s) left over after {span // duration} slot(s) of "
                f"{duration} minutes"
            )

        start = shift.open_time
        while minutes_between(start, shift.close_time) >= duration:
            end = add_minutes(start, duration)
            blocking = next(
                (
                    item
                    for item in day_breaks
                    if intervals_overlap(start, end, item.start_time, item.end_time)
                ),
                None,
            )
            if blocking is None:
                slots.append(Slot(start=start, end=end, shift_label=shift.label))
            start = end

    if not slots:
        return DaySchedule(
            on_date=on_date,
            day_of_week=day_of_week,
            is_open=False,
            closed_reason=(
                "Working hours are configured but produce no slots -- check the slot "
                "duration and breaks"
            ),
            slots=[],
            warnings=warnings,
        )

    return DaySchedule(
        on_date=on_date,
        day_of_week=day_of_week,
        is_open=True,
        closed_reason=None,
        slots=slots,
        warnings=warnings,
    )


def weekly_slot_counts(clinic: Clinic) -> dict[int, int]:
    """Slots per weekday from the configuration alone (holidays ignored).

    Used by the configuration screen to show the effect of a change at a glance.
    """
    counts: dict[int, int] = {}
    # Any Monday works: weekday() only depends on the day of week.
    monday = date(2024, 1, 1)
    for day_of_week in range(7):
        schedule = build_day_schedule(clinic, monday + timedelta(days=day_of_week), holidays=[])
        counts[day_of_week] = len(schedule.slots)
    return counts
