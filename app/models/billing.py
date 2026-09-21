"""Bills, line items, payments and adjustments (Sections 17-19 and 38).

Money is `Numeric(12, 2)` everywhere and handled as `Decimal` in Python -- never
float. Finalised bills are treated as immutable: corrections are recorded as
adjustment rows rather than edits to historical financial data.
"""

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import (
    AdjustmentType,
    BillItemType,
    BillStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.models.mixins import TimestampMixin, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from app.models.clinic import Clinic
    from app.models.patient import Patient
    from app.models.session import TreatmentPackage
    from app.models.user import User


class Bill(Base, TimestampMixin):
    __tablename__ = "bills"
    __table_args__ = (
        CheckConstraint("total_amount >= 0", name="ck_bills_total_non_negative"),
        CheckConstraint("amount_paid >= 0", name="ck_bills_paid_non_negative"),
        Index("ix_bills_clinic_date", "clinic_id", "bill_date"),
        Index("ix_bills_patient", "patient_id"),
        Index("ix_bills_payment_status", "payment_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="RESTRICT"), nullable=False
    )
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    package_id: Mapped[int | None] = mapped_column(
        ForeignKey("treatment_packages.id", ondelete="SET NULL")
    )
    bill_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: Session counts are snapshotted onto the bill so a reprint of an old
    #: invoice shows what it showed on the day it was issued.
    sessions_purchased: Mapped[int | None] = mapped_column(Integer)
    sessions_taken_snapshot: Mapped[int | None] = mapped_column(Integer)
    price_per_session: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    subtotal_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    status: Mapped[BillStatus] = enum_column(BillStatus, default=BillStatus.DRAFT, nullable=False)
    payment_status: Mapped[PaymentStatus] = enum_column(
        PaymentStatus, default=PaymentStatus.UNPAID, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    clinic: Mapped["Clinic"] = relationship(lazy="joined")
    patient: Mapped["Patient"] = relationship(back_populates="bills", lazy="joined")
    package: Mapped["TreatmentPackage | None"] = relationship()
    items: Mapped[list["BillItem"]] = relationship(
        back_populates="bill", cascade="all, delete-orphan"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="bill", cascade="all, delete-orphan", order_by="Payment.payment_date"
    )
    adjustments: Mapped[list["BillAdjustment"]] = relationship(
        back_populates="bill", cascade="all, delete-orphan"
    )

    @property
    def balance_amount(self) -> Decimal:
        return self.total_amount - self.amount_paid

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Bill {self.bill_number} {self.total_amount}>"


class BillItem(Base, TimestampMixin):
    __tablename__ = "bill_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_bill_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_bill_items_price_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(
        ForeignKey("bills.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_type: Mapped[BillItemType] = enum_column(
        BillItemType, default=BillItemType.SESSION_PACKAGE, nullable=False
    )
    #: Which catalogue entry this line came from, when it came from one. Lets
    #: Phase 8 report revenue per service without matching on description text.
    #: The description is still copied onto the line, so renaming a service later
    #: does not rewrite historical bills.
    service_item_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "service_items.id",
            ondelete="SET NULL",
            name="fk_bill_items_service_item_id",
        )
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    bill: Mapped[Bill] = relationship(back_populates="items")


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        Index("ix_payments_bill_date", "bill_id", "payment_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(
        ForeignKey("bills.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_method: Mapped[PaymentMethod] = enum_column(PaymentMethod, nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    reference_number: Mapped[str | None] = mapped_column(String(100))
    received_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(String(500))

    bill: Mapped[Bill] = relationship(back_populates="payments")
    received_by: Mapped["User | None"] = relationship()


class BillAdjustment(Base, TimestampMixin):
    """Credit note / cancellation trail for a finalised bill (Section 38)."""

    __tablename__ = "bill_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(
        ForeignKey("bills.id", ondelete="CASCADE"), nullable=False, index=True
    )
    adjustment_type: Mapped[AdjustmentType] = enum_column(AdjustmentType, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    bill: Mapped[Bill] = relationship(back_populates="adjustments")
