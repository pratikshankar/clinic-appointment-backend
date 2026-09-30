"""Web Push subscription management."""

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.auth.dependencies import ClinicStaffUser, DbSession
from app.config import settings
from app.models.push_subscription import PushSubscription

router = APIRouter(prefix="/push", tags=["Push"])


class SubscribeRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


class VapidKeyResponse(BaseModel):
    public_key: str
    enabled: bool


@router.get("/vapid-public-key", response_model=VapidKeyResponse)
def vapid_public_key():
    """Return the VAPID public key so the browser can subscribe."""
    return VapidKeyResponse(
        public_key=settings.VAPID_PUBLIC_KEY,
        enabled=bool(settings.VAPID_PUBLIC_KEY),
    )


@router.post("/subscribe", status_code=204)
def subscribe(
    payload: SubscribeRequest,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Register (or refresh) a push subscription for this device."""
    clinic_id = (
        current_user.clinic_ids[0] if current_user.clinic_ids else None
    )

    # Upsert: same endpoint → update keys in place.
    existing = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint)
    ).scalar_one_or_none()

    if existing:
        existing.p256dh = payload.p256dh
        existing.auth = payload.auth
        existing.clinic_id = clinic_id
    else:
        db.add(
            PushSubscription(
                user_id=current_user.id,
                clinic_id=clinic_id,
                endpoint=payload.endpoint,
                p256dh=payload.p256dh,
                auth=payload.auth,
            )
        )
    db.commit()


@router.post("/unsubscribe", status_code=204)
def unsubscribe(
    payload: SubscribeRequest,
    db: DbSession,
    current_user: ClinicStaffUser,
):
    """Remove a push subscription."""
    db.execute(
        delete(PushSubscription).where(
            PushSubscription.endpoint == payload.endpoint,
            PushSubscription.user_id == current_user.id,
        )
    )
    db.commit()
