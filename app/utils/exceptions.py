"""Domain exceptions mapped to HTTP responses by `app/utils/error_handlers.py`.

Services raise these instead of `HTTPException` so business logic stays free of
web-framework concerns and stays reusable from scripts and background jobs.
"""

from typing import Any


class AppError(Exception):
    """Base class for expected, client-facing errors."""

    status_code: int = 400
    error_code: str = "bad_request"
    default_message: str = "Request could not be processed"

    def __init__(self, message: str | None = None, details: Any = None):
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)


class ValidationError(AppError):
    status_code = 400
    error_code = "validation_error"
    default_message = "The submitted data is invalid"


class AuthenticationError(AppError):
    status_code = 401
    error_code = "authentication_failed"
    default_message = "Incorrect username or password"


class InactiveUserError(AppError):
    status_code = 403
    error_code = "user_disabled"
    default_message = "This account has been disabled"


class PermissionDeniedError(AppError):
    status_code = 403
    error_code = "permission_denied"
    default_message = "You do not have permission to perform this action"


class ClinicAccessDeniedError(PermissionDeniedError):
    error_code = "clinic_access_denied"
    default_message = "You do not have access to this clinic"


class TooManyAttemptsError(AppError):
    """Rate limit tripped. 429 so a client can distinguish it from bad credentials."""

    status_code = 429
    error_code = "too_many_attempts"
    default_message = "Too many attempts. Please wait and try again."


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    default_message = "The requested resource was not found"


class ConflictError(AppError):
    status_code = 409
    error_code = "conflict"
    default_message = "The request conflicts with the current state"


class DuplicateResourceError(ConflictError):
    error_code = "duplicate_resource"
    default_message = "A record with these details already exists"


class SlotUnavailableError(ConflictError):
    """Raised when a slot's configured capacity is already consumed (Section 26)."""

    error_code = "slot_unavailable"
    default_message = "The selected appointment slot is fully booked"
