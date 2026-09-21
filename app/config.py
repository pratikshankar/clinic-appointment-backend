"""Application configuration.

All settings come from environment variables (or a local ``.env``), so the same
code runs unchanged against SQLite locally and PostgreSQL in production.

Why every variable is prefixed ``CLINIC_``
------------------------------------------
Bare names like ``DATABASE_URL`` are extremely common; a developer machine or a
shared CI runner very often already exports one for an unrelated project. Since
OS environment variables correctly take priority over a local ``.env`` file,
an ambient ``DATABASE_URL`` would silently redirect this application at someone
else's database -- and with ``create_all()`` in development it would happily
create tables there. Namespacing removes that class of accident entirely while
keeping the standard precedence rules (OS env > .env > defaults).

The spec's Section 35 names map one-to-one:

    DATABASE_URL                -> CLINIC_DATABASE_URL
    SECRET_KEY                  -> CLINIC_SECRET_KEY
    ACCESS_TOKEN_EXPIRE_MINUTES -> CLINIC_ACCESS_TOKEN_EXPIRE_MINUTES
    EMAIL_PROVIDER              -> CLINIC_EMAIL_PROVIDER
    EMAIL_API_KEY               -> CLINIC_EMAIL_API_KEY
    WHATSAPP_PROVIDER           -> CLINIC_WHATSAPP_PROVIDER
    WHATSAPP_API_KEY            -> CLINIC_WHATSAPP_API_KEY
"""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLINIC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- application ---
    APP_NAME: str = "Clinic Management App"
    APP_ENV: Literal["development", "staging", "production", "test"] = "development"
    API_PREFIX: str = "/api"
    DEBUG: bool = True
    #: Set when the app is served under a sub-path by a reverse proxy, e.g.
    #: "/clinic-api". Usually unnecessary: the docs pages fall back to
    #: X-Forwarded-Prefix and then to relative URLs. Equivalent to uvicorn's
    #: --root-path.
    ROOT_PATH: str = ""

    # --- login throttling (Phase 9) ---
    #: Failures from one username+IP before a lockout. 0 disables throttling.
    LOGIN_MAX_ATTEMPTS: int = 8
    LOGIN_ATTEMPT_WINDOW_SECONDS: int = 300
    LOGIN_LOCKOUT_SECONDS: int = 300

    # --- documents (Section 19) ---
    #: Brand shown on invoices, receipts and insurance statements.
    BRAND_NAME: str = "PainEasy"
    BRAND_TAGLINE: str = "Physiotherapy & Rehabilitation"
    #: Logo for document headers. Empty means "app/assets/paineasy-logo.png";
    #: a missing file falls back to a text wordmark rather than failing to
    #: generate the document at all.
    LOGO_PATH: str = ""
    #: Printed at the foot of an insurance statement. Put clinic registration
    #: or GST numbers here if your insurer asks for them -- kept as free text so
    #: it needs no schema change when a new insurer wants a new line.
    DOCUMENT_FOOTER: str = ""

    # --- database ---
    # SQLite for local development; swap for postgresql+psycopg://... later.
    DATABASE_URL: str = "sqlite:///./clinic_management.db"
    SQL_ECHO: bool = False
    #: Create tables on startup. Convenient in development; in production use
    #: Alembic migrations instead and leave this off.
    AUTO_CREATE_TABLES: bool = True

    # --- auth ---
    SECRET_KEY: str = "dev-only-insecure-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # --- CORS (comma-separated list in the environment) ---
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # --- external service providers (see app/integrations) ---
    EMAIL_PROVIDER: Literal["mock", "smtp", "msg91"] = "mock"
    EMAIL_API_KEY: str = ""
    EMAIL_FROM: str = "no-reply@clinic.example.com"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""

    #: Sender name shown beside the address, e.g. "PainEasy <no-reply@...>".
    EMAIL_FROM_NAME: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_TIMEOUT_SECONDS: int = 20

    # --- MSG91 (email + WhatsApp via single auth key) ---
    MSG91_AUTH_KEY: str = ""
    #: The domain you verified in MSG91 Email → Sending Domains.
    MSG91_EMAIL_DOMAIN: str = ""
    #: from.email in the send payload; must match the verified domain.
    MSG91_EMAIL_FROM: str = ""
    #: Template IDs as created in MSG91 Email → Templates.
    MSG91_EMAIL_APPT_TEMPLATE_ID: str = ""
    MSG91_EMAIL_BILL_TEMPLATE_ID: str = ""
    MSG91_EMAIL_TIMEOUT_SECONDS: int = 20

    WHATSAPP_PROVIDER: Literal["mock", "business_api"] = "mock"
    WHATSAPP_API_KEY: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_WEBHOOK_VERIFY_TOKEN: str = ""
    #: Cloud API base. Most BSPs (Gupshup, Twilio, 360dialog) proxy this exact
    #: shape, so pointing at their host is usually the only change needed.
    WHATSAPP_API_URL: str = "https://graph.facebook.com/v21.0"
    WHATSAPP_TIMEOUT_SECONDS: int = 20
    #: Meta requires business-initiated messages to use a pre-approved template.
    #: These are the names submitted for approval, one per appointment event.
    WHATSAPP_TEMPLATE_LANGUAGE: str = "en"

    # --- SMS: a fallback for when WhatsApp does not deliver ---
    SMS_PROVIDER: Literal["mock", "http"] = "mock"
    SMS_API_URL: str = ""
    SMS_API_KEY: str = ""
    SMS_SENDER_ID: str = ""
    #: DLT template id, mandatory for transactional SMS in India.
    SMS_TEMPLATE_ID: str = ""
    SMS_TIMEOUT_SECONDS: int = 20
    #: Send an SMS when a WhatsApp message comes back failed. Off by default:
    #: it costs money per message and needs DLT registration to deliver at all.
    SMS_FALLBACK_ENABLED: bool = False

    # --- clinic defaults (seed script + clinic creation) ---
    DEFAULT_SLOT_DURATION_MINUTES: int = 30
    DEFAULT_CAPACITY_PER_SLOT: int = 3

    # --- seed credentials (development only) ---
    SEED_SUPERADMIN_USERNAME: str = "superadmin"
    SEED_SUPERADMIN_PASSWORD: str = "ChangeMe@123"
    SEED_SUPERADMIN_EMAIL: str = "superadmin@clinic.example.com"
    SEED_ADMIN_USERNAME: str = "admin"
    SEED_ADMIN_PASSWORD: str = "ChangeMe@123"
    SEED_CLINIC_USER_PASSWORD: str = "ChangeMe@123"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def safe_database_url(self) -> str:
        """Database URL with any password removed, for logging."""
        url = self.DATABASE_URL
        if "@" in url and "//" in url:
            scheme, _, rest = url.partition("//")
            credentials, _, host = rest.rpartition("@")
            if credentials:
                user = credentials.split(":", 1)[0]
                return f"{scheme}//{user}:***@{host}"
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
