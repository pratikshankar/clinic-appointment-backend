"""User and role schemas."""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.enums import RoleName
from app.schemas.common import ORMModel, clean_text, normalize_mobile


class RoleRead(ORMModel):
    id: int
    name: RoleName
    description: str | None = None


class ClinicAssignmentRead(ORMModel):
    """A clinic a user is assigned to, flattened for the API."""

    clinic_id: int
    clinic_name: str
    clinic_code: str
    is_primary: bool
    designation: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        # Accepts a ClinicUser ORM object and lifts the clinic fields up.
        if hasattr(value, "clinic"):
            return {
                "clinic_id": value.clinic_id,
                "clinic_name": value.clinic.name if value.clinic else "",
                "clinic_code": value.clinic.code if value.clinic else "",
                "is_primary": value.is_primary,
                "designation": value.designation,
            }
        return value


class UserBase(BaseModel):
    full_name: str = Field(min_length=2, max_length=150)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value):
        return normalize_mobile(value)

    @field_validator("full_name")
    @classmethod
    def _clean_name(cls, value):
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("Full name is required")
        return cleaned


class UserCreate(UserBase):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=8, max_length=128)
    role: RoleName
    #: Required for CLINIC_USER, ignored for other roles.
    clinic_ids: list[int] = Field(default_factory=list)
    designation: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _check_clinic_assignment(self):
        if self.role == RoleName.CLINIC_USER and not self.clinic_ids:
            raise ValueError("A Clinic User must be assigned to at least one clinic")
        if self.role != RoleName.CLINIC_USER and self.clinic_ids:
            raise ValueError(
                f"{self.role.value} users have chain-wide access and cannot be "
                "assigned to specific clinics"
            )
        return self
    #: The clinic's own staff number. Optional, unique when given.
    employee_id: str | None = Field(default=None, max_length=40)
    #: Professional registration, for physiotherapists. Printed on the treatment
    #: statement, which is where an insurer looks for it.
    registration_number: str | None = Field(default=None, max_length=60)

class UserUpdate(BaseModel):
    """All fields optional; only what is supplied gets changed."""

    full_name: str | None = Field(default=None, min_length=2, max_length=150)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)
    is_active: bool | None = None
    clinic_ids: list[int] | None = None
    designation: str | None = Field(default=None, max_length=100)

    #: The clinic's own staff number. Optional, unique when given.
    employee_id: str | None = Field(default=None, max_length=40)
    #: Professional registration, for physiotherapists. Printed on the treatment
    #: statement, which is where an insurer looks for it.
    registration_number: str | None = Field(default=None, max_length=60)

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value):
        return normalize_mobile(value)


class UserRead(ORMModel):
    id: int
    username: str
    full_name: str
    email: str | None = None
    phone: str | None = None
    employee_id: str | None = None
    registration_number: str | None = None
    role: RoleName
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime
    clinics: list[ClinicAssignmentRead] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, value):
        if hasattr(value, "role") and hasattr(value, "clinic_links"):
            return {
                "id": value.id,
                "username": value.username,
                "full_name": value.full_name,
                "email": value.email,
                "phone": value.phone,
                "employee_id": value.employee_id,
                "registration_number": value.registration_number,
                "role": value.role.name,
                "is_active": value.is_active,
                "last_login_at": value.last_login_at,
                "created_at": value.created_at,
                "clinics": list(value.clinic_links),
            }
        return value


class PasswordResetRequest(BaseModel):
    """Superadmin-initiated password reset for another user."""

    new_password: str = Field(min_length=8, max_length=128)
