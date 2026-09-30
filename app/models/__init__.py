"""ORM models.

Importing this package registers every mapper on `Base.metadata`, which is what
Alembic autogenerate and `create_all()` rely on. Import models from here rather
than from the individual modules.
"""

from app.models.appointment import Appointment, AppointmentHistory
from app.models.audit import AuditLog, OutboundMessage
from app.models.billing import Bill, BillAdjustment, BillItem, Payment
from app.models.clinic import Clinic, ClinicBreak, ClinicHoliday, ClinicWorkingHour
from app.models.enums import (
    CAPACITY_CONSUMING_STATUSES,
    AdjustmentType,
    PointsTransactionType,
    AppointmentAction,
    AppointmentStatus,
    AuditAction,
    BillItemType,
    BillStatus,
    ClinicStatus,
    Gender,
    MessageChannel,
    MessageStatus,
    NotificationType,
    PackageStatus,
    PaymentMethod,
    PaymentStatus,
    RefundAttachmentType,
    RefundPaymentMethod,
    RefundStatus,
    ReferralStatus,
    RoleName,
)
from app.models.notification import Notification, NotificationAcknowledgement
from app.models.push_subscription import PushSubscription
from app.models.patient import Patient, PatientSource
from app.models.prescription import Prescription
from app.models.physio_points import PhysioPointsLedger
from app.models.refund import RefundAttachment, RefundRequest
from app.models.referral import ReferralRecord
from app.models.service_item import ServiceItem
from app.models.session import PatientSession, TreatmentPackage
from app.models.user import ClinicUser, Role, User

__all__ = [
    "Appointment",
    "AppointmentHistory",
    "AuditLog",
    "OutboundMessage",
    "Bill",
    "BillAdjustment",
    "BillItem",
    "Payment",
    "Clinic",
    "ClinicBreak",
    "ClinicHoliday",
    "ClinicWorkingHour",
    "ClinicUser",
    "Role",
    "User",
    "Notification",
    "NotificationAcknowledgement",
    "PushSubscription",
    "Patient",
    "PatientSource",
    "Prescription",
    "PhysioPointsLedger",
    "PointsTransactionType",
    "ServiceItem",
    "PatientSession",
    "TreatmentPackage",
    "RefundRequest",
    "RefundAttachment",
    "ReferralRecord",
    # enums
    "CAPACITY_CONSUMING_STATUSES",
    "AdjustmentType",
    "AppointmentAction",
    "AppointmentStatus",
    "AuditAction",
    "BillItemType",
    "BillStatus",
    "ClinicStatus",
    "Gender",
    "MessageChannel",
    "MessageStatus",
    "NotificationType",
    "PackageStatus",
    "PaymentMethod",
    "PaymentStatus",
    "RefundAttachmentType",
    "RefundPaymentMethod",
    "RefundStatus",
    "ReferralStatus",
    "RoleName",
]
