"""Role-aware dashboard endpoint (Section 22)."""

from datetime import date

from fastapi import APIRouter

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.schemas.dashboard import DashboardResponse
from app.services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get(
    "/summary",
    response_model=DashboardResponse,
    summary="Dashboard counters for the signed-in user's role and clinic scope",
)
def dashboard_summary(
    db: DbSession,
    current_user: ClinicStaffUser,
    as_of: date | None = None,
):
    return dashboard_service.build_dashboard(db, current_user, today=as_of)
