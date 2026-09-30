"""Refund request endpoints."""

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
import io

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.models.enums import RefundAttachmentType
from app.schemas.refund import (
    RefundCalculationUpdate,
    RefundComplete,
    RefundInitiate,
    RefundListItem,
    RefundPrefill,
    RefundRequestOut,
    RefundReview,
)
from app.services import refund_service

router = APIRouter(prefix="/refunds", tags=["refunds"])


@router.get("/prefill/{package_id}", response_model=RefundPrefill)
def prefill_refund(package_id: int, db: DbSession, current_user: ClinicStaffUser):
    """Return pre-calculated values for the refund initiation form."""
    return refund_service.prefill(db, current_user, package_id)


@router.post("", response_model=RefundRequestOut, status_code=201)
def initiate_refund(
    payload: RefundInitiate,
    db: DbSession,
    current_user: ClinicStaffUser,
    request: Request,
):
    """Initiate a refund request.

    Clinic Users: request goes to PENDING_APPROVAL.
    Admin / Superadmin: auto-approved on creation.
    """
    result = refund_service.initiate(db, current_user, payload, request)
    return RefundRequestOut.from_orm(result)


@router.get("", response_model=list[RefundListItem])
def list_refunds(
    db: DbSession,
    current_user: ClinicStaffUser,
    status: str | None = Query(default=None),
    clinic_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    items, _total = refund_service.list_refunds(
        db, current_user, status=status, clinic_id=clinic_id, page=page, page_size=page_size
    )
    return [RefundListItem.from_orm(r) for r in items]


@router.get("/{refund_id}", response_model=RefundRequestOut)
def get_refund(refund_id: int, db: DbSession, current_user: ClinicStaffUser):
    r = refund_service.get_refund(db, current_user, refund_id)
    return RefundRequestOut.from_orm(r)


@router.put("/{refund_id}/calculation", response_model=RefundRequestOut)
def update_calculation(
    refund_id: int,
    payload: RefundCalculationUpdate,
    db: DbSession,
    current_user: ClinicStaffUser,
    request: Request,
):
    """Update the refund calculation before approval. Open to both staff and admin."""
    r = refund_service.update_calculation(db, current_user, refund_id, payload, request)
    return RefundRequestOut.from_orm(r)


@router.post("/{refund_id}/review", response_model=RefundRequestOut)
def review_refund(
    refund_id: int,
    payload: RefundReview,
    db: DbSession,
    current_user: ClinicStaffUser,
    request: Request,
):
    """Admin / Superadmin approves or rejects a pending refund."""
    r = refund_service.review(db, current_user, refund_id, payload, request)
    return RefundRequestOut.from_orm(r)


@router.post("/{refund_id}/complete", response_model=RefundRequestOut)
def complete_refund(
    refund_id: int,
    payload: RefundComplete,
    db: DbSession,
    current_user: ClinicStaffUser,
    request: Request,
):
    """Admin marks refund as completed after transferring the money."""
    r = refund_service.complete(db, current_user, refund_id, payload, request)
    return RefundRequestOut.from_orm(r)


@router.post("/{refund_id}/attachments", status_code=201)
async def upload_attachment(
    refund_id: int,
    db: DbSession,
    current_user: ClinicStaffUser,
    file: UploadFile = File(...),
    attachment_type: RefundAttachmentType = Form(...),
):
    """Upload a cancellation document or payment proof."""
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:  # 10 MB cap
        from app.utils.exceptions import ValidationError
        raise ValidationError("File must be under 10 MB")

    attachment = refund_service.add_attachment(
        db,
        current_user,
        refund_id,
        attachment_type=attachment_type,
        file_name=file.filename or "upload",
        mime_type=file.content_type or "application/octet-stream",
        file_content=content,
    )
    return {"id": attachment.id, "file_name": attachment.file_name}


@router.get("/{refund_id}/attachments/{attachment_id}")
def download_attachment(
    refund_id: int,
    attachment_id: int,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Download a specific attachment."""
    attachment = refund_service.get_attachment_content(
        db, current_user, refund_id, attachment_id
    )
    return StreamingResponse(
        io.BytesIO(attachment.file_content),
        media_type=attachment.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{attachment.file_name}"'},
    )
