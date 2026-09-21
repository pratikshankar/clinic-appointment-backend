"""Authentication business logic (Section 24)."""

import logging
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.security import (
    TokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.config import settings
from app.models import AuditAction, User
from app.services import audit_service
from app.auth.rate_limit import client_ip, login_limiter
from app.utils.exceptions import (
    AuthenticationError,
    InactiveUserError,
    TooManyAttemptsError,
    ValidationError,
)

logger = logging.getLogger(__name__)


def authenticate(
    db: Session, username: str, password: str, request: Request | None = None
) -> User:
    """Verify credentials and return the user.

    Failures deliberately share one message so the endpoint cannot be used to
    discover which usernames exist.
    """
    identifier = username.strip()
    ip = client_ip(request)

    # Checked before the password is even verified: the point is to stop the
    # guessing loop, and Argon2 verification is the expensive part.
    wait = login_limiter.retry_after(identifier, ip)
    if wait:
        audit_service.record(
            db,
            action=AuditAction.LOGIN_FAILED,
            username=identifier,
            description=f"Login blocked by rate limit for '{identifier}'",
            request=request,
        )
        db.commit()
        raise TooManyAttemptsError(
            f"Too many failed sign-in attempts. Try again in {wait} second(s)."
        )

    user = db.execute(
        select(User).where(func.lower(User.username) == identifier.lower())
    ).scalar_one_or_none()

    if user is None or not verify_password(password, user.hashed_password):
        login_limiter.record_failure(identifier, ip)
        audit_service.record(
            db,
            action=AuditAction.LOGIN_FAILED,
            username=identifier,
            entity_type="user",
            entity_id=user.id if user else None,
            description=f"Failed login attempt for '{identifier}'",
            request=request,
        )
        db.commit()
        raise AuthenticationError()

    if not user.is_active:
        audit_service.record(
            db,
            action=AuditAction.LOGIN_FAILED,
            user=user,
            entity_type="user",
            entity_id=user.id,
            description="Login attempt on a disabled account",
            request=request,
        )
        db.commit()
        raise InactiveUserError()

    # Transparently upgrade the stored hash if Argon2 parameters have changed.
    if password_needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(password)

    # A correct password clears the failure count for this identity.
    login_limiter.record_success(identifier, ip)
    user.last_login_at = datetime.now(timezone.utc)
    audit_service.record(
        db,
        action=AuditAction.LOGIN,
        user=user,
        entity_type="user",
        entity_id=user.id,
        description=f"{user.username} signed in",
        request=request,
    )
    db.commit()
    db.refresh(user)
    return user


def issue_tokens(user: User) -> dict:
    return {
        "access_token": create_access_token(user.id, user.role_name.value),
        "refresh_token": create_refresh_token(user.id),
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


def refresh_tokens(db: Session, refresh_token: str) -> tuple[User, dict]:
    """Exchange a valid refresh token for a new token pair."""
    try:
        payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise AuthenticationError("User no longer exists")
    if not user.is_active:
        raise InactiveUserError()
    return user, issue_tokens(user)


def change_password(
    db: Session,
    user: User,
    current_password: str,
    new_password: str,
    request: Request | None = None,
) -> None:
    if not verify_password(current_password, user.hashed_password):
        raise AuthenticationError("Your current password is incorrect")
    if verify_password(new_password, user.hashed_password):
        raise ValidationError("The new password must be different from the current one")

    user.hashed_password = hash_password(new_password)
    audit_service.record(
        db,
        action=AuditAction.PASSWORD_CHANGED,
        user=user,
        entity_type="user",
        entity_id=user.id,
        description=f"{user.username} changed their password",
        request=request,
    )
    db.commit()


def log_logout(db: Session, user: User, request: Request | None = None) -> None:
    """Record the logout.

    Stateless JWTs cannot be invalidated server-side without a revocation
    store; the access token remains technically valid until it expires, which
    is why the lifetime is kept short. The README documents adding a token
    denylist if immediate revocation becomes a requirement.
    """
    audit_service.record(
        db,
        action=AuditAction.LOGOUT,
        user=user,
        entity_type="user",
        entity_id=user.id,
        description=f"{user.username} signed out",
        request=request,
    )
    db.commit()
