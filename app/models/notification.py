"""In-app clinic notifications and their acknowledgements (Section 15)."""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models.enums import NotificationType
from app.models.mixins import TimestampMixin, enum_column, UTCDateTime

if TYPE_CHECKING:  # pragma: no cover
    from app.models.appointment import Appointment
    from app.models.clinic import Clinic
    from app.models.patient import Patient
    from app.models.user import User


class Notification(Base, TimestampMixin):
    """A notification targeted at a clinic (all its users) or a single user.

    `is_acknowledged` is a denormalised roll-up of the acknowledgement rows: the
    dashboard's unread badge queries it constantly, so a stored flag avoids a
    correlated subquery on every poll. The per-user rows remain the source of
    truth for *who* acknowledged.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_clinic_ack", "clinic_id", "is_acknowledged"),
        Index("ix_notifications_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int | None] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), index=True
    )
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    notification_type: Mapped[NotificationType] = enum_column(NotificationType, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: Structured detail for the frontend (patient name, time, clinic, ...).
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL")
    )
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id", ondelete="SET NULL"))
    is_acknowledged: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime(timezone=True))
    #: Drives the one-time sound alert in the clinic dashboard.
    requires_sound_alert: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    clinic: Mapped["Clinic | None"] = relationship(lazy="joined")
    appointment: Mapped["Appointment | None"] = relationship()
    patient: Mapped["Patient | None"] = relationship()
    acknowledgements: Mapped[list["NotificationAcknowledgement"]] = relationship(
        back_populates="notification", cascade="all, delete-orphan"
    )


class NotificationAcknowledgement(Base):
    __tablename__ = "notification_acknowledgements"
    __table_args__ = (
        UniqueConstraint("notification_id", "user_id", name="uq_notification_ack_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    acknowledged_at: Mapped[datetime] = mapped_column(
        UTCDateTime(timezone=True), server_default=func.now(), nullable=False
    )

    notification: Mapped[Notification] = relationship(back_populates="acknowledgements")
    user: Mapped["User"] = relationship(lazy="joined")
