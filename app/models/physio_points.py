from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.mixins import enum_column
from app.models.enums import PointsTransactionType


class PhysioPointsLedger(Base):
    """Append-only ledger for Physio Points (loyalty programme).

    Positive `points` = credit to patient.
    Negative `points` = debit from patient.
    The patient's live balance is the denormalised `patients.physio_points`
    column, updated atomically with each ledger entry. The ledger is the
    source of truth for history and audit.
    """

    __tablename__ = "physio_points_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    transaction_type: Mapped[PointsTransactionType] = enum_column(
        PointsTransactionType, nullable=False
    )
    payment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payments.id", ondelete="SET NULL"), nullable=True
    )
    bill_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bills.id", ondelete="SET NULL"), nullable=True
    )
    refund_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("refund_requests.id", ondelete="SET NULL"), nullable=True
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    #: Only set on EARNED entries — date after which these points expire.
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: On EXPIRED entries: the id of the EARNED ledger row being expired.
    source_entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    patient = relationship("Patient", lazy="select")
    created_by = relationship("User", lazy="select")
