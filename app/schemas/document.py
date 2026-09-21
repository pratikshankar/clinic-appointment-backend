"""Document delivery schemas (Sections 16 and 19)."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import MessageChannel, MessageStatus
from app.schemas.common import ORMModel, clean_text


class SendRequest(BaseModel):
    """Send a generated document to the patient."""

    channel: MessageChannel = MessageChannel.WHATSAPP
    #: Overrides the patient's stored address. Useful when a relative is paying,
    #: or the number on file is wrong and there is no time to edit the profile.
    recipient: str | None = Field(default=None, max_length=255)

    @field_validator("recipient")
    @classmethod
    def _clean(cls, value):
        return clean_text(value)

    @model_validator(mode="after")
    def _check_channel(self):
        if self.channel not in (MessageChannel.EMAIL, MessageChannel.WHATSAPP):
            raise ValueError("Documents can be sent by EMAIL or WHATSAPP")
        return self


class DeliveryResult(ORMModel):
    """What happened to one send attempt.

    A failed provider call returns 200 with `sent: false` rather than a 5xx: the
    document and the bill are both fine, and the operator's next move is to try
    another channel, not to read an error page.
    """

    id: int
    channel: MessageChannel
    provider: str
    recipient: str
    status: MessageStatus
    error_message: str | None = None
    sent_at: datetime | None = None
    filename: str | None = None

    @property
    def sent(self) -> bool:
        return self.status == MessageStatus.SENT
