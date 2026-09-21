"""Real email, WhatsApp and SMS providers.

The mocks are already covered elsewhere; what matters here is that the *real*
implementations build the right request, and — more importantly — that a
provider failure is always **returned** rather than raised. Every one of these
is called from a path where an invoice has already been generated or an
appointment already booked, so an exception escaping would turn a successful
operation into a 500 for the receptionist.
"""

import email
import threading
from unittest.mock import patch

import httpx

from app.integrations.sms_provider import HTTPSMSService
from app.integrations.smtp_provider import SMTPEmailService
from app.integrations.whatsapp_provider import WhatsAppCloudService
from app.models.enums import MessageChannel


# --------------------------------------------------------------------------- #
# A real SMTP server, in-process
# --------------------------------------------------------------------------- #
class _CaptureSMTP(threading.Thread):
    """A minimal SMTP server that keeps what it is sent.

    Hand-rolled rather than mocking `smtplib`: mocking the library would only
    prove the code calls the functions it calls. Speaking the protocol to a real
    socket proves the message is well-formed and actually deliverable.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.messages: list[bytes] = []
        self.port: int | None = None
        self._ready = threading.Event()
        self._loop = None

    def run(self) -> None:
        import socket

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.port = server.getsockname()[1]
        self._ready.set()

        conn, _ = server.accept()
        with conn:
            conn.sendall(b"220 test ESMTP\r\n")
            data = b""
            in_body = False
            while True:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                if in_body:
                    data += chunk
                    if data.endswith(b"\r\n.\r\n"):
                        self.messages.append(data[:-5])
                        conn.sendall(b"250 Ok\r\n")
                        in_body = False
                        data = b""
                    continue

                line = chunk.strip()
                upper = line.upper()
                if upper.startswith(b"EHLO") or upper.startswith(b"HELO"):
                    conn.sendall(b"250-test\r\n250 SIZE 35882577\r\n")
                elif upper.startswith((b"MAIL", b"RCPT")):
                    conn.sendall(b"250 Ok\r\n")
                elif upper.startswith(b"DATA"):
                    conn.sendall(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                    in_body = True
                elif upper.startswith(b"QUIT"):
                    conn.sendall(b"221 Bye\r\n")
                    break
                else:
                    conn.sendall(b"250 Ok\r\n")
        server.close()

    def start_and_wait(self) -> int:
        self.start()
        assert self._ready.wait(timeout=5), "capture SMTP server did not start"
        return self.port


class TestSMTPEmail:
    def _service(self, port: int) -> SMTPEmailService:
        return SMTPEmailService(
            host="127.0.0.1",
            port=port,
            username="",
            password="",
            use_tls=False,
            sender="no-reply@clinic.example.com",
            sender_name="PainEasy",
            timeout=5,
        )

    def test_a_real_message_reaches_a_real_server(self):
        server = _CaptureSMTP()
        port = server.start_and_wait()

        result = self._service(port).send(
            to="patient@example.com",
            subject="Invoice INV-2026-000001",
            body="Please find your invoice attached.",
        )
        server.join(timeout=5)

        assert result.success is True
        assert result.channel == MessageChannel.EMAIL
        assert result.provider_message_id  # a real Message-ID header

        raw = server.messages[0].decode()
        parsed = email.message_from_string(raw)
        assert parsed["To"] == "patient@example.com"
        assert parsed["Subject"] == "Invoice INV-2026-000001"
        # The friendly name is what stops it looking like spam.
        assert parsed["From"] == "PainEasy <no-reply@clinic.example.com>"

    def test_a_pdf_is_attached_as_a_pdf(self):
        """Not octet-stream: the wrong type downloads as an unnamed blob."""
        server = _CaptureSMTP()
        port = server.start_and_wait()

        result = self._service(port).send(
            to="patient@example.com",
            subject="Invoice",
            body="Attached.",
            attachments=[("Invoice-INV-2026-000001.pdf", b"%PDF-1.4 fake")],
        )
        server.join(timeout=5)
        assert result.success is True

        parsed = email.message_from_string(server.messages[0].decode())
        attachments = [
            part for part in parsed.walk() if part.get_filename()
        ]
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "Invoice-INV-2026-000001.pdf"
        assert attachments[0].get_content_type() == "application/pdf"
        assert attachments[0].get_payload(decode=True) == b"%PDF-1.4 fake"

    def test_an_unreachable_server_is_reported_not_raised(self):
        """The invoice is already generated; this must not become a 500."""
        service = SMTPEmailService(
            host="127.0.0.1", port=9, username="", password="", use_tls=False, timeout=1
        )
        result = service.send(to="patient@example.com", subject="x", body="y")
        assert result.success is False
        assert result.error

    def test_a_missing_host_is_reported(self):
        service = SMTPEmailService(host="", port=587)
        result = service.send(to="patient@example.com", subject="x", body="y")
        assert result.success is False
        assert "No SMTP host" in result.error


# --------------------------------------------------------------------------- #
class TestWhatsAppCloud:
    def _service(self) -> WhatsAppCloudService:
        return WhatsAppCloudService(
            api_url="https://graph.test/v21.0",
            phone_number_id="123456",
            api_key="token",
            timeout=5,
        )

    def test_a_ten_digit_number_is_given_a_country_code(self):
        """WhatsApp wants E.164. A bare local number is accepted and silently
        never delivered, which is the worst possible failure mode."""
        assert WhatsAppCloudService._recipient("9876543210") == "919876543210"
        assert WhatsAppCloudService._recipient("+91 98765 43210") == "919876543210"

    def test_a_template_send_uses_the_template_endpoint(self):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return httpx.Response(
                200, json={"messages": [{"id": "wamid.TEST"}]}, request=httpx.Request("POST", url)
            )

        with patch("httpx.post", side_effect=fake_post):
            result = self._service().send_template(
                to="9876543210",
                template_key="appointment_booked",
                variables={"1": "HSR Layout", "2": "31-Aug-2026", "3": "10:00 AM"},
            )

        assert result.success is True
        assert result.provider_message_id == "wamid.TEST"
        assert captured["url"] == "https://graph.test/v21.0/123456/messages"
        assert captured["headers"]["Authorization"] == "Bearer token"

        body = captured["json"]
        assert body["type"] == "template"
        assert body["template"]["name"] == "appointment_booked"
        # Meta's variables are positional, so order must follow the key order.
        values = [p["text"] for p in body["template"]["components"][0]["parameters"]]
        assert values == ["HSR Layout", "31-Aug-2026", "10:00 AM"]

    def test_a_document_is_uploaded_then_sent_by_id(self):
        """Two calls, because this app is not publicly reachable so the URL
        form of the API is unavailable to it."""
        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            if url.endswith("/media"):
                return httpx.Response(
                    200, json={"id": "media-99"}, request=httpx.Request("POST", url)
                )
            return httpx.Response(
                200,
                json={"messages": [{"id": "wamid.DOC"}]},
                request=httpx.Request("POST", url),
            )

        with patch("httpx.post", side_effect=fake_post) as mocked:
            result = self._service().send_document(
                to="9876543210",
                filename="Invoice.pdf",
                content=b"%PDF-1.4",
                caption="Your invoice",
            )

        assert result.success is True
        assert calls == [
            "https://graph.test/v21.0/123456/media",
            "https://graph.test/v21.0/123456/messages",
        ]
        sent = mocked.call_args_list[-1].kwargs["json"]
        assert sent["document"]["id"] == "media-99"
        assert sent["document"]["filename"] == "Invoice.pdf"

    def test_an_api_error_is_reported_with_metas_message(self):
        def fake_post(url, **kwargs):
            return httpx.Response(
                400,
                json={"error": {"message": "Template name does not exist"}},
                request=httpx.Request("POST", url),
            )

        with patch("httpx.post", side_effect=fake_post):
            result = self._service().send_text(to="9876543210", body="hi")

        assert result.success is False
        assert "Template name does not exist" in result.error

    def test_a_network_error_is_reported_not_raised(self):
        with patch("httpx.post", side_effect=httpx.ConnectTimeout("timed out")):
            result = self._service().send_text(to="9876543210", body="hi")
        assert result.success is False
        assert "ConnectTimeout" in result.error

    def test_missing_credentials_are_reported(self):
        service = WhatsAppCloudService(api_key="", phone_number_id="")
        result = service.send_text(to="9876543210", body="hi")
        assert result.success is False
        assert "not configured" in result.error


# --------------------------------------------------------------------------- #
class TestHTTPSMS:
    def test_a_missing_dlt_template_is_refused_rather_than_sent(self):
        """Indian operators accept an unregistered message and drop it, which
        looks like success. Failing loudly is the honest outcome."""
        service = HTTPSMSService(
            api_url="https://sms.test/send", api_key="k", sender_id="PAINEZ", template_id=""
        )
        result = service.send(to="9876543210", body="hi")
        assert result.success is False
        assert "DLT" in result.error

    def test_a_send_posts_the_expected_fields(self):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
            captured.update({"url": url, "json": json})
            return httpx.Response(
                200, json={"message_id": "sms-1"}, request=httpx.Request("POST", url)
            )

        service = HTTPSMSService(
            api_url="https://sms.test/send",
            api_key="k",
            sender_id="PAINEZ",
            template_id="DLT-77",
        )
        with patch("httpx.post", side_effect=fake_post):
            result = service.send(to="9876543210", body="Your appointment is booked")

        assert result.success is True
        assert result.provider_message_id == "sms-1"
        assert captured["json"]["to"] == "919876543210"
        assert captured["json"]["sender"] == "PAINEZ"
        assert captured["json"]["template_id"] == "DLT-77"

    def test_a_gateway_error_is_reported_not_raised(self):
        service = HTTPSMSService(
            api_url="https://sms.test/send", api_key="k", template_id="DLT-77"
        )
        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            result = service.send(to="9876543210", body="hi")
        assert result.success is False
        assert "ConnectError" in result.error


# --------------------------------------------------------------------------- #
class TestProviderSelection:
    """A real provider missing its credentials must degrade, not crash."""

    def _factory(self):
        from app.integrations import factory

        factory.reset_provider_cache()
        return factory

    def test_smtp_without_a_host_falls_back_to_the_mock(self):
        factory = self._factory()
        with patch.object(factory.settings, "EMAIL_PROVIDER", "smtp"), patch.object(
            factory.settings, "SMTP_HOST", ""
        ):
            assert factory.get_email_service().name == "mock-email"
        factory.reset_provider_cache()

    def test_smtp_with_a_host_selects_the_real_provider(self):
        factory = self._factory()
        with patch.object(factory.settings, "EMAIL_PROVIDER", "smtp"), patch.object(
            factory.settings, "SMTP_HOST", "smtp.example.com"
        ):
            assert factory.get_email_service().name == "smtp"
        factory.reset_provider_cache()

    def test_whatsapp_without_credentials_falls_back_to_the_mock(self):
        factory = self._factory()
        with patch.object(factory.settings, "WHATSAPP_PROVIDER", "business_api"), patch.object(
            factory.settings, "WHATSAPP_API_KEY", ""
        ):
            assert factory.get_whatsapp_service().name == "mock-whatsapp"
        factory.reset_provider_cache()

    def test_whatsapp_with_credentials_selects_the_real_provider(self):
        factory = self._factory()
        with patch.object(factory.settings, "WHATSAPP_PROVIDER", "business_api"), patch.object(
            factory.settings, "WHATSAPP_API_KEY", "token"
        ), patch.object(factory.settings, "WHATSAPP_PHONE_NUMBER_ID", "123"):
            assert factory.get_whatsapp_service().name == "whatsapp-cloud"
        factory.reset_provider_cache()

    def test_msg91_without_credentials_falls_back_to_the_mock(self):
        """MSG91 selected but auth key empty → mock, not a crash."""
        factory = self._factory()
        with patch.object(factory.settings, "EMAIL_PROVIDER", "msg91"), patch.object(
            factory.settings, "MSG91_AUTH_KEY", ""
        ):
            assert factory.get_email_service().name == "mock-email"
        factory.reset_provider_cache()

    def test_whatsapp_and_sms_default_to_mock(self):
        factory = self._factory()
        with patch.object(factory.settings, "WHATSAPP_PROVIDER", "mock"), \
             patch.object(factory.settings, "SMS_PROVIDER", "mock"):
            assert factory.get_whatsapp_service().name == "mock-whatsapp"
            assert factory.get_sms_service().name == "mock-sms"
        factory.reset_provider_cache()
