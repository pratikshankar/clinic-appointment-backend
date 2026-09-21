"""Patient endpoints (Sections 7, 11, 20, 21, 38).

Note the deliberate asymmetry between `GET /patients` (clinic-scoped browsing)
and `GET /patients/lookup` (chain-wide exact match, identity fields only). That
is what lets a Clinic User avoid creating a duplicate for a patient registered
at another branch without exposing the whole patient book.
"""

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.schemas.common import Page
from app.schemas.patient import (
    AppointmentSummary,
    BillSummary,
    DuplicateCheckRequest,
    DuplicateCheckResponse,
    PackageSummary,
    PatientCard,
    PatientCreate,
    PatientProfile,
    PatientQuickCreate,
    PatientRead,
    PatientUpdate,
    SessionSummary,
)
from app.services import patient_service

router = APIRouter(prefix="/patients", tags=["Patients"])


def _card(db, user, patient) -> PatientCard:
    card = PatientCard.model_validate(patient)
    card.in_your_scope = patient_service.can_view_full_record(db, user, patient)
    return card


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
@router.get("", response_model=Page[PatientRead], summary="List patients you may browse")
def list_patients(
    db: DbSession,
    current_user: ClinicStaffUser,
    search: Annotated[str | None, Query(max_length=100, description="Name, mobile or Patient ID")] = None,
    clinic_id: int | None = None,
    source_id: int | None = None,
    is_active: bool | None = True,
    profile_complete: bool | None = None,
    has_active_package: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
):
    patients, total = patient_service.list_patients(
        db,
        current_user,
        search=search,
        clinic_id=clinic_id,
        source_id=source_id,
        is_active=is_active,
        profile_complete=profile_complete,
        has_active_package=has_active_package,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    items = [PatientRead.model_validate(patient) for patient in patients]
    with_packages = patient_service.patients_with_active_packages(
        db, [patient.id for patient in patients]
    )
    for item in items:
        item.has_active_package = item.id in with_packages

    return Page[PatientRead](
        items=items, total=total, page=page, page_size=page_size
    )


@router.get(
    "/lookup",
    response_model=list[PatientCard],
    summary="Find a patient anywhere in the chain by Patient ID, mobile or name",
)
def lookup_patient(
    db: DbSession,
    current_user: ClinicStaffUser,
    patient_code: Annotated[str | None, Query(max_length=20)] = None,
    mobile: Annotated[str | None, Query(max_length=20)] = None,
    name: Annotated[
        str | None,
        Query(max_length=150, description="Partial name; minimum 3 characters"),
    ] = None,
):
    """Lookup that crosses clinic boundaries.

    Returns identity fields only -- no diagnosis, address or complaint.
    `in_your_scope` tells the client whether the full record can be opened, so
    the UI can offer "link this patient" rather than a dead end.

    Name search is partial (3+ characters, capped at 25 results) because
    reception normally has a name and not a Patient ID; refusing it would just
    push staff into creating the duplicate that Section 38 forbids.
    """
    patients = patient_service.lookup(
        db, current_user, patient_code=patient_code, mobile=mobile, name=name
    )
    return [_card(db, current_user, patient) for patient in patients]


@router.post(
    "/duplicate-check",
    response_model=DuplicateCheckResponse,
    summary="Check for an existing patient before creating one",
)
def duplicate_check(
    payload: DuplicateCheckRequest, db: DbSession, current_user: ClinicStaffUser
):
    """Surface possible matches so staff link instead of duplicating.

    An `exact_match` (same mobile *and* name) means `POST /patients` will refuse.
    `same_mobile` is informational -- families share a number.
    """
    found = patient_service.find_duplicates(db, payload)
    return DuplicateCheckResponse(
        exact_match=(
            _card(db, current_user, found["exact_match"]) if found["exact_match"] else None
        ),
        same_mobile=[_card(db, current_user, p) for p in found["same_mobile"]],
        similar_name=[_card(db, current_user, p) for p in found["similar_name"]],
    )


@router.get("/{patient_id}", response_model=PatientRead, summary="Full patient record")
def get_patient(patient_id: int, db: DbSession, current_user: ClinicStaffUser):
    patient = patient_service.get_patient(db, current_user, patient_id)
    result = PatientRead.model_validate(patient)
    result.has_active_package = bool(
        patient_service.patients_with_active_packages(db, [patient.id])
    )
    return result


@router.get(
    "/{patient_id}/profile",
    response_model=PatientProfile,
    summary="Patient timeline: packages, appointments, sessions and billing",
)
def get_patient_profile(patient_id: int, db: DbSession, current_user: ClinicStaffUser):
    data = patient_service.build_profile(db, current_user, patient_id)

    return PatientProfile(
        patient=PatientRead.model_validate(data["patient"]),
        packages=[
            PackageSummary(
                id=package.id,
                package_name=package.package_name,
                clinic_name=package.clinic.name if package.clinic else None,
                sessions_registered=package.sessions_registered,
                sessions_taken=package.sessions_taken,
                sessions_remaining=package.sessions_remaining,
                price_per_session=package.price_per_session,
                total_amount=package.total_amount,
                status=package.status,
                start_date=package.start_date,
            )
            for package in data["packages"]
        ],
        appointments=[
            AppointmentSummary(
                id=appointment.id,
                appointment_code=appointment.appointment_code,
                clinic_name=appointment.clinic.name if appointment.clinic else None,
                appointment_date=appointment.appointment_date,
                start_time=appointment.start_time.strftime("%H:%M"),
                status=appointment.status,
                chief_complaint=appointment.chief_complaint,
            )
            for appointment in data["appointments"]
        ],
        sessions=[
            SessionSummary(
                id=session.id,
                session_number=session.session_number,
                session_date=session.session_date,
                clinic_name=session.clinic.name if session.clinic else None,
                therapist_name=session.therapist.full_name if session.therapist else None,
                treatment_provided=session.treatment_provided,
                notes=session.notes,
            )
            for session in data["sessions"]
        ],
        bills=[
            BillSummary(
                id=bill.id,
                bill_number=bill.bill_number,
                clinic_name=bill.clinic.name if bill.clinic else None,
                bill_date=bill.bill_date,
                total_amount=bill.total_amount,
                amount_paid=bill.amount_paid,
                balance_amount=bill.balance_amount,
                payment_status=bill.payment_status,
            )
            for bill in data["bills"]
        ],
        total_sessions_registered=data["total_sessions_registered"],
        total_sessions_taken=data["total_sessions_taken"],
        total_sessions_remaining=data["total_sessions_remaining"],
        cancelled_package_count=data["cancelled_package_count"],
        total_billed=data["total_billed"],
        total_paid=data["total_paid"],
        total_outstanding=data["total_outstanding"],
        scoped_to_your_clinics=data["scoped_to_your_clinics"],
    )


# --------------------------------------------------------------------------- #
# Writes (any clinic staff; scoping is applied per record)
# --------------------------------------------------------------------------- #
@router.post(
    "",
    response_model=PatientRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a patient",
)
def create_patient(
    payload: PatientCreate, request: Request, db: DbSession, current_user: ClinicStaffUser
):
    """Register a patient with a permanent Patient ID.

    Returns **409** when the same name and mobile already exist, with the
    existing patient in `error.details` so the client can link it instead.
    """
    patient = patient_service.create_patient(db, payload, current_user, request=request)
    return PatientRead.model_validate(patient)


@router.post(
    "/quick",
    response_model=PatientRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register the minimum needed while booking (profile stays incomplete)",
)
def quick_create_patient(
    payload: PatientQuickCreate,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    patient = patient_service.quick_create_patient(
        db, payload, current_user, request=request
    )
    return PatientRead.model_validate(patient)


@router.put(
    "/{patient_id}",
    response_model=PatientRead,
    summary="Edit a patient, or complete a minimal profile",
)
def update_patient(
    patient_id: int,
    payload: PatientUpdate,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Filling in gender, address and source promotes the record to complete."""
    patient = patient_service.update_patient(
        db, patient_id, payload, current_user, request=request
    )
    return PatientRead.model_validate(patient)


@router.post(
    "/{patient_id}/archive", response_model=PatientRead, summary="Archive a patient"
)
def archive_patient(
    patient_id: int, request: Request, db: DbSession, current_user: ClinicStaffUser
):
    patient = patient_service.set_active(db, patient_id, False, current_user, request=request)
    return PatientRead.model_validate(patient)


@router.post(
    "/{patient_id}/restore", response_model=PatientRead, summary="Restore a patient"
)
def restore_patient(
    patient_id: int, request: Request, db: DbSession, current_user: ClinicStaffUser
):
    patient = patient_service.set_active(db, patient_id, True, current_user, request=request)
    return PatientRead.model_validate(patient)
