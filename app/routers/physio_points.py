"""Physio Points loyalty programme endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.auth.dependencies import AdminOrSuperadminUser, ClinicStaffUser, DbSession
from app.config import settings
from app.services import physio_points_service

router = APIRouter(prefix="/patients/{patient_id}/physio-points", tags=["physio-points"])


@router.get("")
def get_points(patient_id: int, db: DbSession, current_user: ClinicStaffUser):
    balance = physio_points_service.get_balance(db, patient_id)
    db.commit()  # persist any expiry entries created lazily above
    ledger = physio_points_service.get_ledger(db, patient_id)
    return {
        "balance": balance,
        "redeem_value": settings.PHYSIO_POINTS_REDEEM_VALUE,
        "earn_per_100": settings.PHYSIO_POINTS_EARN_PER_100,
        "expiry_days": settings.PHYSIO_POINTS_EXPIRY_DAYS,
        "ledger": [
            {
                "id": e.id,
                "points": e.points,
                "transaction_type": e.transaction_type,
                "description": e.description,
                "created_at": e.created_at.isoformat(),
                "created_by": e.created_by.full_name if e.created_by else None,
            }
            for e in ledger
        ],
    }


@router.post("/adjust")
def adjust_points(
    patient_id: int,
    db: DbSession,
    current_user: AdminOrSuperadminUser,
    points: int = Query(..., description="Positive to credit, negative to debit"),
    reason: str = Query(..., min_length=5, max_length=500),
):
    entry = physio_points_service.manual_adjust(db, patient_id, points, reason, current_user)
    db.commit()
    return {
        "id": entry.id,
        "points": entry.points,
        "transaction_type": entry.transaction_type,
        "description": entry.description,
    }
