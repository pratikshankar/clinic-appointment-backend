"""Reusable column mixins."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """DateTime that always returns tz-aware UTC datetimes.

    SQLite stores timestamps as plain strings with no timezone marker.
    SQLAlchemy reads them back as naive datetimes. Pydantic then serialises
    them without 'Z', and the browser treats them as local time — in India
    that means the API sends 10:46 and the browser shows 10:46 IST when the
    real wall-clock UTC time was 10:46 (= 16:16 IST).

    This decorator stamps timezone.utc onto any naive value the DB returns,
    so Pydantic always emits "…T10:46:00+00:00" and the browser correctly
    converts it to 16:16 IST.
    """

    impl = DateTime
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def enum_column(enum_cls, **kwargs):
    """Portable enum column: stored as VARCHAR with a CHECK constraint.

    Using ``native_enum=False`` means the DDL is identical on SQLite and
    PostgreSQL, and adding a new enum member later does not require altering a
    database-level type.
    """
    return mapped_column(
        Enum(
            enum_cls,
            native_enum=False,
            length=40,
            validate_strings=True,
            values_callable=lambda e: [member.value for member in e],
        ),
        **kwargs,
    )


class TimestampMixin:
    """`created_at` / `updated_at` maintained by the database server clock."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
