"""User management: create, edit, enable/disable, clinic assignment (Section 4.1)."""

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import permissions
from app.auth.security import hash_password
from app.models import AuditAction, Clinic, ClinicUser, Role, RoleName, User
from app.schemas.user import UserCreate, UserUpdate
from app.services import audit_service
from app.utils.exceptions import (
    DuplicateResourceError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)


def get_role(db: Session, name: RoleName) -> Role:
    role = db.execute(select(Role).where(Role.name == name)).scalar_one_or_none()
    if role is None:
        raise NotFoundError(f"Role {name.value} is not configured. Run the seed script.")
    return role


def get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found")
    return user


def list_users(
    db: Session,
    *,
    search: str | None = None,
    role: RoleName | None = None,
    clinic_id: int | None = None,
    is_active: bool | None = None,
    offset: int = 0,
    limit: int = 25,
) -> tuple[list[User], int]:
    stmt = select(User).join(Role, User.role_id == Role.id)

    if search:
        pattern = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(User.username).like(pattern),
                func.lower(User.full_name).like(pattern),
                func.lower(func.coalesce(User.email, "")).like(pattern),
            )
        )
    if role is not None:
        stmt = stmt.where(Role.name == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    if clinic_id is not None:
        stmt = stmt.where(User.id.in_(select(ClinicUser.user_id).where(ClinicUser.clinic_id == clinic_id)))

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = (
        db.execute(stmt.order_by(User.full_name).offset(offset).limit(limit))
        .unique()
        .scalars()
        .all()
    )
    return list(rows), total


def _assert_unique(
    db: Session,
    username: str,
    email: str | None,
    exclude_id: int | None = None,
    employee_id: str | None = None,
):
    stmt = select(User).where(func.lower(User.username) == username.lower())
    if exclude_id:
        stmt = stmt.where(User.id != exclude_id)
    if db.execute(stmt).scalar_one_or_none():
        raise DuplicateResourceError(f"Username '{username}' is already taken")

    if email:
        stmt = select(User).where(func.lower(User.email) == email.lower())
        if exclude_id:
            stmt = stmt.where(User.id != exclude_id)
        if db.execute(stmt).scalar_one_or_none():
            raise DuplicateResourceError(f"Email '{email}' is already registered")

    if employee_id:
        stmt = select(User).where(func.lower(User.employee_id) == employee_id.lower())
        if exclude_id:
            stmt = stmt.where(User.id != exclude_id)
        if db.execute(stmt).scalar_one_or_none():
            raise DuplicateResourceError(
                f"Employee ID '{employee_id}' belongs to another user"
            )


def _validate_clinics(db: Session, clinic_ids: list[int]) -> list[Clinic]:
    if not clinic_ids:
        return []
    unique_ids = list(dict.fromkeys(clinic_ids))
    clinics = db.execute(select(Clinic).where(Clinic.id.in_(unique_ids))).unique().scalars().all()
    found = {clinic.id for clinic in clinics}
    missing = [cid for cid in unique_ids if cid not in found]
    if missing:
        raise ValidationError(f"Unknown clinic id(s): {', '.join(map(str, missing))}")
    return list(clinics)


def create_user(
    db: Session, payload: UserCreate, actor: User, request: Request | None = None
) -> User:
    allowed_roles = permissions.assignable_roles(actor)
    if payload.role not in allowed_roles:
        raise PermissionDeniedError(
            f"You cannot create users with the {payload.role.value} role"
        )

    _assert_unique(db, payload.username, payload.email, employee_id=payload.employee_id)
    clinics = _validate_clinics(db, payload.clinic_ids)
    role = get_role(db, payload.role)

    user = User(
        username=payload.username.strip(),
        email=str(payload.email) if payload.email else None,
        full_name=payload.full_name,
        phone=payload.phone,
        employee_id=payload.employee_id,
        registration_number=payload.registration_number,
        hashed_password=hash_password(payload.password),
        role_id=role.id,
        is_active=True,
    )
    db.add(user)
    db.flush()  # assign user.id before creating the assignment rows

    for index, clinic in enumerate(clinics):
        db.add(
            ClinicUser(
                user_id=user.id,
                clinic_id=clinic.id,
                is_primary=(index == 0),
                designation=payload.designation,
                assigned_by_user_id=actor.id,
            )
        )

    audit_service.record(
        db,
        action=AuditAction.CREATED,
        user=actor,
        entity_type="user",
        entity_id=user.id,
        description=f"Created {payload.role.value} '{user.username}'",
        details={
            "role": payload.role.value,
            "clinic_ids": [clinic.id for clinic in clinics],
        },
        request=request,
    )
    db.commit()
    db.refresh(user)
    return user


def update_user(
    db: Session, user_id: int, payload: UserUpdate, actor: User, request: Request | None = None
) -> User:
    user = get_user(db, user_id)

    if user.role_name == RoleName.SUPERADMIN and user.id != actor.id:
        raise PermissionDeniedError("Another Superadmin's account cannot be modified")

    data = payload.model_dump(exclude_unset=True)
    clinic_ids = data.pop("clinic_ids", None)
    designation = data.pop("designation", None)

    if "email" in data and data["email"]:
        _assert_unique(db, user.username, str(data["email"]), exclude_id=user.id)
        data["email"] = str(data["email"])

    if data.get("employee_id"):
        _assert_unique(
            db, user.username, None, exclude_id=user.id, employee_id=data["employee_id"]
        )

    if data.get("is_active") is False and user.id == actor.id:
        raise ValidationError("You cannot disable your own account")

    changed: dict[str, object] = {}
    for field, value in data.items():
        if getattr(user, field) != value:
            changed[field] = value
            setattr(user, field, value)

    if clinic_ids is not None:
        if user.role_name != RoleName.CLINIC_USER:
            raise ValidationError(
                f"{user.role_name.value} users cannot be assigned to specific clinics"
            )
        if not clinic_ids:
            raise ValidationError("A Clinic User must remain assigned to at least one clinic")
        clinics = _validate_clinics(db, clinic_ids)
        _replace_clinic_assignments(db, user, clinics, designation, actor)
        changed["clinic_ids"] = [clinic.id for clinic in clinics]
    elif designation is not None:
        for link in user.clinic_links:
            link.designation = designation

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=actor,
        entity_type="user",
        entity_id=user.id,
        description=f"Updated user '{user.username}'",
        details={"changed_fields": changed} if changed else None,
        request=request,
    )
    db.commit()
    db.refresh(user)
    return user


def _replace_clinic_assignments(
    db: Session,
    user: User,
    clinics: list[Clinic],
    designation: str | None,
    actor: User,
) -> None:
    """Reconcile assignments in place so existing rows keep their identity."""
    target_ids = [clinic.id for clinic in clinics]
    existing = {link.clinic_id: link for link in user.clinic_links}

    for clinic_id, link in list(existing.items()):
        if clinic_id not in target_ids:
            db.delete(link)
            user.clinic_links.remove(link)

    for index, clinic_id in enumerate(target_ids):
        link = existing.get(clinic_id)
        if link is None:
            db.add(
                ClinicUser(
                    user_id=user.id,
                    clinic_id=clinic_id,
                    is_primary=(index == 0),
                    designation=designation,
                    assigned_by_user_id=actor.id,
                )
            )
        else:
            link.is_primary = index == 0
            if designation is not None:
                link.designation = designation


def set_active(
    db: Session, user_id: int, is_active: bool, actor: User, request: Request | None = None
) -> User:
    user = get_user(db, user_id)
    if user.id == actor.id:
        raise ValidationError("You cannot change your own account status")
    if user.role_name == RoleName.SUPERADMIN:
        raise PermissionDeniedError("A Superadmin account cannot be disabled")

    user.is_active = is_active
    audit_service.record(
        db,
        action=AuditAction.ENABLED if is_active else AuditAction.DISABLED,
        user=actor,
        entity_type="user",
        entity_id=user.id,
        description=f"{'Enabled' if is_active else 'Disabled'} user '{user.username}'",
        request=request,
    )
    db.commit()
    db.refresh(user)
    return user


def reset_password(
    db: Session, user_id: int, new_password: str, actor: User, request: Request | None = None
) -> User:
    user = get_user(db, user_id)
    if user.role_name == RoleName.SUPERADMIN and user.id != actor.id:
        raise PermissionDeniedError("Another Superadmin's password cannot be reset here")

    user.hashed_password = hash_password(new_password)
    audit_service.record(
        db,
        action=AuditAction.PASSWORD_CHANGED,
        user=actor,
        entity_type="user",
        entity_id=user.id,
        description=f"Reset password for '{user.username}'",
        request=request,
    )
    db.commit()
    db.refresh(user)
    return user


#: Columns that mean "this person did clinical or financial work". Deleting such
#: a user would `SET NULL` these, erasing who treated a patient or who took a
#: payment -- so deletion is refused and disabling offered instead.
#:
#: Audit and login rows are deliberately *not* in this list: `audit_logs` keeps
#: the username as free text precisely so the trail survives a deleted account,
#: and counting them would make every user who has ever signed in undeletable,
#: which defeats the purpose of having a delete at all.
def _activity_counts(db: Session, user_id: int) -> dict[str, int]:
    from app.models import (
        Appointment,
        Bill,
        Patient,
        PatientSession,
        Payment,
        TreatmentPackage,
    )

    checks = {
        "session(s) delivered": (PatientSession, PatientSession.therapist_user_id),
        "session(s) voided": (PatientSession, PatientSession.voided_by_user_id),
        "appointment(s) booked": (Appointment, Appointment.created_by_user_id),
        "patient(s) registered": (Patient, Patient.created_by_user_id),
        "package(s) registered": (TreatmentPackage, TreatmentPackage.created_by_user_id),
        "bill(s) raised": (Bill, Bill.created_by_user_id),
        "payment(s) received": (Payment, Payment.received_by_user_id),
    }
    found: dict[str, int] = {}
    for label, (model, column) in checks.items():
        count = db.execute(
            select(func.count()).select_from(model).where(column == user_id)
        ).scalar_one()
        if count:
            found[label] = count
    return found


def delete_user(
    db: Session, user_id: int, actor: User, request: Request | None = None
) -> None:
    """Remove a user account outright.

    Only for an account that has done nothing -- created by mistake, wrong
    details, never used. Once someone has treated a patient or taken a payment,
    deleting them would blank the actor out of those records (every one of those
    foreign keys is `ON DELETE SET NULL`), leaving a session with no therapist
    and a payment nobody received. That is unacceptable in clinical and
    financial history, so it is refused with an explanation pointing at Disable,
    which is the reversible action that actually fits.
    """
    user = get_user(db, user_id)

    if user.id == actor.id:
        raise ValidationError("You cannot delete your own account")
    if user.role_name == RoleName.SUPERADMIN:
        raise PermissionDeniedError("A Superadmin account cannot be deleted")

    activity = _activity_counts(db, user.id)
    if activity:
        summary = ", ".join(f"{count} {label}" for label, count in activity.items())
        raise ValidationError(
            f"{user.full_name} has history on file ({summary}) and cannot be deleted "
            "without erasing who did that work. Disable the account instead — it "
            "blocks sign-in immediately and keeps the record intact."
        )

    # Recorded before the row goes, and `username` is stored as free text on the
    # audit entry, so the trail survives the account it describes.
    audit_service.record(
        db,
        action=AuditAction.DELETED,
        user=actor,
        entity_type="user",
        entity_id=user.id,
        description=(
            f"Deleted unused account '{user.username}' ({user.full_name}, "
            f"{user.role_name.value})"
        ),
        request=request,
    )
    db.delete(user)
    db.commit()
