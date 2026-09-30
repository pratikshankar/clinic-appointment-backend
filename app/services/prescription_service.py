"""Prescription CRUD and PDF assembly."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.factory import get_email_service, get_whatsapp_service
from app.models import MessageChannel, MessageStatus, OutboundMessage, User
from app.models.clinic import Clinic
from app.models.patient import Patient
from app.models.prescription import Prescription
from app.schemas.prescription import PrescriptionCreate
from app.services import audit_service, pdf_service
from app.utils.exceptions import NotFoundError

logger = logging.getLogger(__name__)


def create(db: Session, current_user: User, payload: PrescriptionCreate) -> Prescription:
    prx = Prescription(**payload.model_dump())
    db.add(prx)
    db.flush()
    db.refresh(prx)
    return prx


def list_for_patient(db: Session, patient_id: int) -> list[Prescription]:
    return list(
        db.execute(
            select(Prescription)
            .where(Prescription.patient_id == patient_id)
            .order_by(Prescription.created_at.desc())
        ).scalars().all()
    )


def get(db: Session, prescription_id: int) -> Prescription:
    prx = db.get(Prescription, prescription_id)
    if prx is None:
        raise NotFoundError(f"Prescription {prescription_id} not found")
    return prx


def build_pdf(db: Session, prescription_id: int) -> tuple[str, bytes]:
    prx = get(db, prescription_id)
    patient = db.get(Patient, prx.patient_id)
    clinic = db.get(Clinic, prx.clinic_id) if prx.clinic_id else None
    physio = db.get(User, prx.prescribed_by_user_id) if prx.prescribed_by_user_id else None
    content = pdf_service.render_prescription(prx, patient, clinic, physio)
    filename = f"Prescription-{prx.id}-{(patient.patient_code or 'PT') if patient else 'PT'}.pdf"
    return filename, content


def send_email(
    db: Session, current_user: User, prescription_id: int, to_email: str | None = None
) -> dict:
    prx = get(db, prescription_id)
    patient = db.get(Patient, prx.patient_id)
    if patient is None:
        raise NotFoundError("Patient not found")

    recipient = (to_email or "").strip() or (patient.email or "").strip()
    if not recipient:
        return {"success": False, "error": "No email address available for this patient"}

    filename, content = build_pdf(db, prescription_id)

    service = get_email_service()
    if hasattr(service, "send_prescription"):
        result = service.send_prescription(
            to=recipient,
            patient_name=patient.full_name,
            attachments=[(filename, content)],
        )
    else:
        result = service.send(
            to=recipient,
            subject=f"Your Prescription — {patient.full_name}",
            body=(
                f"Dear {patient.full_name},\n\n"
                "Please find your prescription attached.\n\n"
                "If you have any questions, please contact your clinic.\n\n"
                "Regards,\nYour Care Team"
            ),
            attachments=[(filename, content)],
        )

    status = MessageStatus.SENT if result.success else MessageStatus.FAILED
    db.add(
        OutboundMessage(
            channel=MessageChannel.EMAIL,
            recipient=recipient,
            subject=filename,
            status=status,
            provider=result.provider or "unknown",
            provider_message_id=result.provider_message_id,
            error=result.error,
            sent_at=datetime.now(timezone.utc) if result.success else None,
            sent_by_user_id=current_user.id,
        )
    )
    db.flush()
    return {"success": result.success, "error": result.error}


def send_whatsapp(
    db: Session, current_user: User, prescription_id: int, to_phone: str | None = None
) -> dict:
    prx = get(db, prescription_id)
    patient = db.get(Patient, prx.patient_id)
    if patient is None:
        raise NotFoundError("Patient not found")

    recipient = (to_phone or "").strip() or (getattr(patient, "whatsapp_contact", None) or "").strip()
    if not recipient:
        return {
            "success": False,
            "error": "No WhatsApp number available for this patient. Add one to their profile or enter it manually.",
        }

    filename, content = build_pdf(db, prescription_id)
    caption = f"Prescription for {patient.full_name} — please find it attached."

    service = get_whatsapp_service()
    result = service.send_document(to=recipient, filename=filename, content=content, caption=caption)

    status = MessageStatus.SENT if result.success else MessageStatus.FAILED
    db.add(
        OutboundMessage(
            channel=MessageChannel.WHATSAPP,
            recipient=recipient,
            subject=None,
            status=status,
            provider=result.provider or "unknown",
            provider_message_id=result.provider_message_id,
            error=result.error,
            sent_at=datetime.now(timezone.utc) if result.success else None,
            sent_by_user_id=current_user.id,
        )
    )
    db.flush()
    return {"success": result.success, "error": result.error}
