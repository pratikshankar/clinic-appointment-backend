"""Central role/clinic permission rules (Sections 23 and 38).

Every rule lives here rather than being scattered through routers, so the
permission model can be read in one place and tested directly. Routers and
services consume it through the dependencies in `app/auth/dependencies.py`.
"""

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Clinic, ClinicStatus, RoleName, User
from app.utils.exceptions import ClinicAccessDeniedError, PermissionDeniedError

#: Roles allowed to manage users and clinics (system administration).
SYSTEM_ADMIN_ROLES: tuple[RoleName, ...] = (RoleName.SUPERADMIN,)

#: Roles that implicitly reach every clinic.
ALL_CLINIC_ROLES: tuple[RoleName, ...] = (RoleName.SUPERADMIN, RoleName.ADMIN)


def is_superadmin(user: User) -> bool:
    return user.role_name == RoleName.SUPERADMIN


def is_admin(user: User) -> bool:
    return user.role_name == RoleName.ADMIN


def is_clinic_user(user: User) -> bool:
    return user.role_name == RoleName.CLINIC_USER


def has_all_clinic_access(user: User) -> bool:
    return user.role_name in ALL_CLINIC_ROLES


def require_roles(user: User, roles: Iterable[RoleName]) -> None:
    """Raise unless `user` holds one of `roles`."""
    allowed = tuple(roles)
    if user.role_name not in allowed:
        raise PermissionDeniedError(
            f"This action requires one of: {', '.join(role.value for role in allowed)}"
        )


def accessible_clinic_ids(db: Session, user: User) -> list[int] | None:
    """Clinic IDs the user may read.

    Returns ``None`` for Superadmin/Admin to mean "no restriction" -- callers
    should skip the WHERE clause entirely rather than loading every ID. A Clinic
    User gets exactly their assigned clinics, which may be an empty list if the
    Superadmin has not assigned them yet.
    """
    if has_all_clinic_access(user):
        return None
    return list(user.clinic_ids)


def assert_clinic_access(db: Session, user: User, clinic_id: int) -> Clinic:
    """Verify the user may act on `clinic_id` and return the clinic.

    This is the single choke point for clinic scoping. It runs on every request
    that names a clinic, so hiding a selector in the frontend is never what
    enforces the boundary (Section 23).
    """
    clinic = db.get(Clinic, clinic_id)
    if clinic is None:
        from app.utils.exceptions import NotFoundError

        raise NotFoundError(f"Clinic {clinic_id} was not found")

    if not has_all_clinic_access(user) and clinic_id not in user.clinic_ids:
        # Deliberately the same message whether the clinic exists or not, so the
        # endpoint cannot be used to enumerate clinics.
        raise ClinicAccessDeniedError()

    return clinic


def assert_active_clinic(clinic: Clinic) -> Clinic:
    """Operational actions (booking, billing) require an active clinic."""
    if clinic.status != ClinicStatus.ACTIVE:
        raise PermissionDeniedError(f"Clinic '{clinic.name}' is currently inactive")
    return clinic


def visible_clinics(db: Session, user: User) -> Sequence[Clinic]:
    """Clinics the user is allowed to see, ordered by name."""
    stmt = select(Clinic).order_by(Clinic.name)
    clinic_ids = accessible_clinic_ids(db, user)
    if clinic_ids is not None:
        if not clinic_ids:
            return []
        stmt = stmt.where(Clinic.id.in_(clinic_ids))
    return db.execute(stmt).unique().scalars().all()


def can_manage_users(user: User) -> bool:
    return user.role_name in SYSTEM_ADMIN_ROLES


def can_manage_clinics(user: User) -> bool:
    """Only the Superadmin creates/edits clinics (Section 6)."""
    return user.role_name in SYSTEM_ADMIN_ROLES


def assignable_roles(user: User) -> tuple[RoleName, ...]:
    """Roles `user` is permitted to grant when creating another user."""
    if is_superadmin(user):
        return (RoleName.ADMIN, RoleName.CLINIC_USER)
    return ()
