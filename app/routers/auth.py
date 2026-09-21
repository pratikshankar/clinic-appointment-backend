"""Authentication endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.auth.dependencies import CurrentUser, DbSession
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    TokenPair,
)
from app.schemas.common import Message
from app.schemas.user import UserRead
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=LoginResponse, summary="Sign in with username and password")
def login(payload: LoginRequest, request: Request, db: DbSession):
    user = auth_service.authenticate(db, payload.username, payload.password, request=request)
    tokens = auth_service.issue_tokens(user)
    return LoginResponse(**tokens, user=UserRead.model_validate(user))


@router.post(
    "/token",
    response_model=TokenPair,
    include_in_schema=True,
    summary="OAuth2 form login (powers the Authorize button in /docs)",
)
def login_form(
    request: Request,
    db: DbSession,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
):
    user = auth_service.authenticate(db, form_data.username, form_data.password, request=request)
    return TokenPair(**auth_service.issue_tokens(user))


@router.post("/refresh", response_model=TokenPair, summary="Exchange a refresh token")
def refresh(payload: RefreshRequest, db: DbSession):
    _, tokens = auth_service.refresh_tokens(db, payload.refresh_token)
    return TokenPair(**tokens)


@router.get("/me", response_model=UserRead, summary="Current user profile and permissions")
def me(current_user: CurrentUser):
    return UserRead.model_validate(current_user)


@router.post("/change-password", response_model=Message, summary="Change your own password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
):
    auth_service.change_password(
        db,
        current_user,
        payload.current_password,
        payload.new_password,
        request=request,
    )
    return Message(message="Password updated successfully")


@router.post("/logout", response_model=Message, status_code=status.HTTP_200_OK)
def logout(request: Request, db: DbSession, current_user: CurrentUser):
    """Record the logout.

    The client must discard its tokens; access tokens are stateless and stay
    valid until they expire (see `auth_service.log_logout`).
    """
    auth_service.log_logout(db, current_user, request=request)
    return Message(message="Signed out successfully")
