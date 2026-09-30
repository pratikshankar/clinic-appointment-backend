"""Referral programme endpoints."""

from fastapi import APIRouter, Query, Request

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.schemas.referral import (
    ReferralApplyCredit,
    ReferralCreate,
    ReferralListItem,
    ReferralOut,
)
from app.services import referral_service

router = APIRouter(prefix="/referrals", tags=["referrals"])


@router.post("", response_model=ReferralOut, status_code=201)
def create_referral(
    payload: ReferralCreate,
    db: DbSession,
    current_user: ClinicStaffUser,
    request: Request,
):
    """Register a new referral (staff action).

    The referred patient's qualifying package must be ACTIVE at the time of
    registration. If the package is subsequently cancelled or refunded, the
    referral is automatically voided and no credit is applied.
    """
    r = referral_service.create(db, current_user, payload, request)
    return ReferralOut.from_orm(r)


@router.get("", response_model=list[ReferralListItem])
def list_referrals(
    db: DbSession,
    current_user: ClinicStaffUser,
    status: str | None = Query(default=None),
    clinic_id: int | None = Query(default=None),
    patient_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    items, _total = referral_service.list_referrals(
        db, current_user,
        status=status,
        clinic_id=clinic_id,
        patient_id=patient_id,
        page=page,
        page_size=page_size,
    )
    return [ReferralListItem.from_orm(r) for r in items]


@router.get("/{referral_id}", response_model=ReferralOut)
def get_referral(referral_id: int, db: DbSession, current_user: ClinicStaffUser):
    r = referral_service.get_referral(db, current_user, referral_id)
    return ReferralOut.from_orm(r)


@router.post("/{referral_id}/credit", response_model=ReferralOut)
def apply_credit(
    referral_id: int,
    payload: ReferralApplyCredit,
    db: DbSession,
    current_user: ClinicStaffUser,
    request: Request,
):
    """Apply the session credit to one of the referrer's active packages.

    Fails if the referral has already been voided (package was cancelled/refunded).
    """
    r = referral_service.apply_credit(db, current_user, referral_id, payload, request)
    return ReferralOut.from_orm(r)
