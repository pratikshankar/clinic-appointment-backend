"""Billing core: service catalogue, bills, line items and payments.

The properties under test:

* A bill's arithmetic is derived from its lines, never trusted from the client.
* Registering a package and billing for it happen in one transaction.
* An add-on service billed mid-course does **not** consume a package session.
* `payment_status` is derived from the money, never set directly.
* Clinic scoping applies to bills exactly as it does everywhere else.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest


@pytest.fixture
def patient(client, superadmin, clinics, auth):
    return client.post(
        "/api/patients",
        headers=auth("superadmin"),
        json={
            "full_name": "Meera Iyer",
            "mobile": "9876500777",
            "primary_clinic_id": clinics[0].id,
        },
    ).json()


@pytest.fixture
def btm_patient(client, superadmin, clinics, auth):
    return client.post(
        "/api/patients",
        headers=auth("superadmin"),
        json={
            "full_name": "Arun Nair",
            "mobile": "9876500888",
            "primary_clinic_id": clinics[1].id,
        },
    ).json()


@pytest.fixture
def consultation(client, superadmin, auth):
    """A chain-wide catalogue entry."""
    return client.post(
        "/api/service-items",
        headers=auth("superadmin"),
        json={
            "name": "Initial consultation",
            "item_type": "CONSULTATION",
            "default_price": "500.00",
        },
    ).json()


def register_package(client, headers, patient_id, **overrides):
    body = {
        "sessions_registered": 10,
        "price_per_session": "500.00",
        **overrides,
    }
    return client.post(f"/api/patients/{patient_id}/packages", headers=headers, json=body)


# --------------------------------------------------------------------------- #
class TestServiceCatalogue:
    def test_create_and_list(self, client, superadmin, auth, consultation):
        assert consultation["default_price"] == "500.00"
        assert consultation["clinic_id"] is None

        listing = client.get("/api/service-items", headers=auth("superadmin"))
        assert listing.status_code == 200
        assert [item["name"] for item in listing.json()] == ["Initial consultation"]

    def test_chain_wide_entries_are_visible_at_every_clinic(
        self, client, superadmin, hsr_user, btm_user, clinics, auth, consultation
    ):
        for username in ("hsr.reception", "btm.reception"):
            response = client.get("/api/service-items", headers=auth(username))
            assert [item["name"] for item in response.json()] == ["Initial consultation"]

    def test_clinic_specific_entry_is_not_offered_elsewhere(
        self, client, superadmin, hsr_user, btm_user, clinics, auth
    ):
        client.post(
            "/api/service-items",
            headers=auth("superadmin"),
            json={"name": "Hydrotherapy", "default_price": "900.00", "clinic_id": clinics[0].id},
        )

        hsr = client.get(
            "/api/service-items", headers=auth("hsr.reception"), params={"clinic_id": clinics[0].id}
        )
        btm = client.get(
            "/api/service-items", headers=auth("btm.reception"), params={"clinic_id": clinics[1].id}
        )
        assert "Hydrotherapy" in [item["name"] for item in hsr.json()]
        assert "Hydrotherapy" not in [item["name"] for item in btm.json()]

    def test_clinic_user_cannot_set_a_chain_wide_price(
        self, client, superadmin, hsr_user, clinics, auth
    ):
        """A chain-wide entry prices every branch, so it is not a receptionist's call."""
        chain_wide = client.post(
            "/api/service-items",
            headers=auth("hsr.reception"),
            json={"name": "Taping", "default_price": "200.00"},
        )
        assert chain_wide.status_code == 403

        own_clinic = client.post(
            "/api/service-items",
            headers=auth("hsr.reception"),
            json={"name": "Taping", "default_price": "200.00", "clinic_id": clinics[0].id},
        )
        assert own_clinic.status_code == 201

    def test_clinic_user_cannot_reprice_a_chain_wide_service(
        self, client, superadmin, hsr_user, auth, consultation
    ):
        response = client.put(
            f"/api/service-items/{consultation['id']}",
            headers=auth("hsr.reception"),
            json={"default_price": "50.00"},
        )
        assert response.status_code == 403

    def test_duplicate_name_at_the_same_scope_is_rejected(self, client, superadmin, auth, consultation):
        again = client.post(
            "/api/service-items",
            headers=auth("superadmin"),
            json={"name": "Initial consultation", "default_price": "600.00"},
        )
        assert again.status_code == 409

    def test_repricing_does_not_touch_bills_already_issued(
        self, client, superadmin, patient, auth, consultation
    ):
        bill = client.post(
            f"/api/patients/{patient['id']}/bills",
            headers=auth("superadmin"),
            json={
                "items": [
                    {
                        "description": consultation["name"],
                        "unit_price": consultation["default_price"],
                        "service_item_id": consultation["id"],
                        "item_type": "CONSULTATION",
                    }
                ]
            },
        ).json()

        client.put(
            f"/api/service-items/{consultation['id']}",
            headers=auth("superadmin"),
            json={"default_price": "750.00"},
        )

        after = client.get(f"/api/bills/{bill['id']}", headers=auth("superadmin")).json()
        assert after["items"][0]["unit_price"] == "500.00"
        assert after["total_amount"] == "500.00"


# --------------------------------------------------------------------------- #
class TestPackageBilling:
    def test_registration_creates_a_bill_in_the_same_call(
        self, client, superadmin, patient, clinics, auth
    ):
        response = register_package(
            client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id
        )
        assert response.status_code == 201
        body = response.json()

        assert body["bill"] is not None
        assert body["bill"]["total_amount"] == "5000.00"
        assert body["bill"]["package_id"] == body["id"]
        assert body["bill"]["payment_status"] == "UNPAID"
        assert body["bill"]["items"][0]["quantity"] == 10
        assert body["bill"]["items"][0]["item_type"] == "SESSION_PACKAGE"

    def test_one_time_session_plus_consultation_fee(
        self, client, superadmin, patient, clinics, auth, consultation
    ):
        """The walk-in case: a single session and a consultation fee, one bill."""
        response = register_package(
            client,
            auth("superadmin"),
            patient["id"],
            sessions_registered=1,
            price_per_session="600.00",
            clinic_id=clinics[0].id,
            additional_charges=[
                {
                    "description": consultation["name"],
                    "unit_price": "500.00",
                    "service_item_id": consultation["id"],
                    "item_type": "CONSULTATION",
                }
            ],
            payment={"amount": "1100.00", "payment_method": "UPI", "reference_number": "UPI-9931"},
        )
        assert response.status_code == 201
        bill = response.json()["bill"]

        # Two lines, each explainable to the patient, not one merged number.
        assert [(item["description"], item["amount"]) for item in bill["items"]] == [
            ("1-session physiotherapy package", "600.00"),
            ("Initial consultation", "500.00"),
        ]
        assert bill["total_amount"] == "1100.00"
        assert bill["payment_status"] == "PAID"
        assert bill["payments"][0]["reference_number"] == "UPI-9931"
        assert bill["payments"][0]["payment_method"] == "UPI"

    def test_part_payment_leaves_the_bill_partial(
        self, client, superadmin, patient, clinics, auth
    ):
        bill = register_package(
            client,
            auth("superadmin"),
            patient["id"],
            clinic_id=clinics[0].id,
            payment={"amount": "2000.00", "payment_method": "CASH"},
        ).json()["bill"]

        assert bill["payment_status"] == "PARTIAL"
        assert bill["balance_amount"] == "3000.00"

    def test_discount_reduces_the_total(self, client, superadmin, patient, clinics, auth):
        bill = register_package(
            client,
            auth("superadmin"),
            patient["id"],
            clinic_id=clinics[0].id,
            discount_amount="500.00",
        ).json()["bill"]

        assert bill["subtotal_amount"] == "5000.00"
        assert bill["discount_amount"] == "500.00"
        assert bill["total_amount"] == "4500.00"

    def test_discount_larger_than_the_charges_is_rejected(
        self, client, superadmin, patient, clinics, auth
    ):
        response = register_package(
            client,
            auth("superadmin"),
            patient["id"],
            clinic_id=clinics[0].id,
            discount_amount="9000.00",
        )
        assert response.status_code == 422

    def test_payment_larger_than_the_total_is_rejected(
        self, client, superadmin, patient, clinics, auth
    ):
        response = register_package(
            client,
            auth("superadmin"),
            patient["id"],
            clinic_id=clinics[0].id,
            payment={"amount": "9000.00", "payment_method": "CASH"},
        )
        assert response.status_code == 400

    def test_a_rejected_payment_rolls_back_the_package(
        self, client, superadmin, patient, clinics, auth
    ):
        """The package and its bill are one transaction, so neither survives alone."""
        register_package(
            client,
            auth("superadmin"),
            patient["id"],
            clinic_id=clinics[0].id,
            payment={"amount": "9000.00", "payment_method": "CASH"},
        )
        packages = client.get(
            f"/api/patients/{patient['id']}/packages", headers=auth("superadmin")
        ).json()
        assert packages == []

    def test_free_package_creates_no_bill(self, client, superadmin, patient, clinics, auth):
        body = register_package(
            client,
            auth("superadmin"),
            patient["id"],
            price_per_session="0",
            clinic_id=clinics[0].id,
        ).json()
        assert body["bill"] is None

    def test_free_sessions_with_a_paid_consultation_bills_only_the_fee(
        self, client, superadmin, patient, clinics, auth, consultation
    ):
        body = register_package(
            client,
            auth("superadmin"),
            patient["id"],
            sessions_registered=1,
            price_per_session="0",
            clinic_id=clinics[0].id,
            additional_charges=[
                {"description": "Initial consultation", "unit_price": "500.00"}
            ],
        ).json()

        assert [item["description"] for item in body["bill"]["items"]] == [
            "Initial consultation"
        ]
        assert body["bill"]["total_amount"] == "500.00"

    def test_skip_billing_records_terms_without_money(
        self, client, superadmin, patient, clinics, auth
    ):
        body = register_package(
            client,
            auth("superadmin"),
            patient["id"],
            clinic_id=clinics[0].id,
            skip_billing=True,
        ).json()
        assert body["bill"] is None
        assert body["sessions_registered"] == 10


# --------------------------------------------------------------------------- #
class TestAddOnCharges:
    def test_addon_service_does_not_consume_a_package_session(
        self, client, superadmin, patient, clinics, auth
    ):
        """A laser session taken mid-course is charged, not deducted."""
        package = register_package(
            client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id
        ).json()

        # Two sessions of the course delivered so far.
        for _ in range(2):
            client.post(
                f"/api/patients/{patient['id']}/sessions",
                headers=auth("superadmin"),
                json={"package_id": package["id"]},
            )

        charge = client.post(
            f"/api/patients/{patient['id']}/bills",
            headers=auth("superadmin"),
            json={
                "items": [{"description": "Laser therapy (single)", "unit_price": "800.00"}],
                "clinic_id": clinics[0].id,
                "package_id": package["id"],
                "payment": {"amount": "800.00", "payment_method": "CARD"},
            },
        )
        assert charge.status_code == 201

        after = client.get(
            f"/api/patients/{patient['id']}/packages", headers=auth("superadmin")
        ).json()[0]
        assert after["sessions_taken"] == 2
        assert after["sessions_remaining"] == 8
        # Separate bill, so the two amounts never merge into one confusing total.
        assert charge.json()["id"] != package["bill"]["id"]
        assert charge.json()["total_amount"] == "800.00"

    def test_consultation_and_one_treatment_with_no_package_at_all(
        self, client, superadmin, patient, clinics, auth, consultation
    ):
        """The walk-in who buys no course: a consultation and a single laser session.

        Nothing here may invent a treatment package. Forcing one would give the
        patient session counts they never purchased, and every later "sessions
        remaining" figure would be wrong.
        """
        bill = client.post(
            f"/api/patients/{patient['id']}/bills",
            headers=auth("superadmin"),
            json={
                "items": [
                    {
                        "description": consultation["name"],
                        "unit_price": "500.00",
                        "service_item_id": consultation["id"],
                        "item_type": "CONSULTATION",
                    },
                    {"description": "Laser therapy (single)", "unit_price": "800.00"},
                ],
                "clinic_id": clinics[0].id,
                "payment": {"amount": "1300.00", "payment_method": "UPI"},
            },
        )
        assert bill.status_code == 201
        assert bill.json()["total_amount"] == "1300.00"
        assert bill.json()["payment_status"] == "PAID"
        assert bill.json()["package_id"] is None

        # The visit is still recorded as a session -- just not a purchased one.
        session = client.post(
            f"/api/patients/{patient['id']}/sessions",
            headers=auth("superadmin"),
            json={"no_package": True, "treatment_provided": "Laser therapy"},
        )
        assert session.status_code == 201
        assert session.json()["session"]["package_id"] is None
        assert session.json()["package"] is None

        packages = client.get(
            f"/api/patients/{patient['id']}/packages", headers=auth("superadmin")
        ).json()
        assert packages == []

    def test_quantity_multiplies_the_line(self, client, superadmin, patient, clinics, auth):
        bill = client.post(
            f"/api/patients/{patient['id']}/bills",
            headers=auth("superadmin"),
            json={
                "items": [
                    {"description": "Dry needling", "unit_price": "700.00", "quantity": 3}
                ],
                "clinic_id": clinics[0].id,
            },
        ).json()
        assert bill["items"][0]["amount"] == "2100.00"
        assert bill["total_amount"] == "2100.00"

    def test_client_cannot_dictate_the_total(self, client, superadmin, patient, clinics, auth):
        """Totals are computed from the lines; anything sent for them is ignored."""
        bill = client.post(
            f"/api/patients/{patient['id']}/bills",
            headers=auth("superadmin"),
            json={
                "items": [{"description": "Consultation", "unit_price": "500.00"}],
                "clinic_id": clinics[0].id,
                "total_amount": "1.00",
                "amount_paid": "999.00",
            },
        ).json()
        assert bill["total_amount"] == "500.00"
        assert bill["amount_paid"] == "0.00"

    def test_a_bill_needs_at_least_one_line(self, client, superadmin, patient, clinics, auth):
        response = client.post(
            f"/api/patients/{patient['id']}/bills",
            headers=auth("superadmin"),
            json={"items": [], "clinic_id": clinics[0].id},
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------- #
class TestPayments:
    def test_payments_accumulate_to_paid(self, client, superadmin, patient, clinics, auth):
        bill = register_package(
            client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id
        ).json()["bill"]

        first = client.post(
            f"/api/bills/{bill['id']}/payments",
            headers=auth("superadmin"),
            json={"amount": "2000.00", "payment_method": "CASH"},
        )
        assert first.json()["payment_status"] == "PARTIAL"

        second = client.post(
            f"/api/bills/{bill['id']}/payments",
            headers=auth("superadmin"),
            json={"amount": "3000.00", "payment_method": "UPI", "reference_number": "UTR-42"},
        )
        assert second.status_code == 201
        body = second.json()
        assert body["payment_status"] == "PAID"
        assert body["balance_amount"] == "0.00"
        assert len(body["payments"]) == 2

    def test_overpayment_is_refused(self, client, superadmin, patient, clinics, auth):
        bill = register_package(
            client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id
        ).json()["bill"]

        response = client.post(
            f"/api/bills/{bill['id']}/payments",
            headers=auth("superadmin"),
            json={"amount": "5000.01", "payment_method": "CASH"},
        )
        assert response.status_code == 400

    def test_paying_a_settled_bill_is_refused(self, client, superadmin, patient, clinics, auth):
        bill = register_package(
            client,
            auth("superadmin"),
            patient["id"],
            clinic_id=clinics[0].id,
            payment={"amount": "5000.00", "payment_method": "CASH"},
        ).json()["bill"]

        response = client.post(
            f"/api/bills/{bill['id']}/payments",
            headers=auth("superadmin"),
            json={"amount": "100.00", "payment_method": "CASH"},
        )
        assert response.status_code == 400
        assert "already fully paid" in response.json()["error"]["message"]

    def test_zero_or_negative_payments_are_rejected(
        self, client, superadmin, patient, clinics, auth
    ):
        bill = register_package(
            client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id
        ).json()["bill"]

        for amount in ("0", "-100.00"):
            response = client.post(
                f"/api/bills/{bill['id']}/payments",
                headers=auth("superadmin"),
                json={"amount": amount, "payment_method": "CASH"},
            )
            assert response.status_code == 422, amount

    def test_the_receiver_is_recorded(self, client, superadmin, hsr_user, patient, clinics, auth):
        bill = register_package(
            client, auth("hsr.reception"), patient["id"], clinic_id=clinics[0].id
        ).json()["bill"]

        body = client.post(
            f"/api/bills/{bill['id']}/payments",
            headers=auth("hsr.reception"),
            json={"amount": "500.00", "payment_method": "CASH"},
        ).json()
        assert body["payments"][0]["received_by"] == hsr_user.full_name


# --------------------------------------------------------------------------- #
class TestBillScoping:
    def test_clinic_user_sees_only_their_clinics_bills(
        self, client, superadmin, hsr_user, btm_user, patient, btm_patient, clinics, auth
    ):
        register_package(client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id)
        register_package(client, auth("superadmin"), btm_patient["id"], clinic_id=clinics[1].id)

        hsr = client.get("/api/bills", headers=auth("hsr.reception")).json()
        assert hsr["total"] == 1
        assert hsr["items"][0]["patient_name"] == "Meera Iyer"

        everything = client.get("/api/bills", headers=auth("superadmin")).json()
        assert everything["total"] == 2

    def test_reading_another_clinics_bill_is_denied(
        self, client, superadmin, btm_user, patient, clinics, auth
    ):
        bill = register_package(
            client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id
        ).json()["bill"]

        response = client.get(f"/api/bills/{bill['id']}", headers=auth("btm.reception"))
        assert response.status_code == 403

    def test_paying_another_clinics_bill_is_denied(
        self, client, superadmin, btm_user, patient, clinics, auth
    ):
        bill = register_package(
            client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id
        ).json()["bill"]

        response = client.post(
            f"/api/bills/{bill['id']}/payments",
            headers=auth("btm.reception"),
            json={"amount": "100.00", "payment_method": "CASH"},
        )
        assert response.status_code == 403

    def test_unassigned_clinic_user_sees_no_bills(
        self, client, superadmin, unassigned_user, patient, clinics, auth
    ):
        register_package(client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id)
        response = client.get("/api/bills", headers=auth("orphan.user")).json()
        assert response["total"] == 0


# --------------------------------------------------------------------------- #
class TestBillListing:
    def test_counters_report_billed_collected_and_outstanding(
        self, client, superadmin, patient, clinics, auth
    ):
        register_package(
            client,
            auth("superadmin"),
            patient["id"],
            clinic_id=clinics[0].id,
            payment={"amount": "2000.00", "payment_method": "CASH"},
        )
        counters = client.get("/api/bills/counters", headers=auth("superadmin")).json()

        assert counters["bills"] == 1
        assert Decimal(counters["total_billed"]) == Decimal("5000.00")
        assert Decimal(counters["total_collected"]) == Decimal("2000.00")
        assert Decimal(counters["outstanding"]) == Decimal("3000.00")
        assert Decimal(counters["collected_today"]) == Decimal("2000.00")

    def test_filter_by_payment_status(self, client, superadmin, patient, clinics, auth):
        register_package(
            client,
            auth("superadmin"),
            patient["id"],
            clinic_id=clinics[0].id,
            payment={"amount": "5000.00", "payment_method": "CASH"},
        )
        client.post(
            f"/api/patients/{patient['id']}/bills",
            headers=auth("superadmin"),
            json={
                "items": [{"description": "Consultation", "unit_price": "500.00"}],
                "clinic_id": clinics[0].id,
            },
        )

        unpaid = client.get(
            "/api/bills", headers=auth("superadmin"), params={"payment_status": "UNPAID"}
        ).json()
        assert unpaid["total"] == 1
        assert unpaid["items"][0]["balance_amount"] == "500.00"

    def test_search_matches_bill_number_and_patient(
        self, client, superadmin, patient, clinics, auth
    ):
        bill = register_package(
            client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id
        ).json()["bill"]

        by_number = client.get(
            "/api/bills", headers=auth("superadmin"), params={"search": bill["bill_number"]}
        ).json()
        by_name = client.get(
            "/api/bills", headers=auth("superadmin"), params={"search": "meera"}
        ).json()
        assert by_number["total"] == 1
        assert by_name["total"] == 1

    def test_date_range_filter(self, client, superadmin, patient, clinics, auth):
        register_package(client, auth("superadmin"), patient["id"], clinic_id=clinics[0].id)
        yesterday = date.today() - timedelta(days=1)

        past = client.get(
            "/api/bills", headers=auth("superadmin"), params={"to": yesterday.isoformat()}
        ).json()
        assert past["total"] == 0

    def test_patient_bills_include_lines_and_payments(
        self, client, superadmin, patient, clinics, auth
    ):
        register_package(
            client,
            auth("superadmin"),
            patient["id"],
            clinic_id=clinics[0].id,
            payment={"amount": "5000.00", "payment_method": "UPI", "reference_number": "X1"},
        )
        bills = client.get(
            f"/api/patients/{patient['id']}/bills", headers=auth("superadmin")
        ).json()

        assert len(bills) == 1
        assert len(bills[0]["items"]) == 1
        assert bills[0]["payments"][0]["reference_number"] == "X1"
