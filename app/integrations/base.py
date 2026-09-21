"""Provider interfaces for outbound messaging (Section 33).

Business logic depends only on these abstractions, never on a concrete
provider. Swapping the mock for a real WhatsApp Business API or SMTP account is
a configuration change, not a code change in appointment or billing logic.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import MessageChannel


@dataclass(slots=True)
class MessageResult:
    """Outcome of a single delivery attempt."""

    success: bool
    provider: str
    channel: MessageChannel
    recipient: str
    provider_message_id: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class EmailService(ABC):
    """Interface for transactional email (appointment info, bill delivery)."""

    name: str = "email"

    @abstractmethod
    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> MessageResult:
        """Send one email. Must not raise for ordinary delivery failures."""


class WhatsAppService(ABC):
    """Interface for WhatsApp messaging (Section 16)."""

    name: str = "whatsapp"

    @abstractmethod
    def send_text(self, *, to: str, body: str) -> MessageResult:
        """Send a plain text message to an E.164-style number."""

    @abstractmethod
    def send_template(
        self, *, to: str, template_key: str, variables: dict[str, Any]
    ) -> MessageResult:
        """Send a pre-approved template message.

        Real WhatsApp Business accounts can only initiate conversations with
        approved templates, so this is kept separate from `send_text` rather
        than being bolted on later.
        """

    @abstractmethod
    def send_document(
        self, *, to: str, filename: str, content: bytes, caption: str | None = None
    ) -> MessageResult:
        """Send a document (an invoice or receipt PDF).

        Separate from `send_text` because the real Business API handles media
        differently: the file is uploaded for a media id, or referenced by a
        publicly reachable URL, and neither is expressible as a text body.
        Taking `bytes` keeps that choice inside the provider.
        """


class SMSService(ABC):
    """Interface for transactional SMS.

    Exists as a **fallback for WhatsApp**, not as a parallel channel: sending
    both for every appointment doubles the cost and annoys the patient. It is
    used only when a WhatsApp message comes back failed.

    In India a transactional SMS also needs the sender ID and every message
    template registered with the telecom operators under TRAI's DLT rules, which
    is why `template_id` is part of the interface rather than an implementation
    detail -- a provider that cannot supply one will not deliver.
    """

    name: str = "sms"

    @abstractmethod
    def send(self, *, to: str, body: str, template_id: str | None = None) -> MessageResult:
        """Send one SMS. Must not raise for ordinary delivery failures."""
