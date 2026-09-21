"""Bills, payments and the service catalogue (Sections 17-19).

The endpoints are deliberately few. A bill is created finalised with its lines
and (usually) its payment in one call, because that mirrors what happens at the
desk: the patient is charged and pays in the same moment. Editing a finalised
bill is not offered -- corrections go through adjustments in Phase 7.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.models.enums import MessageChannel, PaymentStatus
from app.schemas.billing import (
    BillCounters,
    BillCreate,
    BillRead,
    BillSummaryRow,
    PaymentInput,
    ServiceItemCreate,
    ServiceItemRead,
    ServiceItemUpdate,
)
from app.schemas.common import Page
from app.schemas.document import DeliveryResult, SendRequest
from app.services import billing_service, document_service

router = APIRouter(prefix="/bills", tags=["Billing"])
patient_router = APIRouter(prefix="/patients", tags=["Billing"])
catalogue_router = APIRouter(prefix="/service-items", tags=["Billing"])
payment_router = APIRouter(prefix="/payments", tags=["Documents"])
document_router = APIRouter(prefix="/packages", tags=["Documents"])


def _pdf(filename: str, content: bytes) -> Response:
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            # `inline` so a click opens the browser's PDF viewer, where the
            # staff member can eyeball it before printing or sending; the
            # filename is still what a Save produces.
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(len(content)),
        },
    )


# --------------------------------------------------------------------------- #
# Service catalogue
# --------------------------------------------------------------------------- #
@catalogue_router.get(
    "", response_model=list[ServiceItemRead], summary="Chargeable services"
)
def list_service_items(
    db: DbSession,
    current_user: ClinicStaffUser,
    clinic_id: int | None = None,
    include_inactive: bool = True,
):
    """Entries for one clinic include the chain-wide ones (`clinic_id: null`)."""
    items = billing_service.list_service_items(
        db, current_user, clinic_id=clinic_id, include_inactive=include_inactive
    )
    return [ServiceItemRead.model_validate(item) for item in items]


@catalogue_router.post(
    "",
    response_model=ServiceItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a chargeable service",
)
def create_service_item(
    payload: ServiceItemCreate,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    item = billing_service.create_service_item(db, current_user, payload, request=request)
    return ServiceItemRead.model_validate(item)


@catalogue_router.put(
    "/{service_item_id}", response_model=ServiceItemRead, summary="Edit a service"
)
def update_service_item(
    service_item_id: int,
    payload: ServiceItemUpdate,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Repricing affects future bills only: past bills keep the price they were
    issued at, because the line stores its own price."""
    item = billing_service.update_service_item(
        db, current_user, service_item_id, payload, request=request
    )
    return ServiceItemRead.model_validate(item)


# --------------------------------------------------------------------------- #
# Bills
# --------------------------------------------------------------------------- #
@patient_router.get(
    "/{patient_id}/bills", response_model=list[BillRead], summary="Bills for a patient"
)
def list_patient_bills(patient_id: int, db: DbSession, current_user: ClinicStaffUser):
    bills, _ = billing_service.list_bills(
        db, current_user, patient_id=patient_id, limit=200
    )
    return [BillRead.model_validate(bill) for bill in bills]


@patient_router.post(
    "/{patient_id}/bills",
    response_model=BillRead,
    status_code=status.HTTP_201_CREATED,
    summary="Charge a patient (consultation, add-on therapy, product)",
)
def create_bill(
    patient_id: int,
    payload: BillCreate,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Use this for anything charged outside a package: a consultation fee, a
    one-off laser therapy session taken mid-course, or a product.

    An add-on service billed here does **not** consume a package session -- it is
    charged separately, so the package's remaining count is untouched."""
    bill = billing_service.create_bill(db, current_user, patient_id, payload, request=request)
    return BillRead.model_validate(bill)


@router.get("", response_model=Page[BillSummaryRow], summary="List bills")
def list_bills(
    db: DbSession,
    current_user: ClinicStaffUser,
    clinic_id: int | None = None,
    patient_id: int | None = None,
    payment_status: PaymentStatus | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
):
    bills, total = billing_service.list_bills(
        db,
        current_user,
        clinic_id=clinic_id,
        patient_id=patient_id,
        payment_status=payment_status,
        date_from=date_from,
        date_to=date_to,
        search=search,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[BillSummaryRow](
        items=[BillSummaryRow.model_validate(bill) for bill in bills],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/counters", response_model=BillCounters, summary="Billed, collected and outstanding"
)
def billing_counters(
    db: DbSession, current_user: ClinicStaffUser, clinic_id: int | None = None
):
    return BillCounters(**billing_service.billing_counters(db, current_user, clinic_id))


@router.get("/{bill_id}", response_model=BillRead, summary="One bill with its lines")
def get_bill(bill_id: int, db: DbSession, current_user: ClinicStaffUser):
    return BillRead.model_validate(billing_service.get_bill(db, current_user, bill_id))


@router.post(
    "/{bill_id}/payments",
    response_model=BillRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a payment against a bill",
)
def add_payment(
    bill_id: int,
    payload: PaymentInput,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Payments accumulate; the bill's status moves UNPAID -> PARTIAL -> PAID on
    its own and is never set by hand."""
    bill = billing_service.add_payment(db, current_user, bill_id, payload, request=request)
    return BillRead.model_validate(bill)


# --------------------------------------------------------------------------- #
# Documents (Section 19)
#
# Three documents, each downloadable and each sendable:
#
#   invoice    one per bill      -- everything charged, and what is still owed
#   receipt    one per payment   -- what the patient handed over today
#   statement  one per package   -- session dates and amounts, for an insurer
# --------------------------------------------------------------------------- #
@router.get("/{bill_id}/invoice.pdf", summary="Download the invoice PDF")
def download_invoice(bill_id: int, db: DbSession, current_user: ClinicStaffUser):
    filename, content, _ = document_service.invoice_pdf(db, current_user, bill_id)
    return _pdf(filename, content)


@router.post(
    "/{bill_id}/send",
    response_model=DeliveryResult,
    summary="Email or WhatsApp the invoice to the patient",
)
def send_invoice(
    bill_id: int,
    payload: SendRequest,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """A provider failure returns 200 with `status: FAILED`, not an error page --
    the invoice is unaffected and the next step is another channel."""
    filename, content, bill = document_service.invoice_pdf(db, current_user, bill_id)
    subject, body = document_service.invoice_message(bill)
    message = document_service.send_document(
        db,
        current_user,
        channel=payload.channel,
        filename=filename,
        content=content,
        patient=bill.patient,
        subject=subject,
        body=body,
        recipient_override=payload.recipient,
        entity_type="bill",
        entity_id=bill.id,
        clinic_id=bill.clinic_id,
        request=request,
    )
    result = DeliveryResult.model_validate(message)
    result.filename = filename
    return result


@payment_router.get("/{payment_id}/receipt.pdf", summary="Download the payment receipt")
def download_receipt(payment_id: int, db: DbSession, current_user: ClinicStaffUser):
    """One receipt per payment: the document the patient is handed for the amount
    they just paid, separate from the invoice for the whole bill."""
    filename, content, _ = document_service.receipt_pdf(db, current_user, payment_id)
    return _pdf(filename, content)


@payment_router.post(
    "/{payment_id}/send",
    response_model=DeliveryResult,
    summary="Email or WhatsApp the receipt to the patient",
)
def send_receipt(
    payment_id: int,
    payload: SendRequest,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    filename, content, bill = document_service.receipt_pdf(db, current_user, payment_id)
    payment = next(item for item in bill.payments if item.id == payment_id)
    subject, body = document_service.receipt_message(payment, bill)
    message = document_service.send_document(
        db,
        current_user,
        channel=payload.channel,
        filename=filename,
        content=content,
        patient=bill.patient,
        subject=subject,
        body=body,
        recipient_override=payload.recipient,
        entity_type="payment",
        entity_id=payment_id,
        clinic_id=bill.clinic_id,
        request=request,
    )
    result = DeliveryResult.model_validate(message)
    result.filename = filename
    return result


@document_router.get(
    "/{package_id}/statement.pdf", summary="Download the treatment statement"
)
def download_statement(package_id: int, db: DbSession, current_user: ClinicStaffUser):
    """Session-by-session record with the amounts billed against it, for an
    insurance claim. Marked PROVISIONAL until every session is delivered, so a
    mid-course copy cannot be mistaken for a final claim document."""
    filename, content, _ = document_service.statement_pdf(db, current_user, package_id)
    return _pdf(filename, content)


@document_router.post(
    "/{package_id}/send-statement",
    response_model=DeliveryResult,
    summary="Email or WhatsApp the treatment statement",
)
def send_statement(
    package_id: int,
    payload: SendRequest,
    request: Request,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    from app.services import session_service

    package = session_service.get_package(db, current_user, package_id)
    filename, content, _ = document_service.statement_pdf(db, current_user, package_id)
    subject, body = document_service.statement_message(package)
    message = document_service.send_document(
        db,
        current_user,
        channel=payload.channel,
        filename=filename,
        content=content,
        patient=package.patient,
        subject=subject,
        body=body,
        recipient_override=payload.recipient,
        entity_type="treatment_package",
        entity_id=package_id,
        clinic_id=package.clinic_id,
        request=request,
    )
    result = DeliveryResult.model_validate(message)
    result.filename = filename
    return result
