"""Holiday endpoints.

Holidays live in their own router rather than only under a clinic, because a
chain-wide closure (`clinic_id = null`) belongs to no single clinic. Reads are
scoped like everything else; writes are Superadmin-only.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.auth.dependencies import ClinicStaffUser, DbSession, SuperadminUser
from app.schemas.clinic import HolidayCreate, HolidayDetail
from app.schemas.common import Message
from app.services import clinic_service

router = APIRouter(prefix="/holidays", tags=["Clinics"])


@router.get(
    "",
    response_model=list[HolidayDetail],
    summary="Holidays in scope (a clinic filter also returns chain-wide closures)",
)
def list_holidays(
    db: DbSession,
    current_user: ClinicStaffUser,
    clinic_id: int | None = None,
    from_date: Annotated[date | None, Query(alias="from")] = None,
):
    holidays = clinic_service.list_holidays(
        db, current_user, clinic_id=clinic_id, from_date=from_date
    )
    result = []
    for holiday in holidays:
        detail = HolidayDetail.model_validate(holiday)
        detail.clinic_name = holiday.clinic.name if holiday.clinic else None
        result.append(detail)
    return result


@router.post(
    "",
    response_model=HolidayDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Add a clinic or chain-wide holiday",
)
def add_holiday(
    payload: HolidayCreate, request: Request, db: DbSession, current_user: SuperadminUser
):
    holiday = clinic_service.add_holiday(db, payload, current_user, request=request)
    detail = HolidayDetail.model_validate(holiday)
    detail.clinic_name = holiday.clinic.name if holiday.clinic else None
    return detail


@router.delete("/{holiday_id}", response_model=Message, summary="Remove a holiday")
def remove_holiday(
    holiday_id: int, request: Request, db: DbSession, current_user: SuperadminUser
):
    clinic_service.remove_holiday(db, holiday_id, current_user, request=request)
    return Message(message="Holiday removed")
