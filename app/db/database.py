"""Database engine, session factory and the declarative base.

Everything here is deliberately dialect-agnostic. The only SQLite-specific bits
are isolated in `_engine_kwargs()` and the `PRAGMA foreign_keys` listener, both
of which are no-ops on PostgreSQL.
"""

from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _engine_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"echo": settings.SQL_ECHO, "future": True}
    if settings.is_sqlite:
        # SQLite needs this to be usable from FastAPI's threadpool.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Sensible pooling defaults for PostgreSQL.
        kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)
    return kwargs


engine: Engine = create_engine(settings.effective_database_url, **_engine_kwargs())

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):  # pragma: no cover
    """SQLite ignores FOREIGN KEY constraints unless explicitly enabled.

    Check the actual connection type rather than settings so this is safe
    when multiple engines with different dialects exist (e.g. Alembic targeting
    PostgreSQL while the app uses SQLite via USE_LOCAL_DB).
    """
    import sqlite3
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
