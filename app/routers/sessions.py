"""Treatment package and session endpoints (Sections 12, 13, 14).

Logging a session is a single call that also completes the linked appointment,
so the appointment count and the session count cannot drift apart.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.schemas.common import Message, Page
from app.schemas.billing import BillRead
from app.schemas.session import (
    PackageCancel,
    PackageCreate,
    PackageCreated,
    PackageRead,
    PackageUpdate,
    SessionContext,
    SessionCreate,
    SessionRead,
    SessionResult,
    SessionUpdate,
    SessionVoid,
)
from app.services import session_service

# Packages and sessions hang off a patient; the flat routers below handle
# operations on a single record by id.
patient_router = APIRouter(prefix="/patients", tags=["Sessions"])
package_router = APIRouter(prefix="/packages", tags=["Sessions"])
session_router = APIRouter(prefix="/sessions", tags=["Sessions"])


# --------------------------------------------------------------------------- #
# Packages (Section 12)
# --------------------------------------------------------------------------- #
@patient_router.get(
    "/{patient_id}/packages",
    response_model=list[PackageRead],
    summary="Treatment packages for a patient",
)
def list_packages(
    patient_id: int,
    db: DbSession,
    current_user: ClinicStaffUser,
    include_closed: bool = True,
):
    packages = session_service.list_packages(
        db, current_user, patient_id, include_closed=include_closed
    )
    return [PackageRead.model_validate(item) for item in packages]


@patient_router.post(
    "/{patient_id}/packages",
    response_model=PackageCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Register a block of purchased sessions (and bill for it)",
)
def create_package(
    patient_id: int,
    payload: PackageCreate,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Multiple active packages are allowed: a patient may top up mid-course.
    Sessions consume the oldest active one with sessions remaining.

    Any `additional_charges` (a consultation fee, say) and the `payment` are
    written as one bill in the same transaction, and returned as `bill`."""
    package, bill = session_service.create_package(
        db, current_user, patient_id, payload, request=request
    )
    response = PackageCreated.model_validate(package)
    response.bill = BillRead.model_validate(bill) if bill else None
    return response


@patient_router.get(
    "/{patient_id}/session-context",
    response_model=SessionContext,
    summary="Everything the session form needs, pre-resolved",
)
def get_session_context(patient_id: int, db: DbSession, current_user: ClinicStaffUser):
    """One call returns the patient, their packages, the suggested package, the
    next session number and the selectable therapists."""
    data = session_service.session_context(db, current_user, patient_id)
    return SessionContext(
        **{**data, "packages": [PackageRead.model_validate(p) for p in data["packages"]]}
    )


@package_router.put(
    "/{package_id}", response_model=PackageRead, summary="Edit a treatment package"
)
def update_package(
    package_id: int,
    payload: PackageUpdate,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """`sessions_taken` cannot be set here -- it moves only by logging or voiding
    a session."""
    package = session_service.update_package(
        db, current_user, package_id, payload, request=request
    )
    return PackageRead.model_validate(package)


@package_router.post(
    "/{package_id}/cancel",
    response_model=PackageRead,
    summary="Cancel a package (logged sessions are kept)",
)
def cancel_package(
    package_id: int,
    payload: PackageCancel,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    package = session_service.cancel_package(
        db, current_user, package_id, payload.reason, request=request
    )
    return PackageRead.model_validate(package)


# --------------------------------------------------------------------------- #
# Sessions (Section 13)
# --------------------------------------------------------------------------- #
@patient_router.get(
    "/{patient_id}/sessions",
    response_model=list[SessionRead],
    summary="Session history for a patient",
)
def list_patient_sessions(
    patient_id: int,
    db: DbSession,
    current_user: ClinicStaffUser,
    include_voided: bool = False,
):
    sessions, _ = session_service.list_sessions(
        db, current_user, patient_id=patient_id, include_voided=include_voided, limit=500
    )
    return [SessionRead.model_validate(item) for item in sessions]


@patient_router.post(
    "/{patient_id}/sessions",
    response_model=SessionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Record a session (and complete its appointment)",
)
def log_session(
    patient_id: int,
    payload: SessionCreate,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Increments `sessions_taken` on the consumed package and, when
    `appointment_id` is supplied, marks that appointment COMPLETED in the same
    transaction. Pass `no_package: true` for a one-off assessment."""
    session, package, completed, warnings = session_service.log_session(
        db, current_user, patient_id, payload, request=request
    )
    return SessionResult(
        session=SessionRead.model_validate(session),
        package=PackageRead.model_validate(package) if package else None,
        appointment_completed=completed,
        warnings=warnings,
    )


@session_router.get("", response_model=Page[SessionRead], summary="List sessions")
def list_sessions(
    db: DbSession,
    current_user: ClinicStaffUser,
    clinic_id: int | None = None,
    patient_id: int | None = None,
    therapist_user_id: int | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    include_voided: bool = False,
    search: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
):
    sessions, total = session_service.list_sessions(
        db,
        current_user,
        clinic_id=clinic_id,
        patient_id=patient_id,
        therapist_user_id=therapist_user_id,
        date_from=date_from,
        date_to=date_to,
        include_voided=include_voided,
        search=search,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[SessionRead](
        items=[SessionRead.model_validate(item) for item in sessions],
        total=total,
        page=page,
        page_size=page_size,
    )


@session_router.get("/counters", summary="Session tallies for the header")
def session_counters(
    db: DbSession, current_user: ClinicStaffUser, clinic_id: int | None = None
):
    return session_service.session_counters(db, current_user, clinic_id)


@session_router.get("/{session_id}", response_model=SessionRead, summary="One session")
def get_session(session_id: int, db: DbSession, current_user: ClinicStaffUser):
    return SessionRead.model_validate(
        session_service.get_session(db, current_user, session_id)
    )


@session_router.put(
    "/{session_id}",
    response_model=SessionRead,
    summary="Edit a session's notes (date and package are fixed)",
)
def update_session(
    session_id: int,
    payload: SessionUpdate,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    session = session_service.update_session(
        db, current_user, session_id, payload, request=request
    )
    return SessionRead.model_validate(session)


@session_router.post(
    "/{session_id}/void",
    response_model=SessionResult,
    summary="Void a mis-logged session, returning the place to the package",
)
def void_session(
    session_id: int,
    payload: SessionVoid,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """The row is kept with the reason attached and the counter is decremented.
    Session numbers are never reused."""
    session, package = session_service.void_session(
        db, current_user, session_id, payload.reason, request=request
    )
    return SessionResult(
        session=SessionRead.model_validate(session),
        package=PackageRead.model_validate(package) if package else None,
        warnings=["The session number is retained so the sequence stays auditable."],
    )
