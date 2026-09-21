"""Password hashing and JWT encoding/decoding.

Passwords use Argon2id (the current OWASP recommendation) via argon2-cffi.
Unlike bcrypt it has no 72-byte truncation quirk, and `needs_rehash` lets
parameters be raised later without forcing a password reset.
"""

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import settings

_hasher = PasswordHasher()


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(plain_password: str) -> str:
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Constant-time-ish verification that never raises on bad input."""
    try:
        return _hasher.verify(hashed_password, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(hashed_password: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed_password)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or of the wrong type."""


def _create_token(
    subject: str,
    token_type: TokenType,
    expires_minutes: int,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type.value,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: int, role: str, extra_claims: dict[str, Any] | None = None) -> str:
    claims = {"role": role}
    if extra_claims:
        claims.update(extra_claims)
    return _create_token(
        subject=str(user_id),
        token_type=TokenType.ACCESS,
        expires_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
        extra_claims=claims,
    )


def create_refresh_token(user_id: int) -> str:
    return _create_token(
        subject=str(user_id),
        token_type=TokenType.REFRESH,
        expires_minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES,
    )


def decode_token(token: str, expected_type: TokenType | None = None) -> dict[str, Any]:
    """Decode and validate a token, raising `TokenError` on any problem.

    The role claim is *not* trusted for authorisation -- permissions are always
    re-read from the database (see `app/auth/dependencies.py`), so revoking a
    role or disabling a user takes effect immediately rather than when the
    token expires.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("Could not validate credentials") from exc

    if expected_type is not None and payload.get("type") != expected_type.value:
        raise TokenError(f"Expected a {expected_type.value} token")
    if not payload.get("sub"):
        raise TokenError("Token is missing a subject")
    return payload
