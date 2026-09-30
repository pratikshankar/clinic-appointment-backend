"""Refund request business logic.

Calculation rule
----------------
sessions_consumed < 5  → charge at single_session_rate (walk-in rate) for those sessions
sessions_consumed >= 5 → charge at package_session_rate for those sessions
consultation_fee       → always deducted (non-refundable)
refund_amount          → total_paid - deduction_amount
"""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Bill, BillItem
from app.models.enums import (
    AuditAction,
    BillItemType,
    BillStatus,
    PackageStatus,
    PaymentStatus,
    RefundStatus,
    RoleName,
)
from app.models.refund import RefundAttachment, RefundRequest
from app.models.session import TreatmentPackage
from app.models.user import User
from app.schemas.refund import (
    RefundCalculationUpdate,
    RefundComplete,
    RefundInitiate,
    RefundPrefill,
    RefundReview,
)
from app.services import audit_service
from app.utils.exceptions import NotFoundError, PermissionDeniedError, ValidationError

_SINGLE_SESSION_MULTIPLIER = Decimal("1.5")  # default: 150% of package rate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _assert_can_view(user: User, refund: RefundRequest) -> None:
    if user.role.name == RoleName.CLINIC_USER:
        user_clinic_ids = {cu.clinic_id for cu in user.clinic_users}
        if refund.clinic_id not in user_clinic_ids:
            raise PermissionDeniedError("You can only view refunds for your own clinic")


def _assert_can_mutate(user: User, refund: RefundRequest) -> None:
    _assert_can_view(user, refund)


def _assert_is_reviewer(user: User) -> None:
    if user.role.name == RoleName.CLINIC_USER:
        raise PermissionDeniedError("Only admins and superadmins can approve or reject refunds")


def _get_or_404(db: Session, refund_id: int) -> RefundRequest:
    refund = db.get(RefundRequest, refund_id)
    if not refund:
        raise NotFoundError("Refund request not found")
    return refund


def _calculate(
    sessions_consumed: int,
    single_rate: Decimal,
    package_rate: Decimal,
    consultation_fee: Decimal,
    total_paid: Decimal,
) -> tuple[Decimal, Decimal, str]:
    """Return (deduction, refund, notes)."""
    if sessions_consumed < 5:
        session_charge = Decimal(sessions_consumed) * single_rate
        method = f"{sessions_consumed} session(s) × ₹{single_rate} (single-session rate, <5 sessions consumed)"
    else:
        session_charge = Decimal(sessions_consumed) * package_rate
        method = f"{sessions_consumed} session(s) × ₹{package_rate} (package rate)"

    deduction = session_charge + consultation_fee
    refund = max(Decimal("0"), total_paid - deduction)

    notes_parts = [
        f"Total paid: ₹{total_paid}",
        f"Session deduction: {method} = ₹{session_charge}",
    ]
    if consultation_fee > 0:
        notes_parts.append(f"Consultation fee (non-refundable): ₹{consultation_fee}")
    notes_parts.append(f"Total deduction: ₹{deduction}")
    notes_parts.append(f"Refund amount: ₹{refund}")

    return deduction, refund, "\n".join(notes_parts)


def prefill(db: Session, user: User, package_id: int) -> RefundPrefill:
    """Return pre-filled calculation values for the refund initiation form."""
    package = db.get(TreatmentPackage, package_id)
    if not package:
        raise NotFoundError("Package not found")

    if user.role.name == RoleName.CLINIC_USER:
        user_clinic_ids = {cu.clinic_id for cu in user.clinic_users}
        if package.clinic_id not in user_clinic_ids:
            raise PermissionDeniedError("You can only refund packages at your own clinic")

    # Pull the bill linked to this package (most recent if multiple)
    bill = db.execute(
        select(Bill)
        .where(Bill.package_id == package_id)
        .order_by(Bill.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    total_paid = bill.amount_paid if bill else Decimal("0")
    package_rate = package.price_per_session or Decimal("0")

    # Extract consultation fee from bill items
    consultation_fee = Decimal("0")
    if bill:
        consult_items = db.execute(
            select(BillItem).where(
                BillItem.bill_id == bill.id,
                BillItem.item_type == BillItemType.CONSULTATION,
            )
        ).scalars().all()
        consultation_fee = sum(i.amount for i in consult_items)

    # Default single rate is 150% of package rate
    suggested_single = (package_rate * _SINGLE_SESSION_MULTIPLIER).quantize(Decimal("0.01"))

    sessions_consumed = package.sessions_taken
    deduction, refund, notes = _calculate(
        sessions_consumed,
        suggested_single,
        package_rate,
        consultation_fee,
        total_paid,
    )

    return RefundPrefill(
        package_id=package_id,
        sessions_consumed=sessions_consumed,
        sessions_registered=package.sessions_registered,
        package_session_rate=package_rate,
        consultation_fee=consultation_fee,
        total_paid=total_paid,
        suggested_single_session_rate=suggested_single,
        suggested_deduction=deduction,
        suggested_refund=refund,
        calculation_notes=notes,
    )


def initiate(
    db: Session,
    user: User,
    payload: RefundInitiate,
    request: Request | None = None,
) -> RefundRequest:
    package = db.get(TreatmentPackage, payload.package_id)
    if not package:
        raise NotFoundError("Package not found")

    if user.role.name == RoleName.CLINIC_USER:
        user_clinic_ids = {cu.clinic_id for cu in user.clinic_users}
        if package.clinic_id not in user_clinic_ids:
            raise PermissionDeniedError("You can only initiate refunds for your own clinic")

    # Check there isn't already an open refund for this package
    existing = db.execute(
        select(RefundRequest).where(
            RefundRequest.package_id == payload.package_id,
            RefundRequest.status.in_([RefundStatus.PENDING_APPROVAL, RefundStatus.APPROVED]),
        )
    ).scalar_one_or_none()
    if existing:
        raise ValidationError(
            f"An open refund request (#{existing.id}) already exists for this package"
        )

    bill = db.execute(
        select(Bill)
        .where(Bill.package_id == payload.package_id)
        .order_by(Bill.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    package_rate = package.price_per_session or Decimal("0")
    sessions_consumed = package.sessions_taken
    total_paid = bill.amount_paid if bill else Decimal("0")

    deduction, refund, notes = _calculate(
        sessions_consumed,
        payload.single_session_rate,
        payload.package_session_rate,
        payload.consultation_fee,
        total_paid,
    )

    # Admins/superadmins self-approve; clinic users need approval
    is_admin = user.role.name in (RoleName.ADMIN, RoleName.SUPERADMIN)
    status = RefundStatus.APPROVED if is_admin else RefundStatus.PENDING_APPROVAL

    refund_req = RefundRequest(
        package_id=payload.package_id,
        bill_id=bill.id if bill else None,
        patient_id=package.patient_id,
        clinic_id=package.clinic_id,
        status=status,
        initiated_by_id=user.id,
        reason=payload.reason,
        sessions_consumed=sessions_consumed,
        sessions_registered=package.sessions_registered,
        single_session_rate=payload.single_session_rate,
        package_session_rate=payload.package_session_rate,
        consultation_fee=payload.consultation_fee,
        total_paid=total_paid,
        deduction_amount=deduction,
        refund_amount=refund,
        calculation_notes=payload.calculation_notes or notes,
        payment_method=payload.payment_method,
        bank_account_name=payload.bank_account_name,
        bank_account_number=payload.bank_account_number,
        bank_ifsc=payload.bank_ifsc,
        upi_id=payload.upi_id,
    )

    if is_admin:
        refund_req.reviewed_by_id = user.id
        refund_req.reviewed_at = _now()
        refund_req.review_notes = "Self-approved on initiation"

    db.add(refund_req)
    db.flush()

    audit_service.record(
        db,
        action=AuditAction.REFUND_INITIATED,
        user=user,
        entity_type="refund_request",
        entity_id=refund_req.id,
        clinic_id=package.clinic_id,
        description=f"Refund initiated for package {payload.package_id}; ₹{refund} to be refunded",
        request=request,
    )

    db.commit()
    db.refresh(refund_req)
    return refund_req


def update_calculation(
    db: Session,
    user: User,
    refund_id: int,
    payload: RefundCalculationUpdate,
    request: Request | None = None,
) -> RefundRequest:
    refund = _get_or_404(db, refund_id)
    _assert_can_mutate(user, refund)

    if refund.status not in (RefundStatus.PENDING_APPROVAL, RefundStatus.APPROVED):
        raise ValidationError("Calculation can only be updated on open refund requests")

    # Admins can edit even after approval; clinic users only before approval
    if user.role.name == RoleName.CLINIC_USER and refund.status == RefundStatus.APPROVED:
        raise PermissionDeniedError("Calculation cannot be changed after admin approval")

    refund.single_session_rate = payload.single_session_rate
    refund.package_session_rate = payload.package_session_rate
    refund.consultation_fee = payload.consultation_fee
    refund.deduction_amount = payload.deduction_amount
    refund.refund_amount = payload.refund_amount
    refund.calculation_notes = payload.calculation_notes

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=user,
        entity_type="refund_request",
        entity_id=refund.id,
        clinic_id=refund.clinic_id,
        description=f"Refund calculation updated: deduction=₹{payload.deduction_amount}, refund=₹{payload.refund_amount}",
        request=request,
    )

    db.commit()
    db.refresh(refund)
    return refund


def review(
    db: Session,
    user: User,
    refund_id: int,
    payload: RefundReview,
    request: Request | None = None,
) -> RefundRequest:
    _assert_is_reviewer(user)
    refund = _get_or_404(db, refund_id)

    if refund.status != RefundStatus.PENDING_APPROVAL:
        raise ValidationError("Only pending refund requests can be reviewed")

    refund.reviewed_by_id = user.id
    refund.reviewed_at = _now()
    refund.review_notes = payload.review_notes

    if payload.approved:
        refund.status = RefundStatus.APPROVED
        _apply_financial_effect(db, refund)
        audit_service.record(
            db,
            action=AuditAction.REFUND_APPROVED,
            user=user,
            entity_type="refund_request",
            entity_id=refund.id,
            clinic_id=refund.clinic_id,
            description=f"Refund #{refund.id} approved; ₹{refund.refund_amount} to be transferred",
            request=request,
        )
    else:
        refund.status = RefundStatus.REJECTED
        audit_service.record(
            db,
            action=AuditAction.REFUND_REJECTED,
            user=user,
            entity_type="refund_request",
            entity_id=refund.id,
            clinic_id=refund.clinic_id,
            description=f"Refund #{refund.id} rejected",
            request=request,
        )

    db.commit()
    db.refresh(refund)
    return refund


def _apply_financial_effect(db: Session, refund: RefundRequest) -> None:
    """Cancel the linked bill and mark package as cancelled when refund is approved."""
    if refund.bill_id:
        bill = db.get(Bill, refund.bill_id)
        if bill:
            bill.status = BillStatus.CANCELLED
            bill.notes = (bill.notes or "") + f" | Cancelled via refund request #{refund.id}"

    package = db.get(TreatmentPackage, refund.package_id)
    if package and package.status != PackageStatus.CANCELLED:
        package.status = PackageStatus.CANCELLED
        package.notes = (package.notes or "") + f" | Cancelled via refund request #{refund.id}"

    # Void any pending referral credits tied to this package
    from app.services import referral_service
    referral_service.void_for_package(
        db, refund.package_id,
        reason=f"Package refunded (refund request #{refund.id})",
    )

    # Reverse Physio Points earned/redeemed on this bill
    if refund.bill_id:
        from app.services import physio_points_service
        # actor = None here (system action during approval); reviewer is tracked on the refund
        class _SystemActor:
            id = None
        physio_points_service.reverse_on_refund(db, refund.bill_id, refund.id, _SystemActor())


def complete(
    db: Session,
    user: User,
    refund_id: int,
    payload: RefundComplete,
    request: Request | None = None,
) -> RefundRequest:
    _assert_is_reviewer(user)
    refund = _get_or_404(db, refund_id)

    if refund.status != RefundStatus.APPROVED:
        raise ValidationError("Only approved refunds can be marked complete")

    refund.status = RefundStatus.COMPLETED
    refund.completed_by_id = user.id
    refund.completed_at = _now()
    refund.payment_reference = payload.payment_reference

    audit_service.record(
        db,
        action=AuditAction.REFUND_COMPLETED,
        user=user,
        entity_type="refund_request",
        entity_id=refund.id,
        clinic_id=refund.clinic_id,
        description=f"Refund #{refund.id} completed; reference: {payload.payment_reference}",
        request=request,
    )

    db.commit()
    db.refresh(refund)
    return refund


def add_attachment(
    db: Session,
    user: User,
    refund_id: int,
    attachment_type: str,
    file_name: str,
    mime_type: str,
    file_content: bytes,
) -> RefundAttachment:
    refund = _get_or_404(db, refund_id)
    _assert_can_mutate(user, refund)

    if refund.status == RefundStatus.COMPLETED:
        raise ValidationError("Cannot add attachments to a completed refund")

    attachment = RefundAttachment(
        refund_request_id=refund_id,
        uploaded_by_id=user.id,
        attachment_type=attachment_type,
        file_name=file_name,
        mime_type=mime_type,
        file_content=file_content,
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)
    return attachment


def get_attachment_content(
    db: Session, user: User, refund_id: int, attachment_id: int
) -> RefundAttachment:
    refund = _get_or_404(db, refund_id)
    _assert_can_view(user, refund)
    attachment = db.get(RefundAttachment, attachment_id)
    if not attachment or attachment.refund_request_id != refund_id:
        raise NotFoundError("Attachment not found")
    return attachment


def list_refunds(
    db: Session,
    user: User,
    status: str | None = None,
    clinic_id: int | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[RefundRequest], int]:
    stmt = select(RefundRequest).order_by(RefundRequest.created_at.desc())

    # Clinic users see only their own clinic's refunds
    if user.role.name == RoleName.CLINIC_USER:
        user_clinic_ids = [cu.clinic_id for cu in user.clinic_users]
        stmt = stmt.where(RefundRequest.clinic_id.in_(user_clinic_ids))
    elif clinic_id:
        stmt = stmt.where(RefundRequest.clinic_id == clinic_id)

    if status:
        stmt = stmt.where(RefundRequest.status == status)

    from sqlalchemy import func
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.execute(count_stmt).scalar() or 0

    offset = (page - 1) * page_size
    items = db.execute(stmt.offset(offset).limit(page_size)).scalars().all()
    return list(items), total


def get_refund(db: Session, user: User, refund_id: int) -> RefundRequest:
    refund = _get_or_404(db, refund_id)
    _assert_can_view(user, refund)
    return refund
