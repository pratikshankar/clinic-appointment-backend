"""Real email delivery over SMTP.

Python's standard library, no dependency: `smtplib` and `email.message` do
everything needed, and an SMTP mailbox is something a clinic already has.
Nothing here requires an approval process, which is why email is the one channel
that can go live immediately.

Every failure is returned as an unsuccessful `MessageResult` rather than raised.
A mail server being down must never turn "the invoice was generated" into a 500
for the receptionist -- the document is fine and the caller records the failure
and moves on.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.config import settings
from app.integrations.base import EmailService, MessageResult
from app.models.enums import MessageChannel

logger = logging.getLogger(__name__)


class SMTPEmailService(EmailService):
    """Sends through a configured SMTP server."""

    name = "smtp"

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool | None = None,
        sender: str | None = None,
        sender_name: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.host = host or settings.SMTP_HOST
        self.port = port or settings.SMTP_PORT
        self.username = username if username is not None else settings.SMTP_USERNAME
        self.password = password if password is not None else settings.SMTP_PASSWORD
        self.use_tls = settings.SMTP_USE_TLS if use_tls is None else use_tls
        self.sender = sender or settings.EMAIL_FROM
        self.sender_name = sender_name if sender_name is not None else settings.EMAIL_FROM_NAME
        self.timeout = timeout or settings.SMTP_TIMEOUT_SECONDS

    # ----------------------------------------------------------------- #
    def _from_header(self) -> str:
        return formataddr((self.sender_name, self.sender)) if self.sender_name else self.sender

    def _build(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None,
        attachments: list[tuple[str, bytes]] | None,
    ) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._from_header()
        message["To"] = to
        # A stable Message-ID makes threading work in the patient's client and
        # gives the mail server something to log against.
        message["Message-ID"] = make_msgid()
        message.set_content(body)
        if html_body:
            message.add_alternative(html_body, subtype="html")

        for filename, content in attachments or []:
            # Everything this app attaches is a PDF; declaring the subtype
            # explicitly is what makes it open as a document rather than
            # downloading as an unnamed blob.
            message.add_attachment(
                content,
                maintype="application",
                subtype="pdf" if filename.lower().endswith(".pdf") else "octet-stream",
                filename=filename,
            )
        return message

    def _connect(self) -> smtplib.SMTP | smtplib.SMTP_SSL:
        # Port 465 is implicit TLS from the first byte; 587 connects in the
        # clear and upgrades with STARTTLS. Choosing on the port rather than on
        # a separate flag removes a combination that silently fails to encrypt.
        if self.port == 465:
            return smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout, context=ssl.create_default_context()
            )
        client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        if self.use_tls:
            client.starttls(context=ssl.create_default_context())
        return client

    # ----------------------------------------------------------------- #
    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> MessageResult:
        def failure(error: str) -> MessageResult:
            logger.warning("SMTP send to %s failed: %s", to, error)
            return MessageResult(
                success=False,
                provider=self.name,
                channel=MessageChannel.EMAIL,
                recipient=to,
                error=error[:500],
            )

        if not self.host:
            return failure("No SMTP host is configured")

        message = self._build(
            to=to, subject=subject, body=body, html_body=html_body, attachments=attachments
        )

        try:
            with self._connect() as client:
                if self.username:
                    client.login(self.username, self.password)
                client.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            # Named separately because it is the most common real-world failure
            # and the fix is specific: an app password, not the account password.
            return failure(
                f"SMTP authentication rejected ({exc.smtp_code}). "
                "Most providers require an app-specific password."
            )
        except smtplib.SMTPRecipientsRefused:
            return failure(f"The server refused the address {to}")
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            return failure(f"{type(exc).__name__}: {exc}")

        return MessageResult(
            success=True,
            provider=self.name,
            channel=MessageChannel.EMAIL,
            recipient=to,
            provider_message_id=message["Message-ID"],
            metadata={"attachments": [name for name, _ in attachments or []]},
        )
