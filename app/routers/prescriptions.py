"""Prescription endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.schemas.prescription import PrescriptionCreate, PrescriptionRead, PrescriptionSend
from app.services import prescription_service

router = APIRouter(prefix="/prescriptions", tags=["prescriptions"])


@router.post("", response_model=PrescriptionRead, status_code=201)
def create_prescription(
    payload: PrescriptionCreate,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    prx = prescription_service.create(db, current_user, payload)
    db.commit()
    db.refresh(prx)
    return PrescriptionRead.model_validate(prx)


@router.get("", response_model=list[PrescriptionRead])
def list_prescriptions(
    db: DbSession,
    current_user: ClinicStaffUser,
    patient_id: int = Query(...),
):
    items = prescription_service.list_for_patient(db, patient_id)
    return [PrescriptionRead.model_validate(p) for p in items]


@router.get("/{prescription_id}", response_model=PrescriptionRead)
def get_prescription(
    prescription_id: int,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    prx = prescription_service.get(db, prescription_id)
    return PrescriptionRead.model_validate(prx)


@router.get("/{prescription_id}/prescription.pdf")
def download_prescription_pdf(
    prescription_id: int,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    filename, content = prescription_service.build_pdf(db, prescription_id)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/{prescription_id}/send")
def send_prescription(
    prescription_id: int,
    payload: PrescriptionSend,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    if payload.channel == "whatsapp":
        result = prescription_service.send_whatsapp(
            db, current_user, prescription_id, to_phone=payload.phone
        )
    else:
        result = prescription_service.send_email(
            db, current_user, prescription_id, to_email=payload.email
        )
    db.commit()
    return result
