"""Treatment packages and physiotherapy session logs (Sections 12 and 13)."""

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import PackageStatus
from app.models.mixins import TimestampMixin, enum_column, UTCDateTime

if TYPE_CHECKING:  # pragma: no cover
    from app.models.appointment import Appointment
    from app.models.clinic import Clinic
    from app.models.patient import Patient
    from app.models.user import User


class TreatmentPackage(Base, TimestampMixin):
    """A block of purchased sessions.

    `sessions_remaining` is intentionally *not* a stored column: deriving it
    removes any possibility of the three numbers drifting apart. The database
    guards the invariants that Section 12 requires.
    """

    __tablename__ = "treatment_packages"
    __table_args__ = (
        CheckConstraint("sessions_registered > 0", name="ck_package_registered_positive"),
        CheckConstraint("sessions_taken >= 0", name="ck_package_taken_non_negative"),
        # This is the constraint that makes "sessions remaining can never go
        # negative" a database-level guarantee rather than a hope.
        CheckConstraint(
            "sessions_taken <= sessions_registered", name="ck_package_taken_within_registered"
        ),
        Index("ix_packages_patient_status", "patient_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    package_name: Mapped[str | None] = mapped_column(String(120))
    sessions_registered: Mapped[int] = mapped_column(Integer, nullable=False)
    sessions_taken: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    price_per_session: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    status: Mapped[PackageStatus] = enum_column(
        PackageStatus, default=PackageStatus.ACTIVE, nullable=False
    )
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    patient: Mapped["Patient"] = relationship(back_populates="packages")
    clinic: Mapped["Clinic"] = relationship(lazy="joined")
    sessions: Mapped[list["PatientSession"]] = relationship(
        back_populates="package", order_by="PatientSession.session_number"
    )

    @property
    def live_sessions(self) -> list["PatientSession"]:
        """Sessions that still count -- voided ones are excluded."""
        return [item for item in self.sessions if not item.is_voided]

    @property
    def is_live(self) -> bool:
        """Does this package still entitle the patient to anything?

        A cancelled package does not, however many sessions it was registered
        for. Roll-ups use this so a patient who cancelled a 10-session course is
        never shown as still holding 10 sessions.
        """
        return self.status != PackageStatus.CANCELLED

    @property
    def sessions_remaining(self) -> int:
        # A cancelled package has nothing remaining regardless of its counters.
        # Returning `registered - taken` here would say a patient who cancelled
        # before their first visit still has the full course available, which is
        # both wrong and the kind of thing reception acts on.
        if not self.is_live:
            return 0
        return max(self.sessions_registered - self.sessions_taken, 0)

    @property
    def total_amount(self) -> Decimal:
        return Decimal(self.sessions_registered) * self.price_per_session


class PatientSession(Base, TimestampMixin):
    """One recorded physiotherapy session. History is never overwritten."""

    __tablename__ = "patient_sessions"
    __table_args__ = (
        UniqueConstraint("package_id", "session_number", name="uq_session_package_number"),
        CheckConstraint("session_number > 0", name="ck_session_number_positive"),
        Index("ix_sessions_patient_date", "patient_id", "session_date"),
        Index("ix_sessions_clinic_date", "clinic_id", "session_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    package_id: Mapped[int | None] = mapped_column(
        ForeignKey("treatment_packages.id", ondelete="SET NULL")
    )
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinics.id", ondelete="RESTRICT"), nullable=False
    )
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL")
    )
    session_number: Mapped[int] = mapped_column(Integer, nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    therapist_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    treatment_provided: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    remarks: Mapped[str | None] = mapped_column(Text)

    # --- correction trail ---
    # A mis-logged session is voided rather than deleted: the session count is
    # corrected, but the fact that a correction happened survives (Section 27).
    # The session number is never reused, so the sequence stays auditable.
    is_voided: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    void_reason: Mapped[str | None] = mapped_column(String(500))
    # Named explicitly: SQLite has no ALTER for constraints, so Alembic
    # recreates the whole table in "batch" mode, and it refuses to re-add a
    # constraint it cannot name.
    voided_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_patient_sessions_voided_by_user_id",
        )
    )
    voided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))

    patient: Mapped["Patient"] = relationship(back_populates="sessions")
    package: Mapped[TreatmentPackage | None] = relationship(back_populates="sessions")
    clinic: Mapped["Clinic"] = relationship(lazy="joined")
    appointment: Mapped["Appointment | None"] = relationship()
    # Two FKs point at `users` now (the therapist and whoever voided the row),
    # so the join column has to be stated explicitly.
    therapist: Mapped["User | None"] = relationship(
        lazy="joined", foreign_keys=[therapist_user_id]
    )
    voided_by: Mapped["User | None"] = relationship(foreign_keys=[voided_by_user_id])
