"""Shared schema building blocks and reusable validators (Section 37)."""

import re
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

#: Indian mobile numbers: 10 digits starting 6-9, with optional +91/0 prefix.
_MOBILE_RE = re.compile(r"^(?:\+?91[\-\s]?|0)?([6-9]\d{9})$")
_PIN_RE = re.compile(r"^\d{6}$")


class ORMModel(BaseModel):
    """Base for response schemas read from SQLAlchemy objects."""

    model_config = ConfigDict(from_attributes=True)


class Message(BaseModel):
    """Simple acknowledgement response."""

    message: str


class Page(BaseModel, Generic[T]):
    """Envelope for paginated list endpoints."""

    items: list[T]
    total: int
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def normalize_mobile(value: str | None) -> str | None:
    """Validate and reduce a mobile number to 10 canonical digits.

    Storing one canonical form is what makes "search by mobile" and duplicate
    detection actually work -- otherwise +91 98765 43210 and 9876543210 look
    like two different patients.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    compact = re.sub(r"[\s\-()]", "", candidate)
    match = _MOBILE_RE.match(compact)
    if not match:
        raise ValueError(
            "Enter a valid 10-digit Indian mobile number (optionally prefixed with +91)"
        )
    return match.group(1)


def validate_pin_code(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    compact = value.strip()
    if not _PIN_RE.match(compact):
        raise ValueError("PIN code must be exactly 6 digits")
    return compact


def clean_text(value: str | None) -> str | None:
    """Trim whitespace and turn empty strings into NULL."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None
