"""Provider selection based on configuration.

`CLINIC_EMAIL_PROVIDER` / `CLINIC_WHATSAPP_PROVIDER` / `CLINIC_SMS_PROVIDER`
decide which implementation the application uses.

**A real provider that is missing its credentials falls back to the mock with a
warning rather than failing.** A misconfigured mail server must not stop an
appointment being booked or an invoice being generated -- the document is fine,
and a mock send is recorded as such in `outbound_messages` so the gap is visible
rather than silent.
"""

import logging
from functools import lru_cache

from app.config import settings
from app.integrations.base import EmailService, SMSService, WhatsAppService
from app.integrations.mock_providers import (
    MockEmailService,
    MockSMSService,
    MockWhatsAppService,
)

logger = logging.getLogger(__name__)


@lru_cache
def get_email_service() -> EmailService:
    if settings.EMAIL_PROVIDER == "msg91":
        if not settings.MSG91_AUTH_KEY:
            logger.warning(
                "EMAIL_PROVIDER=msg91 but CLINIC_MSG91_AUTH_KEY is empty; using the mock provider"
            )
            return MockEmailService()

        from app.integrations.msg91_provider import MSG91EmailService

        logger.info("Email: MSG91 template API (domain=%s)", settings.MSG91_EMAIL_DOMAIN)
        return MSG91EmailService(
            auth_key=settings.MSG91_AUTH_KEY,
            domain=settings.MSG91_EMAIL_DOMAIN,
            from_email=settings.MSG91_EMAIL_FROM,
            appt_template_id=settings.MSG91_EMAIL_APPT_TEMPLATE_ID,
            bill_template_id=settings.MSG91_EMAIL_BILL_TEMPLATE_ID,
            timeout=settings.MSG91_EMAIL_TIMEOUT_SECONDS,
        )

    if settings.EMAIL_PROVIDER == "smtp":
        if not settings.SMTP_HOST:
            logger.warning(
                "EMAIL_PROVIDER=smtp but CLINIC_SMTP_HOST is empty; using the mock provider"
            )
            return MockEmailService()

        from app.integrations.smtp_provider import SMTPEmailService

        logger.info("Email: SMTP via %s:%s", settings.SMTP_HOST, settings.SMTP_PORT)
        return SMTPEmailService()
    return MockEmailService()


@lru_cache
def get_whatsapp_service() -> WhatsAppService:
    if settings.WHATSAPP_PROVIDER == "business_api":
        if not (settings.WHATSAPP_API_KEY and settings.WHATSAPP_PHONE_NUMBER_ID):
            logger.warning(
                "WHATSAPP_PROVIDER=business_api but credentials are missing; "
                "using the mock provider"
            )
            return MockWhatsAppService()

        from app.integrations.whatsapp_provider import WhatsAppCloudService

        logger.info("WhatsApp: Cloud API via %s", settings.WHATSAPP_API_URL)
        return WhatsAppCloudService()
    return MockWhatsAppService()


@lru_cache
def get_sms_service() -> SMSService:
    if settings.SMS_PROVIDER == "http":
        if not settings.SMS_API_URL:
            logger.warning(
                "SMS_PROVIDER=http but CLINIC_SMS_API_URL is empty; using the mock provider"
            )
            return MockSMSService()

        from app.integrations.sms_provider import HTTPSMSService

        logger.info("SMS: HTTP gateway at %s", settings.SMS_API_URL)
        return HTTPSMSService()
    return MockSMSService()


def reset_provider_cache() -> None:
    """Clear cached providers (used by tests that change configuration)."""
    get_email_service.cache_clear()
    get_whatsapp_service.cache_clear()
    get_sms_service.cache_clear()
