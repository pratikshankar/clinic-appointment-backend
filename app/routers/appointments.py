"""Appointment endpoints (Sections 7, 8, 26, 27).

`POST /appointments` and `POST /appointments/book-new-patient` are two doors onto
the same booking engine: one for a patient who already exists, one that registers
a first-time caller and books them in a single request. Both return
`409 slot_unavailable` when the slot filled up first.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.models.enums import AppointmentStatus
from app.schemas.appointment import (
    AppointmentCounters,
    AppointmentDetail,
    AppointmentHistoryEntry,
    AppointmentRead,
    BookExistingPatient,
    BookNewPatient,
    BookingResult,
    CancelRequest,
    DayAvailability,
    RescheduleRequest,
    StatusChangeRequest,
)
from app.schemas.common import Page
from app.services import appointment_service

router = APIRouter(prefix="/appointments", tags=["Appointments"])


# --------------------------------------------------------------------------- #
# Availability (Section 26)
# --------------------------------------------------------------------------- #
@router.get(
    "/available-slots",
    response_model=DayAvailability,
    summary="Bookable slots for a clinic on a date, with remaining capacity",
)
def available_slots(
    db: DbSession,
    current_user: ClinicStaffUser,
    clinic_id: int,
    on_date: Annotated[date | None, Query(alias="date")] = None,
):
    """Slots derived from working hours, breaks, holidays and slot duration,
    annotated with how many of each slot's places are already taken.

    Slots that are full or already past are returned with `is_bookable: false`
    and a reason, rather than omitted -- reception wants to see the shape of the
    whole day, not a list with holes in it.
    """
    return DayAvailability(
        **appointment_service.day_availability(
            db, current_user, clinic_id, on_date or date.today()
        )
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
@router.get("", response_model=Page[AppointmentRead], summary="List appointments")
def list_appointments(
    db: DbSession,
    current_user: ClinicStaffUser,
    clinic_id: int | None = None,
    patient_id: int | None = None,
    on_date: Annotated[date | None, Query(alias="date")] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    appointment_status: Annotated[
        list[AppointmentStatus] | None, Query(alias="status")
    ] = None,
    window: Annotated[
        str | None, Query(pattern="^(today|upcoming|past)$", description="Convenience tab")
    ] = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
):
    appointments, total = appointment_service.list_appointments(
        db,
        current_user,
        clinic_id=clinic_id,
        patient_id=patient_id,
        on_date=on_date,
        date_from=date_from,
        date_to=date_to,
        statuses=appointment_status,
        window=window,
        search=search,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[AppointmentRead](
        items=[AppointmentRead.model_validate(item) for item in appointments],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/counters",
    response_model=AppointmentCounters,
    summary="Tallies for the appointment list tabs",
)
def appointment_counters(
    db: DbSession, current_user: ClinicStaffUser, clinic_id: int | None = None
):
    return AppointmentCounters(**appointment_service.counters(db, current_user, clinic_id))


@router.get(
    "/{appointment_id}",
    response_model=AppointmentDetail,
    summary="One appointment with its full history",
)
def get_appointment(appointment_id: int, db: DbSession, current_user: ClinicStaffUser):
    appointment = appointment_service.get_appointment(db, current_user, appointment_id)
    entries = appointment_service.history(db, current_user, appointment_id)
    detail = AppointmentDetail.model_validate(appointment)
    detail.history = [AppointmentHistoryEntry.model_validate(entry) for entry in entries]
    return detail


@router.get(
    "/{appointment_id}/history",
    response_model=list[AppointmentHistoryEntry],
    summary="Audit trail for one appointment (Section 27)",
)
def appointment_history(appointment_id: int, db: DbSession, current_user: ClinicStaffUser):
    entries = appointment_service.history(db, current_user, appointment_id)
    return [AppointmentHistoryEntry.model_validate(entry) for entry in entries]


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #
@router.post(
    "",
    response_model=BookingResult,
    status_code=status.HTTP_201_CREATED,
    summary="Book an existing patient into a slot",
)
def book_appointment(
    payload: BookExistingPatient,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Returns **409 `slot_unavailable`** if the slot filled up first, with the
    capacity and current booking count in `error.details`."""
    appointment, warnings = appointment_service.book_existing_patient(
        db, payload, current_user, request=request
    )
    return BookingResult(
        appointment=AppointmentRead.model_validate(appointment), warnings=warnings
    )


@router.post(
    "/book-new-patient",
    response_model=BookingResult,
    status_code=status.HTTP_201_CREATED,
    summary="Register a first-time caller and book them in one step",
)
def book_new_patient(
    payload: BookNewPatient,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """One request for the new-lead path, so reception does not have to complete
    a registration form and then a booking form while the patient waits on the
    phone. The profile is created incomplete for the clinic to finish later."""
    appointment, warnings = appointment_service.book_new_patient(
        db, payload, current_user, request=request
    )
    return BookingResult(
        appointment=AppointmentRead.model_validate(appointment),
        patient_created=True,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def _status_endpoint(new_status: AppointmentStatus, summary: str):
    def endpoint(
        appointment_id: int,
        payload: StatusChangeRequest | None,
        request: Request,
        db: DbSession,
        current_user: ClinicStaffUser,
    ):
        appointment = appointment_service.change_status(
            db,
            appointment_id,
            new_status,
            current_user,
            reason=payload.note if payload else None,
            request=request,
        )
        return AppointmentRead.model_validate(appointment)

    endpoint.__name__ = f"mark_{new_status.value.lower()}"
    endpoint.__doc__ = summary
    return endpoint


router.add_api_route(
    "/{appointment_id}/confirm",
    _status_endpoint(AppointmentStatus.CONFIRMED, "Confirm a booked appointment"),
    methods=["POST"],
    response_model=AppointmentRead,
    summary="Confirm an appointment",
)
router.add_api_route(
    "/{appointment_id}/check-in",
    _status_endpoint(AppointmentStatus.CHECKED_IN, "Mark the patient as arrived"),
    methods=["POST"],
    response_model=AppointmentRead,
    summary="Check a patient in",
)
router.add_api_route(
    "/{appointment_id}/complete",
    _status_endpoint(AppointmentStatus.COMPLETED, "Mark the appointment completed"),
    methods=["POST"],
    response_model=AppointmentRead,
    summary="Complete an appointment",
)
router.add_api_route(
    "/{appointment_id}/no-show",
    _status_endpoint(AppointmentStatus.NO_SHOW, "Record that the patient did not arrive"),
    methods=["POST"],
    response_model=AppointmentRead,
    summary="Mark a no-show",
)


@router.post(
    "/{appointment_id}/cancel",
    response_model=AppointmentRead,
    summary="Cancel an appointment (the row is kept)",
)
def cancel_appointment(
    appointment_id: int,
    payload: CancelRequest,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Frees the slot for another patient. The appointment is never deleted."""
    appointment = appointment_service.cancel(
        db, appointment_id, payload, current_user, request=request
    )
    return AppointmentRead.model_validate(appointment)


@router.post(
    "/{appointment_id}/reschedule",
    response_model=BookingResult,
    summary="Move an appointment to another slot, preserving the original",
)
def reschedule_appointment(
    appointment_id: int,
    payload: RescheduleRequest,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """The original is marked RESCHEDULED and kept; a new appointment is created
    that points back at it, so the chain is reconstructable (Section 27)."""
    _, replacement, warnings = appointment_service.reschedule(
        db, appointment_id, payload, current_user, request=request
    )
    return BookingResult(
        appointment=AppointmentRead.model_validate(replacement), warnings=warnings
    )
