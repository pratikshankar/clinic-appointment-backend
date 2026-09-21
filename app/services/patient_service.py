"""Patient management (Sections 7, 11, 20, 21, 38).

Two rules shape this module.

**One permanent Patient ID (Section 38).** A patient is a chain-wide identity,
not a per-clinic record. `patient_code` is generated once and never changes.

**Access is asymmetric, on purpose.** Section 9 scopes a Clinic User to their own
clinic, but Section 38 forbids duplicate patients and Section 20 wants global
search -- and strict scoping guarantees duplicates the moment a patient visits a
second clinic. The resolution:

* *Browsing* (`list_patients`) is clinic-scoped.
* *Exact lookup* by Patient ID or full mobile reaches the whole chain but returns
  only an identity card -- enough to link an existing patient, nothing clinical.
* *Full detail* opens up when the patient belongs to the caller's clinic **or has
  activity there** (an appointment, session or bill). That second condition is
  what Phase 4 needs when one clinic books a patient registered at another.
"""

from datetime import date
from decimal import Decimal

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import permissions
from app.models import (
    Appointment,
    AuditAction,
    Bill,
    BillStatus,
    Clinic,
    Patient,
    PatientSession,
    PatientSource,
    RoleName,
    TreatmentPackage,
    User,
)
from app.schemas.patient import (
    DuplicateCheckRequest,
    PatientCreate,
    PatientQuickCreate,
    PatientUpdate,
)
from app.services import audit_service
from app.utils.exceptions import (
    ClinicAccessDeniedError,
    DuplicateResourceError,
    NotFoundError,
    ValidationError,
)
from app.utils.identifiers import generate_patient_code

#: Loader options so a patient read never triggers N+1 queries.
_PATIENT_LOADS = (selectinload(Patient.source), selectinload(Patient.primary_clinic))


def _normalized_name(value: str) -> str:
    """Lower-cased, whitespace-collapsed name, for duplicate comparison."""
    return " ".join((value or "").split()).lower()


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def list_patients(
    db: Session,
    user: User,
    *,
    search: str | None = None,
    clinic_id: int | None = None,
    source_id: int | None = None,
    is_active: bool | None = True,
    profile_complete: bool | None = None,
    has_active_package: bool | None = None,
    offset: int = 0,
    limit: int = 25,
) -> tuple[list[Patient], int]:
    """Patients the caller may browse.

    Clinic-scoped for a Clinic User. `search` here is a convenience filter over
    the visible set -- it is *not* the chain-wide lookup, which is `lookup()`.
    """
    stmt = select(Patient).options(*_PATIENT_LOADS)

    accessible = permissions.accessible_clinic_ids(db, user)
    if accessible is not None:
        # Match nothing rather than everything when unassigned.
        stmt = stmt.where(Patient.primary_clinic_id.in_(accessible or [-1]))
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        stmt = stmt.where(Patient.primary_clinic_id == clinic_id)

    if search:
        term = search.strip()
        pattern = f"%{term.lower()}%"
        conditions = [
            func.lower(Patient.full_name).like(pattern),
            Patient.mobile.like(f"%{term}%"),
            func.lower(Patient.patient_code).like(pattern),
        ]
        stmt = stmt.where(or_(*conditions))

    if source_id is not None:
        stmt = stmt.where(Patient.source_id == source_id)
    if is_active is not None:
        stmt = stmt.where(Patient.is_active.is_(is_active))
    if profile_complete is not None:
        stmt = stmt.where(Patient.is_profile_complete.is_(profile_complete))
    if has_active_package is not None:
        # "Being treated with nothing purchased" is the gap worth finding, so
        # this filter exists to surface it rather than to hide it.
        from app.models import PackageStatus, TreatmentPackage

        owns = (
            select(TreatmentPackage.id)
            .where(
                TreatmentPackage.patient_id == Patient.id,
                TreatmentPackage.status == PackageStatus.ACTIVE,
                TreatmentPackage.sessions_taken < TreatmentPackage.sessions_registered,
            )
            .exists()
        )
        stmt = stmt.where(owns if has_active_package else ~owns)

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = (
        db.execute(stmt.order_by(Patient.full_name).offset(offset).limit(limit))
        .unique()
        .scalars()
        .all()
    )
    return list(rows), total


def patients_with_active_packages(db: Session, patient_ids: list[int]) -> set[int]:
    """Which of these patients hold a package with sessions left.

    One query for the whole page: computing it per row would be an N+1 on the
    patient list, which is the screen most likely to be large.
    """
    if not patient_ids:
        return set()

    from app.models import PackageStatus, TreatmentPackage

    rows = db.execute(
        select(TreatmentPackage.patient_id)
        .where(
            TreatmentPackage.patient_id.in_(patient_ids),
            TreatmentPackage.status == PackageStatus.ACTIVE,
            TreatmentPackage.sessions_taken < TreatmentPackage.sessions_registered,
        )
        .distinct()
    ).scalars()
    return set(rows)


def patient_clinic_ids(db: Session, patient_id: int) -> set[int]:
    """Every clinic where this patient has activity, plus their home clinic."""
    clinic_ids: set[int] = set()

    home = db.execute(
        select(Patient.primary_clinic_id).where(Patient.id == patient_id)
    ).scalar_one_or_none()
    if home:
        clinic_ids.add(home)

    for column, model in (
        (Appointment.clinic_id, Appointment),
        (PatientSession.clinic_id, PatientSession),
        (Bill.clinic_id, Bill),
    ):
        rows = db.execute(
            select(column).where(model.patient_id == patient_id).distinct()
        ).scalars()
        clinic_ids.update(value for value in rows if value)

    return clinic_ids


def can_view_full_record(db: Session, user: User, patient: Patient) -> bool:
    if permissions.has_all_clinic_access(user):
        return True
    mine = set(user.clinic_ids)
    if not mine:
        return False
    return bool(mine & patient_clinic_ids(db, patient.id))


def get_patient(db: Session, user: User, patient_id: int) -> Patient:
    """Fetch a full patient record, enforcing the access rule above."""
    patient = db.execute(
        select(Patient).options(*_PATIENT_LOADS).where(Patient.id == patient_id)
    ).unique().scalar_one_or_none()
    if patient is None:
        raise NotFoundError(f"Patient {patient_id} was not found")

    if not can_view_full_record(db, user, patient):
        raise ClinicAccessDeniedError(
            "This patient belongs to another clinic. Look them up by Patient ID or "
            "mobile number to link them to an appointment at your clinic."
        )
    return patient


#: Shortest name fragment accepted by a chain-wide lookup. Two characters would
#: match a large slice of the patient book, which is browsing, not looking up.
MIN_LOOKUP_NAME_LENGTH = 3

#: Ceiling on chain-wide results. A genuine "is this person already registered?"
#: question is answered by a handful of rows; a hundred means the operator is
#: fishing, and the response says as much.
MAX_LOOKUP_RESULTS = 25


def lookup(
    db: Session,
    user: User,
    *,
    patient_code: str | None = None,
    mobile: str | None = None,
    name: str | None = None,
) -> list[Patient]:
    """Chain-wide lookup (Section 20).

    Crosses clinic boundaries, so it returns identity fields only (see
    `PatientCard`) and never clinical data. Three ways in:

    * `patient_code` -- exact match.
    * `mobile` -- exact, full 10 digits.
    * `name` -- partial match, minimum `MIN_LOOKUP_NAME_LENGTH` characters and
      capped at `MAX_LOOKUP_RESULTS`. Reception usually knows the caller's name
      and not their Patient ID, so refusing this just pushes them into creating a
      duplicate, which is the outcome Section 38 exists to prevent.

    Browsing stays scoped in `list_patients`; this is the explicit "look further"
    step, not the default listing.
    """
    if not patient_code and not mobile and not name:
        raise ValidationError(
            "Provide a Patient ID, a mobile number, or at least "
            f"{MIN_LOOKUP_NAME_LENGTH} characters of the patient's name"
        )

    stmt = select(Patient).options(*_PATIENT_LOADS)

    if patient_code:
        stmt = stmt.where(func.upper(Patient.patient_code) == patient_code.strip().upper())
    if mobile:
        digits = "".join(character for character in mobile if character.isdigit())[-10:]
        if len(digits) < 10:
            raise ValidationError("Enter the full 10-digit mobile number to look up")
        stmt = stmt.where(Patient.mobile == digits)
    if name:
        fragment = " ".join(name.split()).strip()
        if len(fragment) < MIN_LOOKUP_NAME_LENGTH:
            raise ValidationError(
                f"Enter at least {MIN_LOOKUP_NAME_LENGTH} characters of the name to "
                "search other clinics"
            )
        stmt = stmt.where(func.lower(Patient.full_name).like(f"%{fragment.lower()}%"))

    return list(
        db.execute(stmt.order_by(Patient.full_name).limit(MAX_LOOKUP_RESULTS))
        .unique()
        .scalars()
        .all()
    )


def find_duplicates(
    db: Session, payload: DuplicateCheckRequest, exclude_id: int | None = None
) -> dict:
    """Existing patients that might be the person being registered.

    Runs chain-wide regardless of the caller's clinic: a duplicate created at
    another branch is exactly what this is meant to prevent.
    """
    stmt = select(Patient).options(*_PATIENT_LOADS).where(Patient.mobile == payload.mobile)
    if exclude_id is not None:
        stmt = stmt.where(Patient.id != exclude_id)
    same_mobile = list(db.execute(stmt).unique().scalars().all())

    exact = None
    if payload.full_name:
        target = _normalized_name(payload.full_name)
        exact = next(
            (p for p in same_mobile if _normalized_name(p.full_name) == target), None
        )

    similar: list[Patient] = []
    if payload.full_name:
        name_stmt = (
            select(Patient)
            .options(*_PATIENT_LOADS)
            .where(
                func.lower(Patient.full_name) == _normalized_name(payload.full_name),
                Patient.mobile != payload.mobile,
            )
        )
        if exclude_id is not None:
            name_stmt = name_stmt.where(Patient.id != exclude_id)
        similar = list(db.execute(name_stmt).unique().scalars().all())

    return {
        "exact_match": exact,
        "same_mobile": [p for p in same_mobile if p is not exact],
        "similar_name": similar,
    }


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def _resolve_clinic(db: Session, user: User, clinic_id: int | None) -> int | None:
    """Validate the requested home clinic, defaulting sensibly per role."""
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        return clinic_id

    # A Clinic User's patients belong to their own clinic without asking.
    if user.role_name == RoleName.CLINIC_USER:
        primary = user.primary_clinic_id
        if primary is None:
            raise ValidationError(
                "Your account is not assigned to a clinic, so a patient cannot be registered"
            )
        return primary
    return None


def _validate_source(db: Session, source_id: int | None) -> None:
    if source_id is None:
        return
    source = db.get(PatientSource, source_id)
    if source is None:
        raise ValidationError(f"Unknown patient source id {source_id}")
    if not source.is_active:
        raise ValidationError(f"Patient source '{source.name}' is no longer active")


def _assert_not_duplicate(db: Session, full_name: str, mobile: str) -> None:
    """Refuse an exact (mobile + name) duplicate, naming the existing record.

    Mobile-only matches are allowed: families legitimately share a number, and
    `find_duplicates` has already surfaced them to the operator.
    """
    candidates = db.execute(select(Patient).where(Patient.mobile == mobile)).scalars().all()
    target = _normalized_name(full_name)
    for existing in candidates:
        if _normalized_name(existing.full_name) == target:
            raise DuplicateResourceError(
                f"{existing.full_name} ({existing.patient_code}) is already registered with "
                f"this mobile number. Use the existing patient instead of creating a new one.",
                details={
                    "existing_patient_id": existing.id,
                    "patient_code": existing.patient_code,
                    "full_name": existing.full_name,
                    "mobile": existing.mobile,
                },
            )


#: Fields that must be present before a profile counts as complete (Section 11).
REQUIRED_FOR_COMPLETE = ("gender", "address", "source_id")


def is_complete(**fields) -> bool:
    """Whether the supplied values make a profile complete.

    Completeness is always *derived* -- never taken from the request. Trusting a
    client-supplied flag is what let a half-filled record report itself as
    complete, which then hides it from the "needs completing" work queue.
    """
    return all(fields.get(name) is not None for name in REQUIRED_FOR_COMPLETE)


def create_patient(
    db: Session, payload: PatientCreate, actor: User, request: Request | None = None
) -> Patient:
    clinic_id = _resolve_clinic(db, actor, payload.primary_clinic_id)
    _validate_source(db, payload.source_id)
    _assert_not_duplicate(db, payload.full_name, payload.mobile)

    patient = Patient(
        patient_code=generate_patient_code(db),
        full_name=payload.full_name,
        mobile=payload.mobile,
        # Blank means "same as mobile"; the model's `whatsapp_contact` property
        # applies that fallback rather than duplicating the number here.
        whatsapp_number=payload.whatsapp_number,
        email=str(payload.email) if payload.email else None,
        # A known date of birth makes the stored age redundant, and a stored age
        # that disagrees with the DOB is worse than no age at all.
        age=None if payload.date_of_birth else payload.age,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        address=payload.address,
        chief_complaint=payload.chief_complaint,
        diagnosis=payload.diagnosis,
        source_id=payload.source_id,
        source_detail=payload.source_detail,
        registration_date=payload.registration_date or date.today(),
        primary_clinic_id=clinic_id,
        created_by_user_id=actor.id,
        is_profile_complete=is_complete(
            gender=payload.gender,
            address=payload.address,
            source_id=payload.source_id,
        ),
        is_active=True,
    )
    db.add(patient)
    db.flush()

    audit_service.record(
        db,
        action=AuditAction.CREATED,
        user=actor,
        entity_type="patient",
        entity_id=patient.id,
        clinic_id=clinic_id,
        description=f"Registered patient {patient.patient_code} ({patient.full_name})",
        details={"mobile": patient.mobile, "complete": patient.is_profile_complete},
        request=request,
    )
    db.commit()
    db.refresh(patient)
    return patient


def quick_create_patient(
    db: Session, payload: PatientQuickCreate, actor: User, request: Request | None = None
) -> Patient:
    """Create the minimal record an Admin needs while booking (Section 7).

    Flagged incomplete so the clinic can finish the profile when the patient
    physically arrives (Section 11).
    """
    # No gender/address/source are captured here, so the derived completeness
    # is False -- exactly what Section 11 wants the clinic to finish later.
    return create_patient(
        db,
        PatientCreate(
            full_name=payload.full_name,
            mobile=payload.mobile,
            whatsapp_number=payload.whatsapp_number,
            chief_complaint=payload.chief_complaint,
            primary_clinic_id=payload.primary_clinic_id,
        ),
        actor,
        request=request,
    )


def update_patient(
    db: Session,
    patient_id: int,
    payload: PatientUpdate,
    actor: User,
    request: Request | None = None,
) -> Patient:
    patient = get_patient(db, actor, patient_id)
    data = payload.model_dump(exclude_unset=True)

    if "primary_clinic_id" in data and data["primary_clinic_id"] is not None:
        permissions.assert_clinic_access(db, actor, data["primary_clinic_id"])
    if "source_id" in data:
        _validate_source(db, data["source_id"])

    # Changing identity fields must not create a duplicate either.
    new_name = data.get("full_name", patient.full_name)
    new_mobile = data.get("mobile", patient.mobile)
    if (new_name, new_mobile) != (patient.full_name, patient.mobile):
        candidates = (
            db.execute(
                select(Patient).where(Patient.mobile == new_mobile, Patient.id != patient.id)
            )
            .scalars()
            .all()
        )
        target = _normalized_name(new_name)
        for existing in candidates:
            if _normalized_name(existing.full_name) == target:
                raise DuplicateResourceError(
                    f"Those details already belong to {existing.full_name} "
                    f"({existing.patient_code})"
                )

    if "email" in data and data["email"]:
        data["email"] = str(data["email"])

    # A supplied date of birth supersedes any stored age.
    if data.get("date_of_birth"):
        data["age"] = None

    changed: dict[str, object] = {}
    for field, value in data.items():
        if getattr(patient, field) != value:
            changed[field] = str(value) if isinstance(value, date) else value
            setattr(patient, field, value)

    # Recomputed from the record itself, so filling the required fields promotes
    # it automatically and clearing one demotes it again. The flag can never
    # drift away from the data it describes.
    recomputed = is_complete(
        gender=patient.gender, address=patient.address, source_id=patient.source_id
    )
    if patient.is_profile_complete != recomputed:
        patient.is_profile_complete = recomputed
        changed["is_profile_complete"] = recomputed

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=actor,
        entity_type="patient",
        entity_id=patient.id,
        clinic_id=patient.primary_clinic_id,
        description=f"Updated patient {patient.patient_code}",
        details={"changed_fields": changed} if changed else None,
        request=request,
    )
    db.commit()
    db.refresh(patient)
    return patient


def set_active(
    db: Session,
    patient_id: int,
    is_active: bool,
    actor: User,
    request: Request | None = None,
) -> Patient:
    """Archive or restore a patient. Records are never deleted (Section 27)."""
    patient = get_patient(db, actor, patient_id)
    patient.is_active = is_active
    audit_service.record(
        db,
        action=AuditAction.ENABLED if is_active else AuditAction.DISABLED,
        user=actor,
        entity_type="patient",
        entity_id=patient.id,
        clinic_id=patient.primary_clinic_id,
        description=(
            f"{'Restored' if is_active else 'Archived'} patient {patient.patient_code}"
        ),
        request=request,
    )
    db.commit()
    db.refresh(patient)
    return patient


# --------------------------------------------------------------------------- #
# Profile timeline (Section 21)
# --------------------------------------------------------------------------- #
def build_profile(db: Session, user: User, patient_id: int) -> dict:
    """Packages, appointments, sessions and bills for one patient.

    Scoped for a Clinic User: they see this patient's history *at their own
    clinics*, not treatment given elsewhere in the chain.
    """
    patient = get_patient(db, user, patient_id)
    accessible = permissions.accessible_clinic_ids(db, user)
    scoped = accessible is not None

    def clinic_filter(column):
        return column.in_(accessible or [-1]) if scoped else None

    package_stmt = (
        select(TreatmentPackage)
        .options(selectinload(TreatmentPackage.clinic))
        .where(TreatmentPackage.patient_id == patient_id)
        .order_by(TreatmentPackage.start_date.desc().nullslast(), TreatmentPackage.id.desc())
    )
    appointment_stmt = (
        select(Appointment)
        .options(selectinload(Appointment.clinic))
        .where(Appointment.patient_id == patient_id)
        .order_by(Appointment.appointment_date.desc(), Appointment.start_time.desc())
    )
    session_stmt = (
        select(PatientSession)
        .options(selectinload(PatientSession.clinic), selectinload(PatientSession.therapist))
        .where(PatientSession.patient_id == patient_id)
        .order_by(PatientSession.session_date.desc(), PatientSession.session_number.desc())
    )
    bill_stmt = (
        select(Bill)
        .options(selectinload(Bill.clinic))
        .where(Bill.patient_id == patient_id, Bill.status != BillStatus.CANCELLED)
        .order_by(Bill.bill_date.desc(), Bill.id.desc())
    )

    if scoped:
        package_stmt = package_stmt.where(clinic_filter(TreatmentPackage.clinic_id))
        appointment_stmt = appointment_stmt.where(clinic_filter(Appointment.clinic_id))
        session_stmt = session_stmt.where(clinic_filter(PatientSession.clinic_id))
        bill_stmt = bill_stmt.where(clinic_filter(Bill.clinic_id))

    packages = list(db.execute(package_stmt).unique().scalars().all())
    appointments = list(db.execute(appointment_stmt).unique().scalars().all())
    sessions = list(db.execute(session_stmt).unique().scalars().all())
    bills = list(db.execute(bill_stmt).unique().scalars().all())

    billed = sum((bill.total_amount for bill in bills), Decimal("0.00"))
    paid = sum((bill.amount_paid for bill in bills), Decimal("0.00"))

    return {
        "patient": patient,
        "packages": packages,
        "appointments": appointments,
        "sessions": sessions,
        "bills": bills,
        # Cancelled packages stay in `packages` -- the row is history and must
        # not vanish -- but they are excluded from the headline numbers. Counting
        # them would tell reception a patient still holds sessions they gave up,
        # and it keeps the identity registered = taken + remaining true.
        "total_sessions_registered": sum(p.sessions_registered for p in packages if p.is_live),
        "total_sessions_taken": sum(p.sessions_taken for p in packages if p.is_live),
        "total_sessions_remaining": sum(p.sessions_remaining for p in packages if p.is_live),
        "cancelled_package_count": sum(1 for p in packages if not p.is_live),
        "total_billed": billed,
        "total_paid": paid,
        "total_outstanding": max(billed - paid, Decimal("0.00")),
        "scoped_to_your_clinics": scoped,
    }
