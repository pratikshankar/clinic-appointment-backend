"""Clinic notifications and patient messaging (Sections 15 and 16).

Two things happen when an appointment changes, and they have deliberately
different failure behaviour:

* **The clinic notification** is written in the *same transaction* as the
  appointment. If the booking commits, the alert exists; if the booking rolls
  back, so does the alert. A clinic being told about an appointment that was
  never made is worse than not being told.

* **The patient message** is queued in that transaction but *delivered after it
  commits*, outside any transaction. A WhatsApp provider timing out must never
  lose an appointment -- the spec is explicit about this, and it is the reason
  delivery is a second step rather than an inline call.

The queued-then-delivered split also means a crash between the two leaves a
`QUEUED` row in `outbound_messages` rather than silence, so the message is
recoverable rather than merely lost.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import permissions
from app.integrations.base import MessageResult
from app.integrations.factory import get_email_service, get_sms_service, get_whatsapp_service
from app.models import (
    Appointment,
    AuditAction,
    MessageChannel,
    MessageStatus,
    Notification,
    NotificationAcknowledgement,
    NotificationType,
    OutboundMessage,
    User,
)
from app.config import settings
from app.services import audit_service
from app.utils.exceptions import NotFoundError, PermissionDeniedError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AppointmentEvent:
    """One kind of appointment change, and how it is announced."""

    key: str
    notification_type: NotificationType
    #: The heading Section 15 specifies, verbatim.
    headline: str


CREATED = AppointmentEvent("created", NotificationType.NEW_APPOINTMENT, "NEW APPOINTMENT")
RESCHEDULED = AppointmentEvent(
    "rescheduled", NotificationType.RESCHEDULED_APPOINTMENT, "RESCHEDULED APPOINTMENT"
)
CANCELLED = AppointmentEvent(
    "cancelled", NotificationType.CANCELLED_APPOINTMENT, "CANCELLED APPOINTMENT"
)
#: A missed appointment is the moment a patient is most likely to drift away,
#: and it is what later shows up in the drop-off report. Worth a message --
#: written as an invitation to rebook, not as a reprimand, because the reason is
#: as often the clinic's as the patient's.
NO_SHOW = AppointmentEvent(
    "no_show", NotificationType.CANCELLED_APPOINTMENT, "MISSED APPOINTMENT"
)


def slot_time_text(appointment: Appointment) -> str:
    """5:30 PM -- the format Section 15's example uses.

    Public so the seed script formats notification payloads identically; a
    seeded card showing "18:30" while a real one shows "6:00 PM" looks like a
    bug in the app rather than in the fixture.
    """
    hour = appointment.start_time.hour
    period = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{appointment.start_time.minute:02d} {period}"


def _date_text(appointment: Appointment) -> str:
    return f"{appointment.appointment_date:%d-%b-%Y}"


# --------------------------------------------------------------------------- #
# Clinic notifications (Section 15)
# --------------------------------------------------------------------------- #
def should_notify_clinic(actor: User, clinic_id: int) -> bool:
    """Does this clinic need telling, given who acted?

    Only when the actor is **not** part of that clinic. A receptionist booking at
    their own desk already knows; notifying them teaches staff that the badge is
    noise, and a badge people ignore is worse than no badge. Admin and Superadmin
    hold no `clinic_users` rows, so anything they do reaches the clinic.
    """
    return clinic_id not in actor.clinic_ids


def _build_notification(
    actor: User, appointment: Appointment, event: AppointmentEvent, reason: str | None
) -> Notification:
    patient = appointment.patient
    clinic = appointment.clinic
    lines = [
        f"Patient: {patient.full_name}" if patient else None,
        f"Date: {_date_text(appointment)}",
        f"Time: {slot_time_text(appointment)}",
        f"Clinic: {clinic.name}" if clinic else None,
        f"Reason: {reason}" if reason else None,
    ]

    return Notification(
        clinic_id=appointment.clinic_id,
        notification_type=event.notification_type,
        title=event.headline,
        message="\n".join(line for line in lines if line),
        payload={
            "event": event.key,
            "appointment_code": appointment.appointment_code,
            "patient_name": patient.full_name if patient else None,
            "patient_code": patient.patient_code if patient else None,
            "clinic_name": clinic.name if clinic else None,
            "date": appointment.appointment_date.isoformat(),
            "time": slot_time_text(appointment),
            "reason": reason,
            "booked_by": actor.full_name,
        },
        appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        # Every appointment change is worth a sound at the front desk -- a
        # cancellation frees a slot someone is probably waiting for.
        requires_sound_alert=True,
        created_by_user_id=actor.id,
    )


# --------------------------------------------------------------------------- #
# Patient WhatsApp (Section 16)
# --------------------------------------------------------------------------- #
_TEMPLATES = {
    CREATED.key: (
        "appointment_booked",
        "Your physiotherapy appointment has been booked.\n\n"
        "Clinic: {clinic}\nDate: {date}\nTime: {time}\n\n"
        "Please arrive 5-10 minutes before your appointment.",
    ),
    RESCHEDULED.key: (
        "appointment_rescheduled",
        "Your physiotherapy appointment has been rescheduled.\n\n"
        "Clinic: {clinic}\nNew date: {date}\nNew time: {time}\n\n"
        "Please arrive 5-10 minutes before your appointment.",
    ),
    CANCELLED.key: (
        "appointment_cancelled",
        "Your physiotherapy appointment on {date} at {time} ({clinic}) "
        "has been cancelled.\n\nPlease contact the clinic to rebook.",
    ),
    NO_SHOW.key: (
        "appointment_missed",
        "We missed you at your physiotherapy appointment on {date} at {time} "
        "({clinic}).\n\nKeeping to the plan matters for your recovery -- "
        "reply or call us and we will find you another slot.",
    ),
}


def _queue_whatsapp(
    db: Session, appointment: Appointment, event: AppointmentEvent
) -> OutboundMessage | None:
    """Queue a WhatsApp message if the patient has a WhatsApp number."""
    patient = appointment.patient
    recipient = patient.whatsapp_contact if patient else None
    if not recipient:
        return None  # silent — no number on file is not an error

    template_key, template = _TEMPLATES[event.key]
    body = template.format(
        clinic=appointment.clinic.name if appointment.clinic else "the clinic",
        date=_date_text(appointment),
        time=slot_time_text(appointment),
    )
    message = OutboundMessage(
        channel=MessageChannel.WHATSAPP,
        provider=get_whatsapp_service().name,
        recipient=recipient,
        body=body,
        template_key=template_key,
        status=MessageStatus.QUEUED,
        related_entity_type="appointment",
        related_entity_id=appointment.id,
    )
    db.add(message)
    return message


def _queue_email(
    db: Session, appointment: Appointment, event: AppointmentEvent
) -> OutboundMessage | None:
    """Queue an email if the patient has an email address.

    Only BOOKED and RESCHEDULED events send a confirmation email — cancellations
    and no-shows are WhatsApp-only (more immediate channel, same template isn't
    suitable for bad news).
    """
    import json
    patient = appointment.patient
    if not patient or not patient.email:
        return None  # silent — missing email is not an error

    if event.key not in (CREATED.key, RESCHEDULED.key):
        return None

    clinic_name = appointment.clinic.name if appointment.clinic else "the clinic"
    date_time_str = f"{_date_text(appointment)} at {slot_time_text(appointment)}"
    template_key, _ = _TEMPLATES[event.key]
    action = "Confirmed" if event.key == CREATED.key else "Rescheduled"

    # body stores the MSG91 template variables as JSON so deliver() can read them
    body = json.dumps({
        "user_name": patient.full_name,
        "appointment_date_and_time": date_time_str,
        "centre_name": clinic_name,
    })

    message = OutboundMessage(
        channel=MessageChannel.EMAIL,
        provider=get_email_service().name,
        recipient=patient.email,
        subject=f"Appointment {action} — {clinic_name}",
        body=body,
        template_key=template_key,
        status=MessageStatus.QUEUED,
        related_entity_type="appointment",
        related_entity_id=appointment.id,
    )
    db.add(message)
    return message


# --------------------------------------------------------------------------- #
# The entry point appointment_service calls
# --------------------------------------------------------------------------- #
def record_appointment_event(
    db: Session,
    actor: User,
    appointment: Appointment,
    event: AppointmentEvent,
    *,
    reason: str | None = None,
) -> list[int]:
    """Announce an appointment change. **Call before `db.commit()`.**

    Returns the ids of queued outbound messages, which the caller hands to
    `deliver()` *after* committing.
    """
    if should_notify_clinic(actor, appointment.clinic_id):
        db.add(_build_notification(actor, appointment, event, reason))

    queued_ids: list[int] = []
    for msg in (_queue_whatsapp(db, appointment, event),
                _queue_email(db, appointment, event)):
        if msg is not None:
            db.flush()
            queued_ids.append(msg.id)
    return queued_ids


def deliver(db: Session, message_ids: list[int]) -> list[MessageResult]:
    """Hand queued messages to the provider. **Call after `db.commit()`.**

    Routes each message to the right provider by channel.
    Never raises — provider faults are recorded in outbound_messages and logged.
    """
    if not message_ids:
        return []

    results: list[MessageResult] = []
    try:
        messages = (
            db.execute(select(OutboundMessage).where(OutboundMessage.id.in_(message_ids)))
            .scalars()
            .all()
        )
        for message in messages:
            try:
                result = _send_one(message)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Provider raised for %s %s: %s",
                               message.channel, message.recipient, exc)
                message.status = MessageStatus.FAILED
                message.error_message = str(exc)[:500]
                continue

            results.append(result)
            message.status = MessageStatus.SENT if result.success else MessageStatus.FAILED
            message.error_message = result.error
            message.provider_message_id = result.provider_message_id
            message.sent_at = datetime.now(timezone.utc) if result.success else None

            if not result.success and message.channel == MessageChannel.WHATSAPP:
                fallback_to_sms(db, message)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Delivering queued messages failed; the appointment is unaffected")
        db.rollback()
    return results


def _send_one(message: OutboundMessage) -> MessageResult:
    """Dispatch a single queued message to the correct provider."""
    if message.channel == MessageChannel.EMAIL:
        import json
        svc = get_email_service()
        # Body stores MSG91 template variables as JSON (set by _queue_email).
        # Use send_appointment() when available; fall back to generic send().
        try:
            meta = json.loads(message.body or "{}")
        except (ValueError, TypeError):
            meta = {}

        if hasattr(svc, "send_appointment") and meta.get("user_name"):
            return svc.send_appointment(  # type: ignore[union-attr]
                to=message.recipient,
                patient_name=meta["user_name"],
                appointment_datetime=meta.get("appointment_date_and_time", ""),
                centre_name=meta.get("centre_name", ""),
            )
        return svc.send(
            to=message.recipient,
            subject=message.subject or "Appointment update",
            body=meta.get("appointment_date_and_time") or message.body or "",
        )

    # WhatsApp (default)
    return get_whatsapp_service().send_text(to=message.recipient, body=message.body or "")


def fallback_to_sms(db: Session, failed: OutboundMessage) -> OutboundMessage | None:
    """Send an SMS after a WhatsApp message failed.

    A fallback, not a second channel: sending both every time doubles the cost
    and trains patients to ignore one of them. Off unless
    `CLINIC_SMS_FALLBACK_ENABLED` is set, because it costs money per message and
    will not deliver in India at all until the sender id and templates are DLT
    registered.

    Recorded as its own `outbound_messages` row, so "WhatsApp failed, SMS
    succeeded" is legible afterwards rather than one row mutating channel.
    """
    if not settings.SMS_FALLBACK_ENABLED:
        return None
    if failed.channel != MessageChannel.WHATSAPP or not failed.recipient:
        return None

    service = get_sms_service()
    sms = OutboundMessage(
        channel=MessageChannel.SMS,
        provider=service.name,
        recipient=failed.recipient,
        body=failed.body,
        template_key=failed.template_key,
        status=MessageStatus.QUEUED,
        related_entity_type=failed.related_entity_type,
        related_entity_id=failed.related_entity_id,
    )
    db.add(sms)

    try:
        result = service.send(to=failed.recipient, body=failed.body or "")
    except Exception as exc:  # noqa: BLE001 - a fallback must not raise either
        logger.warning("SMS fallback to %s raised: %s", failed.recipient, exc)
        sms.status = MessageStatus.FAILED
        sms.error_message = str(exc)[:500]
        return sms

    sms.status = MessageStatus.SENT if result.success else MessageStatus.FAILED
    sms.error_message = result.error
    sms.provider_message_id = result.provider_message_id
    sms.sent_at = datetime.now(timezone.utc) if result.success else None
    return sms


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
_LOADS = (
    selectinload(Notification.acknowledgements).selectinload(
        NotificationAcknowledgement.user
    ),
)


def _visible(user: User):
    """The scoping rule, as a WHERE clause.

    Clinic Users see their own clinics plus anything addressed to them
    personally. Admin/Superadmin see the whole chain.
    """
    if permissions.has_all_clinic_access(user):
        return None
    return or_(
        Notification.clinic_id.in_(user.clinic_ids or [-1]),
        Notification.target_user_id == user.id,
    )


def list_notifications(
    db: Session,
    user: User,
    *,
    unacknowledged_only: bool = False,
    clinic_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Notification], int]:
    stmt = select(Notification).options(*_LOADS)

    scope = _visible(user)
    if scope is not None:
        stmt = stmt.where(scope)
    if clinic_id is not None:
        permissions.assert_clinic_access(db, user, clinic_id)
        stmt = stmt.where(Notification.clinic_id == clinic_id)
    if unacknowledged_only:
        stmt = stmt.where(Notification.is_acknowledged.is_(False))

    total = db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()

    rows = (
        db.execute(
            # Unacknowledged first, newest first within each group: the thing
            # needing action is always at the top.
            stmt.order_by(
                Notification.is_acknowledged.asc(), Notification.created_at.desc(), Notification.id.desc()
            )
            .offset(offset)
            .limit(limit)
        )
        .unique()
        .scalars()
        .all()
    )
    return list(rows), total


def counters(db: Session, user: User) -> dict:
    """Small enough to poll every few seconds."""
    scope = _visible(user)

    unread_stmt = select(func.count()).select_from(Notification).where(
        Notification.is_acknowledged.is_(False)
    )
    total_stmt = select(func.count()).select_from(Notification)
    latest_stmt = select(func.max(Notification.id))
    if scope is not None:
        unread_stmt = unread_stmt.where(scope)
        total_stmt = total_stmt.where(scope)
        latest_stmt = latest_stmt.where(scope)

    # The newest thing still needing action, for the floating card. One indexed
    # lookup, so it stays cheap enough to sit on a 15-second poll.
    newest_stmt = select(Notification).where(Notification.is_acknowledged.is_(False))
    if scope is not None:
        newest_stmt = newest_stmt.where(scope)
    newest = (
        db.execute(newest_stmt.order_by(Notification.id.desc()).limit(1))
        .unique()
        .scalars()
        .first()
    )

    latest = None
    if newest is not None:
        payload = newest.payload or {}
        latest = {
            "id": newest.id,
            "notification_type": newest.notification_type,
            "title": newest.title,
            "patient_name": payload.get("patient_name"),
            "clinic_name": payload.get("clinic_name") or (
                newest.clinic.name if newest.clinic else None
            ),
            "date": payload.get("date"),
            "time": payload.get("time"),
            "created_at": newest.created_at,
        }

    return {
        "unacknowledged": db.execute(unread_stmt).scalar_one(),
        "total": db.execute(total_stmt).scalar_one(),
        "latest_id": db.execute(latest_stmt).scalar_one(),
        "latest": latest,
        "alerts_enabled": permissions.is_clinic_user(user),
    }


def get_notification(db: Session, user: User, notification_id: int) -> Notification:
    notification = db.execute(
        select(Notification).options(*_LOADS).where(Notification.id == notification_id)
    ).unique().scalar_one_or_none()
    if notification is None:
        raise NotFoundError(f"Notification {notification_id} was not found")

    if not permissions.has_all_clinic_access(user):
        reachable = (
            notification.clinic_id in user.clinic_ids
            or notification.target_user_id == user.id
        )
        if not reachable:
            raise PermissionDeniedError("This notification belongs to another clinic")
    return notification


# --------------------------------------------------------------------------- #
# Acknowledgement
# --------------------------------------------------------------------------- #
def acknowledge(db: Session, user: User, notification_id: int, request=None) -> Notification:
    """Mark a notification acknowledged by this user.

    Idempotent by construction: the per-user row is unique, so acknowledging
    twice is a no-op rather than an error. Two receptionists clicking at the
    same moment is a normal thing to happen, not a conflict to report.
    """
    notification = get_notification(db, user, notification_id)

    already = db.execute(
        select(NotificationAcknowledgement).where(
            NotificationAcknowledgement.notification_id == notification.id,
            NotificationAcknowledgement.user_id == user.id,
        )
    ).scalar_one_or_none()

    if already is None:
        db.add(
            NotificationAcknowledgement(notification_id=notification.id, user_id=user.id)
        )
        # The roll-up flag is what the badge query reads; the per-user rows stay
        # the record of who actually saw it.
        notification.is_acknowledged = True
        notification.acknowledged_at = datetime.now(timezone.utc)
        audit_service.record(
            db,
            action=AuditAction.NOTIFICATION_ACKNOWLEDGED,
            user=user,
            entity_type="notification",
            entity_id=notification.id,
            clinic_id=notification.clinic_id,
            description=f"Acknowledged: {notification.title}",
            request=request,
        )
        db.commit()

    db.refresh(notification)
    return notification


def acknowledge_all(db: Session, user: User, request=None) -> int:
    """Clear the badge in one action. Returns how many were acknowledged."""
    pending, _ = list_notifications(db, user, unacknowledged_only=True, limit=500)
    if not pending:
        return 0

    now = datetime.now(timezone.utc)
    for notification in pending:
        db.add(
            NotificationAcknowledgement(notification_id=notification.id, user_id=user.id)
        )
        notification.is_acknowledged = True
        notification.acknowledged_at = now

    audit_service.record(
        db,
        action=AuditAction.NOTIFICATION_ACKNOWLEDGED,
        user=user,
        entity_type="notification",
        entity_id=None,
        description=f"Acknowledged {len(pending)} notification(s)",
        request=request,
    )
    db.commit()
    return len(pending)
