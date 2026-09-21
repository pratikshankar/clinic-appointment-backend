"""Clinic schemas.

Phase 1 exposes read models plus the create/update payloads used by the seed
script and the Superadmin clinic list. The write endpoints themselves are
Phase 2 (Section 41).
"""

from datetime import date, time

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.enums import ClinicStatus
from app.schemas.common import ORMModel, clean_text, normalize_mobile, validate_pin_code

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class WorkingHourBase(BaseModel):
    day_of_week: int = Field(ge=0, le=6, description="0 = Monday ... 6 = Sunday")
    open_time: time
    close_time: time
    is_closed: bool = False
    label: str | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def _check_order(self):
        if not self.is_closed and self.open_time >= self.close_time:
            raise ValueError("Closing time must be later than opening time")
        return self


class WorkingHourRead(ORMModel, WorkingHourBase):
    id: int

    @property
    def day_name(self) -> str:
        return DAY_NAMES[self.day_of_week]


class BreakBase(BaseModel):
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    start_time: time
    end_time: time
    label: str = Field(default="Lunch break", max_length=50)

    @model_validator(mode="after")
    def _check_order(self):
        if self.start_time >= self.end_time:
            raise ValueError("Break end time must be later than its start time")
        return self


class BreakRead(ORMModel, BreakBase):
    id: int


class HolidayBase(BaseModel):
    holiday_date: date
    reason: str | None = Field(default=None, max_length=255)


class HolidayRead(ORMModel, HolidayBase):
    id: int
    clinic_id: int | None = None


def _clean_prefix(value: str | None) -> str | None:
    """Normalise an invoice-series prefix.

    Uppercased and stripped of anything that is not a letter or digit: the
    prefix is parsed back out of stored bill numbers to find the next in the
    series, so a stray hyphen or space would break that regex and start the
    sequence again at 1.
    """
    if value is None:
        return None
    cleaned = "".join(ch for ch in value.strip().upper() if ch.isalnum())
    return cleaned or None


class ClinicBase(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    address: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=150)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    pin_code: str | None = Field(default=None, max_length=12)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    slot_duration_minutes: int = Field(default=30, ge=5, le=240)
    capacity_per_slot: int = Field(default=3, ge=1, le=100)

    # --- branding (Section 19). All optional: NULL inherits the chain brand,
    # so only a clinic trading under its own name needs any of this. ---
    brand_name: str | None = Field(default=None, max_length=120)
    brand_tagline: str | None = Field(default=None, max_length=160)
    #: Filename inside backend/app/assets/, e.g. "physiocare-logo.png".
    logo_filename: str | None = Field(default=None, max_length=120)
    document_footer: str | None = None
    #: Invoice series, e.g. "PC" -> PC-2026-000001. Blank uses the chain series.
    bill_number_prefix: str | None = Field(default=None, max_length=10)


    @field_validator("name", "address", "location", "city", "state")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value):
        return normalize_mobile(value)

    @field_validator("pin_code")
    @classmethod
    def _check_pin(cls, value):
        return validate_pin_code(value)

    @field_validator("bill_number_prefix")
    @classmethod
    def _normalise_prefix(cls, value):
        return _clean_prefix(value)


class ClinicCreate(ClinicBase):
    code: str | None = Field(
        default=None, max_length=20, description="Auto-derived from the name when omitted"
    )
    status: ClinicStatus = ClinicStatus.ACTIVE
    working_hours: list[WorkingHourBase] = Field(default_factory=list)
    breaks: list[BreakBase] = Field(default_factory=list)


class ClinicUpdate(BaseModel):
    """Editable clinic fields.

    `code` is deliberately absent: it is immutable after creation so that
    historical records and printed material stay unambiguous. `extra="forbid"`
    turns an attempt to change it into a clear 422 rather than a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=150)
    address: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=150)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    pin_code: str | None = Field(default=None, max_length=12)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    status: ClinicStatus | None = None
    slot_duration_minutes: int | None = Field(default=None, ge=5, le=240)
    capacity_per_slot: int | None = Field(default=None, ge=1, le=100)

    # --- branding (Section 19). All optional: NULL inherits the chain brand,
    # so only a clinic trading under its own name needs any of this. ---
    brand_name: str | None = Field(default=None, max_length=120)
    brand_tagline: str | None = Field(default=None, max_length=160)
    #: Filename inside backend/app/assets/, e.g. "physiocare-logo.png".
    logo_filename: str | None = Field(default=None, max_length=120)
    document_footer: str | None = None
    #: Invoice series, e.g. "PC" -> PC-2026-000001. Blank uses the chain series.
    bill_number_prefix: str | None = Field(default=None, max_length=10)


    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value):
        return normalize_mobile(value)

    @field_validator("pin_code")
    @classmethod
    def _check_pin(cls, value):
        return validate_pin_code(value)

    @field_validator("bill_number_prefix")
    @classmethod
    def _normalise_prefix(cls, value):
        return _clean_prefix(value)

class ClinicSummary(ORMModel):
    """Compact form used in selectors and dashboard lists."""

    id: int
    name: str
    code: str
    location: str | None = None
    city: str | None = None
    status: ClinicStatus


class ClinicRead(ClinicSummary):
    address: str | None = None
    state: str | None = None
    pin_code: str | None = None
    phone: str | None = None
    email: str | None = None
    slot_duration_minutes: int
    capacity_per_slot: int
    brand_name: str | None = None
    brand_tagline: str | None = None
    logo_filename: str | None = None
    document_footer: str | None = None
    bill_number_prefix: str | None = None
    working_hours: list[WorkingHourRead] = Field(default_factory=list)
    breaks: list[BreakRead] = Field(default_factory=list)
    assigned_user_count: int = 0
    #: Slots the current configuration yields per weekday (0 = Monday).
    weekly_slot_counts: dict[int, int] = Field(default_factory=dict)
    #: Non-blocking configuration issues, e.g. a slot duration with a remainder.
    configuration_warnings: list[str] = Field(default_factory=list)


class WorkingHoursReplace(BaseModel):
    """Full replacement of a clinic's weekly shifts.

    Replacing the whole set in one request keeps the operation atomic: a partial
    update could momentarily leave overlapping shifts, which is exactly what the
    validation exists to prevent.
    """

    working_hours: list[WorkingHourBase]

    @model_validator(mode="after")
    def _reject_duplicates(self):
        seen: set[tuple[int, time, time]] = set()
        for shift in self.working_hours:
            key = (shift.day_of_week, shift.open_time, shift.close_time)
            if key in seen:
                raise ValueError(
                    f"Duplicate shift for day {shift.day_of_week}: "
                    f"{shift.open_time}-{shift.close_time}"
                )
            seen.add(key)
        return self


class BreakCreate(BreakBase):
    pass


class HolidayCreate(HolidayBase):
    """A closed day. `clinic_id=None` closes every clinic in the chain."""

    clinic_id: int | None = None


class HolidayDetail(HolidayRead):
    clinic_name: str | None = None

    @property
    def is_chain_wide(self) -> bool:
        return self.clinic_id is None


class ClinicStaffRead(ORMModel):
    """A user assigned to a clinic, as shown on the clinic's Staff tab."""

    user_id: int
    username: str
    full_name: str
    email: str | None = None
    phone: str | None = None
    role: str
    is_active: bool
    designation: str | None = None
    is_primary: bool = True

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        # Accepts a ClinicUser ORM row and lifts the user's fields up.
        if hasattr(value, "user") and hasattr(value, "clinic_id"):
            user = value.user
            return {
                "user_id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "email": user.email,
                "phone": user.phone,
                "role": user.role.name.value,
                "is_active": user.is_active,
                "designation": value.designation,
                "is_primary": value.is_primary,
            }
        return value


class StaffAssign(BaseModel):
    user_id: int
    designation: str | None = Field(default=None, max_length=100)


class SlotPreview(BaseModel):
    start_time: time
    end_time: time
    shift_label: str | None = None


class SchedulePreview(BaseModel):
    """What the configuration produces for one day. No bookings involved."""

    clinic_id: int
    on_date: date
    day_of_week: int
    day_name: str
    is_open: bool
    closed_reason: str | None = None
    slot_duration_minutes: int
    capacity_per_slot: int
    brand_name: str | None = None
    brand_tagline: str | None = None
    logo_filename: str | None = None
    document_footer: str | None = None
    bill_number_prefix: str | None = None
    slots: list[SlotPreview] = Field(default_factory=list)
    total_slots: int = 0
    total_capacity: int = 0
    warnings: list[str] = Field(default_factory=list)


class ClinicWriteResult(BaseModel):
    """A clinic write plus anything the Superadmin should be told about it."""

    clinic: ClinicRead
    warnings: list[str] = Field(default_factory=list)
    #: Future slots holding more bookings than the (new) capacity allows.
    overbooked_slots: list[dict] = Field(default_factory=list)
    future_appointment_count: int | None = None
