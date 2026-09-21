"""Role-aware dashboard payloads (Section 22).

One endpoint returns the union of what each role needs; fields that do not
apply to the caller's role are omitted rather than sent as zero, so the
frontend never renders a metric it is not entitled to.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import RoleName
from app.schemas.clinic import ClinicSummary


class ClinicPerformance(BaseModel):
    clinic_id: int
    clinic_name: str
    total_patients: int = 0
    appointments_today: int = 0
    completed_today: int = 0
    revenue: Decimal = Decimal("0.00")
    outstanding: Decimal = Decimal("0.00")


class DashboardCounters(BaseModel):
    # Clinic/user footprint (Superadmin)
    total_clinics: int | None = None
    active_clinics: int | None = None
    total_users: int | None = None
    active_users: int | None = None

    # Patients
    total_patients: int | None = None
    patients_registered_today: int | None = None

    # Appointments
    appointments_today: int | None = None
    upcoming_appointments: int | None = None
    completed_today: int | None = None
    cancelled_today: int | None = None
    checked_in_now: int | None = None
    pending_today: int | None = None

    # Sessions
    sessions_completed_today: int | None = None

    # Money
    revenue_total: Decimal | None = None
    revenue_this_month: Decimal | None = None
    outstanding_amount: Decimal | None = None

    # Notifications
    unacknowledged_notifications: int | None = None


class DashboardResponse(BaseModel):
    role: RoleName
    as_of_date: date
    scope_label: str = Field(description="Human-readable description of the data scope")
    counters: DashboardCounters
    clinics: list[ClinicSummary] = Field(default_factory=list)
    clinic_performance: list[ClinicPerformance] = Field(default_factory=list)
