"""Referral records — staff-managed patient referral programme.

Workflow
--------
Staff creates a referral linking a referrer patient to a referred patient
and their qualifying package.

Status machine
--------------
PENDING   → waiting for staff to apply credit
CREDITED  → staff applied the session credit to the referrer's active package
VOIDED    → referred patient's package was cancelled/refunded before credit
            was applied; no credit is awarded

The void fires automatically whenever the referred package is cancelled,
either through the package-cancel endpoint or through refund approval.
"""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import ReferralStatus
from app.models.mixins import TimestampMixin, UTCDateTime, enum_column

if TYPE_CHECKING:
    from app.models.patient import Patient
    from app.models.session import TreatmentPackage
    from app.models.user import User


class ReferralRecord(Base, TimestampMixin):
    """One referral event: referrer patient earned credit via a referred package."""

    __tablename__ = "referral_records"
    __table_args__ = (
        Index("ix_referrals_referrer", "referrer_patient_id"),
        Index("ix_referrals_referred", "referred_patient_id"),
        Index("ix_referrals_package", "referred_package_id"),
        Index("ix_referrals_status", "status"),
        Index("ix_referrals_clinic", "clinic_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Patient who made the referral (earns the credit)
    referrer_patient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    # Patient who was referred (the new/returning patient)
    referred_patient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    # Package purchased by the referred patient that triggered this referral
    referred_package_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("treatment_packages.id", ondelete="RESTRICT"), nullable=False
    )
    clinic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clinics.id", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[ReferralStatus] = enum_column(
        ReferralStatus, default=ReferralStatus.PENDING, nullable=False
    )

    # Sessions to credit the referrer (staff sets this at creation)
    credit_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    notes: Mapped[str | None] = mapped_column(Text)

    # ── Credit application ───────────────────────────────────────────────────
    # The referrer's package that received the bonus sessions
    credited_to_package_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("treatment_packages.id", ondelete="SET NULL"), nullable=True
    )
    credited_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    credited_at: Mapped[UTCDateTime | None] = mapped_column(UTCDateTime, nullable=True)

    # ── Void ─────────────────────────────────────────────────────────────────
    voided_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Staff who registered the referral
    initiated_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    referrer_patient: Mapped["Patient"] = relationship(
        "Patient", foreign_keys=[referrer_patient_id]
    )
    referred_patient: Mapped["Patient"] = relationship(
        "Patient", foreign_keys=[referred_patient_id]
    )
    referred_package: Mapped["TreatmentPackage"] = relationship(
        "TreatmentPackage", foreign_keys=[referred_package_id]
    )
    credited_to_package: Mapped["TreatmentPackage | None"] = relationship(
        "TreatmentPackage", foreign_keys=[credited_to_package_id]
    )
    initiated_by: Mapped["User | None"] = relationship(
        "User", foreign_keys=[initiated_by_id]
    )
    credited_by: Mapped["User | None"] = relationship(
        "User", foreign_keys=[credited_by_id]
    )
