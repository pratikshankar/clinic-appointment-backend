from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PrescriptionCreate(BaseModel):
    patient_id: int
    clinic_id: int | None = None
    prescribed_by_user_id: int | None = None
    chief_complaint: str | None = Field(default=None, max_length=3000)
    history: str | None = Field(default=None, max_length=3000)
    on_examination: str | None = Field(default=None, max_length=3000)
    diagnosis: str | None = Field(default=None, max_length=3000)
    treatment_plan: str | None = Field(default=None, max_length=6000)
    home_protocol: str | None = Field(default=None, max_length=6000)
    notes: str | None = Field(default=None, max_length=1000)


class PhysioMini(BaseModel):
    id: int
    full_name: str
    registration_number: str | None = None

    model_config = {"from_attributes": True}


class PrescriptionRead(BaseModel):
    id: int
    patient_id: int
    clinic_id: int | None
    prescribed_by_user_id: int | None
    chief_complaint: str | None
    history: str | None
    on_examination: str | None
    diagnosis: str | None
    treatment_plan: str | None
    home_protocol: str | None
    notes: str | None
    created_at: datetime
    prescribed_by: PhysioMini | None = None

    model_config = {"from_attributes": True}


class PrescriptionSend(BaseModel):
    channel: Literal["email", "whatsapp"] = "email"
    email: str | None = None
    phone: str | None = None
