"""Dashboard aggregation (Section 22).

Every number here is a real aggregate over the database, scoped to what the
caller is allowed to see -- never a placeholder or a fabricated figure.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.auth import permissions
from app.models import (
    Appointment,
    AppointmentStatus,
    Bill,
    BillStatus,
    Clinic,
    ClinicStatus,
    Notification,
    Patient,
    PatientSession,
    RoleName,
    User,
)
from app.schemas.clinic import ClinicSummary
from app.schemas.dashboard import ClinicPerformance, DashboardCounters, DashboardResponse

#: Statuses that mean "still to happen today".
_PENDING_STATUSES = (AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED)


_ZERO = Decimal("0.00")


def _scope(stmt: Select, column, clinic_ids: list[int] | None) -> Select:
    """Apply clinic scoping to a query, if the caller is restricted."""
    if clinic_ids is None:
        return stmt
    if not clinic_ids:
        # A restricted user with no assignment must match nothing, rather than
        # falling through to matching everything.
        return stmt.where(column.in_([-1]))
    return stmt.where(column.in_(clinic_ids))


def _count(db: Session, stmt: Select) -> int:
    """COUNT(*) over a query used as a subquery."""
    return db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _bill_totals(db: Session, clinic_ids: list[int] | None) -> tuple[Decimal, Decimal]:
    """(billed, collected) across non-cancelled bills in scope.

    Both sums are taken in one pass, referencing the *scoped* columns directly
    instead of aggregating over a subquery -- summing an outer-table column
    against a subquery silently produces a cross join and multiplies the result.
    """
    stmt = _scope(
        select(
            func.coalesce(func.sum(Bill.total_amount), 0),
            func.coalesce(func.sum(Bill.amount_paid), 0),
        ).where(Bill.status != BillStatus.CANCELLED),
        Bill.clinic_id,
        clinic_ids,
    )
    billed, collected = db.execute(stmt).one()
    return _money(billed), _money(collected)


def _month_collected(db: Session, clinic_ids: list[int] | None, today: date) -> Decimal:
    stmt = _scope(
        select(func.coalesce(func.sum(Bill.amount_paid), 0)).where(
            Bill.status != BillStatus.CANCELLED,
            Bill.bill_date >= today.replace(day=1),
            Bill.bill_date <= today,
        ),
        Bill.clinic_id,
        clinic_ids,
    )
    return _money(db.execute(stmt).scalar_one())


def build_dashboard(db: Session, user: User, today: date | None = None) -> DashboardResponse:
    today = today or date.today()
    clinic_ids = permissions.accessible_clinic_ids(db, user)
    role = user.role_name
    counters = DashboardCounters()

    # --- appointments (all roles) ---
    appts = _scope(select(Appointment.id), Appointment.clinic_id, clinic_ids)
    counters.appointments_today = _count(
        db, appts.where(Appointment.appointment_date == today)
    )
    counters.completed_today = _count(
        db,
        appts.where(
            Appointment.appointment_date == today,
            Appointment.status == AppointmentStatus.COMPLETED,
        ),
    )
    counters.cancelled_today = _count(
        db,
        appts.where(
            Appointment.appointment_date == today,
            Appointment.status == AppointmentStatus.CANCELLED,
        ),
    )
    counters.upcoming_appointments = _count(
        db,
        appts.where(
            Appointment.appointment_date > today,
            Appointment.status.in_(_PENDING_STATUSES),
        ),
    )
    counters.pending_today = _count(
        db,
        appts.where(
            Appointment.appointment_date == today,
            Appointment.status.in_(_PENDING_STATUSES),
        ),
    )
    counters.checked_in_now = _count(
        db,
        appts.where(
            Appointment.appointment_date == today,
            Appointment.status == AppointmentStatus.CHECKED_IN,
        ),
    )

    # --- patients ---
    patients = _scope(select(Patient.id), Patient.primary_clinic_id, clinic_ids)
    counters.total_patients = _count(db, patients)
    counters.patients_registered_today = _count(
        db, patients.where(Patient.registration_date == today)
    )

    # --- sessions ---
    sessions = _scope(select(PatientSession.id), PatientSession.clinic_id, clinic_ids)
    counters.sessions_completed_today = _count(
        db, sessions.where(PatientSession.session_date == today)
    )

    # --- money (Superadmin and Admin only) ---
    if role in (RoleName.SUPERADMIN, RoleName.ADMIN):
        billed, collected = _bill_totals(db, clinic_ids)
        counters.revenue_total = collected
        counters.revenue_this_month = _month_collected(db, clinic_ids, today)
        counters.outstanding_amount = max(billed - collected, _ZERO)

    # --- notifications ---
    notif = _scope(select(Notification.id), Notification.clinic_id, clinic_ids)
    counters.unacknowledged_notifications = _count(
        db, notif.where(Notification.is_acknowledged.is_(False))
    )

    # --- system footprint (Superadmin only) ---
    if role == RoleName.SUPERADMIN:
        counters.total_clinics = db.execute(select(func.count()).select_from(Clinic)).scalar_one()
        counters.active_clinics = db.execute(
            select(func.count()).select_from(Clinic).where(Clinic.status == ClinicStatus.ACTIVE)
        ).scalar_one()
        counters.total_users = db.execute(select(func.count()).select_from(User)).scalar_one()
        counters.active_users = db.execute(
            select(func.count()).select_from(User).where(User.is_active.is_(True))
        ).scalar_one()

    visible = permissions.visible_clinics(db, user)
    clinic_performance: list[ClinicPerformance] = []
    if role in (RoleName.SUPERADMIN, RoleName.ADMIN):
        clinic_performance = _clinic_breakdown(db, visible, today)

    return DashboardResponse(
        role=role,
        as_of_date=today,
        scope_label=_scope_label(user, visible),
        counters=counters,
        clinics=[ClinicSummary.model_validate(clinic) for clinic in visible],
        clinic_performance=clinic_performance,
    )


def _clinic_breakdown(db: Session, clinics, today: date) -> list[ClinicPerformance]:
    """Per-clinic figures via grouped queries (four queries, not four per clinic)."""
    clinic_ids = [clinic.id for clinic in clinics]
    if not clinic_ids:
        return []

    patient_counts = dict(
        db.execute(
            select(Patient.primary_clinic_id, func.count())
            .where(Patient.primary_clinic_id.in_(clinic_ids))
            .group_by(Patient.primary_clinic_id)
        ).all()
    )
    appts_today = dict(
        db.execute(
            select(Appointment.clinic_id, func.count())
            .where(
                Appointment.clinic_id.in_(clinic_ids),
                Appointment.appointment_date == today,
            )
            .group_by(Appointment.clinic_id)
        ).all()
    )
    completed_today = dict(
        db.execute(
            select(Appointment.clinic_id, func.count())
            .where(
                Appointment.clinic_id.in_(clinic_ids),
                Appointment.appointment_date == today,
                Appointment.status == AppointmentStatus.COMPLETED,
            )
            .group_by(Appointment.clinic_id)
        ).all()
    )
    money = {
        clinic_id: (_money(billed), _money(collected))
        for clinic_id, billed, collected in db.execute(
            select(
                Bill.clinic_id,
                func.coalesce(func.sum(Bill.total_amount), 0),
                func.coalesce(func.sum(Bill.amount_paid), 0),
            )
            .where(Bill.clinic_id.in_(clinic_ids), Bill.status != BillStatus.CANCELLED)
            .group_by(Bill.clinic_id)
        ).all()
    }

    breakdown: list[ClinicPerformance] = []
    for clinic in clinics:
        billed, collected = money.get(clinic.id, (_ZERO, _ZERO))
        breakdown.append(
            ClinicPerformance(
                clinic_id=clinic.id,
                clinic_name=clinic.name,
                total_patients=patient_counts.get(clinic.id, 0),
                appointments_today=appts_today.get(clinic.id, 0),
                completed_today=completed_today.get(clinic.id, 0),
                revenue=collected,
                outstanding=max(billed - collected, _ZERO),
            )
        )
    return breakdown


def _scope_label(user: User, visible) -> str:
    if user.role_name == RoleName.SUPERADMIN:
        return "All clinics (system-wide)"
    if user.role_name == RoleName.ADMIN:
        return f"All {len(visible)} clinic(s)"
    names = ", ".join(clinic.name for clinic in visible) or "no clinic assigned"
    return f"Your clinic: {names}"
