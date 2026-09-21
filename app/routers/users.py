"""User management endpoints (Superadmin only)."""

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.auth.dependencies import DbSession, SuperadminUser
from app.models.enums import RoleName
from app.schemas.common import Page
from app.schemas.user import (
    PasswordResetRequest,
    RoleRead,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.services import user_service

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("", response_model=Page[UserRead], summary="List users")
def list_users(
    db: DbSession,
    current_user: SuperadminUser,
    search: Annotated[str | None, Query(max_length=100)] = None,
    role: RoleName | None = None,
    clinic_id: int | None = None,
    is_active: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
):
    users, total = user_service.list_users(
        db,
        search=search,
        role=role,
        clinic_id=clinic_id,
        is_active=is_active,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[UserRead](
        items=[UserRead.model_validate(user) for user in users],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an Admin or Clinic User",
)
def create_user(
    payload: UserCreate, request: Request, db: DbSession, current_user: SuperadminUser
):
    user = user_service.create_user(db, payload, current_user, request=request)
    return UserRead.model_validate(user)


@router.get("/{user_id}", response_model=UserRead, summary="Get one user")
def get_user(user_id: int, db: DbSession, current_user: SuperadminUser):
    return UserRead.model_validate(user_service.get_user(db, user_id))


@router.put("/{user_id}", response_model=UserRead, summary="Edit a user")
def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    user = user_service.update_user(db, user_id, payload, current_user, request=request)
    return UserRead.model_validate(user)


@router.post("/{user_id}/enable", response_model=UserRead, summary="Enable a user")
def enable_user(user_id: int, request: Request, db: DbSession, current_user: SuperadminUser):
    user = user_service.set_active(db, user_id, True, current_user, request=request)
    return UserRead.model_validate(user)


@router.post("/{user_id}/disable", response_model=UserRead, summary="Disable a user")
def disable_user(user_id: int, request: Request, db: DbSession, current_user: SuperadminUser):
    user = user_service.set_active(db, user_id, False, current_user, request=request)
    return UserRead.model_validate(user)


@router.post(
    "/{user_id}/reset-password",
    response_model=UserRead,
    summary="Set a new password for a user",
)
def reset_password(
    user_id: int,
    payload: PasswordResetRequest,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    user = user_service.reset_password(
        db, user_id, payload.new_password, current_user, request=request
    )
    return UserRead.model_validate(user)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an unused account",
)
def delete_user(
    user_id: int,
    request: Request,
    db: DbSession,
    current_user: SuperadminUser,
):
    """For an account created by mistake and never used.

    Refused with a 400 once the person has treated a patient or handled money:
    those foreign keys are `ON DELETE SET NULL`, so removing them would leave a
    session with no therapist and a payment nobody received. **Disable** is the
    right action there — it blocks sign-in at once and keeps the record whole.
    """
    user_service.delete_user(db, user_id, current_user, request=request)


roles_router = APIRouter(prefix="/roles", tags=["Users"])


@roles_router.get("", response_model=list[RoleRead], summary="List assignable roles")
def list_roles(db: DbSession, current_user: SuperadminUser):
    from sqlalchemy import select

    from app.models import Role

    roles = db.execute(select(Role).order_by(Role.id)).scalars().all()
    return [RoleRead.model_validate(role) for role in roles]
