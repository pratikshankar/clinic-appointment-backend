"""Patient schemas (Sections 7, 11, 20, 21, 38).

Age handling deserves a note: the table has both `age` and `date_of_birth`.
A stored age silently rots -- a patient registered at 34 still reads 34 three
years later -- so when a date of birth is known the age is always *derived* from
it. When reception can only get an approximate age, it is stored alongside
`registration_date` so it can still be interpreted later. `age_as_of` in the
response says which of the two the number came from.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.enums import AppointmentStatus, Gender, PackageStatus, PaymentStatus
from app.schemas.common import ORMModel, clean_text, normalize_mobile


def derive_age(date_of_birth: date | None, on_date: date | None = None) -> int | None:
    """Whole years between `date_of_birth` and `on_date` (default today)."""
    if date_of_birth is None:
        return None
    reference = on_date or date.today()
    years = reference.year - date_of_birth.year
    if (reference.month, reference.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return max(years, 0)


class PatientBase(BaseModel):
    full_name: str = Field(min_length=2, max_length=150)
    mobile: str = Field(min_length=10, max_length=20)
    #: Optional. Left blank, WhatsApp messages go to `mobile`.
    whatsapp_number: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    age: int | None = Field(default=None, ge=0, le=130)
    date_of_birth: date | None = None
    gender: Gender | None = None
    address: str | None = Field(default=None, max_length=500)
    chief_complaint: str | None = None
    diagnosis: str | None = None
    source_id: int | None = None
    source_detail: str | None = Field(default=None, max_length=255)

    @field_validator("mobile")
    @classmethod
    def _check_mobile(cls, value):
        normalized = normalize_mobile(value)
        if not normalized:
            raise ValueError("A mobile number is required")
        return normalized

    @field_validator("whatsapp_number")
    @classmethod
    def _check_whatsapp(cls, value):
        # Optional, but validated and normalised the same way as `mobile` when
        # given, so both are comparable and dialable.
        return normalize_mobile(value)

    @field_validator("full_name")
    @classmethod
    def _clean_name(cls, value):
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("Patient name is required")
        # Collapse internal whitespace so "Rahul  Sharma" and "Rahul Sharma"
        # are recognised as the same person by duplicate detection.
        return " ".join(cleaned.split())

    @field_validator("address", "chief_complaint", "diagnosis", "source_detail")
    @classmethod
    def _clean_text_fields(cls, value):
        return clean_text(value)

    @model_validator(mode="after")
    def _check_dob(self):
        if self.date_of_birth and self.date_of_birth > date.today():
            raise ValueError("Date of birth cannot be in the future")
        return self


class PatientCreate(PatientBase):
    """Full registration, used by the patient form.

    There is deliberately no `is_profile_complete` field: completeness is
    *derived* from whether the required fields are present, so a client cannot
    declare a half-filled record complete.
    """

    primary_clinic_id: int | None = None
    registration_date: date | None = None


class PatientQuickCreate(BaseModel):
    """The minimum an Admin needs while booking an appointment (Section 7).

    Kept separate from `PatientCreate` so the booking flow cannot be forced to
    invent values for fields reception has not asked for yet. The resulting
    record is flagged incomplete for the clinic to finish (Section 11).
    """

    full_name: str = Field(min_length=2, max_length=150)
    mobile: str = Field(min_length=10, max_length=20)
    whatsapp_number: str | None = Field(default=None, max_length=20)
    chief_complaint: str | None = None
    primary_clinic_id: int | None = None

    @field_validator("mobile")
    @classmethod
    def _check_mobile(cls, value):
        normalized = normalize_mobile(value)
        if not normalized:
            raise ValueError("A mobile number is required")
        return normalized

    @field_validator("whatsapp_number")
    @classmethod
    def _check_whatsapp(cls, value):
        return normalize_mobile(value)

    @field_validator("full_name")
    @classmethod
    def _clean_name(cls, value):
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("Patient name is required")
        return " ".join(cleaned.split())


class PatientUpdate(BaseModel):
    """Partial update; also how a minimal record becomes a complete profile."""

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=150)
    mobile: str | None = Field(default=None, min_length=10, max_length=20)
    whatsapp_number: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    age: int | None = Field(default=None, ge=0, le=130)
    date_of_birth: date | None = None
    gender: Gender | None = None
    address: str | None = Field(default=None, max_length=500)
    chief_complaint: str | None = None
    diagnosis: str | None = None
    source_id: int | None = None
    source_detail: str | None = Field(default=None, max_length=255)
    primary_clinic_id: int | None = None

    @field_validator("mobile", "whatsapp_number")
    @classmethod
    def _check_numbers(cls, value):
        return normalize_mobile(value) if value is not None else None

    @field_validator("full_name")
    @classmethod
    def _clean_name(cls, value):
        if value is None:
            return None
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("Patient name cannot be blank")
        return " ".join(cleaned.split())


class PatientCard(ORMModel):
    """Minimal identity, safe to return chain-wide (Section 20).

    This is what a Clinic User sees when looking up a patient who belongs to
    another clinic: enough to recognise the person and link them instead of
    creating a duplicate, and nothing clinical.
    """

    id: int
    patient_code: str
    full_name: str
    mobile: str
    primary_clinic_id: int | None = None
    primary_clinic_name: str | None = None
    is_profile_complete: bool = True
    is_active: bool = True
    #: False when the caller may not open the full record.
    in_your_scope: bool = True

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if hasattr(value, "patient_code"):
            return {
                "id": value.id,
                "patient_code": value.patient_code,
                "full_name": value.full_name,
                "mobile": value.mobile,
                "primary_clinic_id": value.primary_clinic_id,
                "primary_clinic_name": (
                    value.primary_clinic.name if value.primary_clinic else None
                ),
                "is_profile_complete": value.is_profile_complete,
                "is_active": value.is_active,
            }
        return value


class PatientRead(PatientCard):
    """Full patient record."""

    email: str | None = None
    whatsapp_number: str | None = None
    #: Where a WhatsApp message would actually go (falls back to `mobile`).
    whatsapp_contact: str | None = None
    age: int | None = None
    date_of_birth: date | None = None
    #: "date_of_birth" when derived, "registration" when the stored age was used.
    age_as_of: str | None = None
    gender: Gender | None = None
    address: str | None = None
    chief_complaint: str | None = None
    diagnosis: str | None = None
    source_id: int | None = None
    source_name: str | None = None
    source_detail: str | None = None
    registration_date: date | None = None
    created_at: date | None = None
    #: Whether the patient currently has a package with sessions left. Filled in
    #: by the router from one batched query, not per row.
    has_active_package: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_full(cls, value):
        if not hasattr(value, "patient_code"):
            return value

        derived = derive_age(value.date_of_birth)
        return {
            "id": value.id,
            "patient_code": value.patient_code,
            "full_name": value.full_name,
            "mobile": value.mobile,
            "whatsapp_number": value.whatsapp_number,
            "whatsapp_contact": value.whatsapp_contact,
            "email": value.email,
            # A known date of birth always wins: it cannot go stale.
            "age": derived if derived is not None else value.age,
            "age_as_of": (
                "date_of_birth"
                if derived is not None
                else ("registration" if value.age is not None else None)
            ),
            "date_of_birth": value.date_of_birth,
            "gender": value.gender,
            "address": value.address,
            "chief_complaint": value.chief_complaint,
            "diagnosis": value.diagnosis,
            "source_id": value.source_id,
            "source_name": value.source.name if value.source else None,
            "source_detail": value.source_detail,
            "registration_date": value.registration_date,
            "primary_clinic_id": value.primary_clinic_id,
            "primary_clinic_name": (
                value.primary_clinic.name if value.primary_clinic else None
            ),
            "is_profile_complete": value.is_profile_complete,
            "is_active": value.is_active,
            "created_at": value.created_at.date() if value.created_at else None,
        }


class DuplicateCheckRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=150)
    mobile: str = Field(min_length=10, max_length=20)

    @field_validator("mobile")
    @classmethod
    def _check_mobile(cls, value):
        normalized = normalize_mobile(value)
        if not normalized:
            raise ValueError("Enter a valid mobile number to check")
        return normalized


class DuplicateCheckResponse(BaseModel):
    """Result of looking for an existing patient before creating one.

    `exact_match` means same mobile *and* same name -- creation will be refused.
    `same_mobile` is informational: families share a number, so this is shown to
    staff to choose between "existing patient" and "new family member".
    """

    exact_match: PatientCard | None = None
    same_mobile: list[PatientCard] = Field(default_factory=list)
    similar_name: list[PatientCard] = Field(default_factory=list)

    @property
    def has_candidates(self) -> bool:
        return bool(self.exact_match or self.same_mobile or self.similar_name)


# --------------------------------------------------------------------------- #
# Profile timeline (Section 21)
# --------------------------------------------------------------------------- #
class PackageSummary(BaseModel):
    id: int
    package_name: str | None = None
    clinic_name: str | None = None
    sessions_registered: int
    sessions_taken: int
    sessions_remaining: int
    price_per_session: Decimal
    total_amount: Decimal
    status: PackageStatus
    start_date: date | None = None


class AppointmentSummary(BaseModel):
    id: int
    appointment_code: str
    clinic_name: str | None = None
    appointment_date: date
    start_time: str
    status: AppointmentStatus
    chief_complaint: str | None = None


class SessionSummary(BaseModel):
    id: int
    session_number: int
    session_date: date
    clinic_name: str | None = None
    therapist_name: str | None = None
    treatment_provided: str | None = None
    notes: str | None = None


class BillSummary(BaseModel):
    id: int
    bill_number: str
    clinic_name: str | None = None
    bill_date: date
    total_amount: Decimal
    amount_paid: Decimal
    balance_amount: Decimal
    payment_status: PaymentStatus


class PatientProfile(BaseModel):
    """Everything about one patient, in the order Section 21 lays out."""

    patient: PatientRead
    packages: list[PackageSummary] = Field(default_factory=list)
    appointments: list[AppointmentSummary] = Field(default_factory=list)
    sessions: list[SessionSummary] = Field(default_factory=list)
    bills: list[BillSummary] = Field(default_factory=list)

    # Roll-ups, so the UI does not re-derive them. These count **live packages
    # only**: a cancelled package is still listed in `packages` as history, but
    # it entitles the patient to nothing and must not inflate these numbers.
    total_sessions_registered: int = 0
    total_sessions_taken: int = 0
    total_sessions_remaining: int = 0
    #: How many cancelled packages were left out of the totals above, so the UI
    #: can say why the headline number differs from the table.
    cancelled_package_count: int = 0
    total_billed: Decimal = Decimal("0.00")
    total_paid: Decimal = Decimal("0.00")
    total_outstanding: Decimal = Decimal("0.00")
    #: True when the timeline was filtered to the caller's clinics.
    scoped_to_your_clinics: bool = False
