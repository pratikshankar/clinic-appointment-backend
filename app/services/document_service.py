"""Building and delivering billing documents (Sections 16 and 19).

Splits cleanly in two:

* **Assembling** a document — load exactly what the renderer needs, enforce
  clinic scoping, hand back `(filename, bytes)`.
* **Delivering** it — write an `outbound_messages` row, hand the file to a
  provider, record what happened.

Delivery never raises for a provider fault. A failed send is recorded as
`FAILED` and reported to the caller as an unsuccessful result, not as a 500:
the invoice itself is unaffected, and the receptionist's next move is to try
another channel, not to read a stack trace.
"""

import logging
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.integrations.factory import get_email_service, get_whatsapp_service
from app.models import (
    AuditAction,
    Bill,
    MessageChannel,
    MessageStatus,
    OutboundMessage,
    Patient,
    PatientSession,
    Payment,
    TreatmentPackage,
    User,
)
from app.services import audit_service, billing_service, pdf_service
from app.utils.exceptions import NotFoundError, ValidationError

logger = logging.getLogger(__name__)

INVOICE = "invoice"
RECEIPT = "receipt"
STATEMENT = "statement"


# --------------------------------------------------------------------------- #
# Assembling
# --------------------------------------------------------------------------- #
def invoice_pdf(db: Session, user: User, bill_id: int) -> tuple[str, bytes, Bill]:
    bill = billing_service.get_bill(db, user, bill_id)
    return f"Invoice-{bill.bill_number}.pdf", pdf_service.render_invoice(bill), bill


def receipt_pdf(db: Session, user: User, payment_id: int) -> tuple[str, bytes, Bill]:
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise NotFoundError(f"Payment {payment_id} was not found")
    # Scoping is the bill's, checked through the same choke point as everything
    # else -- a payment is only reachable via a bill the caller may see.
    bill = billing_service.get_bill(db, user, payment.bill_id)
    return (
        f"Receipt-{pdf_service.receipt_number(payment, bill)}.pdf",
        pdf_service.render_receipt(payment, bill),
        bill,
    )


def statement_pdf(db: Session, user: User, package_id: int) -> tuple[str, bytes, Bill | None]:
    """The insurance document: sessions delivered, with what was billed for them."""
    package = db.execute(
        select(TreatmentPackage)
        .options(
            selectinload(TreatmentPackage.patient),
            selectinload(TreatmentPackage.clinic),
            selectinload(TreatmentPackage.sessions).selectinload(PatientSession.therapist),
        )
        .where(TreatmentPackage.id == package_id)
    ).unique().scalars().first()
    if package is None:
        raise NotFoundError(f"Package {package_id} was not found")

    from app.auth import permissions

    permissions.assert_clinic_access(db, user, package.clinic_id)

    bills = list(
        db.execute(
            select(Bill)
            .options(selectinload(Bill.payments))
            .where(Bill.package_id == package.id)
            .order_by(Bill.bill_date, Bill.id)
        )
        .unique()
        .scalars()
        .all()
    )

    sessions = sorted(package.sessions, key=lambda item: item.session_number)
    filename = (
        f"Treatment-statement-{package.patient.patient_code}-{package.id}.pdf"
        if package.patient
        else f"Treatment-statement-{package.id}.pdf"
    )
    return filename, pdf_service.render_session_statement(package, sessions, bills), None


# --------------------------------------------------------------------------- #
# Delivering
# --------------------------------------------------------------------------- #
def _recipient(patient: Patient | None, channel: MessageChannel, override: str | None) -> str:
    if override:
        return override.strip()
    if patient is None:
        raise ValidationError("No recipient could be determined for this document")

    if channel == MessageChannel.EMAIL:
        if not patient.email:
            raise ValidationError(
                f"{patient.full_name} has no email address on file. "
                "Add one to their profile, or send by WhatsApp."
            )
        return patient.email
    return patient.whatsapp_contact


def send_document(
    db: Session,
    user: User,
    *,
    channel: MessageChannel,
    filename: str,
    content: bytes,
    patient: Patient | None,
    subject: str,
    body: str,
    recipient_override: str | None = None,
    entity_type: str,
    entity_id: int,
    clinic_id: int | None = None,
    request: Request | None = None,
) -> OutboundMessage:
    """Send one document and record the attempt. Never raises for a provider fault."""
    if channel not in (MessageChannel.EMAIL, MessageChannel.WHATSAPP):
        raise ValidationError(f"{channel.value} delivery is not available yet")

    to = _recipient(patient, channel, recipient_override)

    service = get_email_service() if channel == MessageChannel.EMAIL else get_whatsapp_service()
    message = OutboundMessage(
        channel=channel,
        provider=service.name,
        recipient=to,
        subject=subject if channel == MessageChannel.EMAIL else None,
        body=body,
        template_key=f"{entity_type}_document",
        status=MessageStatus.QUEUED,
        related_entity_type=entity_type,
        related_entity_id=entity_id,
    )
    db.add(message)
    db.flush()

    try:
        if channel == MessageChannel.EMAIL:
            # Use the bill/invoice template for all financial documents.
            is_bill = entity_type in ("bill", "payment", "package", "treatment_package")
            if is_bill and hasattr(service, "send_bill"):
                result = service.send_bill(  # type: ignore[union-attr]
                    to=to,
                    patient_name=patient.full_name if patient else to,
                    attachments=[(filename, content)],
                )
            else:
                result = service.send(
                    to=to, subject=subject, body=body, attachments=[(filename, content)]
                )
        else:
            result = service.send_document(
                to=to, filename=filename, content=content, caption=body
            )
    except Exception as exc:  # noqa: BLE001 - provider faults are data, not crashes
        logger.warning("Sending %s to %s raised: %s", filename, to, exc)
        message.status = MessageStatus.FAILED
        message.error_message = str(exc)[:500]
    else:
        message.status = MessageStatus.SENT if result.success else MessageStatus.FAILED
        message.error_message = result.error
        message.provider_message_id = result.provider_message_id
        message.sent_at = datetime.now(timezone.utc) if result.success else None

    audit_service.record(
        db,
        action=AuditAction.UPDATED,
        user=user,
        entity_type=entity_type,
        entity_id=entity_id,
        clinic_id=clinic_id,
        description=(
            f"{filename} {message.status.value.lower()} to {to} via {channel.value}"
        ),
        request=request,
    )
    db.commit()
    db.refresh(message)
    return message


# --------------------------------------------------------------------------- #
# Message copy
# --------------------------------------------------------------------------- #
def invoice_message(bill) -> tuple[str, str]:
    subject = f"Invoice {bill.bill_number} from {_brand_of(bill)}"
    body = (
        f"Dear {bill.patient.full_name if bill.patient else 'patient'},\n\n"
        f"Please find attached invoice {bill.bill_number} dated "
        f"{bill.bill_date:%d-%b-%Y} for {pdf_service.money(bill.total_amount)}.\n"
    )
    if bill.balance_amount > 0:
        body += f"Outstanding balance: {pdf_service.money(bill.balance_amount)}.\n"
    else:
        body += "This invoice is fully paid. Thank you.\n"
    body += f"\n{_brand_of(bill)}\n{bill.clinic.name if bill.clinic else ''}"
    return subject, body


def receipt_message(payment, bill) -> tuple[str, str]:
    subject = f"Receipt {pdf_service.receipt_number(payment, bill)} from {_brand_of(bill)}"
    body = (
        f"Dear {bill.patient.full_name if bill.patient else 'patient'},\n\n"
        f"Thank you. We have received {pdf_service.money(payment.amount)} on "
        f"{payment.payment_date:%d-%b-%Y} by "
        f"{pdf_service.payment_method_label(payment.payment_method)}.\n"
        f"The receipt is attached.\n\n{_brand_of(bill)}"
    )
    return subject, body


def statement_message(package) -> tuple[str, str]:
    patient = package.patient
    final = package.sessions_taken >= package.sessions_registered
    subject = f"Treatment statement from {_brand_of(package)}"
    body = (
        f"Dear {patient.full_name if patient else 'patient'},\n\n"
        f"Attached is your treatment statement listing the "
        f"{package.sessions_taken} session(s) delivered and the amounts billed, "
        "for your records or for an insurance claim.\n"
    )
    if not final:
        body += (
            "\nNote: this course is still in progress, so the statement is marked "
            "provisional. A final statement can be issued once it is complete.\n"
        )
    body += f"\n{_brand_of(package)}"
    return subject, body


def _brand_of(record) -> str:
    """The name the *issuing clinic* trades under.

    An email from a separately-branded clinic signed with the chain name would
    look like a phishing attempt to the patient who chose that clinic.
    """
    return pdf_service.brand_for(getattr(record, "clinic", None)).name
