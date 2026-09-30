"""Schemas for the referral programme."""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.enums import ReferralStatus


class ReferralCreate(BaseModel):
    referrer_patient_id: int
    referred_patient_id: int
    referred_package_id: int          # the qualifying package bought by the referred patient
    credit_sessions: int = Field(default=2, ge=1, le=10)
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _no_self_referral(self):
        if self.referrer_patient_id == self.referred_patient_id:
            raise ValueError("A patient cannot refer themselves")
        return self


class ReferralApplyCredit(BaseModel):
    """Confirm referral credit — sessions are added to the referrer's balance."""
    pass


class PackageRedeemReferralCredit(BaseModel):
    """Redeem referrer credit sessions onto a package (no session-count restriction)."""
    sessions: int = Field(ge=1, le=50)


class PackageRedeemReferredCredit(BaseModel):
    """Redeem referred-patient credit sessions onto a package (≥5 sessions required)."""
    sessions: int = Field(ge=1, le=50)


class ReferralListItem(BaseModel):
    id: int
    status: ReferralStatus
    referrer_patient_name: str | None
    referrer_patient_code: str | None
    referred_patient_name: str | None
    referred_patient_code: str | None
    clinic_name: str | None
    credit_sessions: int
    credited_to_package_id: int | None
    notes: str | None
    initiated_by_name: str | None
    credited_at: datetime | None
    voided_reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, r):
        return cls(
            id=r.id,
            status=r.status,
            referrer_patient_name=r.referrer_patient.full_name if r.referrer_patient else None,
            referrer_patient_code=r.referrer_patient.patient_code if r.referrer_patient else None,
            referred_patient_name=r.referred_patient.full_name if r.referred_patient else None,
            referred_patient_code=r.referred_patient.patient_code if r.referred_patient else None,
            clinic_name=r.referred_package.clinic.name if r.referred_package and r.referred_package.clinic else None,
            credit_sessions=r.credit_sessions,
            credited_to_package_id=r.credited_to_package_id,
            notes=r.notes,
            initiated_by_name=r.initiated_by.full_name if r.initiated_by else None,
            credited_at=r.credited_at,
            voided_reason=r.voided_reason,
            created_at=r.created_at,
        )


class ReferralOut(ReferralListItem):
    referrer_patient_id: int
    referred_patient_id: int
    referred_package_id: int
    credited_by_name: str | None
    updated_at: datetime

    @classmethod
    def from_orm(cls, r):
        base = ReferralListItem.from_orm(r)
        return cls(
            **base.model_dump(),
            referrer_patient_id=r.referrer_patient_id,
            referred_patient_id=r.referred_patient_id,
            referred_package_id=r.referred_package_id,
            credited_by_name=r.credited_by.full_name if r.credited_by else None,
            updated_at=r.updated_at,
        )
