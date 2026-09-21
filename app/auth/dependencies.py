"""FastAPI dependencies for authentication and authorisation."""

from collections.abc import Iterable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.auth import permissions
from app.auth.security import TokenError, TokenType, decode_token
from app.config import settings
from app.db.database import get_db
from app.models import RoleName, User
from app.utils.exceptions import AuthenticationError, InactiveUserError

# HTTPBearer drives the "Authorize" button in /docs; the OAuth2 scheme is
# declared too so the interactive docs can log in via the form endpoint.
bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_PREFIX}/auth/token", auto_error=False
)

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """Resolve the caller from the bearer token.

    The user (and therefore the role and clinic assignments) is re-read from the
    database on every request. Disabling an account or reassigning a clinic
    takes effect on the next call, not whenever the token happens to expire.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Not authenticated")

    try:
        payload = decode_token(credentials.credentials, expected_type=TokenType.ACCESS)
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("Malformed token subject") from exc

    user = db.get(User, user_id)
    if user is None:
        raise AuthenticationError("User no longer exists")
    if not user.is_active:
        raise InactiveUserError()

    request.state.current_user = user
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: RoleName):
    """Dependency factory enforcing that the caller holds one of `roles`."""

    def _dependency(user: CurrentUser) -> User:
        permissions.require_roles(user, roles)
        return user

    return _dependency


def require_any_role(roles: Iterable[RoleName]):
    return require_roles(*roles)


# Convenience aliases used by the routers.
SuperadminUser = Annotated[User, Depends(require_roles(RoleName.SUPERADMIN))]
AdminOrSuperadminUser = Annotated[
    User, Depends(require_roles(RoleName.SUPERADMIN, RoleName.ADMIN))
]
ClinicStaffUser = Annotated[
    User, Depends(require_roles(RoleName.SUPERADMIN, RoleName.ADMIN, RoleName.CLINIC_USER))
]
