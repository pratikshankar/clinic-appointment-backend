"""Treatment packages and session logging (Sections 12, 13, 14).

The invariant this module exists to protect: **`sessions_taken` moves only by
logging or voiding a session, and never leaves the range
`0 <= taken <= registered`.** `sessions_remaining` is derived, so it cannot
disagree with the other two, and the database enforces the bounds with `CHECK`
constraints as a backstop.

Two behaviours follow from that:

* Logging a session and completing its appointment happen in **one transaction**,
  so the two records cannot drift apart (a clinic with 10 completed appointments
  and 4 logged sessions is a data problem nobody can untangle later).
* A mis-logged session is **voided**, not deleted: the counter is corrected and
  the correction itself stays on the record. Session numbers are never reused.
"""

import logging
from datetime import date, datetime, timezone

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import permissions
from app.models import (
    Appointment,
    AppointmentAction,
    AppointmentStatus,
    AuditAction,
    Bill,
    Clinic,
    ClinicUser,
    PackageStatus,
    Patient,
    PatientSession,
    RoleName,
    TreatmentPackage,
    User,
)
from app.schemas.session import (
    PackageCreate,
    PackageUpdate,
    SessionCreate,
    SessionUpdate,
)
from app.services import audit_service, billing_service, patient_service
from app.utils.exceptions import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)

logger = logging.getLogger(__name__)

_PACKAGE_LOADS = (selectinload(TreatmentPackage.clinic),)
_SESSION_LOADS = (
    selectinload(PatientSession.patient),
    selectinload(PatientSession.clinic),
    selectinload(PatientSession.package),
    selectinload(PatientSession.appointment),
    selectinload(PatientSession.therapist),
    selectinload(PatientSession.voided_by),
)


# --------------------------------------------------------------------------- #
# Packages
# --------------------------------------------------------------------------- #
def list_packages(
    db: Session, user: User, patient_id: int, include_closed: bool = True
) -> list[TreatmentPackage]:
    """Packages for a patient, scoped to clinics the caller may see."""
    patient_service.get_patient(db, user, patient_id)

    stmt = (
        select(TreatmentPackage)
        .options(*_PACKAGE_LOADS)
        .where(TreatmentPackage.patient_id == patient_id)
        .order_by(TreatmentPackage.start_date.desc().nullslast(), TreatmentPackage.id.desc())
    )
    accessible = permissions.accessible_clinic_ids(db, user)
    if accessible is not None:
        stmt = stmt.where(TreatmentPackage.clinic_id.in_(accessible or [-1]))
    if not include_closed:
        stmt = stmt.where(TreatmentPackage.status == PackageStatus.ACTIVE)

    return list(db.execute(stmt).unique().scalars().all())


def get_package(db: Session, user: User, package_id: int) -> TreatmentPackage:
    package = db.execute(
        select(TreatmentPackage)
        .options(*_PACKAGE_LOADS)
        .where(TreatmentPackage.id == package_id)
    ).unique().scalar_one_or_none()
    if package is None:
        raise NotFoundError(f"Treatment package {package_id} was not found")
    permissions.assert_clinic_access(db, user, package.clinic_id)
    return package


def _resolve_clinic(db: Session, user: User, patient: Patient, clinic_id: int | None) -> int:
    """Which clinic a package or session belongs to."""
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        return clinic_id
    if user.role_name == RoleName.CLINIC_USER and user.primary_clinic_id:
        return user.primary_clinic_id
    if patient.primary_clinic_id:
        return patient.primary_clinic_id
    raise ValidationError(
        "No clinic could be determined. Pass clinic_id explicitly."
    )


def create_package(
    db: Session,
    user: User,
    patient_id: int,
    payload: PackageCreate,
    request: Request | None = None,
) -> tuple[TreatmentPackage, "Bill | None"]:
    """Register a block of purchased sessions and bill for it.

    Multiple active packages are allowed by design: a patient may top up
    mid-course. Sessions consume the oldest active one with room left, so the
    order in which they were bought is the order in which they are used.

    The bill is written in the *same transaction* as the package. Registering a
    package is the moment money changes hands, and a package with no matching
    bill is exactly the kind of gap that shows up later as an unexplained
    difference between sessions delivered and revenue collected.
    """
    patient = patient_service.get_patient(db, user, patient_id)
    clinic_id = _resolve_clinic(db, user, patient, payload.clinic_id)

    package = TreatmentPackage(
        patient_id=patient.id,
        clinic_id=clinic_id,
        package_name=payload.package_name
        or f"{payload.sessions_registered}-session physiotherapy package",
        sessions_registered=payload.sessions_registered,
        sessions_taken=payload.sessions_taken,
        price_per_session=payload.price_per_session,
        start_date=payload.start_date or date.today(),
        end_date=payload.end_date,
        notes=payload.notes,
        created_by_user_id=user.id,
        # A package entered as already fully delivered is closed immediately.
        status=(
            PackageStatus.COMPLETED
            if payload.sessions_taken >= payload.sessions_registered
            else PackageStatus.ACTIVE
        ),
    )
    db.add(package)
    db.flush()

    audit_service.record(
        db,
        action=AuditAction.CREATED,
        user=user,
        entity_type="treatment_package",
        entity_id=package.id,
        clinic_id=clinic_id,
        description=(
            f"Registered {payload.sessions_registered} sessions for "
            f"{patient.patient_code} at {package.price_per_session}/session"
        ),
        request=request,
    )

    bill = None
    if not payload.skip_billing and payload.gross_amount > 0:
        lines = [
            billing_service.package_line(
                payload.sessions_registered,
                payload.price_per_session,
                package.package_name,
            ),
            *payload.additional_charges,
        ]
        # A zero-priced package with paid extras is legitimate (a free trial
        # session alongside a consultation fee), so the package line is dropped
        # rather than billed at nil.
        if payload.price_per_session <= 0:
            lines = lines[1:]

        try:
            bill = billing_service.build_bill(
                db,
                user,
                patient,
                clinic_id=clinic_id,
                lines=lines,
                # Dated today, not by the course start. The money changed hands
                # now; a patient paying today for a course beginning next month
                # would otherwise get an invoice dated next month, and the
                # payment would land in the wrong month's revenue.
                bill_date=date.today(),
                discount_amount=payload.discount_amount,
                payment=payload.payment,
                package_id=package.id,
            )
        except Exception:
            # Discard the package too. Nothing is committed yet, so relying on
            # the session being closed would probably do the same thing -- but
            # "probably" is not a guarantee to hang a financial record on, and a
            # package that outlived its own rejected bill is precisely the
            # orphan this call exists to prevent.
            db.rollback()
            raise

        audit_service.record(
            db,
            action=AuditAction.BILL_GENERATED,
            user=user,
            entity_type="bill",
            entity_id=bill.id,
            clinic_id=clinic_id,
            description=(
                f"Bill {bill.bill_number} raised with package {package.id} for "
                f"{patient.patient_code}: {bill.total_amount}, paid {bill.amount_paid}"
            ),
            request=request,
        )

    db.commit()
    db.refresh(package)
    if bill is not None:
        db.refresh(bill)
    return package, bill


def update_package(
    db: Session,
    user: User,
    package_id: int,
    payload: PackageUpdate,
    request: Request | None = None,
) -> TreatmentPackage:
    package = get_package(db, user, package_id)
    if package.status == PackageStatus.CANCELLED:
        raise ValidationError("A cancelled package cannot be edited")

    data = payload.model_dump(exclude_unset=True)

    # Shrinking a package below what has already been delivered would break the
    # invariant the database also enforces, so it is refused with a clear reason.
    new_registered = data.get("sessions_registered", package.sessions_registered)
    if new_registered < package.sessions_taken:
        raise ValidationError(
            f"This package already has {package.sessions_taken} session(s) logged, "
            f"so it cannot be reduced to {new_registered}. Void a session first."
        )

    changed: dict[str, object] = {}
    for field, value in data.items():
        if getattr(package, field) != value:
            changed[field] = str(value)
            setattr(package, field, value)

    _sync_package_status(package)

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=user,
        entity_type="treatment_package",
        entity_id=package.id,
        clinic_id=package.clinic_id,
        description=f"Updated package {package.id}",
        details={"changed_fields": changed} if changed else None,
        request=request,
    )
    db.commit()
    db.refresh(package)
    return package


def cancel_package(
    db: Session,
    user: User,
    package_id: int,
    reason: str | None,
    request: Request | None = None,
) -> TreatmentPackage:
    """Stop a package from being consumed. Logged sessions are untouched."""
    package = get_package(db, user, package_id)
    if package.status == PackageStatus.CANCELLED:
        raise ValidationError("This package is already cancelled")

    package.status = PackageStatus.CANCELLED
    package.notes = " | ".join(filter(None, [package.notes, f"Cancelled: {reason}" if reason else "Cancelled"]))

    audit_service.record(
        db,
        action=AuditAction.DISABLED,
        user=user,
        entity_type="treatment_package",
        entity_id=package.id,
        clinic_id=package.clinic_id,
        description=f"Cancelled package {package.id}"
        + (f" ({reason})" if reason else ""),
        request=request,
    )
    db.commit()
    db.refresh(package)
    return package


def _sync_package_status(package: TreatmentPackage) -> None:
    """Keep status consistent with the counters.

    Closes a package the moment its last session is delivered, and reopens it if
    a void frees a place. Without this, a fully-used package sits at ACTIVE
    forever -- which is exactly the drift found in the seeded data.
    """
    if package.status == PackageStatus.CANCELLED:
        return
    if package.sessions_taken >= package.sessions_registered:
        package.status = PackageStatus.COMPLETED
    else:
        package.status = PackageStatus.ACTIVE


# --------------------------------------------------------------------------- #
# Session numbering
# --------------------------------------------------------------------------- #
def _next_session_number(db: Session, patient_id: int, package_id: int | None) -> int:
    """Next number in the sequence.

    Numbered per package when there is one (so "session 5 of 10" is meaningful),
    otherwise per patient across their package-less sessions. Voided rows still
    hold their number -- the sequence is a record of what happened, not a count.
    """
    if package_id is not None:
        highest = db.execute(
            select(func.max(PatientSession.session_number)).where(
                PatientSession.package_id == package_id
            )
        ).scalar()
    else:
        highest = db.execute(
            select(func.max(PatientSession.session_number)).where(
                PatientSession.patient_id == patient_id,
                PatientSession.package_id.is_(None),
            )
        ).scalar()
    return (highest or 0) + 1


def _pick_package(
    db: Session, patient_id: int, clinic_ids: list[int] | None
) -> TreatmentPackage | None:
    """Oldest active package with sessions remaining."""
    stmt = (
        select(TreatmentPackage)
        .where(
            TreatmentPackage.patient_id == patient_id,
            TreatmentPackage.status == PackageStatus.ACTIVE,
            TreatmentPackage.sessions_taken < TreatmentPackage.sessions_registered,
        )
        .order_by(TreatmentPackage.start_date.asc().nullsfirst(), TreatmentPackage.id.asc())
    )
    if clinic_ids is not None:
        stmt = stmt.where(TreatmentPackage.clinic_id.in_(clinic_ids or [-1]))
    return db.execute(stmt).scalars().first()


def session_context(db: Session, user: User, patient_id: int) -> dict:
    """Everything the session form needs, in one call."""
    patient = patient_service.get_patient(db, user, patient_id)
    accessible = permissions.accessible_clinic_ids(db, user)
    packages = list_packages(db, user, patient_id)
    suggested = _pick_package(db, patient_id, accessible)

    clinic_id = None
    if user.role_name == RoleName.CLINIC_USER:
        clinic_id = user.primary_clinic_id
    clinic_id = clinic_id or patient.primary_clinic_id
    clinic = db.get(Clinic, clinic_id) if clinic_id else None

    # Therapists to choose from: staff at the clinic the session belongs to.
    therapists: list[dict] = []
    if clinic_id:
        rows = db.execute(
            select(User)
            .join(ClinicUser, ClinicUser.user_id == User.id)
            .where(ClinicUser.clinic_id == clinic_id, User.is_active.is_(True))
            .order_by(User.full_name)
        ).unique().scalars().all()
        therapists = [
            {"id": item.id, "full_name": item.full_name, "username": item.username}
            for item in rows
        ]
    if user.role_name != RoleName.CLINIC_USER and not any(
        item["id"] == user.id for item in therapists
    ):
        therapists.insert(
            0, {"id": user.id, "full_name": user.full_name, "username": user.username}
        )

    active = [p for p in packages if p.status == PackageStatus.ACTIVE]
    warnings: list[str] = []
    if not active:
        warnings.append(
            "This patient has no active treatment package. The session can still be "
            "logged as a one-off, or register a package first."
        )
    elif len(active) > 1:
        warnings.append(
            f"{len(active)} active packages. The oldest with sessions remaining is "
            "pre-selected; change it if this session belongs to another."
        )

    return {
        "patient_id": patient.id,
        "patient_name": patient.full_name,
        "patient_code": patient.patient_code,
        "clinic_id": clinic_id,
        "clinic_name": clinic.name if clinic else None,
        "suggested_package_id": suggested.id if suggested else None,
        "next_session_number": _next_session_number(
            db, patient_id, suggested.id if suggested else None
        ),
        "packages": packages,
        "therapists": therapists,
        # Live packages only -- see the note in `patient_service.profile`.
        "total_sessions_registered": sum(p.sessions_registered for p in packages if p.is_live),
        "total_sessions_taken": sum(p.sessions_taken for p in packages if p.is_live),
        "total_sessions_remaining": sum(p.sessions_remaining for p in packages if p.is_live),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Logging a session (Section 13)
# --------------------------------------------------------------------------- #
def log_session(
    db: Session,
    user: User,
    patient_id: int,
    payload: SessionCreate,
    request: Request | None = None,
) -> tuple[PatientSession, TreatmentPackage | None, bool, list[str]]:
    """Record a delivered session, and complete its appointment if one is linked.

    Returns `(session, package, appointment_completed, warnings)`. Everything
    happens in one transaction: if the counter cannot be incremented, the
    appointment is not completed either.
    """
    patient = patient_service.get_patient(db, user, patient_id)
    session_date = payload.session_date or date.today()
    warnings: list[str] = []

    # --- appointment, if this session came from one ---
    appointment: Appointment | None = None
    if payload.appointment_id is not None:
        appointment = db.get(Appointment, payload.appointment_id)
        if appointment is None:
            raise NotFoundError(f"Appointment {payload.appointment_id} was not found")
        if appointment.patient_id != patient.id:
            raise ValidationError(
                "That appointment belongs to a different patient"
            )
        permissions.assert_clinic_access(db, user, appointment.clinic_id)

        existing = db.execute(
            select(PatientSession).where(
                PatientSession.appointment_id == appointment.id,
                PatientSession.is_voided.is_(False),
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ValidationError(
                f"A session is already recorded for {appointment.appointment_code} "
                f"(session {existing.session_number}). Void it first to re-record."
            )

    # --- which package, if any ---
    # Resolved before the clinic on purpose: an explicitly named package is the
    # most specific thing the caller told us, so its problems ("that belongs to
    # another patient") should be reported ahead of a vaguer clinic failure, and
    # its own clinic is a sensible fallback for the session's.
    package: TreatmentPackage | None = None
    if not payload.no_package:
        if payload.package_id is not None:
            package = get_package(db, user, payload.package_id)
            if package.patient_id != patient.id:
                raise ValidationError("That package belongs to a different patient")
            if package.status == PackageStatus.CANCELLED:
                raise ValidationError("That package has been cancelled")
            if package.sessions_remaining <= 0:
                raise ValidationError(
                    f"That package is fully used ({package.sessions_taken} of "
                    f"{package.sessions_registered}). Register a new package or log "
                    "this session without one."
                )
        else:
            package = _pick_package(
                db, patient_id, permissions.accessible_clinic_ids(db, user)
            )
            if package is None:
                warnings.append(
                    "No active package had sessions remaining, so this session was "
                    "logged without consuming one."
                )

    clinic_id = _resolve_clinic(
        db,
        user,
        patient,
        payload.clinic_id
        or (appointment.clinic_id if appointment else None)
        or (package.clinic_id if package else None),
    )

    session = PatientSession(
        patient_id=patient.id,
        package_id=package.id if package else None,
        clinic_id=clinic_id,
        appointment_id=appointment.id if appointment else None,
        session_number=_next_session_number(db, patient.id, package.id if package else None),
        session_date=session_date,
        # Defaults to whoever is recording it, which is right for a therapist and
        # overridable when reception logs on someone's behalf.
        therapist_user_id=payload.therapist_user_id or user.id,
        treatment_provided=payload.treatment_provided,
        notes=payload.notes,
        remarks=payload.remarks,
    )
    db.add(session)

    if package is not None:
        # A session dated before the course begins is almost always a mis-pick:
        # the patient paid today for a package starting next week, and this
        # session belongs to an earlier course or to none. Reported rather than
        # refused -- the clinic may be recording a genuine backdated visit, and
        # blocking it would leave them no way to enter the truth.
        if package.start_date and session_date < package.start_date:
            warnings.append(
                f"This session is dated {session_date:%d-%b-%Y}, before the package "
                f"starts on {package.start_date:%d-%b-%Y}. Check it is against the "
                "right package."
            )
        package.sessions_taken += 1
        _sync_package_status(package)
        if package.sessions_remaining == 0:
            warnings.append(
                f"That was the last session of this package "
                f"({package.sessions_taken} of {package.sessions_registered}). "
                "Register a new package for further treatment."
            )
        elif package.sessions_remaining <= 2:
            warnings.append(
                f"{package.sessions_remaining} session(s) remaining in this package."
            )

    # --- complete the appointment in the same transaction ---
    appointment_completed = False
    if appointment is not None and appointment.status != AppointmentStatus.COMPLETED:
        from app.services.appointment_service import ALLOWED_TRANSITIONS, _record_history

        if appointment.status not in ALLOWED_TRANSITIONS[AppointmentStatus.COMPLETED]:
            raise ValidationError(
                f"{appointment.appointment_code} is {appointment.status.value} and "
                "cannot be completed"
            )
        previous = appointment.status
        appointment.status = AppointmentStatus.COMPLETED
        appointment.completed_at = datetime.now(timezone.utc)
        _record_history(
            db,
            appointment,
            AppointmentAction.COMPLETED,
            user,
            old_status=previous,
            new_status=AppointmentStatus.COMPLETED,
            reason="Session recorded",
        )
        appointment_completed = True

    db.flush()
    audit_service.record(
        db,
        action=AuditAction.SESSION_RECORDED,
        user=user,
        entity_type="patient_session",
        entity_id=session.id,
        clinic_id=clinic_id,
        description=(
            f"Session {session.session_number} recorded for {patient.patient_code}"
            + (
                f" ({package.sessions_taken} of {package.sessions_registered})"
                if package
                else " (no package)"
            )
        ),
        details={
            "appointment": appointment.appointment_code if appointment else None,
            "package_id": package.id if package else None,
        },
        request=request,
    )
    db.commit()
    db.refresh(session)
    if package is not None:
        db.refresh(package)
    return session, package, appointment_completed, warnings


def update_session(
    db: Session,
    user: User,
    session_id: int,
    payload: SessionUpdate,
    request: Request | None = None,
) -> PatientSession:
    """Edit the clinical narrative only (see `SessionUpdate`)."""
    session = get_session(db, user, session_id)
    if session.is_voided:
        raise ValidationError("A voided session cannot be edited")

    data = payload.model_dump(exclude_unset=True)
    changed = {}
    for field, value in data.items():
        if getattr(session, field) != value:
            changed[field] = value
            setattr(session, field, value)

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=user,
        entity_type="patient_session",
        entity_id=session.id,
        clinic_id=session.clinic_id,
        description=f"Updated notes on session {session.session_number}",
        details={"changed_fields": list(changed)} if changed else None,
        request=request,
    )
    db.commit()
    db.refresh(session)
    return session


def void_session(
    db: Session,
    user: User,
    session_id: int,
    reason: str,
    request: Request | None = None,
) -> tuple[PatientSession, TreatmentPackage | None]:
    """Undo a mis-logged session.

    The row stays with `is_voided = true` and the reason attached; the package
    counter is decremented and its status re-synced, so a completed package can
    reopen. The session number is not reused.
    """
    session = get_session(db, user, session_id)
    if session.is_voided:
        raise ValidationError("This session is already voided")

    session.is_voided = True
    session.void_reason = reason
    session.voided_by_user_id = user.id
    session.voided_at = datetime.now(timezone.utc)

    package = session.package
    if package is not None:
        package.sessions_taken = max(package.sessions_taken - 1, 0)
        _sync_package_status(package)

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=user,
        entity_type="patient_session",
        entity_id=session.id,
        clinic_id=session.clinic_id,
        description=f"Voided session {session.session_number}: {reason}",
        request=request,
    )
    db.commit()
    db.refresh(session)
    if package is not None:
        db.refresh(package)
    return session, package


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get_session(db: Session, user: User, session_id: int) -> PatientSession:
    session = db.execute(
        select(PatientSession).options(*_SESSION_LOADS).where(PatientSession.id == session_id)
    ).unique().scalar_one_or_none()
    if session is None:
        raise NotFoundError(f"Session {session_id} was not found")
    permissions.assert_clinic_access(db, user, session.clinic_id)
    return session


def list_sessions(
    db: Session,
    user: User,
    *,
    patient_id: int | None = None,
    clinic_id: int | None = None,
    therapist_user_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    include_voided: bool = False,
    search: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[PatientSession], int]:
    stmt = select(PatientSession).options(*_SESSION_LOADS)

    accessible = permissions.accessible_clinic_ids(db, user)
    if accessible is not None:
        stmt = stmt.where(PatientSession.clinic_id.in_(accessible or [-1]))
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        stmt = stmt.where(PatientSession.clinic_id == clinic_id)
    if patient_id is not None:
        patient_service.get_patient(db, user, patient_id)
        stmt = stmt.where(PatientSession.patient_id == patient_id)
    if therapist_user_id is not None:
        stmt = stmt.where(PatientSession.therapist_user_id == therapist_user_id)
    if date_from is not None:
        stmt = stmt.where(PatientSession.session_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(PatientSession.session_date <= date_to)
    if not include_voided:
        stmt = stmt.where(PatientSession.is_voided.is_(False))
    if search:
        pattern = f"%{search.strip().lower()}%"
        stmt = stmt.join(Patient, PatientSession.patient_id == Patient.id).where(
            or_(
                func.lower(Patient.full_name).like(pattern),
                func.lower(Patient.patient_code).like(pattern),
                Patient.mobile.like(f"%{search.strip()}%"),
            )
        )

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(
                PatientSession.session_date.desc(), PatientSession.id.desc()
            )
            .offset(offset)
            .limit(limit)
        )
        .unique()
        .scalars()
        .all()
    )
    return list(rows), total


def session_counters(db: Session, user: User, clinic_id: int | None = None) -> dict:
    """Tallies for the sessions screen."""
    today = date.today()

    def count(*conditions) -> int:
        stmt = select(func.count()).select_from(PatientSession).where(
            PatientSession.is_voided.is_(False), *conditions
        )
        accessible = permissions.accessible_clinic_ids(db, user)
        if accessible is not None:
            stmt = stmt.where(PatientSession.clinic_id.in_(accessible or [-1]))
        if clinic_id is not None:
            stmt = stmt.where(PatientSession.clinic_id == clinic_id)
        return db.execute(stmt).scalar_one()

    return {
        "today": count(PatientSession.session_date == today),
        "this_month": count(PatientSession.session_date >= today.replace(day=1)),
        "total": count(),
    }
