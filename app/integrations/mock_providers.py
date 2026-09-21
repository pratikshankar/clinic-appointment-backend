"""Mock providers used when no external credentials are configured.

They log every message and keep the last N in memory so local development and
tests can assert on what *would* have been sent. Nothing is silently dropped:
each call still returns a `MessageResult`.
"""

import logging
import uuid
from collections import deque
from typing import Any

from app.integrations.base import (
    EmailService,
    MessageResult,
    SMSService,
    WhatsAppService,
)
from app.models.enums import MessageChannel

logger = logging.getLogger("app.integrations.mock")

#: Rolling buffer of everything the mocks have "sent" (newest last).
SENT_MESSAGES: deque[dict[str, Any]] = deque(maxlen=200)


def _record(channel: MessageChannel, provider: str, recipient: str, **extra) -> str:
    message_id = f"mock-{uuid.uuid4().hex[:12]}"
    entry = {
        "id": message_id,
        "channel": channel.value,
        "provider": provider,
        "recipient": recipient,
        **extra,
    }
    SENT_MESSAGES.append(entry)
    logger.info("[MOCK %s] to=%s %s", channel.value, recipient, extra.get("subject") or "")
    return message_id


class MockEmailService(EmailService):
    name = "mock-email"

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> MessageResult:
        message_id = _record(
            MessageChannel.EMAIL,
            self.name,
            to,
            subject=subject,
            body=body,
            attachments=[name for name, _ in (attachments or [])],
        )
        return MessageResult(
            success=True,
            provider=self.name,
            channel=MessageChannel.EMAIL,
            recipient=to,
            provider_message_id=message_id,
        )


class MockWhatsAppService(WhatsAppService):
    name = "mock-whatsapp"

    def send_text(self, *, to: str, body: str) -> MessageResult:
        message_id = _record(MessageChannel.WHATSAPP, self.name, to, body=body)
        return MessageResult(
            success=True,
            provider=self.name,
            channel=MessageChannel.WHATSAPP,
            recipient=to,
            provider_message_id=message_id,
        )

    def send_template(
        self, *, to: str, template_key: str, variables: dict[str, Any]
    ) -> MessageResult:
        message_id = _record(
            MessageChannel.WHATSAPP,
            self.name,
            to,
            template_key=template_key,
            variables=variables,
        )
        return MessageResult(
            success=True,
            provider=self.name,
            channel=MessageChannel.WHATSAPP,
            recipient=to,
            provider_message_id=message_id,
            metadata={"template_key": template_key},
        )

    def send_document(
        self, *, to: str, filename: str, content: bytes, caption: str | None = None
    ) -> MessageResult:
        # The bytes are deliberately not kept in the buffer: a few hundred KB of
        # PDF per message would turn the debug log into a memory leak. Size is
        # recorded instead, which is what a test actually wants to assert.
        message_id = _record(
            MessageChannel.WHATSAPP,
            self.name,
            to,
            filename=filename,
            document_bytes=len(content),
            caption=caption,
        )
        return MessageResult(
            success=True,
            provider=self.name,
            channel=MessageChannel.WHATSAPP,
            recipient=to,
            provider_message_id=message_id,
            metadata={"filename": filename, "bytes": len(content)},
        )


class MockSMSService(SMSService):
    name = "mock-sms"

    def send(self, *, to: str, body: str, template_id: str | None = None) -> MessageResult:
        message_id = _record(
            MessageChannel.SMS, self.name, to, body=body, template_id=template_id
        )
        return MessageResult(
            success=True,
            provider=self.name,
            channel=MessageChannel.SMS,
            recipient=to,
            provider_message_id=message_id,
        )
