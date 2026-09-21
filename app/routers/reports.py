"""Reporting endpoints (Phase 8).

Every report takes the same three parameters — `from`, `to`, `clinic_id` — and
every one can be exported by appending `format=csv`. Uniform on purpose: the
frontend uses one filter bar and one download button for all five.
"""

from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.services import report_service

router = APIRouter(prefix="/reports", tags=["Reports"])

REPORTS = {
    "clinics": report_service.clinic_report,
    "appointments": report_service.appointment_report,
    "patient-sources": report_service.source_report,
    "revenue": report_service.revenue_report,
    "sessions": report_service.session_report,
    "retention": report_service.retention_report,
}


def _csv_response(name: str, payload: dict, window) -> Response:
    rows = payload.get(report_service.CSV_ROWS_KEY[name], [])
    body = report_service.to_csv(rows, report_service.CSV_COLUMNS[name])
    filename = f"{name}-{window.date_from:%Y%m%d}-{window.date_to:%Y%m%d}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _run(
    name: str,
    db,
    user,
    date_from: date | None,
    date_to: date | None,
    clinic_id: int | None,
    export: str | None,
):
    window = report_service.resolve_window(
        db, user, date_from=date_from, date_to=date_to, clinic_id=clinic_id
    )
    payload = REPORTS[name](db, user, window)
    if export == "csv":
        return _csv_response(name, payload, window)

    # The window is echoed back so a saved or shared report is unambiguous about
    # what it covered, rather than depending on whatever the filters say now.
    return {
        "report": name,
        "date_from": window.date_from,
        "date_to": window.date_to,
        "clinic_id": window.clinic_id,
        "days": window.days,
        # Decimals to strings: see report_service.jsonable for why a bare dict
        # cannot be left to FastAPI's encoder where money is involved.
        **report_service.jsonable(payload),
    }


# Declared *before* `/{name}`: FastAPI matches routes in registration order, so
# a catch-all path parameter registered first would swallow this and reject
# "month-comparison" as an unknown report name.
@router.get(
    "/month-comparison",
    summary="This month to a given day vs the same span in previous months",
)
def month_comparison(
    db: DbSession,
    current_user: ClinicStaffUser,
    as_of: Annotated[date | None, Query(description="Compare up to this day of the month")] = None,
    months: Annotated[int, Query(ge=2, le=12)] = 4,
    clinic_id: int | None = None,
    format: Annotated[str | None, Query(pattern="^(json|csv)$")] = None,
):
    """Cuts every month at the **same day number**, so a part-month is never
    compared against whole previous months.

    Defaults to yesterday: today is still in progress, and including a half-done
    day makes the current month look worse than it is."""
    cutoff = as_of or (date.today() - timedelta(days=1))
    payload = report_service.month_comparison(
        db, current_user, as_of=cutoff, months=months, clinic_id=clinic_id
    )
    if format == "csv":
        body = report_service.to_csv(
            payload["periods"], report_service.CSV_COLUMNS["month-comparison"]
        )
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="month-comparison-{cutoff:%Y%m%d}.csv"'
                )
            },
        )
    return report_service.jsonable(payload)


ReportName = Literal[
    "clinics", "appointments", "patient-sources", "revenue", "sessions", "retention"
]


@router.get(
    "/{name}",
    summary="Run a report (add format=csv to export)",
)
def run_report(
    name: ReportName,
    db: DbSession,
    current_user: ClinicStaffUser,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    clinic_id: int | None = None,
    format: Annotated[str | None, Query(pattern="^(json|csv)$")] = None,
):
    """Defaults to the last 30 days. Clinic Users are scoped to their own
    clinics; Admin and Superadmin see the chain unless `clinic_id` narrows it.

    - `clinics` — activity and money per clinic
    - `appointments` — status mix, daily volume, no-show and cancellation rates
    - `patient-sources` — acquisition, and revenue attributed to each source
    - `revenue` — billed vs collected, by day, clinic, payment method and service
    - `sessions` — delivered sessions by therapist, clinic and day; utilisation
    """
    return _run(name, db, current_user, date_from, date_to, clinic_id, format)
