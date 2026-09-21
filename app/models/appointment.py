"""Appointments and their immutable history trail (Sections 8 and 27)."""

from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import AppointmentAction, AppointmentStatus
from app.models.mixins import TimestampMixin, enum_column, UTCDateTime

if TYPE_CHECKING:  # pragma: no cover
    from app.models.clinic import Clinic
    from app.models.patient import Patient
    from app.models.user import User


class Appointment(Base, TimestampMixin):
    __tablename__ = "appointments"
    __table_args__ = (
        # The slot-availability query filters on exactly these three columns, so
        # this index is what keeps capacity checks cheap under load.
        Index("ix_appointments_clinic_date_time", "clinic_id", "appointment_date", "start_time"),
        Index("ix_appointments_patient_date", "patient_id", "appointment_date"),
        Index("ix_appointments_status_date", "status", "appointment_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_code: Mapped[str] = mapped_column(
        String(24), unique=True, nullable=False, index=True
    )
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="RESTRICT"), nullable=False
    )
    appointment_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    status: Mapped[AppointmentStatus] = enum_column(
        AppointmentStatus, default=AppointmentStatus.BOOKED, nullable=False
    )
    chief_complaint: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    package_id: Mapped[int | None] = mapped_column(
        ForeignKey("treatment_packages.id", ondelete="SET NULL")
    )
    #: Set on the *new* appointment created by a reschedule; the old row is kept.
    rescheduled_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
    checked_in_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))

    patient: Mapped["Patient"] = relationship(back_populates="appointments", lazy="joined")
    clinic: Mapped["Clinic"] = relationship(lazy="joined")
    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_user_id])
    history: Mapped[list["AppointmentHistory"]] = relationship(
        back_populates="appointment",
        cascade="all, delete-orphan",
        order_by="AppointmentHistory.changed_at",
    )
    rescheduled_from: Mapped["Appointment | None"] = relationship(remote_side=[id])

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Appointment {self.appointment_code} {self.appointment_date} {self.start_time}>"


class AppointmentHistory(Base):
    """Append-only trail. Rows are never updated or deleted (Section 27)."""

    __tablename__ = "appointment_history"
    __table_args__ = (Index("ix_appointment_history_appointment", "appointment_id", "changed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[AppointmentAction] = enum_column(AppointmentAction, nullable=False)
    old_date: Mapped[date | None] = mapped_column(Date)
    old_time: Mapped[time | None] = mapped_column(Time)
    new_date: Mapped[date | None] = mapped_column(Date)
    new_time: Mapped[time | None] = mapped_column(Time)
    old_status: Mapped[AppointmentStatus | None] = enum_column(AppointmentStatus, nullable=True)
    new_status: Mapped[AppointmentStatus | None] = enum_column(AppointmentStatus, nullable=True)
    old_clinic_id: Mapped[int | None] = mapped_column(ForeignKey("clinics.id", ondelete="SET NULL"))
    new_clinic_id: Mapped[int | None] = mapped_column(ForeignKey("clinics.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(String(500))
    changed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    changed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(timezone=True), server_default=func.now(), nullable=False
    )

    appointment: Mapped[Appointment] = relationship(back_populates="history")
    changed_by: Mapped["User | None"] = relationship()
