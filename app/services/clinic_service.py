"""Clinic reads and configuration helpers.

Phase 1 needs listing/reading (for selectors, dashboards and user assignment)
plus the creation helper the seed script uses. Full CRUD endpoints and
working-hour editing are Phase 2.
"""

from datetime import date, time

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import permissions
from app.config import settings
from app.models import (
    AuditAction,
    Clinic,
    ClinicBreak,
    ClinicHoliday,
    ClinicStatus,
    ClinicUser,
    ClinicWorkingHour,
    Role,
    RoleName,
    User,
)
from app.schemas.clinic import (
    BreakCreate,
    ClinicCreate,
    ClinicUpdate,
    HolidayCreate,
    StaffAssign,
    WorkingHourBase,
)
from app.services import audit_service, clinic_validation
from app.services.schedule import build_day_schedule, weekly_slot_counts
from app.utils.exceptions import (
    DuplicateResourceError,
    NotFoundError,
    ValidationError,
)
from app.utils.identifiers import slugify_clinic_code


def list_clinics(
    db: Session,
    user: User,
    *,
    status: ClinicStatus | None = None,
    search: str | None = None,
) -> list[Clinic]:
    """Clinics visible to `user`.

    A Clinic User only ever receives their assigned clinic here -- this is the
    backend enforcement referenced in Section 23, not a frontend filter.
    """
    stmt = (
        select(Clinic)
        .options(selectinload(Clinic.working_hours), selectinload(Clinic.breaks))
        .order_by(Clinic.name)
    )

    accessible = permissions.accessible_clinic_ids(db, user)
    if accessible is not None:
        if not accessible:
            return []
        stmt = stmt.where(Clinic.id.in_(accessible))

    if status is not None:
        stmt = stmt.where(Clinic.status == status)
    if search:
        pattern = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Clinic.name).like(pattern) | func.lower(Clinic.code).like(pattern)
        )

    return list(db.execute(stmt).unique().scalars().all())


def get_clinic(db: Session, user: User, clinic_id: int) -> Clinic:
    """Fetch one clinic, enforcing clinic-level access."""
    return permissions.assert_clinic_access(db, user, clinic_id)


def assigned_user_count(db: Session, clinic_id: int) -> int:
    return db.execute(
        select(func.count()).select_from(ClinicUser).where(ClinicUser.clinic_id == clinic_id)
    ).scalar_one()


def default_working_hours(
    open_morning: time = time(9, 0),
    close_morning: time = time(13, 0),
    open_evening: time = time(16, 0),
    close_evening: time = time(20, 0),
    working_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5),
) -> list[ClinicWorkingHour]:
    """Monday-Saturday split shifts, matching the Section 5 example."""
    hours: list[ClinicWorkingHour] = []
    for day in working_days:
        # `is_closed` is set explicitly: a column default is only applied at
        # flush, so it would still be None while the object is being validated.
        hours.append(
            ClinicWorkingHour(
                day_of_week=day,
                open_time=open_morning,
                close_time=close_morning,
                is_closed=False,
                label="Morning",
            )
        )
        hours.append(
            ClinicWorkingHour(
                day_of_week=day,
                open_time=open_evening,
                close_time=close_evening,
                is_closed=False,
                label="Evening",
            )
        )
    return hours


def create_clinic(
    db: Session,
    payload: ClinicCreate,
    actor: User | None = None,
    request: Request | None = None,
) -> Clinic:
    """Create a clinic with its working hours and breaks.

    Shared by the seed script and the Superadmin endpoint, so both go through
    the same validation.
    """
    if db.execute(
        select(Clinic).where(func.lower(Clinic.name) == payload.name.lower())
    ).scalar_one_or_none():
        raise DuplicateResourceError(f"A clinic named '{payload.name}' already exists")

    existing_codes = set(db.execute(select(Clinic.code)).scalars().all())
    code = (payload.code or slugify_clinic_code(payload.name, existing_codes)).upper()
    if code in existing_codes:
        raise DuplicateResourceError(f"Clinic code '{code}' is already in use")

    clinic = Clinic(
        name=payload.name,
        code=code,
        address=payload.address,
        location=payload.location,
        city=payload.city,
        state=payload.state,
        pin_code=payload.pin_code,
        phone=payload.phone,
        email=str(payload.email) if payload.email else None,
        status=payload.status,
        slot_duration_minutes=payload.slot_duration_minutes
        or settings.DEFAULT_SLOT_DURATION_MINUTES,
        capacity_per_slot=payload.capacity_per_slot or settings.DEFAULT_CAPACITY_PER_SLOT,
        # Branding, for a clinic trading under its own name. Listed explicitly
        # like every other field here -- omitting them meant the create form
        # accepted a trading name and invoice series and silently discarded
        # both, which surfaced as a Physiocare bill numbered INV-.
        brand_name=payload.brand_name,
        brand_tagline=payload.brand_tagline,
        logo_filename=payload.logo_filename,
        document_footer=payload.document_footer,
        bill_number_prefix=payload.bill_number_prefix,
    )

    if payload.working_hours:
        clinic_validation.validate_shifts(payload.working_hours)
        clinic.working_hours = [
            ClinicWorkingHour(
                day_of_week=item.day_of_week,
                open_time=item.open_time,
                close_time=item.close_time,
                is_closed=item.is_closed,
                label=item.label,
            )
            for item in payload.working_hours
        ]
        effective_shifts = payload.working_hours
    else:
        clinic.working_hours = default_working_hours()
        effective_shifts = clinic_validation.shifts_from_models(clinic)

    for item in payload.breaks:
        clinic_validation.validate_break_within_hours(item, effective_shifts)
    clinic.breaks = [
        ClinicBreak(
            day_of_week=item.day_of_week,
            start_time=item.start_time,
            end_time=item.end_time,
            label=item.label,
        )
        for item in payload.breaks
    ]

    db.add(clinic)
    db.flush()

    if actor is not None:
        audit_service.record(
            db,
            action=AuditAction.CREATED,
            user=actor,
            entity_type="clinic",
            entity_id=clinic.id,
            clinic_id=clinic.id,
            description=f"Created clinic '{clinic.name}' ({clinic.code})",
            details={
                "slot_duration_minutes": clinic.slot_duration_minutes,
                "capacity_per_slot": clinic.capacity_per_slot,
                "shifts": len(clinic.working_hours),
            },
            request=request,
        )
    return clinic


# --------------------------------------------------------------------------- #
# Phase 2: configuration writes (Superadmin only, enforced in the router)
# --------------------------------------------------------------------------- #
def update_clinic(
    db: Session,
    clinic_id: int,
    payload: ClinicUpdate,
    actor: User,
    request: Request | None = None,
) -> tuple[Clinic, list[str], list[dict]]:
    """Edit clinic details and appointment configuration.

    Returns `(clinic, warnings, overbooked_slots)`. Lowering capacity below what
    is already booked is allowed on purpose -- an appointment a patient was
    already promised is never invalidated by a configuration change. The
    affected slots are reported instead, and Phase 4's booking logic will refuse
    new bookings there until they fall below the new capacity.
    """
    clinic = db.get(Clinic, clinic_id)
    if clinic is None:
        raise NotFoundError(f"Clinic {clinic_id} was not found")

    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        clash = db.execute(
            select(Clinic).where(
                func.lower(Clinic.name) == data["name"].lower(), Clinic.id != clinic_id
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise DuplicateResourceError(f"A clinic named '{data['name']}' already exists")
    if "email" in data and data["email"]:
        data["email"] = str(data["email"])

    warnings: list[str] = []
    overbooked: list[dict] = []

    new_capacity = data.get("capacity_per_slot", clinic.capacity_per_slot)
    if new_capacity < clinic.capacity_per_slot:
        overbooked = clinic_validation.overbooked_slots(db, clinic_id, new_capacity)
        if overbooked:
            warnings.append(
                f"{len(overbooked)} future slot(s) already hold more than {new_capacity} "
                "appointment(s). Existing bookings are kept; those slots will accept no "
                "new bookings until they drop below the new capacity."
            )

    changed: dict[str, object] = {}
    for field, value in data.items():
        if getattr(clinic, field) != value:
            changed[field] = value
            setattr(clinic, field, value)

    # Slot duration changes can leave a remainder in existing shifts.
    if "slot_duration_minutes" in changed:
        warnings.extend(
            clinic_validation.slot_duration_warnings(
                clinic_validation.shifts_from_models(clinic), clinic.slot_duration_minutes
            )
        )

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=actor,
        entity_type="clinic",
        entity_id=clinic.id,
        clinic_id=clinic.id,
        description=f"Updated clinic '{clinic.name}'",
        details={"changed_fields": changed} if changed else None,
        request=request,
    )
    db.commit()
    db.refresh(clinic)
    return clinic, warnings, overbooked


def set_clinic_status(
    db: Session,
    clinic_id: int,
    status: ClinicStatus,
    actor: User,
    request: Request | None = None,
) -> tuple[Clinic, int]:
    """Activate or deactivate a clinic.

    Deactivation succeeds even with future appointments -- a closing clinic still
    has patients to phone -- but the count is returned so the UI can warn. The
    appointments stay visible and cancellable; no new ones will be accepted.
    """
    clinic = db.get(Clinic, clinic_id)
    if clinic is None:
        raise NotFoundError(f"Clinic {clinic_id} was not found")

    future_count = 0
    if status == ClinicStatus.INACTIVE:
        future_count = clinic_validation.future_appointment_count(db, clinic_id)

    clinic.status = status
    audit_service.record(
        db,
        action=AuditAction.ENABLED if status == ClinicStatus.ACTIVE else AuditAction.DISABLED,
        user=actor,
        entity_type="clinic",
        entity_id=clinic.id,
        clinic_id=clinic.id,
        description=(
            f"{'Activated' if status == ClinicStatus.ACTIVE else 'Deactivated'} "
            f"clinic '{clinic.name}'"
        ),
        details={"future_appointments": future_count} if future_count else None,
        request=request,
    )
    db.commit()
    db.refresh(clinic)
    return clinic, future_count


def replace_working_hours(
    db: Session,
    clinic_id: int,
    shifts: list[WorkingHourBase],
    actor: User,
    request: Request | None = None,
) -> tuple[Clinic, list[str]]:
    """Replace the whole weekly shift set atomically."""
    clinic = db.get(Clinic, clinic_id)
    if clinic is None:
        raise NotFoundError(f"Clinic {clinic_id} was not found")

    clinic_validation.validate_shifts(shifts)

    # Existing breaks must still fall inside the new hours, otherwise they would
    # silently stop applying.
    for existing_break in clinic.breaks:
        clinic_validation.validate_break_within_hours(
            BreakCreate(
                day_of_week=existing_break.day_of_week,
                start_time=existing_break.start_time,
                end_time=existing_break.end_time,
                label=existing_break.label,
            ),
            shifts,
        )

    clinic.working_hours.clear()
    db.flush()
    for shift in shifts:
        clinic.working_hours.append(
            ClinicWorkingHour(
                day_of_week=shift.day_of_week,
                open_time=shift.open_time,
                close_time=shift.close_time,
                is_closed=shift.is_closed,
                label=shift.label,
            )
        )

    warnings = clinic_validation.slot_duration_warnings(shifts, clinic.slot_duration_minutes)
    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=actor,
        entity_type="clinic_working_hours",
        entity_id=clinic.id,
        clinic_id=clinic.id,
        description=f"Updated working hours for '{clinic.name}'",
        details={"shift_count": len(shifts)},
        request=request,
    )
    db.commit()
    db.refresh(clinic)
    return clinic, warnings


def add_break(
    db: Session,
    clinic_id: int,
    payload: BreakCreate,
    actor: User,
    request: Request | None = None,
) -> ClinicBreak:
    clinic = db.get(Clinic, clinic_id)
    if clinic is None:
        raise NotFoundError(f"Clinic {clinic_id} was not found")

    clinic_validation.validate_break_within_hours(
        payload, clinic_validation.shifts_from_models(clinic)
    )

    duplicate = next(
        (
            item
            for item in clinic.breaks
            if item.day_of_week == payload.day_of_week
            and item.start_time == payload.start_time
            and item.end_time == payload.end_time
        ),
        None,
    )
    if duplicate is not None:
        raise DuplicateResourceError("An identical break already exists for this clinic")

    clinic_break = ClinicBreak(
        clinic_id=clinic_id,
        day_of_week=payload.day_of_week,
        start_time=payload.start_time,
        end_time=payload.end_time,
        label=payload.label,
    )
    db.add(clinic_break)
    audit_service.record(
        db,
        action=AuditAction.CREATED,
        user=actor,
        entity_type="clinic_break",
        entity_id=clinic_id,
        clinic_id=clinic_id,
        description=(
            f"Added break '{payload.label}' "
            f"{payload.start_time:%H:%M}-{payload.end_time:%H:%M} to '{clinic.name}'"
        ),
        request=request,
    )
    db.commit()
    db.refresh(clinic_break)
    return clinic_break


def remove_break(
    db: Session, clinic_id: int, break_id: int, actor: User, request: Request | None = None
) -> None:
    clinic_break = db.get(ClinicBreak, break_id)
    if clinic_break is None or clinic_break.clinic_id != clinic_id:
        raise NotFoundError(f"Break {break_id} was not found at this clinic")

    db.delete(clinic_break)
    audit_service.record(
        db,
        action=AuditAction.DELETED,
        user=actor,
        entity_type="clinic_break",
        entity_id=break_id,
        clinic_id=clinic_id,
        description=f"Removed break '{clinic_break.label}'",
        request=request,
    )
    db.commit()


# --------------------------------------------------------------------------- #
# Holidays
# --------------------------------------------------------------------------- #
def list_holidays(
    db: Session,
    user: User,
    clinic_id: int | None = None,
    from_date: date | None = None,
) -> list[ClinicHoliday]:
    """Holidays in scope.

    When `clinic_id` is given, chain-wide holidays are included too, because
    they close that clinic as well.
    """
    stmt = select(ClinicHoliday).order_by(ClinicHoliday.holiday_date)
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        stmt = stmt.where(
            or_(ClinicHoliday.clinic_id == clinic_id, ClinicHoliday.clinic_id.is_(None))
        )
    else:
        accessible = permissions.accessible_clinic_ids(db, user)
        if accessible is not None:
            stmt = stmt.where(
                or_(ClinicHoliday.clinic_id.in_(accessible or [-1]), ClinicHoliday.clinic_id.is_(None))
            )
    if from_date is not None:
        stmt = stmt.where(ClinicHoliday.holiday_date >= from_date)
    return list(db.execute(stmt).unique().scalars().all())


def add_holiday(
    db: Session, payload: HolidayCreate, actor: User, request: Request | None = None
) -> ClinicHoliday:
    """Create a clinic-specific or chain-wide (clinic_id=None) closure."""
    if payload.clinic_id is not None:
        clinic = db.get(Clinic, payload.clinic_id)
        if clinic is None:
            raise NotFoundError(f"Clinic {payload.clinic_id} was not found")

    clinic_validation.assert_holiday_not_duplicated(
        db, payload.holiday_date, payload.clinic_id
    )

    holiday = ClinicHoliday(
        clinic_id=payload.clinic_id,
        holiday_date=payload.holiday_date,
        reason=payload.reason,
    )
    db.add(holiday)
    audit_service.record(
        db,
        action=AuditAction.CREATED,
        user=actor,
        entity_type="clinic_holiday",
        entity_id=payload.clinic_id,
        clinic_id=payload.clinic_id,
        description=(
            f"Added {'chain-wide' if payload.clinic_id is None else 'clinic'} holiday on "
            f"{payload.holiday_date:%d-%b-%Y}"
            + (f" ({payload.reason})" if payload.reason else "")
        ),
        request=request,
    )
    db.commit()
    db.refresh(holiday)
    return holiday


def remove_holiday(
    db: Session, holiday_id: int, actor: User, request: Request | None = None
) -> None:
    holiday = db.get(ClinicHoliday, holiday_id)
    if holiday is None:
        raise NotFoundError(f"Holiday {holiday_id} was not found")

    db.delete(holiday)
    audit_service.record(
        db,
        action=AuditAction.DELETED,
        user=actor,
        entity_type="clinic_holiday",
        entity_id=holiday_id,
        clinic_id=holiday.clinic_id,
        description=f"Removed holiday on {holiday.holiday_date:%d-%b-%Y}",
        request=request,
    )
    db.commit()


# --------------------------------------------------------------------------- #
# Staff assignment
# --------------------------------------------------------------------------- #
def list_staff(db: Session, user: User, clinic_id: int) -> list[ClinicUser]:
    permissions.assert_clinic_access(db, user, clinic_id)
    return list(
        db.execute(
            select(ClinicUser)
            .where(ClinicUser.clinic_id == clinic_id)
            .options(selectinload(ClinicUser.user))
        )
        .unique()
        .scalars()
        .all()
    )


def assign_staff(
    db: Session,
    clinic_id: int,
    payload: StaffAssign,
    actor: User,
    request: Request | None = None,
) -> ClinicUser:
    """Assign a Clinic User to this clinic.

    Only CLINIC_USER accounts are assignable: Superadmin and Admin reach every
    clinic by role, and giving them a row here would imply their access is
    scoped, which it is not (Section 38).
    """
    clinic = db.get(Clinic, clinic_id)
    if clinic is None:
        raise NotFoundError(f"Clinic {clinic_id} was not found")

    target = db.get(User, payload.user_id)
    if target is None:
        raise NotFoundError(f"User {payload.user_id} was not found")
    if target.role_name != RoleName.CLINIC_USER:
        raise ValidationError(
            f"{target.role_name.value} users already have access to every clinic and "
            "cannot be assigned to one"
        )

    existing = db.execute(
        select(ClinicUser).where(
            ClinicUser.clinic_id == clinic_id, ClinicUser.user_id == payload.user_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateResourceError(
            f"{target.full_name} is already assigned to {clinic.name}"
        )

    link = ClinicUser(
        clinic_id=clinic_id,
        user_id=payload.user_id,
        # First assignment becomes the primary clinic.
        is_primary=not target.clinic_links,
        designation=payload.designation,
        assigned_by_user_id=actor.id,
    )
    db.add(link)
    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=actor,
        entity_type="clinic_user",
        entity_id=payload.user_id,
        clinic_id=clinic_id,
        description=f"Assigned '{target.username}' to clinic '{clinic.name}'",
        request=request,
    )
    db.commit()
    db.refresh(link)
    return link


def unassign_staff(
    db: Session, clinic_id: int, user_id: int, actor: User, request: Request | None = None
) -> None:
    """Remove a Clinic User from this clinic.

    Refused when it would be their last clinic: a Clinic User with no assignment
    can see nothing at all, which looks like a broken account rather than a
    deliberate state. Disable the account instead.
    """
    link = db.execute(
        select(ClinicUser).where(
            ClinicUser.clinic_id == clinic_id, ClinicUser.user_id == user_id
        )
    ).scalar_one_or_none()
    if link is None:
        raise NotFoundError("That user is not assigned to this clinic")

    target = db.get(User, user_id)
    if target is not None and len(target.clinic_links) <= 1:
        raise ValidationError(
            f"{target.full_name} would be left without any clinic. Assign them to "
            "another clinic first, or disable the account instead."
        )

    was_primary = link.is_primary
    db.delete(link)
    db.flush()

    # Keep exactly one primary assignment.
    if was_primary and target is not None:
        remaining = [item for item in target.clinic_links if item.id != link.id]
        if remaining:
            remaining[0].is_primary = True

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=actor,
        entity_type="clinic_user",
        entity_id=user_id,
        clinic_id=clinic_id,
        description=f"Unassigned user {user_id} from clinic {clinic_id}",
        request=request,
    )
    db.commit()


# --------------------------------------------------------------------------- #
# Configuration preview
# --------------------------------------------------------------------------- #
def schedule_preview(db: Session, user: User, clinic_id: int, on_date: date):
    """Slots the configuration yields for a date. No bookings are consulted."""
    clinic = permissions.assert_clinic_access(db, user, clinic_id)
    holidays = list_holidays(db, user, clinic_id=clinic_id)
    return clinic, build_day_schedule(clinic, on_date, holidays=holidays)


def configuration_summary(clinic: Clinic) -> tuple[dict[int, int], list[str]]:
    """Per-weekday slot counts plus non-blocking configuration warnings."""
    counts = weekly_slot_counts(clinic)
    warnings = clinic_validation.slot_duration_warnings(
        clinic_validation.shifts_from_models(clinic), clinic.slot_duration_minutes
    )
    if not any(counts.values()):
        warnings.append(
            "This configuration produces no bookable slots on any day of the week"
        )
    return counts, warnings
