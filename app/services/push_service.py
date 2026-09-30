"""Web Push delivery — sends OS-level push notifications to subscribed devices.

Silently skips when VAPID keys are not configured (development / optional
feature). Never raises — a push failure must never affect the appointment
transaction.
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def send_to_clinic(
    db: Session,
    clinic_id: int,
    title: str,
    body: str,
    url: str = "/clinic/notifications",
) -> None:
    """Fire a push notification to every subscribed device in a clinic.

    Expired subscriptions (HTTP 404/410 from the push service) are removed so
    they do not accumulate. All errors are logged and swallowed.
    """
    try:
        from app.config import settings

        if not settings.VAPID_PRIVATE_KEY:
            return

        from pywebpush import WebPushException, webpush

        from app.models.push_subscription import PushSubscription

        subs = (
            db.execute(
                select(PushSubscription).where(PushSubscription.clinic_id == clinic_id)
            )
            .scalars()
            .all()
        )
        if not subs:
            return

        payload = json.dumps({"title": title, "body": body, "url": url})
        stale: list[PushSubscription] = []

        for sub in subs:
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    },
                    data=payload,
                    vapid_private_key=settings.VAPID_PRIVATE_KEY,
                    vapid_claims={
                        "sub": f"mailto:{settings.VAPID_CONTACT_EMAIL}"
                    },
                )
            except WebPushException as exc:
                if exc.response is not None and exc.response.status_code in (404, 410):
                    stale.append(sub)
                else:
                    logger.warning("Push failed for sub %s: %s", sub.id, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Push error for sub %s: %s", sub.id, exc)

        for sub in stale:
            db.delete(sub)
        if stale:
            db.commit()

    except Exception:  # noqa: BLE001
        logger.exception("send_to_clinic failed; appointment is unaffected")
