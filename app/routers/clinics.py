"""Clinic endpoints.

Reads are available to every authenticated role but are *scoped*: a Clinic User
calling `GET /clinics` receives only their own clinic, which is the specific
example called out in Section 23.

All writes are Superadmin-only (Section 6: the Admin has no system
administration rights), enforced by the `SuperadminUser` dependency rather than
by the frontend hiding buttons.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.auth.dependencies import ClinicStaffUser, DbSession, SuperadminUser
from app.models.enums import ClinicStatus
from app.schemas.clinic import (
    BreakCreate,
    BreakRead,
    ClinicCreate,
    ClinicRead,
    ClinicStaffRead,
    ClinicSummary,
    ClinicUpdate,
    ClinicWriteResult,
    SchedulePreview,
    SlotPreview,
    StaffAssign,
    WorkingHourRead,
    WorkingHoursReplace,
)
from app.schemas.common import Message
from app.services import clinic_service

router = APIRouter(prefix="/clinics", tags=["Clinics"])


def _to_read(db, clinic) -> ClinicRead:
    """Build the clinic response, including derived configuration insight."""
    payload = ClinicRead.model_validate(clinic)
    payload.assigned_user_count = clinic_service.assigned_user_count(db, clinic.id)
    counts, warnings = clinic_service.configuration_summary(clinic)
    payload.weekly_slot_counts = counts
    payload.configuration_warnings = warnings
    return payload


# --------------------------------------------------------------------------- #
# Reads (all roles, scoped)
# --------------------------------------------------------------------------- #
@router.get("", response_model=list[ClinicSummary], summary="List clinics you may access")
def list_clinics(
    db: DbSession,
    current_user: ClinicStaffUser,
    status: ClinicStatus | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
):
    clinics = clinic_service.list_clinics(db, current_user, status=status, search=search)
    return [ClinicSummary.model_validate(clinic) for clinic in clinics]


@router.get("/{clinic_id}", response_model=ClinicRead, summary="Clinic detail and configuration")
def get_clinic(clinic_id: int, db: DbSession, current_user: ClinicStaffUser):
    clinic = clinic_service.get_clinic(db, current_user, clinic_id)
    return _to_read(db, clinic)


@router.get(
    "/{clinic_id}/working-hours",
    response_model=list[WorkingHourRead],
    summary="Weekly shifts",
)
def get_working_hours(clinic_id: int, db: DbSession, current_user: ClinicStaffUser):
    clinic = clinic_service.get_clinic(db, current_user, clinic_id)
    return [
        WorkingHourRead.model_validate(hour)
        for hour in sorted(
            clinic.working_hours, key=lambda h: (h.day_of_week, h.open_time)
        )
    ]


@router.get("/{clinic_id}/breaks", response_model=list[BreakRead], summary="Recurring breaks")
def get_breaks(clinic_id: int, db: DbSession, current_user: ClinicStaffUser):
    clinic = clinic_service.get_clinic(db, current_user, clinic_id)
    return [
        BreakRead.model_validate(item)
        for item in sorted(clinic.breaks, key=lambda b: (b.day_of_week or -1, b.start_time))
    ]


@router.get(
    "/{clinic_id}/schedule-preview",
    response_model=SchedulePreview,
    summary="Slots this configuration produces for a date (no bookings involved)",
)
def get_schedule_preview(
    clinic_id: int,
    db: DbSession,
    current_user: ClinicStaffUser,
    on_date: Annotated[date | None, Query(alias="date")] = None,
):
    """Preview the effect of the current configuration.

    This is configuration verification, not availability: capacity and existing
    appointments are deliberately not consulted. Phase 4's
    `/appointments/available-slots` adds those on top of the same generator.
    """
    target = on_date or date.today()
    clinic, schedule = clinic_service.schedule_preview(db, current_user, clinic_id, target)
    return SchedulePreview(
        clinic_id=clinic.id,
        on_date=schedule.on_date,
        day_of_week=schedule.day_of_week,
        day_name=schedule.day_name,
        is_open=schedule.is_open,
        closed_reason=schedule.closed_reason,
        slot_duration_minutes=clinic.slot_duration_minutes,
        capacity_per_slot=clinic.capacity_per_slot,
        slots=[
            SlotPreview(start_time=slot.start, end_time=slot.end, shift_label=slot.shift_label)
            for slot in schedule.slots
        ],
        total_slots=len(schedule.slots),
        total_capacity=len(schedule.slots) * clinic.capacity_per_slot,
        warnings=schedule.warnings,
    )


@router.get(
    "/{clinic_id}/users",
    response_model=list[ClinicStaffRead],
    summary="Staff assigned to a clinic",
)
def get_staff(clinic_id: int, db: DbSession, current_user: ClinicStaffUser):
    links = clinic_service.list_staff(db, current_user, clinic_id)
    return [ClinicStaffRead.model_validate(link) for link in links]


# --------------------------------------------------------------------------- #
# Writes (Superadmin only)
# --------------------------------------------------------------------------- #
@router.post(
    "",
    response_model=ClinicWriteResult,
    status_code=status.HTTP_201_CREATED,
    summary="Create a clinic",
)
def create_clinic(
    payload: ClinicCreate, request: Request, db: DbSession, current_user: SuperadminUser
):
    clinic = clinic_service.create_clinic(db, payload, current_user, request=request)
    db.commit()
    db.refresh(clinic)
    read = _to_read(db, clinic)
    return ClinicWriteResult(clinic=read, warnings=read.configuration_warnings)


@router.put(
    "/{clinic_id}",
    response_model=ClinicWriteResult,
    summary="Edit clinic details and appointment configuration",
)
def update_clinic(
    clinic_id: int,
    payload: ClinicUpdate,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    clinic, warnings, overbooked = clinic_service.update_clinic(
        db, clinic_id, payload, current_user, request=request
    )
    return ClinicWriteResult(
        clinic=_to_read(db, clinic), warnings=warnings, overbooked_slots=overbooked
    )


@router.post(
    "/{clinic_id}/activate", response_model=ClinicWriteResult, summary="Activate a clinic"
)
def activate_clinic(
    clinic_id: int, request: Request, db: DbSession, current_user: SuperadminUser
):
    clinic, _ = clinic_service.set_clinic_status(
        db, clinic_id, ClinicStatus.ACTIVE, current_user, request=request
    )
    return ClinicWriteResult(clinic=_to_read(db, clinic))


@router.post(
    "/{clinic_id}/deactivate",
    response_model=ClinicWriteResult,
    summary="Deactivate a clinic (existing appointments are kept)",
)
def deactivate_clinic(
    clinic_id: int, request: Request, db: DbSession, current_user: SuperadminUser
):
    clinic, future_count = clinic_service.set_clinic_status(
        db, clinic_id, ClinicStatus.INACTIVE, current_user, request=request
    )
    warnings = []
    if future_count:
        warnings.append(
            f"{future_count} future appointment(s) remain at this clinic. They are kept "
            "and stay cancellable, but no new bookings will be accepted."
        )
    return ClinicWriteResult(
        clinic=_to_read(db, clinic),
        warnings=warnings,
        future_appointment_count=future_count,
    )


@router.put(
    "/{clinic_id}/working-hours",
    response_model=ClinicWriteResult,
    summary="Replace the weekly shift set",
)
def replace_working_hours(
    clinic_id: int,
    payload: WorkingHoursReplace,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    clinic, warnings = clinic_service.replace_working_hours(
        db, clinic_id, payload.working_hours, current_user, request=request
    )
    return ClinicWriteResult(clinic=_to_read(db, clinic), warnings=warnings)


@router.post(
    "/{clinic_id}/breaks",
    response_model=BreakRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a recurring break",
)
def add_break(
    clinic_id: int,
    payload: BreakCreate,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    clinic_break = clinic_service.add_break(
        db, clinic_id, payload, current_user, request=request
    )
    return BreakRead.model_validate(clinic_break)


@router.delete(
    "/{clinic_id}/breaks/{break_id}", response_model=Message, summary="Remove a break"
)
def remove_break(
    clinic_id: int,
    break_id: int,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    clinic_service.remove_break(db, clinic_id, break_id, current_user, request=request)
    return Message(message="Break removed")


@router.post(
    "/{clinic_id}/users",
    response_model=ClinicStaffRead,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a Clinic User to this clinic",
)
def assign_staff(
    clinic_id: int,
    payload: StaffAssign,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    link = clinic_service.assign_staff(db, clinic_id, payload, current_user, request=request)
    return ClinicStaffRead.model_validate(link)


@router.delete(
    "/{clinic_id}/users/{user_id}",
    response_model=Message,
    summary="Remove a Clinic User from this clinic",
)
def unassign_staff(
    clinic_id: int,
    user_id: int,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    clinic_service.unassign_staff(db, clinic_id, user_id, current_user, request=request)
    return Message(message="User unassigned from clinic")
