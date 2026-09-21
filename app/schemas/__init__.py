"""Pydantic request/response schemas.

API contracts live here; SQLAlchemy models are never returned directly
(Section 31).
"""

from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    TokenPair,
)
from app.schemas.clinic import (
    BreakBase,
    BreakRead,
    ClinicCreate,
    ClinicRead,
    ClinicSummary,
    ClinicUpdate,
    HolidayBase,
    HolidayRead,
    WorkingHourBase,
    WorkingHourRead,
)
from app.schemas.common import Message, Page, PaginationParams
from app.schemas.dashboard import ClinicPerformance, DashboardCounters, DashboardResponse
from app.schemas.user import (
    ClinicAssignmentRead,
    PasswordResetRequest,
    RoleRead,
    UserCreate,
    UserRead,
    UserUpdate,
)

__all__ = [
    "ChangePasswordRequest",
    "LoginRequest",
    "LoginResponse",
    "RefreshRequest",
    "TokenPair",
    "BreakBase",
    "BreakRead",
    "ClinicCreate",
    "ClinicRead",
    "ClinicSummary",
    "ClinicUpdate",
    "HolidayBase",
    "HolidayRead",
    "WorkingHourBase",
    "WorkingHourRead",
    "Message",
    "Page",
    "PaginationParams",
    "ClinicPerformance",
    "DashboardCounters",
    "DashboardResponse",
    "ClinicAssignmentRead",
    "PasswordResetRequest",
    "RoleRead",
    "UserCreate",
    "UserRead",
    "UserUpdate",
]
