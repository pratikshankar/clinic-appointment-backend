"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import inspect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

from app.config import settings
from app.db.database import Base, engine
from app.routers import (
    appointments,
    audit,
    auth,
    billing,
    clinics,
    dashboard,
    holidays,
    meta,
    notifications,
    patient_sources,
    patients,
    reports,
    sessions,
    users,
    whatsapp,
)
from app.utils.error_handlers import register_error_handlers

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class _SuppressPollingRoutes(logging.Filter):
    """Drop routine access-log lines for high-frequency polling endpoints.

    The notification counter is polled every 30 s. Logging every hit at INFO
    floods the terminal without adding diagnostic value. Genuine errors still
    appear because uvicorn only calls this logger for 2xx responses.
    """
    _SUPPRESS = {"/api/notifications/counters", "/api/health", "/api/whatsapp/webhook"}

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(path in msg for path in self._SUPPRESS)


logging.getLogger("uvicorn.access").addFilter(_SuppressPollingRoutes())

OPENAPI_URL = "/openapi.json"


def _openapi_url_for(request: Request) -> str:
    """Return a URL for openapi.json that is correct from the browser's point of view.

    FastAPI's built-in docs pages hardcode the absolute path ``/openapi.json``.
    That breaks whenever the app is reached through a proxy that mounts it under
    a sub-path -- VS Code / Codespaces port forwarding, JupyterHub, or an nginx
    ``location /clinic-api/`` block. Such proxies strip the prefix before
    forwarding, so ``/docs`` itself loads fine while the browser then requests
    ``https://host/openapi.json`` at the domain root and gets a 404.

    Resolution order:

    1. An explicit ``CLINIC_ROOT_PATH`` (or ``--root-path``), if configured.
    2. ``X-Forwarded-Prefix``, which many reverse proxies set.
    3. A *relative* URL, which the browser resolves against the current page and
       is therefore correct under any prefix without configuration.
    """
    prefix = (request.scope.get("root_path") or "").rstrip("/")
    if not prefix:
        prefix = (request.headers.get("x-forwarded-prefix") or "").rstrip("/")
    if prefix:
        return f"{prefix}{OPENAPI_URL}"

    # "/docs" -> "openapi.json"; "/docs/" -> "../openapi.json". Both resolve to
    # a sibling of the docs page, with or without a prefix in front.
    return "../openapi.json" if request.url.path.endswith("/") else "openapi.json"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s (%s)", settings.APP_NAME, settings.APP_ENV)
    logger.info("Database: %s", settings.safe_database_url)

    if settings.AUTO_CREATE_TABLES:
        if settings.is_production:
            # Schema changes in production belong to Alembic, where they are
            # reviewable and reversible.
            logger.warning("AUTO_CREATE_TABLES is ignored in production; use Alembic migrations")
        else:
            import app.models  # noqa: F401 - registers mappers before create_all

            # Only bootstrap a database that Alembic has never touched.
            #
            # Letting create_all() run on a migrated database is actively
            # harmful: it creates *new* tables but never adds columns to
            # existing ones, so a half-applied schema appears out of nowhere and
            # the next `alembic upgrade` fails with "table already exists". On a
            # --reload server this happens the instant a new model is imported,
            # racing whatever the developer is doing in another terminal.
            with engine.connect() as connection:
                stamped = inspect(connection).has_table("alembic_version")

            if stamped:
                logger.info("Database is managed by Alembic; skipping create_all()")
            else:
                Base.metadata.create_all(bind=engine)
                logger.info("Bootstrapped %d tables", len(Base.metadata.tables))

    if settings.is_production:
        # Fail fast rather than serving traffic signed with a guessable key.
        if settings.SECRET_KEY.startswith("dev-only"):
            raise RuntimeError("CLINIC_SECRET_KEY must be set to a real secret in production")
        if len(settings.SECRET_KEY) < 32:
            raise RuntimeError(
                "CLINIC_SECRET_KEY must be at least 32 characters for HS256 "
                "(see RFC 7518 section 3.2)"
            )
    elif len(settings.SECRET_KEY) < 32:
        logger.warning(
            "CLINIC_SECRET_KEY is shorter than the 32 characters recommended for HS256"
        )

    yield
    logger.info("Shutting down %s", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Backend for a chain of physiotherapy clinics: role-based access, "
        "clinics, patients, appointments, sessions, billing and notifications.\n\n"
        "**Phases 1-5 plus the billing core** are live: authentication and RBAC, "
        "user management, full clinic configuration, patient management, "
        "appointment booking, physiotherapy session tracking, and bills with "
        "line items and payments."
    ),
    version="0.1.0",
    lifespan=lifespan,
    # The docs pages are served by the custom routes below so their reference to
    # openapi.json survives being mounted behind a path prefix.
    docs_url=None,
    redoc_url=None,
    openapi_url=OPENAPI_URL,
    root_path=settings.ROOT_PATH,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)

api_prefix = settings.API_PREFIX
app.include_router(meta.router, prefix=api_prefix)
app.include_router(auth.router, prefix=api_prefix)
app.include_router(users.router, prefix=api_prefix)
app.include_router(users.roles_router, prefix=api_prefix)
app.include_router(clinics.router, prefix=api_prefix)
app.include_router(holidays.router, prefix=api_prefix)
app.include_router(patient_sources.router, prefix=api_prefix)
app.include_router(patients.router, prefix=api_prefix)
app.include_router(appointments.router, prefix=api_prefix)
app.include_router(sessions.patient_router, prefix=api_prefix)
app.include_router(sessions.package_router, prefix=api_prefix)
app.include_router(sessions.session_router, prefix=api_prefix)
app.include_router(notifications.router, prefix=api_prefix)
app.include_router(billing.catalogue_router, prefix=api_prefix)
app.include_router(billing.patient_router, prefix=api_prefix)
app.include_router(billing.router, prefix=api_prefix)
app.include_router(billing.payment_router, prefix=api_prefix)
app.include_router(billing.document_router, prefix=api_prefix)
app.include_router(reports.router, prefix=api_prefix)
app.include_router(audit.router, prefix=api_prefix)
app.include_router(dashboard.router, prefix=api_prefix)
app.include_router(whatsapp.router, prefix=api_prefix)


@app.get("/docs", include_in_schema=False)
def swagger_ui(request: Request):
    return get_swagger_ui_html(
        openapi_url=_openapi_url_for(request),
        title=f"{settings.APP_NAME} — API docs",
        swagger_ui_parameters={"persistAuthorization": True, "docExpansion": "none"},
    )


@app.get("/redoc", include_in_schema=False)
def redoc_ui(request: Request):
    return get_redoc_html(
        openapi_url=_openapi_url_for(request),
        title=f"{settings.APP_NAME} — API reference",
    )


@app.get("/", include_in_schema=False)
def root():
    return {
        "app": settings.APP_NAME,
        "version": app.version,
        "docs": "/docs",
        "api": api_prefix,
    }
