"""Referral programme business logic.

Key rule
--------
A referral's credit is VOIDED automatically whenever the referred patient's
qualifying package is cancelled (either directly or via an approved refund).
The void is applied by calling `void_for_package()` from two places:

  1. session_service.cancel_package()      — direct cancellation
  2. refund_service._apply_financial_effect() — refund approval

This means a referral can never stay PENDING against a cancelled package.
"""

from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import AuditAction, PackageStatus, ReferralStatus, RoleName
from app.models.patient import Patient
from app.models.referral import ReferralRecord
from app.models.session import TreatmentPackage
from app.models.user import User
from app.schemas.referral import ReferralApplyCredit, ReferralCreate, PackageRedeemReferralCredit, PackageRedeemReferredCredit
from app.services import audit_service
from app.utils.exceptions import NotFoundError, PermissionDeniedError, ValidationError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_or_404(db: Session, referral_id: int) -> ReferralRecord:
    r = db.get(ReferralRecord, referral_id)
    if not r:
        raise NotFoundError("Referral not found")
    return r


def _assert_can_view(user: User, referral: ReferralRecord) -> None:
    if user.role.name == RoleName.CLINIC_USER:
        user_clinic_ids = {cu.clinic_id for cu in user.clinic_users}
        if referral.clinic_id not in user_clinic_ids:
            raise PermissionDeniedError("You can only view referrals for your own clinic")


def _assert_can_mutate(user: User, referral: ReferralRecord) -> None:
    _assert_can_view(user, referral)


# ── Public API ────────────────────────────────────────────────────────────────

def create(
    db: Session,
    user: User,
    payload: ReferralCreate,
    request: Request | None = None,
) -> ReferralRecord:
    """Register a new referral. Staff pick the qualifying package."""

    # Verify the qualifying package exists and belongs to the referred patient
    package = db.get(TreatmentPackage, payload.referred_package_id)
    if not package:
        raise NotFoundError("Package not found")
    if package.patient_id != payload.referred_patient_id:
        raise ValidationError("Package does not belong to the referred patient")

    # Clinic-user scope check
    if user.role.name == RoleName.CLINIC_USER:
        user_clinic_ids = {cu.clinic_id for cu in user.clinic_users}
        if package.clinic_id not in user_clinic_ids:
            raise PermissionDeniedError("You can only register referrals for your own clinic")

    # The qualifying package must not already be cancelled
    if package.status == PackageStatus.CANCELLED:
        raise ValidationError(
            "The referred patient's package is already cancelled. "
            "No referral credit can be awarded for a cancelled package."
        )

    # Prevent duplicate referrals for the same package
    existing = db.execute(
        select(ReferralRecord).where(
            ReferralRecord.referred_package_id == payload.referred_package_id,
            ReferralRecord.status != ReferralStatus.VOIDED,
        )
    ).scalar_one_or_none()
    if existing:
        raise ValidationError(
            f"A referral (#{existing.id}) already exists for this package"
        )

    referral = ReferralRecord(
        referrer_patient_id=payload.referrer_patient_id,
        referred_patient_id=payload.referred_patient_id,
        referred_package_id=payload.referred_package_id,
        clinic_id=package.clinic_id,
        credit_sessions=payload.credit_sessions,
        notes=payload.notes,
        initiated_by_id=user.id,
        status=ReferralStatus.PENDING,
    )
    db.add(referral)
    db.flush()

    audit_service.record(
        db,
        action=AuditAction.REFERRAL_CREATED,
        user=user,
        entity_type="referral_record",
        entity_id=referral.id,
        clinic_id=package.clinic_id,
        description=(
            f"Referral #{referral.id} created: "
            f"patient {payload.referrer_patient_id} referred patient {payload.referred_patient_id}, "
            f"{payload.credit_sessions} session(s) credit pending"
        ),
        request=request,
    )

    db.commit()
    db.refresh(referral)
    return referral


def apply_credit(
    db: Session,
    user: User,
    referral_id: int,
    payload: ReferralApplyCredit,
    request: Request | None = None,
) -> ReferralRecord:
    """Confirm the referral credit — adds sessions to the referrer's balance.

    The balance (Patient.referral_session_credits) can later be redeemed by staff
    against any future package via POST /packages/{id}/apply-referral-credit.
    """
    referral = _get_or_404(db, referral_id)
    _assert_can_mutate(user, referral)

    if referral.status == ReferralStatus.VOIDED:
        raise ValidationError(
            "This referral has been voided because the referred patient's package "
            "was cancelled or refunded. No credit can be applied."
        )
    if referral.status == ReferralStatus.CREDITED:
        raise ValidationError("Credit has already been applied for this referral")

    # Credit the referrer's balance (redeemable on any package)
    referrer = db.get(Patient, referral.referrer_patient_id)
    if not referrer:
        raise NotFoundError("Referrer patient not found")
    referrer.referral_session_credits += referral.credit_sessions

    # Credit the referred patient's balance (redeemable only on packages with ≥5 sessions)
    referred = db.get(Patient, referral.referred_patient_id)
    if referred:
        referred.referred_session_credits += referral.credit_sessions

    referral.status = ReferralStatus.CREDITED
    referral.credited_by_id = user.id
    referral.credited_at = _now()

    audit_service.record(
        db,
        action=AuditAction.REFERRAL_CREDITED,
        user=user,
        entity_type="referral_record",
        entity_id=referral.id,
        clinic_id=referral.clinic_id,
        description=(
            f"Referral #{referral.id} credited: "
            f"{referral.credit_sessions} session(s) added to referrer "
            f"patient {referral.referrer_patient_id} balance "
            f"({referrer.referral_session_credits} total) and to referred "
            f"patient {referral.referred_patient_id} balance "
            f"({referred.referred_session_credits if referred else 0} total)"
        ),
        request=request,
    )

    db.commit()
    db.refresh(referral)
    return referral


def redeem_credit_to_package(
    db: Session,
    user: User,
    package_id: int,
    payload: PackageRedeemReferralCredit,
    request: Request | None = None,
) -> TreatmentPackage:
    """Redeem sessions from a patient's referral balance onto an active package.

    Deducts `sessions` from Patient.referral_session_credits and increments
    TreatmentPackage.sessions_registered by the same amount.
    """
    package = db.get(TreatmentPackage, package_id)
    if not package:
        raise NotFoundError("Package not found")
    if package.status == PackageStatus.CANCELLED:
        raise ValidationError("Cannot apply credits to a cancelled package")

    patient = db.get(Patient, package.patient_id)
    if not patient:
        raise NotFoundError("Patient not found")

    if patient.referral_session_credits < payload.sessions:
        raise ValidationError(
            f"Insufficient credit balance: patient has "
            f"{patient.referral_session_credits} session(s), "
            f"requested {payload.sessions}"
        )

    patient.referral_session_credits -= payload.sessions
    package.sessions_registered += payload.sessions

    audit_service.record(
        db,
        action=AuditAction.REFERRAL_CREDITED,
        user=user,
        entity_type="treatment_package",
        entity_id=package.id,
        clinic_id=package.clinic_id,
        description=(
            f"{payload.sessions} referral credit session(s) redeemed onto "
            f"package #{package.id} for patient {patient.id} "
            f"(remaining balance: {patient.referral_session_credits})"
        ),
        request=request,
    )

    db.commit()
    db.refresh(package)
    return package


def redeem_referred_credit_to_package(
    db: Session,
    user: User,
    package_id: int,
    payload: PackageRedeemReferredCredit,
    request: Request | None = None,
) -> TreatmentPackage:
    """Redeem referred-patient credit sessions onto a package.

    Requires: package sessions_registered >= 5 (minimum qualifying commitment).
    Deducts `sessions` from Patient.referred_session_credits.
    """
    package = db.get(TreatmentPackage, package_id)
    if not package:
        raise NotFoundError("Package not found")
    if package.status == PackageStatus.CANCELLED:
        raise ValidationError("Cannot apply credits to a cancelled package")
    if package.sessions_registered < 5:
        raise ValidationError(
            f"Referred-patient credits can only be redeemed on packages with at least "
            f"5 sessions registered. This package has {package.sessions_registered}."
        )

    patient = db.get(Patient, package.patient_id)
    if not patient:
        raise NotFoundError("Patient not found")

    if patient.referred_session_credits < payload.sessions:
        raise ValidationError(
            f"Insufficient referred credit balance: patient has "
            f"{patient.referred_session_credits} session(s), "
            f"requested {payload.sessions}"
        )

    patient.referred_session_credits -= payload.sessions
    package.sessions_registered += payload.sessions

    audit_service.record(
        db,
        action=AuditAction.REFERRAL_CREDITED,
        user=user,
        entity_type="treatment_package",
        entity_id=package.id,
        clinic_id=package.clinic_id,
        description=(
            f"{payload.sessions} referred-patient credit session(s) redeemed onto "
            f"package #{package.id} for patient {patient.id} "
            f"(remaining balance: {patient.referred_session_credits})"
        ),
        request=request,
    )

    db.commit()
    db.refresh(package)
    return package


def maybe_auto_credit(
    db: Session,
    patient: "Patient",
    package: "TreatmentPackage",
    actor: User,
    request=None,
) -> bool:
    """Fire referral credit automatically when a patient's first qualifying package is registered.

    Qualifying condition: package has ≥5 sessions AND the patient has a `referred_by_code`.
    Does nothing (and returns False) if:
    - The patient has no `referred_by_code`
    - The referrer cannot be found by that code
    - This would be a self-referral
    - A non-voided referral already exists for this patient as referee

    Called from session_service.create_package() — must be invoked *before* commit.
    """
    if not patient.referred_by_code:
        return False

    # Avoid re-triggering if a referral already exists for this patient
    existing = db.execute(
        select(ReferralRecord).where(
            ReferralRecord.referred_patient_id == patient.id,
            ReferralRecord.status.in_([ReferralStatus.PENDING, ReferralStatus.CREDITED]),
        )
    ).scalar_one_or_none()
    if existing:
        return False

    # Resolve referrer by their referral_code
    referrer = db.execute(
        select(Patient).where(Patient.referral_code == patient.referred_by_code)
    ).scalar_one_or_none()
    if not referrer or referrer.id == patient.id:
        return False

    referral = ReferralRecord(
        referrer_patient_id=referrer.id,
        referred_patient_id=patient.id,
        referred_package_id=package.id,
        clinic_id=package.clinic_id,
        credit_sessions=2,
        initiated_by_id=actor.id,
        status=ReferralStatus.CREDITED,
        credited_by_id=actor.id,
        credited_at=_now(),
        notes="Auto-credited: qualifying package (≥ 5 sessions) registered.",
    )
    db.add(referral)
    db.flush()

    referrer.referral_session_credits += 2
    patient.referred_session_credits += 2

    audit_service.record(
        db,
        action=AuditAction.REFERRAL_CREDITED,
        user=actor,
        entity_type="referral_record",
        entity_id=referral.id,
        clinic_id=package.clinic_id,
        description=(
            f"Auto-credited referral #{referral.id}: "
            f"{referrer.patient_code} referred {patient.patient_code}, "
            f"2 sessions each. Triggered by package #{package.id}."
        ),
        request=request,
    )
    return True


def void_for_package(
    db: Session,
    package_id: int,
    reason: str = "Referred package was cancelled or refunded",
) -> int:
    """Handle referral records tied to a cancelled/refunded qualifying package.

    - PENDING referrals: voided (no credits were given).
    - CREDITED referrals: referred patient's credit balance zeroed (the referral
      was already credited, but the qualifying package no longer qualifies).

    Called automatically from session_service.cancel_package() and
    refund_service._apply_financial_effect(). Returns the count of PENDING voided.
    """
    referrals = db.execute(
        select(ReferralRecord).where(
            ReferralRecord.referred_package_id == package_id,
            ReferralRecord.status.in_([ReferralStatus.PENDING, ReferralStatus.CREDITED]),
        )
    ).scalars().all()

    voided_count = 0
    for r in referrals:
        if r.status == ReferralStatus.PENDING:
            r.status = ReferralStatus.VOIDED
            r.voided_reason = reason
            voided_count += 1

        # Reverse credits for CREDITED referrals (subtract only what this referral granted)
        if r.status == ReferralStatus.CREDITED:
            referred = db.get(Patient, r.referred_patient_id)
            if referred:
                referred.referred_session_credits = max(
                    0, referred.referred_session_credits - r.credit_sessions
                )
            referrer = db.get(Patient, r.referrer_patient_id)
            if referrer:
                referrer.referral_session_credits = max(
                    0, referrer.referral_session_credits - r.credit_sessions
                )

    db.flush()
    return voided_count


def list_referrals(
    db: Session,
    user: User,
    status: str | None = None,
    clinic_id: int | None = None,
    patient_id: int | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[ReferralRecord], int]:
    from sqlalchemy import func

    stmt = select(ReferralRecord).order_by(ReferralRecord.created_at.desc())

    if user.role.name == RoleName.CLINIC_USER:
        user_clinic_ids = [cu.clinic_id for cu in user.clinic_users]
        stmt = stmt.where(ReferralRecord.clinic_id.in_(user_clinic_ids))
    elif clinic_id:
        stmt = stmt.where(ReferralRecord.clinic_id == clinic_id)

    if status:
        stmt = stmt.where(ReferralRecord.status == status)

    if patient_id:
        stmt = stmt.where(
            (ReferralRecord.referrer_patient_id == patient_id)
            | (ReferralRecord.referred_patient_id == patient_id)
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
    offset = (page - 1) * page_size
    items = db.execute(stmt.offset(offset).limit(page_size)).scalars().all()
    return list(items), total


def get_referral(db: Session, user: User, referral_id: int) -> ReferralRecord:
    r = _get_or_404(db, referral_id)
    _assert_can_view(user, r)
    return r
