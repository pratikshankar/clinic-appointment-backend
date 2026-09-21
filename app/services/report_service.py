"""Reporting (Section 40, Phase 8).

Five reports, all date-ranged and clinic-scoped, **all aggregated in SQL** —
grouped queries, never a Python loop over rows.

The rule this module is written around: **each metric comes from its own scoped
query.** It is tempting to join appointments, sessions and bills and read
everything off one result set, but those are one-to-many in different directions,
so the join multiplies rows and every sum comes out inflated. Exactly that bug
reported revenue as 4x reality in Phase 1. The cost of separate queries is a few
extra round trips; the cost of the join is a number nobody can trust.

Consequently every report exposes its own breakdown, and the tests assert that
`total == sum(breakdown)` — a total that disagrees with the rows beneath it is
the specific failure mode being guarded against.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.orm import Session

from app.auth import permissions
from app.models import (
    Appointment,
    AppointmentStatus,
    Bill,
    BillItem,
    BillStatus,
    Clinic,
    PackageStatus,
    Patient,
    PatientSession,
    PatientSource,
    Payment,
    TreatmentPackage,
    User,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

#: Longest range a single report may cover. A five-year unbounded scan is never
#: what anyone meant to ask for, and it is a cheap way to hang the database.
MAX_RANGE_DAYS = 800


@dataclass(frozen=True, slots=True)
class Window:
    """The resolved date range and clinic scope for one report."""

    date_from: date
    date_to: date
    #: None means "no clinic restriction" (Superadmin/Admin, all clinics).
    clinic_ids: list[int] | None
    clinic_id: int | None = None

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1


def resolve_window(
    db: Session,
    user: User,
    *,
    date_from: date | None,
    date_to: date | None,
    clinic_id: int | None,
    default_days: int = 30,
) -> Window:
    """Work out what range and which clinics this report covers."""
    from app.utils.exceptions import ValidationError

    today = date.today()
    end = date_to or today
    start = date_from or (end - timedelta(days=default_days - 1))
    if start > end:
        raise ValidationError("The start of the range cannot be after its end")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValidationError(
            f"That range covers more than {MAX_RANGE_DAYS} days. Narrow it, or export "
            "in slices."
        )

    accessible = permissions.accessible_clinic_ids(db, user)
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        return Window(start, end, [clinic_id], clinic_id)
    return Window(start, end, accessible, None)


def _scope(stmt: Select, column, clinic_ids: list[int] | None) -> Select:
    if clinic_ids is None:
        return stmt
    # A restricted user with no assignment must match nothing rather than
    # falling through to matching everything.
    return stmt.where(column.in_(clinic_ids or [-1]))


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _collected_stmt(window: Window, *group_by):
    """Money **actually received** in the window, from `payments`.

    Not `bills.amount_paid`: that is a running total with no date of its own, so
    filtering it by `bill_date` credits a September payment to an August bill's
    month. Harmless while every bill is settled the day it is raised; wrong the
    moment anyone pays in instalments, and it would quietly corrupt exactly the
    month-on-month comparison this module exists to support.
    """
    stmt = select(*group_by, func.coalesce(func.sum(Payment.amount), 0)).select_from(
        Payment
    ).join(Bill, Payment.bill_id == Bill.id).where(
        Bill.status != BillStatus.CANCELLED,
        Payment.payment_date >= window.date_from,
        Payment.payment_date <= window.date_to,
    )
    return _scope(stmt, Bill.clinic_id, window.clinic_ids)


def _clinic_names(db: Session, window: Window) -> dict[int, str]:
    stmt = _scope(select(Clinic.id, Clinic.name), Clinic.id, window.clinic_ids)
    return {row.id: row.name for row in db.execute(stmt.order_by(Clinic.name))}


# --------------------------------------------------------------------------- #
# 1. Clinic report
# --------------------------------------------------------------------------- #
def clinic_report(db: Session, user: User, window: Window) -> dict:
    """One row per clinic: activity and money side by side.

    Each column is its own aggregate keyed by clinic, merged in Python. Joining
    them in SQL would multiply appointments by sessions by bills.
    """
    names = _clinic_names(db, window)

    def by_clinic(stmt: Select) -> dict[int, tuple]:
        return {row[0]: tuple(row[1:]) for row in db.execute(stmt)}

    appointments = by_clinic(
        _scope(
            select(
                Appointment.clinic_id,
                func.count(),
                func.sum(
                    case((Appointment.status == AppointmentStatus.COMPLETED, 1), else_=0)
                ),
                func.sum(
                    case((Appointment.status == AppointmentStatus.CANCELLED, 1), else_=0)
                ),
                func.sum(
                    case((Appointment.status == AppointmentStatus.NO_SHOW, 1), else_=0)
                ),
            ).where(
                Appointment.appointment_date >= window.date_from,
                Appointment.appointment_date <= window.date_to,
            ),
            Appointment.clinic_id,
            window.clinic_ids,
        ).group_by(Appointment.clinic_id)
    )

    sessions = by_clinic(
        _scope(
            select(PatientSession.clinic_id, func.count()).where(
                PatientSession.is_voided.is_(False),
                PatientSession.session_date >= window.date_from,
                PatientSession.session_date <= window.date_to,
            ),
            PatientSession.clinic_id,
            window.clinic_ids,
        ).group_by(PatientSession.clinic_id)
    )

    bills = by_clinic(
        _scope(
            select(
                Bill.clinic_id,
                func.count(),
                func.coalesce(func.sum(Bill.total_amount), 0),
                # What is still owed *on bills raised in this window*.
                func.coalesce(func.sum(Bill.total_amount - Bill.amount_paid), 0),
            ).where(
                Bill.status != BillStatus.CANCELLED,
                Bill.bill_date >= window.date_from,
                Bill.bill_date <= window.date_to,
            ),
            Bill.clinic_id,
            window.clinic_ids,
        ).group_by(Bill.clinic_id)
    )

    collected = {
        row[0]: row[1]
        for row in db.execute(_collected_stmt(window, Bill.clinic_id).group_by(Bill.clinic_id))
    }

    new_patients = by_clinic(
        _scope(
            select(Patient.primary_clinic_id, func.count()).where(
                func.date(Patient.created_at) >= window.date_from,
                func.date(Patient.created_at) <= window.date_to,
            ),
            Patient.primary_clinic_id,
            window.clinic_ids,
        ).group_by(Patient.primary_clinic_id)
    )

    rows = []
    for clinic_id, name in names.items():
        appt = appointments.get(clinic_id, (0, 0, 0, 0))
        bill_row = bills.get(clinic_id, (0, 0, 0))
        rows.append(
            {
                "clinic_id": clinic_id,
                "clinic_name": name,
                "appointments": appt[0] or 0,
                "completed": int(appt[1] or 0),
                "cancelled": int(appt[2] or 0),
                "no_shows": int(appt[3] or 0),
                "sessions": sessions.get(clinic_id, (0,))[0] or 0,
                "new_patients": new_patients.get(clinic_id, (0,))[0] or 0,
                "bills": bill_row[0] or 0,
                "billed": money(bill_row[1]),
                "collected": money(collected.get(clinic_id, 0)),
                "outstanding": money(bill_row[2]),
            }
        )

    rows.sort(key=lambda row: row["collected"], reverse=True)
    return {
        "rows": rows,
        "totals": _sum_rows(
            rows,
            integers=(
                "appointments",
                "completed",
                "cancelled",
                "no_shows",
                "sessions",
                "new_patients",
                "bills",
            ),
            money_fields=("billed", "collected", "outstanding"),
        ),
    }


def _sum_rows(rows, *, integers=(), money_fields=()) -> dict:
    """Totals derived from the rows themselves, so the two can never disagree."""
    totals: dict[str, object] = {field: sum(row[field] for row in rows) for field in integers}
    for field in money_fields:
        totals[field] = money(sum((row[field] for row in rows), ZERO))
    return totals


# --------------------------------------------------------------------------- #
# 2. Appointment report
# --------------------------------------------------------------------------- #
def appointment_report(db: Session, user: User, window: Window) -> dict:
    by_status = {
        row[0].value if hasattr(row[0], "value") else str(row[0]): row[1]
        for row in db.execute(
            _scope(
                select(Appointment.status, func.count()).where(
                    Appointment.appointment_date >= window.date_from,
                    Appointment.appointment_date <= window.date_to,
                ),
                Appointment.clinic_id,
                window.clinic_ids,
            ).group_by(Appointment.status)
        )
    }

    daily = [
        {"date": row[0], "appointments": row[1], "completed": int(row[2] or 0)}
        for row in db.execute(
            _scope(
                select(
                    Appointment.appointment_date,
                    func.count(),
                    func.sum(
                        case((Appointment.status == AppointmentStatus.COMPLETED, 1), else_=0)
                    ),
                ).where(
                    Appointment.appointment_date >= window.date_from,
                    Appointment.appointment_date <= window.date_to,
                ),
                Appointment.clinic_id,
                window.clinic_ids,
            )
            .group_by(Appointment.appointment_date)
            .order_by(Appointment.appointment_date)
        )
    ]

    total = sum(by_status.values())
    completed = by_status.get("COMPLETED", 0)
    no_shows = by_status.get("NO_SHOW", 0)
    cancelled = by_status.get("CANCELLED", 0)

    new_appointments, returning_appointments, new_patients, returning_patients = (
        _new_vs_returning(db, window)
    )

    # Staffing signal: which weekday and which hour actually carry the load.
    by_weekday = _appointments_by_weekday(db, window)
    by_hour = _appointments_by_hour(db, window)

    return {
        "total": total,
        "by_status": by_status,
        "daily": daily,
        # "New" means the patient's *first ever* appointment falls in this
        # window -- acquisition, as opposed to repeat business.
        "new_patient_appointments": new_appointments,
        "returning_patient_appointments": returning_appointments,
        "new_patients": new_patients,
        "returning_patients": returning_patients,
        "new_patient_share": _rate(new_appointments, total),
        "by_weekday": by_weekday,
        "by_hour": by_hour,
        # Rates are reported against the total in the window rather than against
        # "resolved" appointments: the denominator is stated so the figure cannot
        # be quietly reinterpreted.
        "completion_rate": _rate(completed, total),
        "no_show_rate": _rate(no_shows, total),
        "cancellation_rate": _rate(cancelled, total),
        "busiest_day": max(daily, key=lambda row: row["appointments"]) if daily else None,
    }


def _rate(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


# --------------------------------------------------------------------------- #
# 3. Patient source report
# --------------------------------------------------------------------------- #
def source_report(db: Session, user: User, window: Window) -> dict:
    """Where patients came from, and what those patients were billed.

    Revenue is attributed through the patient's source, computed as its own
    grouped query over bills joined to patients -- a many-to-one direction, so
    it cannot multiply.
    """
    counts = {
        row[0]: row[1]
        for row in db.execute(
            _scope(
                select(Patient.source_id, func.count()).where(
                    func.date(Patient.created_at) >= window.date_from,
                    func.date(Patient.created_at) <= window.date_to,
                ),
                Patient.primary_clinic_id,
                window.clinic_ids,
            ).group_by(Patient.source_id)
        )
    }

    revenue = {
        row[0]: (money(row[1]), money(row[2]))
        for row in db.execute(
            _scope(
                select(
                    Patient.source_id,
                    func.coalesce(func.sum(Bill.total_amount), 0),
                    func.coalesce(func.sum(Bill.amount_paid), 0),
                )
                .join(Patient, Bill.patient_id == Patient.id)
                .where(
                    Bill.status != BillStatus.CANCELLED,
                    Bill.bill_date >= window.date_from,
                    Bill.bill_date <= window.date_to,
                ),
                Bill.clinic_id,
                window.clinic_ids,
            ).group_by(Patient.source_id)
        )
    }

    names = {row.id: row.name for row in db.execute(select(PatientSource.id, PatientSource.name))}

    rows = []
    for source_id in set(counts) | set(revenue):
        billed, collected = revenue.get(source_id, (ZERO, ZERO))
        rows.append(
            {
                "source_id": source_id,
                "source_name": names.get(source_id, "Not recorded"),
                "new_patients": counts.get(source_id, 0),
                "billed": billed,
                "collected": collected,
            }
        )
    rows.sort(key=lambda row: (-row["new_patients"], row["source_name"]))

    totals = _sum_rows(rows, integers=("new_patients",), money_fields=("billed", "collected"))
    for row in rows:
        row["share_percent"] = _rate(row["new_patients"], totals["new_patients"])

    return {"rows": rows, "totals": totals}


# --------------------------------------------------------------------------- #
# 4. Revenue report
# --------------------------------------------------------------------------- #
def revenue_report(db: Session, user: User, window: Window) -> dict:
    """Billed vs collected, broken down four ways.

    **`billed` and `collected` answer different questions and are dated
    differently on purpose:** `billed` is the value of bills raised in the
    window; `collected` is money that actually arrived in it, which may settle a
    bill raised earlier. Comparing the two across a short window is therefore
    normal and not a discrepancy.

    `outstanding` is the balance still owed *on bills raised in this window*, so
    it is a property of those bills rather than the difference of two
    differently-dated figures.
    """
    billed, outstanding, bill_count = db.execute(
        _scope(
            select(
                func.coalesce(func.sum(Bill.total_amount), 0),
                func.coalesce(func.sum(Bill.total_amount - Bill.amount_paid), 0),
                func.count(),
            ).where(
                Bill.status != BillStatus.CANCELLED,
                Bill.bill_date >= window.date_from,
                Bill.bill_date <= window.date_to,
            ),
            Bill.clinic_id,
            window.clinic_ids,
        )
    ).one()

    collected = db.execute(_collected_stmt(window)).scalar_one()

    # Billed is keyed on bill_date, collected on payment_date, so the two daily
    # series are built separately and merged on the date.
    billed_daily = {
        row[0]: money(row[1])
        for row in db.execute(
            _scope(
                select(Bill.bill_date, func.coalesce(func.sum(Bill.total_amount), 0)).where(
                    Bill.status != BillStatus.CANCELLED,
                    Bill.bill_date >= window.date_from,
                    Bill.bill_date <= window.date_to,
                ),
                Bill.clinic_id,
                window.clinic_ids,
            ).group_by(Bill.bill_date)
        )
    }
    collected_daily = {
        row[0]: money(row[1])
        for row in db.execute(
            _collected_stmt(window, Payment.payment_date).group_by(Payment.payment_date)
        )
    }
    daily = [
        {
            "date": day,
            "billed": billed_daily.get(day, ZERO),
            "collected": collected_daily.get(day, ZERO),
        }
        for day in sorted(set(billed_daily) | set(collected_daily))
    ]

    billed_by_clinic = {
        row[0]: money(row[1])
        for row in db.execute(
            _scope(
                select(Clinic.name, func.coalesce(func.sum(Bill.total_amount), 0))
                .join(Clinic, Bill.clinic_id == Clinic.id)
                .where(
                    Bill.status != BillStatus.CANCELLED,
                    Bill.bill_date >= window.date_from,
                    Bill.bill_date <= window.date_to,
                ),
                Bill.clinic_id,
                window.clinic_ids,
            ).group_by(Clinic.name)
        )
    }
    collected_by_clinic = {
        row[0]: money(row[1])
        for row in db.execute(
            _collected_stmt(window, Clinic.name)
            .join(Clinic, Bill.clinic_id == Clinic.id)
            .group_by(Clinic.name)
        )
    }
    by_clinic = sorted(
        (
            {
                "clinic_name": clinic,
                "billed": billed_by_clinic.get(clinic, ZERO),
                "collected": collected_by_clinic.get(clinic, ZERO),
            }
            for clinic in set(billed_by_clinic) | set(collected_by_clinic)
        ),
        key=lambda row: row["collected"],
        reverse=True,
    )

    by_method = [
        {
            "payment_method": row[0].value if hasattr(row[0], "value") else str(row[0]),
            "amount": money(row[2]),
            "payments": row[1],
        }
        for row in db.execute(
            _collected_stmt(window, Payment.payment_method, func.count())
            .group_by(Payment.payment_method)
            .order_by(func.coalesce(func.sum(Payment.amount), 0).desc())
        )
    ]

    by_service = [
        {"description": row[0], "quantity": row[1], "amount": money(row[2])}
        for row in db.execute(
            _scope(
                select(
                    BillItem.description,
                    func.coalesce(func.sum(BillItem.quantity), 0),
                    func.coalesce(func.sum(BillItem.amount), 0),
                )
                .join(Bill, BillItem.bill_id == Bill.id)
                .where(
                    Bill.status != BillStatus.CANCELLED,
                    Bill.bill_date >= window.date_from,
                    Bill.bill_date <= window.date_to,
                ),
                Bill.clinic_id,
                window.clinic_ids,
            )
            .group_by(BillItem.description)
            .order_by(func.coalesce(func.sum(BillItem.amount), 0).desc())
            .limit(25)
        )
    ]

    # Distinct payers, so "average per patient" is per person rather than per bill.
    payers = db.execute(
        _scope(
            select(func.count(func.distinct(Bill.patient_id))).where(
                Bill.status != BillStatus.CANCELLED,
                Bill.bill_date >= window.date_from,
                Bill.bill_date <= window.date_to,
            ),
            Bill.clinic_id,
            window.clinic_ids,
        )
    ).scalar_one()

    return {
        "billed": money(billed),
        "collected": money(collected),
        "outstanding": money(outstanding),
        "collection_rate": _rate(int(money(collected) * 100), int(money(billed) * 100)),
        "bills": bill_count,
        "paying_patients": payers,
        "average_bill": money(money(billed) / bill_count) if bill_count else ZERO,
        "average_per_patient": money(money(billed) / payers) if payers else ZERO,
        "daily": daily,
        "by_clinic": by_clinic,
        "by_payment_method": by_method,
        "by_service": by_service,
    }


# --------------------------------------------------------------------------- #
# 5. Session report
# --------------------------------------------------------------------------- #
def session_report(db: Session, user: User, window: Window) -> dict:
    base = (
        PatientSession.is_voided.is_(False),
        PatientSession.session_date >= window.date_from,
        PatientSession.session_date <= window.date_to,
    )

    total = db.execute(
        _scope(
            select(func.count()).select_from(PatientSession).where(*base),
            PatientSession.clinic_id,
            window.clinic_ids,
        )
    ).scalar_one()

    voided = db.execute(
        _scope(
            select(func.count())
            .select_from(PatientSession)
            .where(
                PatientSession.is_voided.is_(True),
                PatientSession.session_date >= window.date_from,
                PatientSession.session_date <= window.date_to,
            ),
            PatientSession.clinic_id,
            window.clinic_ids,
        )
    ).scalar_one()

    by_therapist = [
        {"therapist": row[0] or "Not recorded", "sessions": row[1]}
        for row in db.execute(
            _scope(
                select(User.full_name, func.count())
                .select_from(PatientSession)
                .outerjoin(User, PatientSession.therapist_user_id == User.id)
                .where(*base),
                PatientSession.clinic_id,
                window.clinic_ids,
            )
            .group_by(User.full_name)
            .order_by(func.count().desc())
        )
    ]

    by_clinic = [
        {"clinic_name": row[0], "sessions": row[1]}
        for row in db.execute(
            _scope(
                select(Clinic.name, func.count())
                .select_from(PatientSession)
                .join(Clinic, PatientSession.clinic_id == Clinic.id)
                .where(*base),
                PatientSession.clinic_id,
                window.clinic_ids,
            )
            .group_by(Clinic.name)
            .order_by(func.count().desc())
        )
    ]

    daily = [
        {"date": row[0], "sessions": row[1]}
        for row in db.execute(
            _scope(
                select(PatientSession.session_date, func.count()).where(*base),
                PatientSession.clinic_id,
                window.clinic_ids,
            )
            .group_by(PatientSession.session_date)
            .order_by(PatientSession.session_date)
        )
    ]

    packages = db.execute(
        _scope(
            select(
                func.count(),
                func.coalesce(func.sum(TreatmentPackage.sessions_registered), 0),
                func.coalesce(func.sum(TreatmentPackage.sessions_taken), 0),
            ).where(
                TreatmentPackage.start_date >= window.date_from,
                TreatmentPackage.start_date <= window.date_to,
            ),
            TreatmentPackage.clinic_id,
            window.clinic_ids,
        )
    ).one()

    registered = int(packages[1] or 0)
    taken = int(packages[2] or 0)

    return {
        "total": total,
        "voided": voided,
        "by_therapist": by_therapist,
        "by_clinic": by_clinic,
        "daily": daily,
        "packages_registered": packages[0] or 0,
        "sessions_purchased": registered,
        "sessions_used": taken,
        "utilisation_rate": _rate(taken, registered),
        "average_per_day": round(total / window.days, 2) if window.days else 0.0,
    }


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #
def to_csv(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    """Rows to CSV using `(key, header)` pairs.

    Explicit columns rather than `row.keys()`: a report gaining a field should
    not silently change the shape of a file someone's spreadsheet depends on.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([header for _, header in columns])
    for row in rows:
        writer.writerow([_csv_value(row.get(key)) for key, _ in columns])
    return buffer.getvalue()


def _csv_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        # Plain decimal, no currency symbol or thousands separator: a spreadsheet
        # must be able to sum the column.
        return f"{value:.2f}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def jsonable(value):
    """Recursively turn Decimals into strings for the JSON response.

    Reports return plain dicts rather than Pydantic models -- their shape varies
    per report -- and a bare dict goes through FastAPI's `jsonable_encoder`,
    which renders Decimal as a **float**. That reintroduces binary-fraction
    error at the edge of a system that is careful about it everywhere else, so
    the conversion is done explicitly here instead.
    """
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


#: Column sets for each report's CSV, kept beside the report they describe.
CSV_COLUMNS = {
    "clinics": [
        ("clinic_name", "Clinic"),
        ("appointments", "Appointments"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
        ("no_shows", "No shows"),
        ("sessions", "Sessions"),
        ("new_patients", "New patients"),
        ("bills", "Bills"),
        ("billed", "Billed"),
        ("collected", "Collected"),
        ("outstanding", "Outstanding"),
    ],
    "appointments": [
        ("date", "Date"),
        ("appointments", "Appointments"),
        ("completed", "Completed"),
    ],
    "patient-sources": [
        ("source_name", "Source"),
        ("new_patients", "New patients"),
        ("share_percent", "Share %"),
        ("billed", "Billed"),
        ("collected", "Collected"),
    ],
    "revenue": [
        ("date", "Date"),
        ("billed", "Billed"),
        ("collected", "Collected"),
    ],
    "sessions": [
        ("date", "Date"),
        ("sessions", "Sessions"),
    ],
    "retention": [
        ("patient_name", "Patient"),
        ("patient_code", "Patient ID"),
        ("mobile", "Mobile"),
        ("clinic_name", "Clinic"),
        ("last_seen", "Last seen"),
        ("days_since", "Days since"),
        ("sessions_remaining", "Sessions owed"),
        ("value_at_risk", "Value at risk"),
    ],
    "month-comparison": [
        ("label", "Month"),
        ("period_end", "Up to"),
        ("days", "Days"),
        ("collected", "Collected"),
        ("billed", "Billed"),
        ("sessions", "Sessions"),
        ("new_patients", "New patients"),
    ],
}

#: Which key inside each report payload holds the exportable rows.
CSV_ROWS_KEY = {
    "clinics": "rows",
    "appointments": "daily",
    "patient-sources": "rows",
    "revenue": "daily",
    "sessions": "daily",
    "retention": "rows",
}


# --------------------------------------------------------------------------- #
# New vs returning, and load distribution
# --------------------------------------------------------------------------- #
def _first_appointment_subquery(window: Window):
    """Each patient's first-ever appointment date, within the caller's scope.

    Scoped the same way as everything else: for a Clinic User, "first visit"
    means first visit at a clinic they can see, which is the only thing they
    could verify anyway.
    """
    stmt = _scope(
        select(
            Appointment.patient_id.label("patient_id"),
            func.min(Appointment.appointment_date).label("first_date"),
        ),
        Appointment.clinic_id,
        window.clinic_ids,
    ).group_by(Appointment.patient_id)
    return stmt.subquery()


def _new_vs_returning(db: Session, window: Window) -> tuple[int, int, int, int]:
    """Appointments and patients split by whether the patient is new.

    Two counts rather than a join onto the appointment rows: joining a
    per-patient aggregate back onto appointments is safe here, but counting the
    *set* of new patients separately keeps the two figures independent and makes
    the reconciliation in the tests meaningful.
    """
    first = _first_appointment_subquery(window)

    new_ids = select(first.c.patient_id).where(
        first.c.first_date >= window.date_from,
        first.c.first_date <= window.date_to,
    )

    in_window = (
        Appointment.appointment_date >= window.date_from,
        Appointment.appointment_date <= window.date_to,
    )

    new_appointments = db.execute(
        _scope(
            select(func.count()).select_from(Appointment).where(
                *in_window, Appointment.patient_id.in_(new_ids)
            ),
            Appointment.clinic_id,
            window.clinic_ids,
        )
    ).scalar_one()

    total = db.execute(
        _scope(
            select(func.count()).select_from(Appointment).where(*in_window),
            Appointment.clinic_id,
            window.clinic_ids,
        )
    ).scalar_one()

    distinct_patients = db.execute(
        _scope(
            select(func.count(func.distinct(Appointment.patient_id)))
            .select_from(Appointment)
            .where(*in_window),
            Appointment.clinic_id,
            window.clinic_ids,
        )
    ).scalar_one()

    new_patients = db.execute(
        _scope(
            select(func.count(func.distinct(Appointment.patient_id)))
            .select_from(Appointment)
            .where(*in_window, Appointment.patient_id.in_(new_ids)),
            Appointment.clinic_id,
            window.clinic_ids,
        )
    ).scalar_one()

    return (
        new_appointments,
        total - new_appointments,
        new_patients,
        distinct_patients - new_patients,
    )


#: Monday-first, matching how a clinic thinks about its week.
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _appointments_by_weekday(db: Session, window: Window) -> list[dict]:
    """Counted in Python from the daily series rather than with a SQL weekday
    function, because `strftime('%w')` (SQLite) and `EXTRACT(DOW)` (PostgreSQL)
    differ in both syntax and in which day is zero -- a portability trap for a
    figure that is trivial to derive."""
    rows = db.execute(
        _scope(
            select(Appointment.appointment_date, func.count()).where(
                Appointment.appointment_date >= window.date_from,
                Appointment.appointment_date <= window.date_to,
            ),
            Appointment.clinic_id,
            window.clinic_ids,
        ).group_by(Appointment.appointment_date)
    ).all()

    totals = dict.fromkeys(range(7), 0)
    for day, count in rows:
        totals[day.weekday()] += count
    return [
        {"weekday": _WEEKDAY_NAMES[index], "appointments": totals[index]}
        for index in range(7)
    ]


def _appointments_by_hour(db: Session, window: Window) -> list[dict]:
    rows = db.execute(
        _scope(
            select(Appointment.start_time, func.count()).where(
                Appointment.appointment_date >= window.date_from,
                Appointment.appointment_date <= window.date_to,
            ),
            Appointment.clinic_id,
            window.clinic_ids,
        ).group_by(Appointment.start_time)
    ).all()

    totals: dict[int, int] = {}
    for start, count in rows:
        totals[start.hour] = totals.get(start.hour, 0) + count
    return [
        {
            "hour": hour,
            "label": f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}",
            "appointments": totals[hour],
        }
        for hour in sorted(totals)
    ]


# --------------------------------------------------------------------------- #
# 6. Month-on-month comparison
# --------------------------------------------------------------------------- #
def _month_start(value: date, months_back: int) -> date:
    month = value.month - months_back
    year = value.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return date(year, month, 1)


def _days_in_month(value: date) -> int:
    return (_month_start(value, -1) - timedelta(days=1)).day


def month_comparison(
    db: Session, user: User, *, as_of: date, months: int, clinic_id: int | None
) -> dict:
    """This month to a given day, against the same span in previous months.

    Comparing a part-month against whole previous months is the classic way to
    conclude business is collapsing on the 3rd of the month. Each period is
    therefore cut at the **same day number**, clamped to that month's length so
    31 January compares against 28/29 February rather than silently overflowing.
    """
    from app.utils.exceptions import ValidationError

    if months < 2 or months > 12:
        raise ValidationError("Compare between 2 and 12 months")

    accessible = permissions.accessible_clinic_ids(db, user)
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        clinic_ids = [clinic_id]
    else:
        clinic_ids = accessible

    periods = []
    for offset in range(months):
        start = _month_start(as_of, offset)
        day = min(as_of.day, _days_in_month(start))
        end = start.replace(day=day)
        window = Window(start, end, clinic_ids, clinic_id)

        billed, outstanding = db.execute(
            _scope(
                select(
                    func.coalesce(func.sum(Bill.total_amount), 0),
                    func.coalesce(func.sum(Bill.total_amount - Bill.amount_paid), 0),
                ).where(
                    Bill.status != BillStatus.CANCELLED,
                    Bill.bill_date >= start,
                    Bill.bill_date <= end,
                ),
                Bill.clinic_id,
                clinic_ids,
            )
        ).one()
        collected = db.execute(_collected_stmt(window)).scalar_one()

        sessions = db.execute(
            _scope(
                select(func.count()).select_from(PatientSession).where(
                    PatientSession.is_voided.is_(False),
                    PatientSession.session_date >= start,
                    PatientSession.session_date <= end,
                ),
                PatientSession.clinic_id,
                clinic_ids,
            )
        ).scalar_one()

        new_patients = db.execute(
            _scope(
                select(func.count()).select_from(Patient).where(
                    func.date(Patient.created_at) >= start,
                    func.date(Patient.created_at) <= end,
                ),
                Patient.primary_clinic_id,
                clinic_ids,
            )
        ).scalar_one()

        periods.append(
            {
                "label": f"{start:%b %Y}",
                "month_start": start,
                "period_end": end,
                "days": (end - start).days + 1,
                "billed": money(billed),
                "collected": money(collected),
                "outstanding": money(outstanding),
                "sessions": sessions,
                "new_patients": new_patients,
            }
        )

    current = periods[0]
    previous = periods[1:]
    average = (
        money(sum((row["collected"] for row in previous), ZERO) / len(previous))
        if previous
        else ZERO
    )
    last_month = previous[0]["collected"] if previous else ZERO

    return {
        "as_of": as_of,
        "day_of_month": as_of.day,
        "clinic_id": clinic_id,
        # Newest first, so the current month reads at the top of the table.
        "periods": periods,
        "current": current,
        "average_of_previous": average,
        "change_vs_last_month_percent": _change(current["collected"], last_month),
        "change_vs_average_percent": _change(current["collected"], average),
    }


def _change(current: Decimal, baseline: Decimal) -> float | None:
    """Percent change, or None when there is no baseline to divide by.

    None rather than 0 or 100: "we earned nothing in the comparison period" is
    not a percentage, and reporting one invents a number.
    """
    if baseline == 0:
        return None
    return round(float((current - baseline) / baseline * 100), 1)


# --------------------------------------------------------------------------- #
# 7. Retention: patients who have stopped coming
# --------------------------------------------------------------------------- #
#: A gap this long with sessions still owed means they have stopped coming,
#: not that they are between appointments.
DROPOUT_DAYS = 60


def retention_report(
    db: Session, user: User, window: Window, *, dropout_days: int = DROPOUT_DAYS
) -> dict:
    """Patients who paid for sessions and stopped turning up.

    This is money already taken for treatment not delivered: a service
    obligation, and the clearest signal of where the business is leaking. Keyed
    off the **last delivered session**, not the package start, so someone
    part-way through a course counts from their last visit.
    """
    cutoff = date.today() - timedelta(days=dropout_days)

    last_session = (
        select(
            PatientSession.package_id.label("package_id"),
            func.max(PatientSession.session_date).label("last_date"),
        )
        .where(PatientSession.is_voided.is_(False))
        .group_by(PatientSession.package_id)
        .subquery()
    )

    stmt = (
        select(
            TreatmentPackage,
            last_session.c.last_date,
        )
        .outerjoin(last_session, last_session.c.package_id == TreatmentPackage.id)
        .where(
            TreatmentPackage.status == PackageStatus.ACTIVE,
            TreatmentPackage.sessions_taken < TreatmentPackage.sessions_registered,
        )
    )
    stmt = _scope(stmt, TreatmentPackage.clinic_id, window.clinic_ids)

    rows = []
    active_with_room = 0
    for package, last_date in db.execute(stmt).unique().all():
        active_with_room += 1
        # No session yet: measure from when the course was sold.
        reference = last_date or package.start_date
        if reference is None or reference > cutoff:
            continue

        remaining = package.sessions_registered - package.sessions_taken
        rows.append(
            {
                "package_id": package.id,
                "patient_id": package.patient_id,
                "patient_name": package.patient.full_name if package.patient else None,
                "patient_code": package.patient.patient_code if package.patient else None,
                "mobile": package.patient.mobile if package.patient else None,
                "clinic_name": package.clinic.name if package.clinic else None,
                "sessions_registered": package.sessions_registered,
                "sessions_taken": package.sessions_taken,
                "sessions_remaining": remaining,
                "last_seen": last_date,
                "days_since": (date.today() - reference).days,
                "value_at_risk": money(package.price_per_session * remaining),
            }
        )

    rows.sort(key=lambda row: row["days_since"], reverse=True)

    return {
        "dropout_days": dropout_days,
        "cutoff": cutoff,
        "dropped_out": len(rows),
        "active_packages_with_sessions_left": active_with_room,
        "dropout_rate": _rate(len(rows), active_with_room),
        "value_at_risk": money(sum((row["value_at_risk"] for row in rows), ZERO)),
        "sessions_owed": sum(row["sessions_remaining"] for row in rows),
        "rows": rows,
        "repeat_patients": _repeat_patients(db, window),
    }


def _repeat_patients(db: Session, window: Window) -> dict:
    """How many patients bought more than one package -- the renewal signal."""
    per_patient = _scope(
        select(TreatmentPackage.patient_id, func.count().label("packages")),
        TreatmentPackage.clinic_id,
        window.clinic_ids,
    ).group_by(TreatmentPackage.patient_id).subquery()

    total = db.execute(select(func.count()).select_from(per_patient)).scalar_one()
    repeat = db.execute(
        select(func.count()).select_from(per_patient).where(per_patient.c.packages > 1)
    ).scalar_one()
    return {
        "patients_with_packages": total,
        "repeat_patients": repeat,
        "repeat_rate": _rate(repeat, total),
    }
