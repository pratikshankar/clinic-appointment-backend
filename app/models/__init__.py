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
    RoleName,
)
from app.models.notification import Notification, NotificationAcknowledgement
from app.models.patient import Patient, PatientSource
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
    "Patient",
    "PatientSource",
    "ServiceItem",
    "PatientSession",
    "TreatmentPackage",
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
    "RoleName",
]
