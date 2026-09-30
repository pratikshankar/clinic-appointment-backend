"""Schemas for refund requests."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import RefundAttachmentType, RefundPaymentMethod, RefundStatus


# --------------------------------------------------------------------------- #
# Initiation
# --------------------------------------------------------------------------- #

class RefundInitiate(BaseModel):
    package_id: int
    reason: str = Field(min_length=10, max_length=2000)
    # Calculation inputs (pre-filled by backend; staff can override before submit)
    single_session_rate: Decimal = Field(ge=0)
    package_session_rate: Decimal = Field(ge=0)
    consultation_fee: Decimal = Field(ge=0, default=Decimal("0"))
    calculation_notes: str | None = None
    # Payment details
    payment_method: RefundPaymentMethod
    bank_account_name: str | None = Field(default=None, max_length=120)
    bank_account_number: str | None = Field(default=None, max_length=30)
    bank_ifsc: str | None = Field(default=None, max_length=15)
    upi_id: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _validate_payment_details(self):
        if self.payment_method == RefundPaymentMethod.BANK_TRANSFER:
            missing = [
                f for f in ("bank_account_name", "bank_account_number", "bank_ifsc")
                if not getattr(self, f)
            ]
            if missing:
                raise ValueError(
                    f"Bank transfer requires: {', '.join(missing)}"
                )
        elif self.payment_method == RefundPaymentMethod.UPI:
            if not self.upi_id:
                raise ValueError("UPI payment requires a UPI ID")
        return self


# --------------------------------------------------------------------------- #
# Calculation update (editable independently before approval)
# --------------------------------------------------------------------------- #

class RefundCalculationUpdate(BaseModel):
    single_session_rate: Decimal = Field(ge=0)
    package_session_rate: Decimal = Field(ge=0)
    consultation_fee: Decimal = Field(ge=0)
    deduction_amount: Decimal = Field(ge=0)
    refund_amount: Decimal = Field(ge=0)
    calculation_notes: str | None = None


# --------------------------------------------------------------------------- #
# Admin review
# --------------------------------------------------------------------------- #

class RefundReview(BaseModel):
    approved: bool
    review_notes: str | None = Field(default=None, max_length=1000)


# --------------------------------------------------------------------------- #
# Completion (admin uploads proof and closes)
# --------------------------------------------------------------------------- #

class RefundComplete(BaseModel):
    payment_reference: str = Field(min_length=1, max_length=120)


# --------------------------------------------------------------------------- #
# Response models
# --------------------------------------------------------------------------- #

class AttachmentOut(BaseModel):
    id: int
    attachment_type: RefundAttachmentType
    file_name: str
    mime_type: str
    uploaded_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, obj):
        return cls(
            id=obj.id,
            attachment_type=obj.attachment_type,
            file_name=obj.file_name,
            mime_type=obj.mime_type,
            uploaded_at=obj.created_at,
        )


class RefundRequestOut(BaseModel):
    id: int
    status: RefundStatus
    package_id: int
    bill_id: int | None
    patient_id: int
    patient_name: str | None
    patient_code: str | None
    clinic_id: int
    clinic_name: str | None
    reason: str
    sessions_consumed: int
    sessions_registered: int
    single_session_rate: Decimal
    package_session_rate: Decimal
    consultation_fee: Decimal
    total_paid: Decimal
    deduction_amount: Decimal
    refund_amount: Decimal
    calculation_notes: str | None
    payment_method: RefundPaymentMethod | None
    bank_account_name: str | None
    bank_account_number: str | None
    bank_ifsc: str | None
    upi_id: str | None
    initiated_by_name: str | None
    reviewed_by_name: str | None
    reviewed_at: datetime | None
    review_notes: str | None
    completed_by_name: str | None
    completed_at: datetime | None
    payment_reference: str | None
    attachments: list[AttachmentOut]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, r):
        return cls(
            id=r.id,
            status=r.status,
            package_id=r.package_id,
            bill_id=r.bill_id,
            patient_id=r.patient_id,
            patient_name=r.patient.full_name if r.patient else None,
            patient_code=r.patient.patient_code if r.patient else None,
            clinic_id=r.clinic_id,
            clinic_name=r.package.clinic.name if r.package and r.package.clinic else None,
            reason=r.reason,
            sessions_consumed=r.sessions_consumed,
            sessions_registered=r.sessions_registered,
            single_session_rate=r.single_session_rate,
            package_session_rate=r.package_session_rate,
            consultation_fee=r.consultation_fee,
            total_paid=r.total_paid,
            deduction_amount=r.deduction_amount,
            refund_amount=r.refund_amount,
            calculation_notes=r.calculation_notes,
            payment_method=r.payment_method,
            bank_account_name=r.bank_account_name,
            bank_account_number=r.bank_account_number,
            bank_ifsc=r.bank_ifsc,
            upi_id=r.upi_id,
            initiated_by_name=r.initiated_by.full_name if r.initiated_by else None,
            reviewed_by_name=r.reviewed_by.full_name if r.reviewed_by else None,
            reviewed_at=r.reviewed_at,
            review_notes=r.review_notes,
            completed_by_name=r.completed_by.full_name if r.completed_by else None,
            completed_at=r.completed_at,
            payment_reference=r.payment_reference,
            attachments=[AttachmentOut.from_orm(a) for a in r.attachments],
            created_at=r.created_at,
            updated_at=r.updated_at,
        )


class RefundListItem(BaseModel):
    id: int
    status: RefundStatus
    patient_name: str | None
    patient_code: str | None
    clinic_name: str | None
    reason: str
    sessions_consumed: int
    sessions_registered: int
    refund_amount: Decimal
    payment_method: RefundPaymentMethod | None
    initiated_by_name: str | None
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, r):
        return cls(
            id=r.id,
            status=r.status,
            patient_name=r.patient.full_name if r.patient else None,
            patient_code=r.patient.patient_code if r.patient else None,
            clinic_name=r.package.clinic.name if r.package and r.package.clinic else None,
            reason=r.reason,
            sessions_consumed=r.sessions_consumed,
            sessions_registered=r.sessions_registered,
            refund_amount=r.refund_amount,
            payment_method=r.payment_method,
            initiated_by_name=r.initiated_by.full_name if r.initiated_by else None,
            created_at=r.created_at,
        )


class RefundPrefill(BaseModel):
    """Pre-filled calculation values for the initiation form."""
    package_id: int
    sessions_consumed: int
    sessions_registered: int
    package_session_rate: Decimal
    consultation_fee: Decimal
    total_paid: Decimal
    suggested_single_session_rate: Decimal
    suggested_deduction: Decimal
    suggested_refund: Decimal
    calculation_notes: str
