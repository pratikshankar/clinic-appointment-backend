"""Refund requests for cancelled treatment packages.

Workflow
--------
CLINIC_USER initiates  → status PENDING_APPROVAL  → admin receives notification
ADMIN/SUPERADMIN       → status APPROVED (self-approved if they initiate)
                       → or they can review a PENDING_APPROVAL and approve/reject
On APPROVED            → linked bill is cancelled, clinic earnings adjusted
On COMPLETED           → admin attaches payment proof and closes the request

The calculation is always editable before the request is approved.
"""

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import RefundAttachmentType, RefundPaymentMethod, RefundStatus
from app.models.mixins import TimestampMixin, UTCDateTime, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from app.models.billing import Bill
    from app.models.patient import Patient
    from app.models.session import TreatmentPackage
    from app.models.user import User


class RefundRequest(Base, TimestampMixin):
    """One refund request per cancelled package."""

    __tablename__ = "refund_requests"
    __table_args__ = (
        CheckConstraint("refund_amount >= 0", name="ck_refund_amount_non_negative"),
        CheckConstraint("deduction_amount >= 0", name="ck_refund_deduction_non_negative"),
        Index("ix_refund_requests_clinic", "clinic_id"),
        Index("ix_refund_requests_patient", "patient_id"),
        Index("ix_refund_requests_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- linked records ---
    package_id: Mapped[int] = mapped_column(
        ForeignKey("treatment_packages.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bill_id: Mapped[int | None] = mapped_column(
        ForeignKey("bills.id", ondelete="SET NULL")
    )
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="RESTRICT"), nullable=False
    )

    # --- status ---
    status: Mapped[RefundStatus] = enum_column(
        RefundStatus, default=RefundStatus.PENDING_APPROVAL, nullable=False
    )

    # --- initiation ---
    initiated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # --- calculation (all editable before approval) ---
    sessions_consumed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sessions_registered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Rate charged per session when consumed < 5 (higher "walk-in" rate)
    single_session_rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    # Package rate per session for the purchased block
    package_session_rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    # Non-refundable consultation fee extracted from the original bill
    consultation_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    # Total amount originally paid on the bill
    total_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    # Computed or manually overridden deduction
    deduction_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    # Computed or manually overridden refund
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    # Free-text note explaining the calculation breakdown
    calculation_notes: Mapped[str | None] = mapped_column(Text)

    # --- payment details ---
    payment_method: Mapped[RefundPaymentMethod | None] = enum_column(
        RefundPaymentMethod, nullable=True
    )
    bank_account_name: Mapped[str | None] = mapped_column(String(120))
    bank_account_number: Mapped[str | None] = mapped_column(String(30))
    bank_ifsc: Mapped[str | None] = mapped_column(String(15))
    upi_id: Mapped[str | None] = mapped_column(String(60))

    # --- review (admin/superadmin) ---
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[UTCDateTime | None] = mapped_column(UTCDateTime, nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text)

    # --- completion ---
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    completed_at: Mapped[UTCDateTime | None] = mapped_column(UTCDateTime, nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(120))

    # --- relationships ---
    package: Mapped["TreatmentPackage"] = relationship(lazy="joined")
    bill: Mapped["Bill | None"] = relationship()
    patient: Mapped["Patient"] = relationship(lazy="joined")
    initiated_by: Mapped["User | None"] = relationship(
        foreign_keys=[initiated_by_id], lazy="joined"
    )
    reviewed_by: Mapped["User | None"] = relationship(foreign_keys=[reviewed_by_id])
    completed_by: Mapped["User | None"] = relationship(foreign_keys=[completed_by_id])
    attachments: Mapped[list["RefundAttachment"]] = relationship(
        back_populates="refund_request", cascade="all, delete-orphan"
    )


class RefundAttachment(Base, TimestampMixin):
    """File attached to a refund request — cancellation documents or payment proof."""

    __tablename__ = "refund_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    refund_request_id: Mapped[int] = mapped_column(
        ForeignKey("refund_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    uploaded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    attachment_type: Mapped[RefundAttachmentType] = enum_column(
        RefundAttachmentType, nullable=False
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # Stored as binary in PostgreSQL — suitable for PDFs/images up to a few MB
    file_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    refund_request: Mapped[RefundRequest] = relationship(back_populates="attachments")
    uploaded_by: Mapped["User | None"] = relationship()
