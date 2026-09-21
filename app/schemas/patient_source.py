"""Patient-source schemas (Section 11).

The source list is configurable rather than a hard-coded enum, because a clinic
chain adds acquisition channels over time and Section 11 explicitly requires the
Superadmin to be able to edit them.
"""

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel, clean_text


class PatientSourceBase(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=0, le=999)

    @field_validator("name")
    @classmethod
    def _clean(cls, value):
        cleaned = clean_text(value)
        if not cleaned:
            raise ValueError("Source name is required")
        return cleaned


class PatientSourceCreate(PatientSourceBase):
    pass


class PatientSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=999)

    @field_validator("name")
    @classmethod
    def _clean(cls, value):
        return clean_text(value) if value is not None else None


class PatientSourceRead(ORMModel, PatientSourceBase):
    id: int
    #: Patients already attributed to this source. Shown so the Superadmin can
    #: see the cost of deactivating it, and it is why sources are deactivated
    #: rather than deleted.
    patient_count: int = 0
