"""SMS through a generic HTTP provider.

Deliberately generic: Indian SMS aggregators (MSG91, Gupshup, TextLocal, Kaleyra)
all expose "POST a form or JSON with an API key, a sender id and a template id",
differing mainly in field names. Rather than pick one and lock the clinic in,
the field names are configurable and the shared shape is implemented once.

**This will not deliver in India without DLT registration.** The sender id and
every message template must be registered with the telecom operators under
TRAI's rules first; until then providers accept the request and drop the
message. That is why `SMS_TEMPLATE_ID` exists and why a missing one is reported
as a failure rather than silently sent.
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.integrations.base import MessageResult, SMSService
from app.models.enums import MessageChannel

logger = logging.getLogger(__name__)


class HTTPSMSService(SMSService):
    """POSTs to a configured SMS gateway."""

    name = "sms-http"

    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        sender_id: str | None = None,
        template_id: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.api_url = api_url or settings.SMS_API_URL
        self.api_key = api_key or settings.SMS_API_KEY
        self.sender_id = sender_id or settings.SMS_SENDER_ID
        self.template_id = template_id or settings.SMS_TEMPLATE_ID
        self.timeout = timeout or settings.SMS_TIMEOUT_SECONDS

    @staticmethod
    def _recipient(to: str) -> str:
        digits = "".join(ch for ch in to if ch.isdigit())
        return f"91{digits}" if len(digits) == 10 else digits

    def _fail(self, to: str, error: str) -> MessageResult:
        logger.warning("SMS send to %s failed: %s", to, error)
        return MessageResult(
            success=False,
            provider=self.name,
            channel=MessageChannel.SMS,
            recipient=to,
            error=error[:500],
        )

    def send(self, *, to: str, body: str, template_id: str | None = None) -> MessageResult:
        if not self.api_url:
            return self._fail(to, "No SMS gateway URL is configured")

        dlt_template = template_id or self.template_id
        if not dlt_template:
            # Reported rather than sent: without a registered template the
            # message is accepted by the gateway and then quietly discarded by
            # the operator, which looks like success and is not.
            return self._fail(
                to,
                "No DLT template id configured. Indian operators drop transactional "
                "SMS that is not sent against a registered template.",
            )

        payload = {
            "to": self._recipient(to),
            "sender": self.sender_id,
            "template_id": dlt_template,
            "message": body,
        }
        try:
            response = httpx.post(
                self.api_url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            return self._fail(to, f"{type(exc).__name__}: {exc}")

        if response.status_code >= 400:
            return self._fail(to, f"HTTP {response.status_code}: {response.text[:300]}")

        message_id = None
        try:
            data = response.json()
            message_id = data.get("message_id") or data.get("id") or data.get("request_id")
        except Exception:  # noqa: BLE001 - a plain-text OK is a valid response
            pass

        return MessageResult(
            success=True,
            provider=self.name,
            channel=MessageChannel.SMS,
            recipient=to,
            provider_message_id=message_id,
        )
