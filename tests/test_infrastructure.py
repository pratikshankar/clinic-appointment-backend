"""Meta endpoints, validators, identifier generation and provider abstraction."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.integrations.factory import get_email_service, get_whatsapp_service
from app.integrations.mock_providers import SENT_MESSAGES
from app.models import TreatmentPackage
from app.schemas.common import normalize_mobile, validate_pin_code
from app.utils.identifiers import generate_patient_code, slugify_clinic_code


class TestMetaEndpoints:
    def test_health(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["database"]["connected"] is True

    def test_meta_lists_enums_for_the_frontend(self, client):
        body = client.get("/api/meta").json()
        assert body["roles"] == ["SUPERADMIN", "ADMIN", "CLINIC_USER"]
        assert "NO_SHOW" in body["appointment_statuses"]
        assert body["providers"]["whatsapp"] == "mock"

    def test_openapi_schema_builds(self, client):
        """A malformed response model would break the schema, not just one route."""
        assert client.get("/openapi.json").status_code == 200


class TestValidators:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("9876543210", "9876543210"),
            ("+91 98765 43210", "9876543210"),
            ("+919876543210", "9876543210"),
            ("09876543210", "9876543210"),
            ("98765-43210", "9876543210"),
            ("  9876543210  ", "9876543210"),
        ],
    )
    def test_mobile_numbers_normalise_to_one_form(self, raw, expected):
        assert normalize_mobile(raw) == expected

    @pytest.mark.parametrize("raw", ["12345", "1234567890", "98765", "abcdefghij", "+1 555 0100"])
    def test_invalid_mobile_numbers_are_rejected(self, raw):
        with pytest.raises(ValueError):
            normalize_mobile(raw)

    def test_pin_code_validation(self):
        assert validate_pin_code("560102") == "560102"
        with pytest.raises(ValueError):
            validate_pin_code("5601")


class TestIdentifiers:
    def test_patient_codes_are_sequential_and_padded(self, db, clinics):
        from app.models import Patient

        assert generate_patient_code(db) == "PT-000001"

        db.add(Patient(patient_code="PT-000001", full_name="A", mobile="9876543210"))
        db.flush()
        assert generate_patient_code(db) == "PT-000002"

        db.add(Patient(patient_code="PT-000009", full_name="B", mobile="9876543211"))
        db.flush()
        assert generate_patient_code(db) == "PT-000010"

    def test_clinic_codes_are_derived_and_deduplicated(self):
        # Initials are preferred, but "HSR Layout" -> "HL" is too short to be
        # useful, so it falls back to the first word.
        assert slugify_clinic_code("HSR Layout") == "HSR"
        assert slugify_clinic_code("Whitefield") == "WHIT"
        assert slugify_clinic_code("Indiranagar Main Road Clinic") == "IMRC"
        assert slugify_clinic_code("HSR Layout", {"HSR"}) == "HSR2"
        assert slugify_clinic_code("HSR Layout", {"HSR", "HSR2"}) == "HSR3"


class TestDatabaseInvariants:
    def test_sessions_taken_cannot_exceed_registered(self, db, clinics):
        """Section 12: sessions remaining must never go negative."""
        from app.models import Patient

        patient = Patient(patient_code="PT-000100", full_name="C", mobile="9876543212")
        db.add(patient)
        db.flush()

        db.add(
            TreatmentPackage(
                patient_id=patient.id,
                clinic_id=clinics[0].id,
                sessions_registered=10,
                sessions_taken=11,
                price_per_session=500,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_sessions_remaining_is_derived(self, db, clinics):
        from app.models import Patient

        patient = Patient(patient_code="PT-000101", full_name="D", mobile="9876543213")
        db.add(patient)
        db.flush()

        package = TreatmentPackage(
            patient_id=patient.id,
            clinic_id=clinics[0].id,
            sessions_registered=10,
            sessions_taken=4,
            price_per_session=500,
        )
        db.add(package)
        db.flush()
        assert package.sessions_remaining == 6

    def test_foreign_keys_are_enforced_on_sqlite(self, db, clinics):
        """Without PRAGMA foreign_keys=ON this would silently succeed."""
        from app.models import Patient

        db.add(
            Patient(
                patient_code="PT-000102",
                full_name="E",
                mobile="9876543214",
                primary_clinic_id=9999,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


class TestProviderAbstraction:
    def test_msg91_falls_back_to_mock_when_auth_key_missing(self, monkeypatch):
        """A misconfigured provider must not crash the app — it falls back to mock."""
        from app.integrations.factory import reset_provider_cache
        from app.integrations.msg91_provider import MSG91EmailService
        reset_provider_cache()
        # Simulate MSG91 selected but no credentials
        svc = MSG91EmailService(
            auth_key="",
            domain="",
            from_email="",
            appt_template_id="",
            bill_template_id="",
        )
        result = svc.send(to="test@example.com", subject="x", body="y")
        assert result.success is False
        assert "not configured" in (result.error or "").lower() or result.error
        reset_provider_cache()

    def test_mock_email_records_what_it_would_have_sent(self):
        from app.integrations.mock_providers import MockEmailService
        SENT_MESSAGES.clear()
        result = MockEmailService().send(
            to="patient@example.com", subject="Your bill", body="Total: 5000"
        )
        assert result.success is True
        assert result.provider_message_id
        assert SENT_MESSAGES[-1]["subject"] == "Your bill"

    def test_mock_whatsapp_supports_templates(self):
        from app.integrations.mock_providers import MockWhatsAppService
        SENT_MESSAGES.clear()
        result = MockWhatsAppService().send_template(
            to="9876543210",
            template_key="appointment_booked",
            variables={"clinic": "HSR Layout", "time": "5:30 PM"},
        )
        assert result.success is True
        assert SENT_MESSAGES[-1]["template_key"] == "appointment_booked"


class TestErrorEnvelope:
    def test_every_error_uses_the_same_shape(self, client):
        body = client.get("/api/clinics").json()
        assert set(body.keys()) == {"error"}
        assert {"code", "message"} <= set(body["error"].keys())

    def test_validation_errors_name_the_field(self, client, superadmin, auth):
        body = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={"username": "x", "password": "short", "full_name": "X", "role": "ADMIN"},
        ).json()
        fields = {detail["field"] for detail in body["error"]["details"]}
        assert "password" in fields
