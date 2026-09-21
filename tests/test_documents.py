"""Phase 7b: invoice, receipt and statement PDFs, and their delivery.

Asserting on extracted PDF *text* rather than just "it returned bytes": a
document that renders but omits the bill number, the amount or the session dates
is worse than one that fails outright, because nobody notices until an insurer
rejects the claim.
"""

from datetime import date, timedelta
from io import BytesIO
from unittest.mock import patch

import pytest
from pypdf import PdfReader

from app.integrations.base import MessageResult
from app.models.enums import MessageChannel


def text_of(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


def next_weekday(target: int = 0, weeks_ahead: int = 1) -> date:
    today = date.today()
    ahead = (target - today.weekday()) % 7
    return today + timedelta(days=ahead + 7 * weeks_ahead)


@pytest.fixture
def patient(client, superadmin, clinics, auth):
    return client.post(
        "/api/patients",
        headers=auth("superadmin"),
        json={
            "full_name": "Meera Iyer",
            "mobile": "9876500777",
            "email": "meera@example.com",
            "primary_clinic_id": clinics[0].id,
        },
    ).json()


@pytest.fixture
def paid_bill(client, superadmin, patient, clinics, auth):
    """A 10-session package billed at 500 each, half paid."""
    body = client.post(
        f"/api/patients/{patient['id']}/packages",
        headers=auth("superadmin"),
        json={
            "sessions_registered": 10,
            "price_per_session": "500.00",
            "clinic_id": clinics[0].id,
            "additional_charges": [
                {"description": "Initial consultation", "unit_price": "500.00"}
            ],
            "payment": {
                "amount": "2000.00",
                "payment_method": "UPI",
                "reference_number": "UPI-4417",
            },
        },
    ).json()
    return body["bill"], body


# --------------------------------------------------------------------------- #
class TestInvoicePdf:
    def test_invoice_contains_the_numbers_that_matter(
        self, client, superadmin, paid_bill, auth
    ):
        bill, _ = paid_bill
        response = client.get(
            f"/api/bills/{bill['id']}/invoice.pdf", headers=auth("superadmin")
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert bill["bill_number"] in response.headers["content-disposition"]
        assert response.content[:5] == b"%PDF-"

        text = text_of(response.content)
        assert bill["bill_number"] in text
        assert "Meera Iyer" in text
        assert "HSR Layout" in text
        # Both lines, and the arithmetic.
        assert "Initial consultation" in text
        assert "5,500.00" in text  # total
        assert "2,000.00" in text  # paid
        assert "3,500.00" in text  # balance
        # UPI must not be mangled to "Upi" on a document a patient keeps.
        assert "UPI" in text
        assert "UPI-4417" in text

    def test_the_brand_appears_even_without_a_logo_file(
        self, client, superadmin, paid_bill, auth
    ):
        """A missing logo degrades to a wordmark; it never blocks the invoice."""
        bill, _ = paid_bill
        text = text_of(
            client.get(
                f"/api/bills/{bill['id']}/invoice.pdf", headers=auth("superadmin")
            ).content
        )
        assert "PainEasy" in text

    def test_another_clinics_invoice_is_denied(
        self, client, superadmin, btm_user, paid_bill, auth
    ):
        bill, _ = paid_bill
        response = client.get(
            f"/api/bills/{bill['id']}/invoice.pdf", headers=auth("btm.reception")
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------- #
class TestReceiptPdf:
    def test_one_receipt_per_payment_showing_only_that_amount(
        self, client, superadmin, paid_bill, auth
    ):
        """The patient's ask: a separate document per amount handed over."""
        bill, _ = paid_bill
        headers = auth("superadmin")

        # A second, smaller payment against the same bill.
        client.post(
            f"/api/bills/{bill['id']}/payments",
            headers=headers,
            json={"amount": "1500.00", "payment_method": "CASH"},
        )
        refreshed = client.get(f"/api/bills/{bill['id']}", headers=headers).json()
        first, second = refreshed["payments"][0], refreshed["payments"][1]

        first_text = text_of(
            client.get(f"/api/payments/{first['id']}/receipt.pdf", headers=headers).content
        )
        second_text = text_of(
            client.get(f"/api/payments/{second['id']}/receipt.pdf", headers=headers).content
        )

        assert "2,000.00" in first_text
        assert "1,500.00" in second_text
        # Each receipt shows the balance *as at that payment*, so the two are
        # genuinely different documents rather than the same total twice.
        assert "3,500.00" in first_text  # 5500 - 2000
        assert "2,000.00" in second_text  # 5500 - 3500
        assert "RCP-" in first_text and "RCP-" in second_text
        assert bill["bill_number"] in first_text

    def test_receipt_numbers_are_stable_across_regeneration(
        self, client, superadmin, paid_bill, auth
    ):
        """A reprint months later must carry the number the patient was given."""
        bill, _ = paid_bill
        headers = auth("superadmin")
        payment_id = bill["payments"][0]["id"]

        first = client.get(f"/api/payments/{payment_id}/receipt.pdf", headers=headers)
        again = client.get(f"/api/payments/{payment_id}/receipt.pdf", headers=headers)
        assert first.headers["content-disposition"] == again.headers["content-disposition"]
        assert "RCP-" in first.headers["content-disposition"]

    def test_unknown_payment_is_404(self, client, superadmin, auth):
        response = client.get("/api/payments/99999/receipt.pdf", headers=auth("superadmin"))
        assert response.status_code == 404


# --------------------------------------------------------------------------- #
class TestSessionStatement:
    def _log_sessions(self, client, headers, patient_id, package_id, count):
        for _ in range(count):
            response = client.post(
                f"/api/patients/{patient_id}/sessions",
                headers=headers,
                json={"package_id": package_id},
            )
            assert response.status_code == 201, response.text

    def test_statement_lists_every_session_date(
        self, client, superadmin, patient, paid_bill, auth
    ):
        """The insurance requirement: the dates treatment was actually given."""
        _, created = paid_bill
        headers = auth("superadmin")
        self._log_sessions(client, headers, patient["id"], created["id"], 3)

        response = client.get(
            f"/api/packages/{created['id']}/statement.pdf", headers=headers
        )
        assert response.status_code == 200
        text = text_of(response.content)

        assert "TREATMENT STATEMENT" in text
        assert "Meera Iyer" in text
        assert f"{date.today():%d-%b-%Y}" in text
        assert "Sessions delivered: 3" in text
        assert "Sessions registered: 10" in text
        # The money, so an insurer can see what was actually charged.
        assert "5,500.00" in text

    def test_incomplete_course_is_stamped_provisional(
        self, client, superadmin, patient, paid_bill, auth
    ):
        _, created = paid_bill
        headers = auth("superadmin")
        self._log_sessions(client, headers, patient["id"], created["id"], 2)

        text = text_of(
            client.get(
                f"/api/packages/{created['id']}/statement.pdf", headers=headers
            ).content
        )
        assert "PROVISIONAL" in text
        assert "2 of 10 sessions delivered" in text

    def test_completed_course_is_marked_final(
        self, client, superadmin, patient, clinics, auth
    ):
        headers = auth("superadmin")
        created = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 2,
                "price_per_session": "400.00",
                "clinic_id": clinics[0].id,
                "payment": {"amount": "800.00", "payment_method": "CASH"},
            },
        ).json()
        self._log_sessions(client, headers, patient["id"], created["id"], 2)

        text = text_of(
            client.get(
                f"/api/packages/{created['id']}/statement.pdf", headers=headers
            ).content
        )
        assert "PROVISIONAL" not in text
        assert "Final" in text

    def test_voided_sessions_are_left_off(
        self, client, superadmin, patient, paid_bill, auth
    ):
        """A voided session was not delivered, so claiming for it would be false."""
        _, created = paid_bill
        headers = auth("superadmin")
        self._log_sessions(client, headers, patient["id"], created["id"], 2)

        sessions = client.get(
            f"/api/patients/{patient['id']}/sessions", headers=headers
        ).json()
        client.post(
            f"/api/sessions/{sessions[0]['id']}/void",
            headers=headers,
            json={"reason": "Logged against the wrong patient"},
        )

        text = text_of(
            client.get(
                f"/api/packages/{created['id']}/statement.pdf", headers=headers
            ).content
        )
        assert "Sessions delivered: 1" in text

    def test_another_clinics_statement_is_denied(
        self, client, superadmin, btm_user, paid_bill, auth
    ):
        _, created = paid_bill
        response = client.get(
            f"/api/packages/{created['id']}/statement.pdf", headers=auth("btm.reception")
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------- #
class TestDelivery:
    def _messages(self, db):
        from app.models import OutboundMessage

        return db.query(OutboundMessage).order_by(OutboundMessage.id).all()

    def test_send_invoice_by_whatsapp_logs_the_attachment(
        self, client, superadmin, paid_bill, auth, db
    ):
        bill, _ = paid_bill
        before = len(self._messages(db))

        response = client.post(
            f"/api/bills/{bill['id']}/send",
            headers=auth("superadmin"),
            json={"channel": "WHATSAPP"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "SENT"
        assert body["channel"] == "WHATSAPP"
        assert body["recipient"] == "9876500777"
        assert body["filename"].startswith("Invoice-")

        messages = self._messages(db)
        assert len(messages) == before + 1
        assert messages[-1].related_entity_type == "bill"

    def test_send_invoice_by_email_uses_the_patients_address(
        self, client, superadmin, paid_bill, auth
    ):
        bill, _ = paid_bill
        body = client.post(
            f"/api/bills/{bill['id']}/send",
            headers=auth("superadmin"),
            json={"channel": "EMAIL"},
        ).json()
        assert body["recipient"] == "meera@example.com"
        assert body["status"] == "SENT"

    def test_email_without_an_address_is_refused_clearly(
        self, client, superadmin, clinics, auth
    ):
        """A confusing failure here means the receptionist retries forever."""
        no_email = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json={
                "full_name": "Arun Nair",
                "mobile": "9876500888",
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        created = client.post(
            f"/api/patients/{no_email['id']}/packages",
            headers=auth("superadmin"),
            json={
                "sessions_registered": 1,
                "price_per_session": "600.00",
                "clinic_id": clinics[0].id,
            },
        ).json()

        response = client.post(
            f"/api/bills/{created['bill']['id']}/send",
            headers=auth("superadmin"),
            json={"channel": "EMAIL"},
        )
        assert response.status_code == 400
        assert "no email address" in response.json()["error"]["message"]

    def test_recipient_can_be_overridden(self, client, superadmin, paid_bill, auth):
        """A relative pays, or the number on file is wrong and time is short."""
        bill, _ = paid_bill
        body = client.post(
            f"/api/bills/{bill['id']}/send",
            headers=auth("superadmin"),
            json={"channel": "WHATSAPP", "recipient": "9812345678"},
        ).json()
        assert body["recipient"] == "9812345678"

    def test_send_receipt_and_statement(self, client, superadmin, patient, paid_bill, auth):
        bill, created = paid_bill
        headers = auth("superadmin")

        receipt = client.post(
            f"/api/payments/{bill['payments'][0]['id']}/send",
            headers=headers,
            json={"channel": "WHATSAPP"},
        )
        assert receipt.status_code == 200
        assert receipt.json()["filename"].startswith("Receipt-RCP-")

        statement = client.post(
            f"/api/packages/{created['id']}/send-statement",
            headers=headers,
            json={"channel": "EMAIL"},
        )
        assert statement.status_code == 200
        assert statement.json()["filename"].startswith("Treatment-statement-")

    def test_a_provider_failure_is_reported_not_raised(
        self, client, superadmin, paid_bill, auth, db
    ):
        """The document is fine; the operator's next move is another channel."""
        bill, _ = paid_bill
        broken = MessageResult(
            success=False,
            provider="mock-whatsapp",
            channel=MessageChannel.WHATSAPP,
            recipient="9876500777",
            error="media upload rejected",
        )
        with patch(
            "app.integrations.mock_providers.MockWhatsAppService.send_document",
            return_value=broken,
        ):
            response = client.post(
                f"/api/bills/{bill['id']}/send",
                headers=auth("superadmin"),
                json={"channel": "WHATSAPP"},
            )

        assert response.status_code == 200
        assert response.json()["status"] == "FAILED"
        assert response.json()["error_message"] == "media upload rejected"
        assert self._messages(db)[-1].status.value == "FAILED"

    def test_a_provider_raising_is_also_contained(
        self, client, superadmin, paid_bill, auth, db
    ):
        bill, _ = paid_bill
        with patch(
            "app.integrations.mock_providers.MockWhatsAppService.send_document",
            side_effect=RuntimeError("connection reset"),
        ):
            response = client.post(
                f"/api/bills/{bill['id']}/send",
                headers=auth("superadmin"),
                json={"channel": "WHATSAPP"},
            )
        assert response.status_code == 200
        assert response.json()["status"] == "FAILED"

    def test_unsupported_channel_is_rejected(self, client, superadmin, paid_bill, auth):
        bill, _ = paid_bill
        response = client.post(
            f"/api/bills/{bill['id']}/send",
            headers=auth("superadmin"),
            json={"channel": "SMS"},
        )
        assert response.status_code == 422

    def test_sending_another_clinics_invoice_is_denied(
        self, client, superadmin, btm_user, paid_bill, auth
    ):
        bill, _ = paid_bill
        response = client.post(
            f"/api/bills/{bill['id']}/send",
            headers=auth("btm.reception"),
            json={"channel": "WHATSAPP"},
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------- #
class TestSecondBrand:
    """One clinic in the chain trades under its own name and registration.

    The chain-branded clinics must be entirely unaffected — every branding field
    is NULL for them — while the branded one gets its own name, footer and an
    **independent invoice series**, because two legal entities sharing one
    sequence leaves gaps in both sets of books.
    """

    @pytest.fixture
    def branded_clinic(self, client, superadmin, clinics, auth, db):
        from app.models import Clinic

        clinic = db.get(Clinic, clinics[1].id)
        clinic.brand_name = "Physiocare by Dr Swati"
        clinic.brand_tagline = "Bellandur"
        clinic.document_footer = "Physiocare Pvt Ltd · GSTIN 29ABCDE1234F1Z5"
        clinic.bill_number_prefix = "PC"
        db.commit()
        return clinic

    def _bill_at(self, client, headers, clinics, index, name, mobile):
        patient = client.post(
            "/api/patients",
            headers=headers,
            json={
                "full_name": name,
                "mobile": mobile,
                "primary_clinic_id": clinics[index].id,
            },
        ).json()
        created = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 4,
                "price_per_session": "500.00",
                "clinic_id": clinics[index].id,
                "payment": {"amount": "2000.00", "payment_method": "CASH"},
            },
        ).json()
        return created["bill"], created

    def test_the_branded_clinic_issues_its_own_series(
        self, client, superadmin, clinics, branded_clinic, auth
    ):
        headers = auth("superadmin")
        chain_bill, _ = self._bill_at(client, headers, clinics, 0, "Chain Patient", "9876500061")
        branded_bill, _ = self._bill_at(
            client, headers, clinics, 1, "Branded Patient", "9876500062"
        )

        assert chain_bill["bill_number"].startswith("INV-")
        assert branded_bill["bill_number"].startswith("PC-")

    def test_each_series_counts_independently_and_unbroken(
        self, client, superadmin, clinics, branded_clinic, auth
    ):
        """Gaps in either sequence are exactly what an auditor asks about."""
        headers = auth("superadmin")
        numbers = {"INV": [], "PC": []}
        # Interleaved on purpose: a shared counter would show up immediately.
        for index in range(3):
            chain, _ = self._bill_at(
                client, headers, clinics, 0, f"Chain {index}", f"98765001{index}0"
            )
            branded, _ = self._bill_at(
                client, headers, clinics, 1, f"Branded {index}", f"98765002{index}0"
            )
            numbers["INV"].append(chain["bill_number"])
            numbers["PC"].append(branded["bill_number"])

        year = date.today().year
        assert numbers["INV"] == [f"INV-{year}-{n:06d}" for n in (1, 2, 3)]
        assert numbers["PC"] == [f"PC-{year}-{n:06d}" for n in (1, 2, 3)]

    def test_the_invoice_carries_the_second_brand(
        self, client, superadmin, clinics, branded_clinic, auth
    ):
        headers = auth("superadmin")
        bill, _ = self._bill_at(client, headers, clinics, 1, "Branded Patient", "9876500063")

        text = text_of(
            client.get(f"/api/bills/{bill['id']}/invoice.pdf", headers=headers).content
        )
        assert "Physiocare by Dr Swati" in text
        assert "GSTIN 29ABCDE1234F1Z5" in text
        # The chain brand must not appear on another entity's tax invoice.
        assert "PainEasy" not in text

    def test_a_chain_clinic_is_untouched(
        self, client, superadmin, clinics, branded_clinic, auth
    ):
        """The second brand must not leak onto the other ten clinics."""
        headers = auth("superadmin")
        bill, _ = self._bill_at(client, headers, clinics, 0, "Chain Patient", "9876500064")

        text = text_of(
            client.get(f"/api/bills/{bill['id']}/invoice.pdf", headers=headers).content
        )
        assert "PainEasy" in text
        assert "Physiocare" not in text

    def test_receipts_and_statements_follow_the_brand(
        self, client, superadmin, clinics, branded_clinic, auth
    ):
        headers = auth("superadmin")
        bill, package = self._bill_at(
            client, headers, clinics, 1, "Branded Patient", "9876500065"
        )

        receipt = client.get(
            f"/api/payments/{bill['payments'][0]['id']}/receipt.pdf", headers=headers
        )
        # A Physiocare receipt should not look like a PainEasy one.
        assert "PC-RCP-" in receipt.headers["content-disposition"]
        receipt_text = text_of(receipt.content)
        assert "Physiocare by Dr Swati" in receipt_text

        statement = text_of(
            client.get(f"/api/packages/{package['id']}/statement.pdf", headers=headers).content
        )
        assert "Physiocare by Dr Swati" in statement
        assert "GSTIN 29ABCDE1234F1Z5" in statement

    def test_the_email_is_signed_by_the_issuing_clinic(
        self, client, superadmin, clinics, branded_clinic, auth
    ):
        """An email signed with the chain name would read as phishing."""
        from app.services import document_service

        headers = auth("superadmin")
        bill_json, _ = self._bill_at(
            client, headers, clinics, 1, "Branded Patient", "9876500066"
        )
        bill = client.get(f"/api/bills/{bill_json['id']}", headers=headers).json()
        del bill

        from app.models import Bill
        from app.db.database import SessionLocal

        with SessionLocal() as session:
            record = session.get(Bill, bill_json["id"])
            subject, body = document_service.invoice_message(record)
        assert "Physiocare by Dr Swati" in subject
        assert "PainEasy" not in body

    def test_a_missing_logo_file_falls_back_rather_than_failing(
        self, client, superadmin, clinics, branded_clinic, auth, db
    ):
        """A deployment that forgets to copy the file must still issue invoices."""
        from app.models import Clinic

        clinic = db.get(Clinic, clinics[1].id)
        clinic.logo_filename = "does-not-exist.png"
        db.commit()

        headers = auth("superadmin")
        bill, _ = self._bill_at(client, headers, clinics, 1, "Branded Patient", "9876500067")
        response = client.get(f"/api/bills/{bill['id']}/invoice.pdf", headers=headers)
        assert response.status_code == 200
        assert "Physiocare by Dr Swati" in text_of(response.content)

    def test_branding_is_editable_through_the_api(
        self, client, superadmin, clinics, auth
    ):
        """A second brand is a data change, not a deployment."""
        response = client.put(
            f"/api/clinics/{clinics[1].id}",
            headers=auth("superadmin"),
            json={
                "brand_name": "Physiocare by Dr Swati",
                "bill_number_prefix": " pc ",
            },
        )
        assert response.status_code == 200
        clinic = client.get(f"/api/clinics/{clinics[1].id}", headers=auth("superadmin")).json()
        assert clinic["brand_name"] == "Physiocare by Dr Swati"
        # Normalised: the prefix is parsed back out of stored bill numbers.
        assert clinic["bill_number_prefix"] == "PC"

    def test_creating_a_clinic_persists_its_branding(self, client, superadmin, auth):
        """The create form accepted branding and dropped it on the floor.

        Symptom: a Physiocare bill numbered INV- with the chain logo, because
        `create_clinic` built the row from an explicit field list that omitted
        every branding column while `update_clinic` applied them generically.
        """
        created = client.post(
            "/api/clinics",
            headers=auth("superadmin"),
            json={
                "name": "Physiocare By Dr Swati",
                "code": "PBDS",
                "city": "Bengaluru",
                "brand_name": "Physiocare by Dr Swati",
                "brand_tagline": "Bellandur",
                "logo_filename": "physiocare_logo.jpg",
                "document_footer": "Physiocare Pvt Ltd · GSTIN 29ABCDE1234F1Z5",
                "bill_number_prefix": "pc",
            },
        )
        assert created.status_code == 201, created.text
        clinic_id = (created.json().get("clinic") or created.json())["id"]

        clinic = client.get(f"/api/clinics/{clinic_id}", headers=auth("superadmin")).json()
        assert clinic["brand_name"] == "Physiocare by Dr Swati"
        assert clinic["logo_filename"] == "physiocare_logo.jpg"
        assert clinic["bill_number_prefix"] == "PC"  # normalised on the way in
        assert "GSTIN" in clinic["document_footer"]

    def test_the_first_bill_at_a_new_branded_clinic_uses_its_series(
        self, client, superadmin, auth
    ):
        """End to end: create with branding, bill, and check the number."""
        headers = auth("superadmin")
        created = client.post(
            "/api/clinics",
            headers=headers,
            json={
                "name": "Physiocare By Dr Swati",
                "code": "PBDS",
                "city": "Bengaluru",
                "brand_name": "Physiocare by Dr Swati",
                "bill_number_prefix": "PC",
            },
        ).json()
        clinic_id = (created.get("clinic") or created)["id"]

        patient = client.post(
            "/api/patients",
            headers=headers,
            json={"full_name": "Ramu", "mobile": "9876500091", "primary_clinic_id": clinic_id},
        ).json()
        bill = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 4,
                "price_per_session": "500.00",
                "clinic_id": clinic_id,
            },
        ).json()["bill"]

        assert bill["bill_number"].startswith("PC-"), bill["bill_number"]

    def test_the_invoice_prints_the_clinics_street_address(
        self, client, superadmin, paid_bill, auth, db
    ):
        """A tax document with no street address is not much of a tax document."""
        from app.models import Clinic

        clinic = db.get(Clinic, 1)
        clinic.address = "Margosa Ave, opposite Sobha Dahlia"
        clinic.location = "Green Glen Layout, Bellandur"
        db.commit()

        bill, _ = paid_bill
        text = text_of(
            client.get(
                f"/api/bills/{bill['id']}/invoice.pdf", headers=auth("superadmin")
            ).content
        )
        assert "Margosa Ave" in text
        assert "Green Glen Layout" in text
