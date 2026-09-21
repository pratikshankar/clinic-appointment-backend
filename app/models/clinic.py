"""Clinic, working hours, breaks and holidays.

Working hours are stored as *shifts* (one row per contiguous open period) so a
clinic can run 09:00-13:00 and 16:00-20:00 on the same day, which is the exact
example given in Section 5. Breaks are separate rows so a lunch break can be
carved out of a single long shift without splitting it.
"""

from datetime import date, time
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import ClinicStatus
from app.models.mixins import TimestampMixin, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from app.models.user import ClinicUser


class Clinic(Base, TimestampMixin):
    __tablename__ = "clinics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    address: Mapped[str | None] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(150))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    pin_code: Mapped[str | None] = mapped_column(String(12))
    phone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[ClinicStatus] = enum_column(
        ClinicStatus, default=ClinicStatus.ACTIVE, nullable=False, index=True
    )

    # --- appointment configuration (Section 5) ---
    slot_duration_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    capacity_per_slot: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    # --- branding on invoices, receipts and treatment statements ---
    #
    # A document is issued *by a clinic*, so the brand on it belongs to the
    # clinic rather than to the deployment. Every field here is NULL for a
    # clinic that trades under the chain name, which is what makes adding a
    # differently-branded branch a data change rather than a code change.
    brand_name: Mapped[str | None] = mapped_column(String(120))
    brand_tagline: Mapped[str | None] = mapped_column(String(160))
    #: Filename inside `app/assets/`. NULL falls back to the chain logo.
    logo_filename: Mapped[str | None] = mapped_column(String(120))
    #: Registration / GST lines printed at the foot of a document. A separately
    #: registered clinic files under its own numbers, so this cannot be global.
    document_footer: Mapped[str | None] = mapped_column(Text)
    #: Invoice series for this clinic, e.g. "PC" -> PC-2026-000001. NULL uses the
    #: chain series ("INV"). A separately registered entity needs its own
    #: unbroken sequence: sharing one across two entities leaves gaps in each
    #: set of books, which is the first thing an auditor asks about.
    bill_number_prefix: Mapped[str | None] = mapped_column(String(10))

    __table_args__ = (
        CheckConstraint("slot_duration_minutes > 0", name="ck_clinics_slot_duration_positive"),
        CheckConstraint("capacity_per_slot > 0", name="ck_clinics_capacity_positive"),
    )

    working_hours: Mapped[list["ClinicWorkingHour"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan", order_by="ClinicWorkingHour.day_of_week"
    )
    breaks: Mapped[list["ClinicBreak"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    holidays: Mapped[list["ClinicHoliday"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )
    user_links: Mapped[list["ClinicUser"]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.status == ClinicStatus.ACTIVE

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Clinic {self.code} {self.name}>"


class ClinicWorkingHour(Base, TimestampMixin):
    """One open shift for one weekday. `day_of_week`: 0 = Monday .. 6 = Sunday."""

    __tablename__ = "clinic_working_hours"
    __table_args__ = (
        CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="ck_working_hours_dow"),
        Index("ix_working_hours_clinic_day", "clinic_id", "day_of_week"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    open_time: Mapped[time] = mapped_column(Time, nullable=False)
    close_time: Mapped[time] = mapped_column(Time, nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    label: Mapped[str | None] = mapped_column(String(50))

    clinic: Mapped[Clinic] = relationship(back_populates="working_hours")


class ClinicBreak(Base, TimestampMixin):
    """A recurring break. `day_of_week = NULL` means it applies every working day."""

    __tablename__ = "clinic_breaks"
    __table_args__ = (
        CheckConstraint(
            "day_of_week IS NULL OR (day_of_week >= 0 AND day_of_week <= 6)",
            name="ck_clinic_breaks_dow",
        ),
        Index("ix_clinic_breaks_clinic_day", "clinic_id", "day_of_week"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    day_of_week: Mapped[int | None] = mapped_column(Integer)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    label: Mapped[str] = mapped_column(String(50), default="Lunch break", nullable=False)

    clinic: Mapped[Clinic] = relationship(back_populates="breaks")


class ClinicHoliday(Base, TimestampMixin):
    """A closed day. `clinic_id = NULL` means the whole chain is closed."""

    __tablename__ = "clinic_holidays"
    __table_args__ = (
        UniqueConstraint("clinic_id", "holiday_date", name="uq_clinic_holiday_date"),
        Index("ix_clinic_holidays_date", "holiday_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int | None] = mapped_column(ForeignKey("clinics.id", ondelete="CASCADE"))
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))

    clinic: Mapped[Clinic | None] = relationship(back_populates="holidays")
