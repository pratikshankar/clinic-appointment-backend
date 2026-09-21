"""Patient-source configuration endpoints (Section 11).

Any clinic staff member may read the list, because Phase 3's patient form needs
it to populate a dropdown. Only the Superadmin may change it.
"""

from fastapi import APIRouter, Request, status

from app.auth.dependencies import ClinicStaffUser, DbSession, SuperadminUser
from app.schemas.patient_source import (
    PatientSourceCreate,
    PatientSourceRead,
    PatientSourceUpdate,
)
from app.services import patient_source_service

router = APIRouter(prefix="/patient-sources", tags=["Configuration"])


@router.get("", response_model=list[PatientSourceRead], summary="List patient sources")
def list_sources(
    db: DbSession,
    current_user: ClinicStaffUser,
    include_inactive: bool = True,
):
    rows = patient_source_service.list_sources(db, include_inactive=include_inactive)
    result = []
    for source, count in rows:
        payload = PatientSourceRead.model_validate(source)
        payload.patient_count = count
        result.append(payload)
    return result


@router.post(
    "",
    response_model=PatientSourceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a patient source",
)
def create_source(
    payload: PatientSourceCreate,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    source = patient_source_service.create_source(db, payload, current_user, request=request)
    return PatientSourceRead.model_validate(source)


@router.put(
    "/{source_id}",
    response_model=PatientSourceRead,
    summary="Rename, reorder or deactivate a patient source",
)
def update_source(
    source_id: int,
    payload: PatientSourceUpdate,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    source = patient_source_service.update_source(
        db, source_id, payload, current_user, request=request
    )
    result = PatientSourceRead.model_validate(source)
    result.patient_count = patient_source_service.patient_count(db, source.id)
    return result
