"""Treatment package and session schemas (Sections 12, 13, 14).

`sessions_remaining` is never accepted as input anywhere in here. It is derived
from `registered - taken`, and `taken` is only ever moved by logging or voiding a
session, so the three numbers cannot drift apart.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import PackageStatus
from app.schemas.billing import BillLineInput, BillRead, PaymentInput
from app.schemas.common import ORMModel, clean_text


# --------------------------------------------------------------------------- #
# Treatment packages (Section 12)
# --------------------------------------------------------------------------- #
class PackageCreate(BaseModel):
    """A block of purchased sessions, and the money that changed hands for it.

    Registration is where the patient actually pays, so the charges and the
    payment are part of the same request rather than a second screen. That is
    also what keeps the two consistent: the package and its bill are written in
    one transaction, so a package can never exist without the bill that explains
    what was collected for it.
    """

    sessions_registered: int = Field(ge=1, le=500)
    price_per_session: Decimal = Field(default=Decimal("0.00"), ge=0, le=1_000_000)
    package_name: str | None = Field(default=None, max_length=120)
    clinic_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = None
    #: Sessions already delivered before this package was entered into the
    #: system. Kept explicit so a mid-course migration does not need direct
    #: database access, and bounded by the same invariant.
    sessions_taken: int = Field(default=0, ge=0)

    #: One-off charges billed alongside the package: consultation fee, an
    #: assessment, a support belt. Each becomes its own bill line, so the total
    #: is never a package price with something invisible folded into it.
    additional_charges: list[BillLineInput] = Field(default_factory=list)
    discount_amount: Decimal = Field(default=Decimal("0.00"), ge=0, le=1_000_000)
    #: Money received now. Omit it when the patient is being invoiced to pay
    #: later -- the bill is still created, and shows as unpaid.
    payment: PaymentInput | None = None
    #: Skip billing entirely (correcting a record, migrating history).
    skip_billing: bool = False

    @field_validator("package_name", "notes")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)

    @model_validator(mode="after")
    def _check(self):
        if self.sessions_taken > self.sessions_registered:
            raise ValueError("Sessions already taken cannot exceed sessions registered")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("End date cannot be before the start date")
        if self.skip_billing and (self.additional_charges or self.payment):
            raise ValueError(
                "skip_billing cannot be combined with charges or a payment"
            )
        if self.discount_amount > self.gross_amount:
            raise ValueError("Discount cannot be greater than the total charges")
        return self

    @property
    def package_amount(self) -> Decimal:
        return (self.price_per_session * self.sessions_registered).quantize(
            Decimal("0.01")
        )

    @property
    def gross_amount(self) -> Decimal:
        """Package plus one-off charges, before any discount."""
        extras = sum(
            (line.amount for line in self.additional_charges), Decimal("0.00")
        )
        return (self.package_amount + extras).quantize(Decimal("0.01"))

    @property
    def payable_amount(self) -> Decimal:
        return (self.gross_amount - self.discount_amount).quantize(Decimal("0.01"))


class PackageUpdate(BaseModel):
    """Editable package fields.

    `sessions_taken` is absent on purpose: it moves only by logging or voiding a
    session, never by direct assignment.
    """

    model_config = ConfigDict(extra="forbid")

    package_name: str | None = Field(default=None, max_length=120)
    price_per_session: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    sessions_registered: int | None = Field(default=None, ge=1, le=500)
    end_date: date | None = None
    notes: str | None = None

    @field_validator("package_name", "notes")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)


class PackageRead(ORMModel):
    id: int
    patient_id: int
    clinic_id: int
    clinic_name: str | None = None
    package_name: str | None = None
    sessions_registered: int
    sessions_taken: int
    sessions_remaining: int
    price_per_session: Decimal
    total_amount: Decimal
    status: PackageStatus
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = None
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if not hasattr(value, "sessions_registered"):
            return value
        return {
            "id": value.id,
            "patient_id": value.patient_id,
            "clinic_id": value.clinic_id,
            "clinic_name": value.clinic.name if value.clinic else None,
            "package_name": value.package_name,
            "sessions_registered": value.sessions_registered,
            "sessions_taken": value.sessions_taken,
            "sessions_remaining": value.sessions_remaining,
            "price_per_session": value.price_per_session,
            "total_amount": value.total_amount,
            "status": value.status,
            "start_date": value.start_date,
            "end_date": value.end_date,
            "notes": value.notes,
            "created_at": value.created_at,
        }


class PackageCreated(PackageRead):
    """The registered package, plus the bill it generated.

    Extends `PackageRead` rather than wrapping it so the create response stays
    shape-compatible with every other package response; `bill` is simply absent
    when nothing was billed.
    """

    bill: BillRead | None = None


class PackageCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- #
# Sessions (Section 13)
# --------------------------------------------------------------------------- #
class SessionCreate(BaseModel):
    """Record one delivered physiotherapy session.

    `package_id` omitted means "the oldest active package with sessions left";
    pass `null` explicitly via `no_package=true` for a one-off assessment that
    should not consume purchased sessions.
    """

    session_date: date | None = None
    clinic_id: int | None = None
    package_id: int | None = None
    #: Log without consuming a package (assessment, consultation).
    no_package: bool = False
    #: Completes this appointment in the same transaction when supplied.
    appointment_id: int | None = None
    therapist_user_id: int | None = None
    treatment_provided: str | None = None
    notes: str | None = None
    remarks: str | None = None

    @field_validator("treatment_provided", "notes", "remarks")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)

    @model_validator(mode="after")
    def _check(self):
        if self.no_package and self.package_id is not None:
            raise ValueError("Choose either a package or no package, not both")
        if self.session_date and self.session_date > date.today():
            raise ValueError("A session cannot be recorded for a future date")
        return self


class SessionUpdate(BaseModel):
    """Only the clinical narrative is editable.

    Date, patient and package are fixed: changing them would silently move a
    session between counters. A wrong entry is voided instead.
    """

    model_config = ConfigDict(extra="forbid")

    treatment_provided: str | None = None
    notes: str | None = None
    remarks: str | None = None
    therapist_user_id: int | None = None

    @field_validator("treatment_provided", "notes", "remarks")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)


class SessionVoid(BaseModel):
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _clean(cls, value):
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("A reason is required when voiding a session")
        return cleaned


class SessionRead(ORMModel):
    id: int
    patient_id: int
    patient_name: str | None = None
    patient_code: str | None = None
    package_id: int | None = None
    clinic_id: int
    clinic_name: str | None = None
    appointment_id: int | None = None
    appointment_code: str | None = None
    session_number: int
    session_date: date
    therapist_user_id: int | None = None
    therapist_name: str | None = None
    treatment_provided: str | None = None
    notes: str | None = None
    remarks: str | None = None
    is_voided: bool = False
    void_reason: str | None = None
    voided_by: str | None = None
    voided_at: datetime | None = None
    created_at: datetime
    #: "4 of 10" at the time of reading, or None without a package.
    package_progress: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if not hasattr(value, "session_number"):
            return value
        package = value.package
        return {
            "id": value.id,
            "patient_id": value.patient_id,
            "patient_name": value.patient.full_name if value.patient else None,
            "patient_code": value.patient.patient_code if value.patient else None,
            "package_id": value.package_id,
            "clinic_id": value.clinic_id,
            "clinic_name": value.clinic.name if value.clinic else None,
            "appointment_id": value.appointment_id,
            "appointment_code": (
                value.appointment.appointment_code if value.appointment else None
            ),
            "session_number": value.session_number,
            "session_date": value.session_date,
            "therapist_user_id": value.therapist_user_id,
            "therapist_name": value.therapist.full_name if value.therapist else None,
            "treatment_provided": value.treatment_provided,
            "notes": value.notes,
            "remarks": value.remarks,
            "is_voided": value.is_voided,
            "void_reason": value.void_reason,
            "voided_by": value.voided_by.full_name if value.voided_by else None,
            "voided_at": value.voided_at,
            "created_at": value.created_at,
            "package_progress": (
                f"{value.session_number} of {package.sessions_registered}"
                if package
                else None
            ),
        }


class SessionResult(BaseModel):
    """A logged session, plus what it changed."""

    session: SessionRead
    package: PackageRead | None = None
    #: True when the linked appointment was completed by this call.
    appointment_completed: bool = False
    warnings: list[str] = Field(default_factory=list)


class SessionContext(BaseModel):
    """What the session form needs to pre-fill itself (Section 13).

    Returned by one call so the form does not have to assemble the patient, the
    candidate packages and the next session number from three endpoints.
    """

    patient_id: int
    patient_name: str
    patient_code: str
    clinic_id: int | None = None
    clinic_name: str | None = None
    suggested_package_id: int | None = None
    next_session_number: int
    packages: list[PackageRead] = Field(default_factory=list)
    therapists: list[dict] = Field(default_factory=list)
    total_sessions_registered: int = 0
    total_sessions_taken: int = 0
    total_sessions_remaining: int = 0
    warnings: list[str] = Field(default_factory=list)
