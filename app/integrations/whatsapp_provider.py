"""Real WhatsApp delivery through the Cloud API shape.

Written against **Meta's Cloud API**, which Gupshup, Twilio and 360dialog all
proxy — for those, pointing `CLINIC_WHATSAPP_API_URL` at their host is usually
the only change. A BSP with a proprietary API (AiSensy, Interakt) needs a small
subclass overriding `_endpoint` and `_payload`; everything else here still
applies.

Two facts about WhatsApp shape this file, and neither is optional:

1. **A business cannot send free text first.** Outside a 24-hour window opened
   by the patient messaging you, only a **template pre-approved by Meta** is
   deliverable. `send_text` is therefore kept for replies inside that window,
   and appointment messages go through `send_template`.
2. **A document cannot be sent as text.** The file is uploaded to get a media
   id, then that id is sent. A public URL would also work, but this app sits
   behind authentication, so upload is the only route available.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.integrations.base import MessageResult, WhatsAppService
from app.models.enums import MessageChannel

logger = logging.getLogger(__name__)


class WhatsAppCloudService(WhatsAppService):
    name = "whatsapp-cloud"

    def __init__(
        self,
        *,
        api_url: str | None = None,
        phone_number_id: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
        language: str | None = None,
    ) -> None:
        self.api_url = (api_url or settings.WHATSAPP_API_URL).rstrip("/")
        self.phone_number_id = phone_number_id or settings.WHATSAPP_PHONE_NUMBER_ID
        self.api_key = api_key or settings.WHATSAPP_API_KEY
        self.timeout = timeout or settings.WHATSAPP_TIMEOUT_SECONDS
        self.language = language or settings.WHATSAPP_TEMPLATE_LANGUAGE

    # ----------------------------------------------------------------- #
    def _endpoint(self, path: str) -> str:
        return f"{self.api_url}/{self.phone_number_id}/{path}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _recipient(to: str) -> str:
        """WhatsApp wants an E.164 number without the leading +.

        The app stores ten local digits, so a bare number is assumed Indian.
        Getting this wrong is silent: the API accepts it and nothing arrives.
        """
        digits = "".join(ch for ch in to if ch.isdigit())
        if len(digits) == 10:
            return f"91{digits}"
        return digits

    def _fail(self, to: str, error: str) -> MessageResult:
        logger.warning("WhatsApp send to %s failed: %s", to, error)
        return MessageResult(
            success=False,
            provider=self.name,
            channel=MessageChannel.WHATSAPP,
            recipient=to,
            error=error[:500],
        )

    def _post(self, path: str, payload: dict[str, Any], to: str) -> MessageResult:
        if not (self.api_key and self.phone_number_id):
            return self._fail(to, "WhatsApp API key or phone number id is not configured")
        try:
            response = httpx.post(
                self._endpoint(path),
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            return self._fail(to, f"{type(exc).__name__}: {exc}")

        if response.status_code >= 400:
            # Meta puts the useful part in error.message; falling back to the
            # raw body means a BSP with a different envelope still reports
            # something actionable rather than just a status code.
            try:
                detail = response.json()["error"]["message"]
            except Exception:  # noqa: BLE001
                detail = response.text[:300]
            return self._fail(to, f"HTTP {response.status_code}: {detail}")

        try:
            message_id = response.json()["messages"][0]["id"]
        except Exception:  # noqa: BLE001
            message_id = None

        return MessageResult(
            success=True,
            provider=self.name,
            channel=MessageChannel.WHATSAPP,
            recipient=to,
            provider_message_id=message_id,
        )

    # ----------------------------------------------------------------- #
    def send_text(self, *, to: str, body: str) -> MessageResult:
        """Free-form text.

        Only deliverable inside the 24-hour window the patient opened by
        messaging first. Outside it Meta rejects the send, which is why
        appointment messages use `send_template`.
        """
        return self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "to": self._recipient(to),
                "type": "text",
                "text": {"preview_url": False, "body": body},
            },
            to,
        )

    def send_template(
        self, *, to: str, template_key: str, variables: dict[str, Any]
    ) -> MessageResult:
        """A pre-approved template — the only way to start a conversation.

        Variables are positional in Meta's model (`{{1}}`, `{{2}}`…), so they
        are sent in sorted key order and the template must be written to match.
        """
        parameters = [
            {"type": "text", "text": str(variables[key])} for key in sorted(variables)
        ]
        return self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "to": self._recipient(to),
                "type": "template",
                "template": {
                    "name": template_key,
                    "language": {"code": self.language},
                    "components": [{"type": "body", "parameters": parameters}]
                    if parameters
                    else [],
                },
            },
            to,
        )

    def send_document(
        self, *, to: str, filename: str, content: bytes, caption: str | None = None
    ) -> MessageResult:
        """Upload the file, then send the resulting media id.

        Two calls rather than one because the app is not publicly reachable, so
        the URL form of this API is unavailable to it.
        """
        if not (self.api_key and self.phone_number_id):
            return self._fail(to, "WhatsApp API key or phone number id is not configured")

        try:
            upload = httpx.post(
                self._endpoint("media"),
                headers=self._headers(),
                data={"messaging_product": "whatsapp", "type": "application/pdf"},
                files={"file": (filename, content, "application/pdf")},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            return self._fail(to, f"Media upload failed: {type(exc).__name__}: {exc}")

        if upload.status_code >= 400:
            return self._fail(to, f"Media upload HTTP {upload.status_code}: {upload.text[:300]}")

        media_id = upload.json().get("id")
        if not media_id:
            return self._fail(to, "Media upload returned no id")

        return self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "to": self._recipient(to),
                "type": "document",
                "document": {
                    "id": media_id,
                    "filename": filename,
                    **({"caption": caption} if caption else {}),
                },
            },
            to,
        )
