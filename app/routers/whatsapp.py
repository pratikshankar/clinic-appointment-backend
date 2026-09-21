"""WhatsApp Cloud API webhook (Meta verification + incoming event sink).

Two endpoints:

* GET  /api/whatsapp/webhook  — Meta's one-time verification handshake.
* POST /api/whatsapp/webhook  — Incoming messages and delivery-status updates.

The GET handler is the one you fill in the Meta dashboard:
  Callback URL  → https://<your-domain>/api/whatsapp/webhook
  Verify Token  → whatever you set as CLINIC_WHATSAPP_WEBHOOK_VERIFY_TOKEN

Meta sends a GET with hub.mode=subscribe, hub.verify_token, and hub.challenge.
We echo hub.challenge back as plain text if the token matches; Meta confirms the
subscription. A mismatch returns 403 so Meta does not silently succeed with the
wrong token.

Incoming events (POST) are logged at DEBUG and dropped — there is no inbound
message flow in this version. The 200 response is what matters: Meta retries
any non-200 with exponential back-off, which would flood the logs.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


@router.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(alias="hub.mode", default=""),
    hub_verify_token: str = Query(alias="hub.verify_token", default=""),
    hub_challenge: str = Query(alias="hub.challenge", default=""),
):
    """Meta webhook verification handshake."""
    if hub_mode != "subscribe":
        raise HTTPException(status_code=400, detail="Unexpected hub.mode")

    expected = settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN
    if not expected:
        logger.error(
            "CLINIC_WHATSAPP_WEBHOOK_VERIFY_TOKEN is not set — "
            "all webhook verifications will fail"
        )
        raise HTTPException(status_code=500, detail="Webhook verify token not configured")

    if hub_verify_token != expected:
        logger.warning("WhatsApp webhook: verify token mismatch")
        raise HTTPException(status_code=403, detail="Verify token mismatch")

    logger.info("WhatsApp webhook verified by Meta")
    return Response(content=hub_challenge, media_type="text/plain")


@router.post("/webhook")
async def receive_webhook(request: Request):
    """Receive incoming messages and delivery-status updates from Meta."""
    try:
        payload = await request.json()
        logger.debug("WhatsApp webhook event: %s", payload)
    except Exception:
        pass
    # Always return 200 immediately — Meta retries on anything else.
    return {"status": "ok"}
