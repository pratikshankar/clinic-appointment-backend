"""Audit log and the outbound-message log (Sections 28, 19)."""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import AuditAction, MessageChannel, MessageStatus
from app.models.mixins import TimestampMixin, enum_column, UTCDateTime

if TYPE_CHECKING:  # pragma: no cover
    from app.models.user import User


class AuditLog(Base):
    """Append-only record of significant actions. Never updated or deleted."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    #: Kept as free text so the log survives a user record being removed.
    username: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[AuditAction] = enum_column(AuditAction, nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    clinic_id: Mapped[int | None] = mapped_column(ForeignKey("clinics.id", ondelete="SET NULL"))
    description: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    user: Mapped["User | None"] = relationship()


class OutboundMessage(Base, TimestampMixin):
    """Log of every message handed to an email/WhatsApp/SMS provider.

    The mock providers write here too, so the delivery flow is fully observable
    in local development and the same table becomes the audit trail once real
    credentials are configured.
    """

    __tablename__ = "outbound_messages"
    __table_args__ = (Index("ix_outbound_channel_status", "channel", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[MessageChannel] = enum_column(MessageChannel, nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text)
    template_key: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[MessageStatus] = enum_column(
        MessageStatus, default=MessageStatus.QUEUED, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(120))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
    related_entity_type: Mapped[str | None] = mapped_column(String(60))
    related_entity_id: Mapped[int | None] = mapped_column(Integer)
