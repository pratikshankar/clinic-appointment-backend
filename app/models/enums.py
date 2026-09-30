"""Enumerations shared by models and schemas.

All enums are persisted as VARCHAR + CHECK constraint (`native_enum=False`)
rather than as native PostgreSQL ENUM types. That keeps SQLite and PostgreSQL
byte-for-byte compatible and avoids the `ALTER TYPE` migration pain that native
enums cause when a new value is added later.
"""

from enum import Enum


class StrEnum(str, Enum):
    """String-valued enum so FastAPI/Pydantic serialise the value, not the name."""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


class RoleName(StrEnum):
    SUPERADMIN = "SUPERADMIN"
    ADMIN = "ADMIN"
    CLINIC_USER = "CLINIC_USER"


class ClinicStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class Gender(StrEnum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"
    UNDISCLOSED = "UNDISCLOSED"


class AppointmentStatus(StrEnum):
    BOOKED = "BOOKED"
    CONFIRMED = "CONFIRMED"
    CHECKED_IN = "CHECKED_IN"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
    NO_SHOW = "NO_SHOW"


#: Statuses that still occupy a slot for capacity purposes.
CAPACITY_CONSUMING_STATUSES: tuple[AppointmentStatus, ...] = (
    AppointmentStatus.BOOKED,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.CHECKED_IN,
    AppointmentStatus.COMPLETED,
)


class AppointmentAction(StrEnum):
    CREATED = "CREATED"
    CONFIRMED = "CONFIRMED"
    CHECKED_IN = "CHECKED_IN"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
    NO_SHOW = "NO_SHOW"
    UPDATED = "UPDATED"


class PackageStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class BillStatus(StrEnum):
    DRAFT = "DRAFT"
    FINALIZED = "FINALIZED"
    CANCELLED = "CANCELLED"


class PaymentStatus(StrEnum):
    UNPAID = "UNPAID"
    PARTIAL = "PARTIAL"
    PAID = "PAID"


class PaymentMethod(StrEnum):
    CASH = "CASH"
    UPI = "UPI"
    CARD = "CARD"
    BANK_TRANSFER = "BANK_TRANSFER"
    OTHER = "OTHER"
    POINTS = "POINTS"  # Physio Points redemption


class BillItemType(StrEnum):
    SESSION_PACKAGE = "SESSION_PACKAGE"
    CONSULTATION = "CONSULTATION"
    PRODUCT = "PRODUCT"
    OTHER = "OTHER"


class AdjustmentType(StrEnum):
    CREDIT_NOTE = "CREDIT_NOTE"
    CANCELLATION = "CANCELLATION"
    CORRECTION = "CORRECTION"


class PointsTransactionType(StrEnum):
    EARNED = "EARNED"
    REDEEMED = "REDEEMED"
    REFUND_REVOKE = "REFUND_REVOKE"
    REFUND_RESTORE = "REFUND_RESTORE"
    MANUAL_CREDIT = "MANUAL_CREDIT"
    MANUAL_DEBIT = "MANUAL_DEBIT"
    EXPIRED = "EXPIRED"


class NotificationType(StrEnum):
    NEW_APPOINTMENT = "NEW_APPOINTMENT"
    RESCHEDULED_APPOINTMENT = "RESCHEDULED_APPOINTMENT"
    CANCELLED_APPOINTMENT = "CANCELLED_APPOINTMENT"
    APPOINTMENT_REMINDER = "APPOINTMENT_REMINDER"
    BILL_GENERATED = "BILL_GENERATED"
    SYSTEM = "SYSTEM"


class MessageChannel(StrEnum):
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    SMS = "SMS"
    IN_APP = "IN_APP"


class MessageStatus(StrEnum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class RefundStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    COMPLETED = "COMPLETED"


class RefundPaymentMethod(StrEnum):
    BANK_TRANSFER = "BANK_TRANSFER"
    UPI = "UPI"


class RefundAttachmentType(StrEnum):
    CANCELLATION_DOCUMENT = "CANCELLATION_DOCUMENT"
    PAYMENT_PROOF = "PAYMENT_PROOF"


class ReferralStatus(StrEnum):
    PENDING = "PENDING"      # referred patient registered; waiting for credit threshold
    CREDITED = "CREDITED"    # credit applied to referrer's package
    VOIDED = "VOIDED"        # referred package cancelled/refunded before credit applied


class AuditAction(StrEnum):
    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    DELETED = "DELETED"
    APPOINTMENT_CREATED = "APPOINTMENT_CREATED"
    APPOINTMENT_RESCHEDULED = "APPOINTMENT_RESCHEDULED"
    APPOINTMENT_CANCELLED = "APPOINTMENT_CANCELLED"
    SESSION_RECORDED = "SESSION_RECORDED"
    BILL_GENERATED = "BILL_GENERATED"
    PAYMENT_RECORDED = "PAYMENT_RECORDED"
    NOTIFICATION_ACKNOWLEDGED = "NOTIFICATION_ACKNOWLEDGED"
    REFUND_INITIATED = "REFUND_INITIATED"
    REFUND_APPROVED = "REFUND_APPROVED"
    REFUND_REJECTED = "REFUND_REJECTED"
    REFUND_COMPLETED = "REFUND_COMPLETED"
    REFERRAL_CREATED = "REFERRAL_CREATED"
    REFERRAL_CREDITED = "REFERRAL_CREDITED"
    REFERRAL_VOIDED = "REFERRAL_VOIDED"
