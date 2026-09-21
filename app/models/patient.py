"""Patients and the configurable patient-source lookup."""

from datetime import date
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import Gender
from app.models.mixins import TimestampMixin, enum_column

if TYPE_CHECKING:  # pragma: no cover
    from app.models.appointment import Appointment
    from app.models.billing import Bill
    from app.models.clinic import Clinic
    from app.models.session import PatientSession, TreatmentPackage


class PatientSource(Base, TimestampMixin):
    """Marketing/acquisition source. Editable by the Superadmin (Section 11)."""

    __tablename__ = "patient_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    patients: Mapped[list["Patient"]] = relationship(back_populates="source")


class Patient(Base, TimestampMixin):
    """A patient is chain-wide: one permanent Patient ID across all clinics.

    `primary_clinic_id` records where they registered / are usually treated, but
    the identity itself is never duplicated per clinic (Section 38).
    """

    __tablename__ = "patients"
    __table_args__ = (
        CheckConstraint("age IS NULL OR (age >= 0 AND age <= 130)", name="ck_patients_age_range"),
        Index("ix_patients_name_mobile", "full_name", "mobile"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Human-facing identifier, e.g. PT-000001.
    patient_code: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    mobile: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    #: Optional, and often different from `mobile` -- a patient may give a
    #: landline or a relative's number to call but their own for WhatsApp.
    #: Indexed because Phase 6 will look patients up from inbound messages.
    whatsapp_number: Mapped[str | None] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    age: Mapped[int | None] = mapped_column(Integer)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[Gender | None] = enum_column(Gender, nullable=True)
    address: Mapped[str | None] = mapped_column(String(500))
    chief_complaint: Mapped[str | None] = mapped_column(Text)
    diagnosis: Mapped[str | None] = mapped_column(Text)

    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("patient_sources.id", ondelete="SET NULL"), index=True
    )
    source_detail: Mapped[str | None] = mapped_column(String(255))
    registration_date: Mapped[date | None] = mapped_column(Date)
    primary_clinic_id: Mapped[int | None] = mapped_column(
        ForeignKey("clinics.id", ondelete="SET NULL"), index=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    #: False while only the minimal Admin-booking fields are filled in (Section 11).
    is_profile_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def whatsapp_contact(self) -> str:
        """Number to send WhatsApp to: the dedicated one, else the mobile.

        Having this in one place means Phase 6/7 message senders cannot forget
        the fallback and silently skip patients who left the field blank.
        """
        return self.whatsapp_number or self.mobile

    source: Mapped[PatientSource | None] = relationship(back_populates="patients", lazy="joined")
    primary_clinic: Mapped["Clinic | None"] = relationship(lazy="joined")
    appointments: Mapped[list["Appointment"]] = relationship(
        back_populates="patient", order_by="desc(Appointment.appointment_date)"
    )
    packages: Mapped[list["TreatmentPackage"]] = relationship(back_populates="patient")
    sessions: Mapped[list["PatientSession"]] = relationship(back_populates="patient")
    bills: Mapped[list["Bill"]] = relationship(back_populates="patient")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Patient {self.patient_code} {self.full_name}>"
