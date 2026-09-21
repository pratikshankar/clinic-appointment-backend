"""Phase 3: patient registration, duplicate prevention, search and profile.

The rules under test are the ones that matter for data integrity: one permanent
Patient ID, no exact duplicates, and the deliberate asymmetry between scoped
browsing and chain-wide exact lookup.
"""

from datetime import date, timedelta

import pytest


def patient_payload(**overrides):
    body = {
        "full_name": "Rahul Sharma",
        "mobile": "9876543210",
        "email": "rahul@example.com",
        "age": 34,
        "gender": "MALE",
        "address": "HSR Layout, Bengaluru",
        "chief_complaint": "Lower back pain",
    }
    body.update(overrides)
    return body


@pytest.fixture
def sources(db):
    from app.models import PatientSource

    made = {}
    for index, name in enumerate(["Google", "Walk-in", "Doctor Referral"]):
        source = PatientSource(name=name, sort_order=index)
        db.add(source)
        made[name] = source
    db.commit()
    return made


class TestRegistration:
    def test_register_a_patient(self, client, superadmin, clinics, sources, auth):
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(
                primary_clinic_id=clinics[0].id, source_id=sources["Google"].id
            ),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["patient_code"] == "PT-000001"
        assert body["full_name"] == "Rahul Sharma"
        assert body["source_name"] == "Google"
        assert body["primary_clinic_name"] == "HSR Layout"
        assert body["is_profile_complete"] is True
        assert body["registration_date"] == date.today().isoformat()

    def test_patient_ids_are_sequential(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        codes = []
        for index in range(3):
            response = client.post(
                "/api/patients",
                headers=headers,
                json=patient_payload(
                    full_name=f"Patient {index}", mobile=f"98765432{index}0"
                ),
            )
            codes.append(response.json()["patient_code"])
        assert codes == ["PT-000001", "PT-000002", "PT-000003"]

    def test_mobile_is_normalised(self, client, superadmin, auth):
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(mobile="+91 98765 43210"),
        )
        assert response.json()["mobile"] == "9876543210"

    def test_name_whitespace_is_collapsed(self, client, superadmin, auth):
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(full_name="  Rahul   Sharma  "),
        )
        assert response.json()["full_name"] == "Rahul Sharma"

    def test_invalid_mobile_is_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/patients", headers=auth("superadmin"), json=patient_payload(mobile="12345")
        )
        assert response.status_code == 422

    def test_unknown_source_is_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/patients", headers=auth("superadmin"), json=patient_payload(source_id=9999)
        )
        assert response.status_code == 400
        assert "Unknown patient source" in response.json()["error"]["message"]

    def test_inactive_source_is_rejected(self, client, superadmin, sources, db, auth):
        sources["Walk-in"].is_active = False
        db.commit()
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(source_id=sources["Walk-in"].id),
        )
        assert response.status_code == 400
        assert "no longer active" in response.json()["error"]["message"]

    def test_clinic_user_patients_default_to_their_clinic(
        self, client, hsr_user, clinics, auth
    ):
        response = client.post(
            "/api/patients", headers=auth("hsr.reception"), json=patient_payload()
        )
        assert response.status_code == 201
        assert response.json()["primary_clinic_id"] == clinics[0].id

    def test_clinic_user_cannot_register_into_another_clinic(
        self, client, hsr_user, clinics, auth
    ):
        response = client.post(
            "/api/patients",
            headers=auth("hsr.reception"),
            json=patient_payload(primary_clinic_id=clinics[1].id),
        )
        assert response.status_code == 403

    def test_unassigned_clinic_user_cannot_register(self, client, unassigned_user, auth):
        response = client.post(
            "/api/patients", headers=auth("orphan.user"), json=patient_payload()
        )
        assert response.status_code == 400
        assert "not assigned to a clinic" in response.json()["error"]["message"]

    def test_anonymous_access_is_refused(self, client):
        assert client.post("/api/patients", json=patient_payload()).status_code == 401


class TestAgeAndDateOfBirth:
    def test_date_of_birth_derives_the_age(self, client, superadmin, auth):
        born = date.today().replace(year=date.today().year - 40) - timedelta(days=1)
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(age=None, date_of_birth=born.isoformat()),
        )
        body = response.json()
        assert body["age"] == 40
        assert body["age_as_of"] == "date_of_birth"

    def test_date_of_birth_wins_over_a_supplied_age(self, client, superadmin, auth):
        """A stored age that disagrees with the DOB is worse than no age."""
        born = date.today().replace(year=date.today().year - 40) - timedelta(days=1)
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(age=99, date_of_birth=born.isoformat()),
        )
        assert response.json()["age"] == 40
        assert response.json()["age_as_of"] == "date_of_birth"

    def test_age_only_is_kept_and_labelled(self, client, superadmin, auth):
        response = client.post(
            "/api/patients", headers=auth("superadmin"), json=patient_payload(age=34)
        )
        assert response.json()["age"] == 34
        assert response.json()["age_as_of"] == "registration"

    def test_future_date_of_birth_is_rejected(self, client, superadmin, auth):
        tomorrow = date.today() + timedelta(days=1)
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(date_of_birth=tomorrow.isoformat()),
        )
        assert response.status_code == 422

    def test_absurd_age_is_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/patients", headers=auth("superadmin"), json=patient_payload(age=200)
        )
        assert response.status_code == 422


class TestDuplicatePrevention:
    """Decision: block exact (mobile + name); allow and surface mobile-only."""

    def test_exact_duplicate_is_refused_with_the_existing_patient(
        self, client, superadmin, auth
    ):
        headers = auth("superadmin")
        first = client.post("/api/patients", headers=headers, json=patient_payload())
        assert first.status_code == 201

        again = client.post("/api/patients", headers=headers, json=patient_payload())
        assert again.status_code == 409
        error = again.json()["error"]
        assert error["code"] == "duplicate_resource"
        assert error["details"]["patient_code"] == first.json()["patient_code"]
        assert error["details"]["existing_patient_id"] == first.json()["id"]

    def test_duplicate_detection_ignores_case_and_spacing(self, client, superadmin, auth):
        headers = auth("superadmin")
        client.post("/api/patients", headers=headers, json=patient_payload())
        again = client.post(
            "/api/patients", headers=headers, json=patient_payload(full_name="rahul   SHARMA")
        )
        assert again.status_code == 409

    def test_duplicate_detection_ignores_mobile_formatting(self, client, superadmin, auth):
        headers = auth("superadmin")
        client.post("/api/patients", headers=headers, json=patient_payload())
        again = client.post(
            "/api/patients", headers=headers, json=patient_payload(mobile="+91-98765-43210")
        )
        assert again.status_code == 409

    def test_family_members_may_share_a_mobile(self, client, superadmin, auth):
        """A mother booking for her child is not a duplicate."""
        headers = auth("superadmin")
        client.post("/api/patients", headers=headers, json=patient_payload())
        child = client.post(
            "/api/patients", headers=headers, json=patient_payload(full_name="Aarav Sharma", age=8)
        )
        assert child.status_code == 201
        assert child.json()["patient_code"] == "PT-000002"

    def test_duplicates_are_detected_across_clinics(self, client, superadmin, clinics, auth):
        """The whole point of Section 38: no second Patient ID at another branch."""
        headers = auth("superadmin")
        client.post(
            "/api/patients",
            headers=headers,
            json=patient_payload(primary_clinic_id=clinics[0].id),
        )
        again = client.post(
            "/api/patients",
            headers=headers,
            json=patient_payload(primary_clinic_id=clinics[1].id),
        )
        assert again.status_code == 409

    def test_editing_into_an_existing_identity_is_refused(self, client, superadmin, auth):
        headers = auth("superadmin")
        first = client.post("/api/patients", headers=headers, json=patient_payload()).json()
        second = client.post(
            "/api/patients",
            headers=headers,
            json=patient_payload(full_name="Anita Desai", mobile="9876543211"),
        ).json()

        response = client.put(
            f"/api/patients/{second['id']}",
            headers=headers,
            json={"full_name": "Rahul Sharma", "mobile": "9876543210"},
        )
        assert response.status_code == 409
        assert first["patient_code"] in response.json()["error"]["message"]


class TestDuplicateCheckEndpoint:
    def test_reports_an_exact_match(self, client, superadmin, auth):
        headers = auth("superadmin")
        created = client.post("/api/patients", headers=headers, json=patient_payload()).json()

        response = client.post(
            "/api/patients/duplicate-check",
            headers=headers,
            json={"full_name": "Rahul Sharma", "mobile": "9876543210"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["exact_match"]["patient_code"] == created["patient_code"]
        assert body["same_mobile"] == []

    def test_reports_same_mobile_without_an_exact_match(self, client, superadmin, auth):
        headers = auth("superadmin")
        client.post("/api/patients", headers=headers, json=patient_payload()).json()

        response = client.post(
            "/api/patients/duplicate-check",
            headers=headers,
            json={"full_name": "Aarav Sharma", "mobile": "9876543210"},
        )
        body = response.json()
        assert body["exact_match"] is None
        assert [p["full_name"] for p in body["same_mobile"]] == ["Rahul Sharma"]

    def test_reports_the_same_name_on_a_different_number(self, client, superadmin, auth):
        headers = auth("superadmin")
        client.post("/api/patients", headers=headers, json=patient_payload()).json()

        response = client.post(
            "/api/patients/duplicate-check",
            headers=headers,
            json={"full_name": "Rahul Sharma", "mobile": "9999988888"},
        )
        body = response.json()
        assert body["exact_match"] is None
        assert [p["full_name"] for p in body["similar_name"]] == ["Rahul Sharma"]

    def test_clean_number_returns_nothing(self, client, superadmin, auth):
        response = client.post(
            "/api/patients/duplicate-check",
            headers=auth("superadmin"),
            json={"full_name": "Nobody Here", "mobile": "9000000001"},
        )
        body = response.json()
        assert body["exact_match"] is None and not body["same_mobile"]

    def test_invalid_mobile_is_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/patients/duplicate-check",
            headers=auth("superadmin"),
            json={"mobile": "123"},
        )
        assert response.status_code == 422


class TestScopingAndLookup:
    """Browsing is clinic-scoped; exact lookup crosses the chain."""

    @pytest.fixture
    def two_clinic_patients(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        hsr = client.post(
            "/api/patients",
            headers=headers,
            json=patient_payload(primary_clinic_id=clinics[0].id),
        ).json()
        btm = client.post(
            "/api/patients",
            headers=headers,
            json=patient_payload(
                full_name="Meera Krishnan",
                mobile="9876543213",
                primary_clinic_id=clinics[1].id,
            ),
        ).json()
        return {"hsr": hsr, "btm": btm}

    def test_superadmin_browses_every_patient(
        self, client, superadmin, two_clinic_patients, auth
    ):
        response = client.get("/api/patients", headers=auth("superadmin"))
        assert response.json()["total"] == 2

    def test_admin_browses_every_patient(
        self, client, superadmin, admin, two_clinic_patients, auth
    ):
        response = client.get("/api/patients", headers=auth("admin"))
        assert response.json()["total"] == 2

    def test_clinic_user_browses_only_their_own(
        self, client, superadmin, hsr_user, two_clinic_patients, auth
    ):
        response = client.get("/api/patients", headers=auth("hsr.reception"))
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["patient_code"] == two_clinic_patients["hsr"]["patient_code"]

    def test_unassigned_clinic_user_browses_nothing(
        self, client, superadmin, unassigned_user, two_clinic_patients, auth
    ):
        response = client.get("/api/patients", headers=auth("orphan.user"))
        assert response.json()["total"] == 0

    def test_clinic_user_cannot_open_another_clinics_patient(
        self, client, superadmin, hsr_user, two_clinic_patients, auth
    ):
        response = client.get(
            f"/api/patients/{two_clinic_patients['btm']['id']}",
            headers=auth("hsr.reception"),
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "clinic_access_denied"

    def test_lookup_by_patient_id_crosses_clinics(
        self, client, superadmin, hsr_user, two_clinic_patients, auth
    ):
        """Prevents a duplicate being created at the second branch."""
        code = two_clinic_patients["btm"]["patient_code"]
        response = client.get(
            f"/api/patients/lookup?patient_code={code}", headers=auth("hsr.reception")
        )
        assert response.status_code == 200
        found = response.json()
        assert len(found) == 1
        assert found[0]["patient_code"] == code
        # Identity only, and flagged as outside their scope.
        assert found[0]["in_your_scope"] is False
        assert "diagnosis" not in found[0]
        assert "address" not in found[0]

    def test_lookup_by_mobile_crosses_clinics(
        self, client, superadmin, hsr_user, two_clinic_patients, auth
    ):
        response = client.get(
            "/api/patients/lookup?mobile=9876543213", headers=auth("hsr.reception")
        )
        assert [p["patient_code"] for p in response.json()] == [
            two_clinic_patients["btm"]["patient_code"]
        ]

    def test_lookup_marks_your_own_patients_in_scope(
        self, client, superadmin, hsr_user, two_clinic_patients, auth
    ):
        response = client.get(
            "/api/patients/lookup?mobile=9876543210", headers=auth("hsr.reception")
        )
        assert response.json()[0]["in_your_scope"] is True

    def test_lookup_requires_a_full_mobile_number(self, client, hsr_user, auth):
        """Partial matching would turn lookup into a chain-wide browse."""
        response = client.get(
            "/api/patients/lookup?mobile=98765", headers=auth("hsr.reception")
        )
        assert response.status_code == 400
        assert "full 10-digit" in response.json()["error"]["message"]

    def test_lookup_needs_a_criterion(self, client, hsr_user, auth):
        response = client.get("/api/patients/lookup", headers=auth("hsr.reception"))
        assert response.status_code == 400

    def test_lookup_route_is_not_read_as_a_patient_id(self, client, hsr_user, auth):
        """`/patients/lookup` must not be captured by `/patients/{id}`."""
        response = client.get(
            "/api/patients/lookup?mobile=9876543210", headers=auth("hsr.reception")
        )
        assert response.status_code in (200, 400)  # never 422 from int parsing

    def test_activity_at_your_clinic_grants_full_access(
        self, client, db, superadmin, hsr_user, two_clinic_patients, clinics, auth
    ):
        """The rule Phase 4 needs: one clinic books a patient from another."""
        from datetime import time

        from app.models import Appointment, AppointmentStatus

        db.add(
            Appointment(
                appointment_code="APT-XCLINIC-1",
                patient_id=two_clinic_patients["btm"]["id"],
                clinic_id=clinics[0].id,  # BTM patient, appointment at HSR
                appointment_date=date.today(),
                start_time=time(10, 0),
                end_time=time(10, 30),
                duration_minutes=30,
                status=AppointmentStatus.BOOKED,
            )
        )
        db.commit()

        response = client.get(
            f"/api/patients/{two_clinic_patients['btm']['id']}",
            headers=auth("hsr.reception"),
        )
        assert response.status_code == 200
        assert response.json()["patient_code"] == two_clinic_patients["btm"]["patient_code"]


class TestSearchAndFilters:
    @pytest.fixture
    def population(self, client, superadmin, clinics, sources, auth):
        headers = auth("superadmin")
        people = [
            ("Rahul Sharma", "9876543210", clinics[0].id, sources["Google"].id),
            ("Anita Desai", "9876543211", clinics[0].id, sources["Walk-in"].id),
            ("Meera Krishnan", "9876543213", clinics[1].id, sources["Google"].id),
        ]
        for name, mobile, clinic_id, source_id in people:
            client.post(
                "/api/patients",
                headers=headers,
                json=patient_payload(
                    full_name=name,
                    mobile=mobile,
                    primary_clinic_id=clinic_id,
                    source_id=source_id,
                ),
            )

    def test_search_by_name_fragment(self, client, superadmin, population, auth):
        response = client.get("/api/patients?search=desai", headers=auth("superadmin"))
        assert [p["full_name"] for p in response.json()["items"]] == ["Anita Desai"]

    def test_search_by_mobile_fragment(self, client, superadmin, population, auth):
        response = client.get("/api/patients?search=543213", headers=auth("superadmin"))
        assert [p["full_name"] for p in response.json()["items"]] == ["Meera Krishnan"]

    def test_search_by_patient_id(self, client, superadmin, population, auth):
        response = client.get("/api/patients?search=PT-000001", headers=auth("superadmin"))
        assert response.json()["total"] == 1

    def test_filter_by_clinic(self, client, superadmin, population, clinics, auth):
        response = client.get(
            f"/api/patients?clinic_id={clinics[1].id}", headers=auth("superadmin")
        )
        assert response.json()["total"] == 1

    def test_filter_by_source(self, client, superadmin, population, sources, auth):
        response = client.get(
            f"/api/patients?source_id={sources['Google'].id}", headers=auth("superadmin")
        )
        assert response.json()["total"] == 2

    def test_clinic_user_cannot_filter_to_another_clinic(
        self, client, superadmin, hsr_user, population, clinics, auth
    ):
        response = client.get(
            f"/api/patients?clinic_id={clinics[1].id}", headers=auth("hsr.reception")
        )
        assert response.status_code == 403

    def test_pagination(self, client, superadmin, population, auth):
        response = client.get("/api/patients?page=1&page_size=2", headers=auth("superadmin"))
        body = response.json()
        assert body["total"] == 3 and len(body["items"]) == 2


class TestProfileCompletion:
    def test_quick_create_leaves_the_profile_incomplete(
        self, client, admin, clinics, auth
    ):
        """Section 7: an Admin books with the minimum; the clinic finishes it."""
        response = client.post(
            "/api/patients/quick",
            headers=auth("admin"),
            json={
                "full_name": "Walk In Patient",
                "mobile": "9876500777",
                "chief_complaint": "Knee pain",
                "primary_clinic_id": clinics[0].id,
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["is_profile_complete"] is False
        assert body["patient_code"] == "PT-000001"
        assert body["gender"] is None

    def test_completing_the_required_fields_promotes_the_record(
        self, client, admin, hsr_user, clinics, sources, auth
    ):
        created = client.post(
            "/api/patients/quick",
            headers=auth("admin"),
            json={
                "full_name": "Walk In Patient",
                "mobile": "9876500777",
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        assert created["is_profile_complete"] is False

        response = client.put(
            f"/api/patients/{created['id']}",
            headers=auth("hsr.reception"),
            json={
                "gender": "FEMALE",
                "address": "BTM Layout",
                "source_id": sources["Walk-in"].id,
                "diagnosis": "Patellar tendinitis",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["is_profile_complete"] is True
        assert body["source_name"] == "Walk-in"

    def test_partial_completion_stays_incomplete(
        self, client, admin, hsr_user, clinics, auth
    ):
        created = client.post(
            "/api/patients/quick",
            headers=auth("admin"),
            json={
                "full_name": "Walk In Patient",
                "mobile": "9876500777",
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        response = client.put(
            f"/api/patients/{created['id']}",
            headers=auth("hsr.reception"),
            json={"gender": "FEMALE"},
        )
        assert response.json()["is_profile_complete"] is False

    def test_filter_by_incomplete_profiles(
        self, client, admin, superadmin, clinics, sources, auth
    ):
        client.post(
            "/api/patients/quick",
            headers=auth("admin"),
            json={
                "full_name": "Walk In Patient",
                "mobile": "9876500777",
                "primary_clinic_id": clinics[0].id,
            },
        )
        # Needs a source as well as gender and address to count as complete.
        client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(
                primary_clinic_id=clinics[0].id, source_id=sources["Google"].id
            ),
        )
        response = client.get(
            "/api/patients?profile_complete=false", headers=auth("superadmin")
        )
        assert [p["full_name"] for p in response.json()["items"]] == ["Walk In Patient"]

    def test_unknown_patient_is_404(self, client, superadmin, auth):
        assert client.get("/api/patients/9999", headers=auth("superadmin")).status_code == 404


class TestArchiving:
    def test_archive_hides_from_the_default_list(self, client, superadmin, auth):
        headers = auth("superadmin")
        created = client.post("/api/patients", headers=headers, json=patient_payload()).json()

        archived = client.post(f"/api/patients/{created['id']}/archive", headers=headers)
        assert archived.status_code == 200
        assert archived.json()["is_active"] is False
        assert client.get("/api/patients", headers=headers).json()["total"] == 0
        assert (
            client.get("/api/patients?is_active=false", headers=headers).json()["total"] == 1
        )

    def test_restore(self, client, superadmin, auth):
        headers = auth("superadmin")
        created = client.post("/api/patients", headers=headers, json=patient_payload()).json()
        client.post(f"/api/patients/{created['id']}/archive", headers=headers)
        restored = client.post(f"/api/patients/{created['id']}/restore", headers=headers)
        assert restored.json()["is_active"] is True


class TestProfileTimeline:
    def test_timeline_returns_the_whole_history(
        self, client, superadmin, sample_clinical_data, auth
    ):
        patient = sample_clinical_data["patients"][0]
        response = client.get(
            f"/api/patients/{patient.id}/profile", headers=auth("superadmin")
        )
        assert response.status_code == 200
        body = response.json()
        assert body["patient"]["full_name"] == "Rahul Sharma"
        assert len(body["appointments"]) == 1
        assert body["appointments"][0]["clinic_name"] == "HSR Layout"
        assert len(body["bills"]) == 1
        assert body["total_billed"] == "5000.00"
        assert body["total_paid"] == "5000.00"
        assert body["total_outstanding"] == "0.00"
        assert body["scoped_to_your_clinics"] is False

    def test_cancelled_bills_are_excluded(
        self, client, superadmin, sample_clinical_data, auth
    ):
        """The fixture has a cancelled 9999 bill against the second patient."""
        patient = sample_clinical_data["patients"][1]
        body = client.get(
            f"/api/patients/{patient.id}/profile", headers=auth("superadmin")
        ).json()
        assert body["bills"] == []
        assert body["total_billed"] == "0.00"

    def test_timeline_is_scoped_for_a_clinic_user(
        self, client, db, superadmin, hsr_user, sample_clinical_data, clinics, auth
    ):
        """A Clinic User sees this patient's history at their clinic, not elsewhere."""
        from datetime import time

        from app.models import Appointment, AppointmentStatus

        patient = sample_clinical_data["patients"][0]  # HSR patient
        db.add(
            Appointment(
                appointment_code="APT-ELSEWHERE-1",
                patient_id=patient.id,
                clinic_id=clinics[1].id,  # treated at BTM
                appointment_date=date.today() - timedelta(days=1),
                start_time=time(11, 0),
                end_time=time(11, 30),
                duration_minutes=30,
                status=AppointmentStatus.COMPLETED,
            )
        )
        db.commit()

        full = client.get(
            f"/api/patients/{patient.id}/profile", headers=auth("superadmin")
        ).json()
        scoped = client.get(
            f"/api/patients/{patient.id}/profile", headers=auth("hsr.reception")
        ).json()

        assert len(full["appointments"]) == 2
        assert len(scoped["appointments"]) == 1
        assert scoped["appointments"][0]["clinic_name"] == "HSR Layout"
        assert scoped["scoped_to_your_clinics"] is True

    def test_session_totals_are_rolled_up(self, client, db, superadmin, clinics, auth):
        from decimal import Decimal

        from app.models import Patient, TreatmentPackage

        patient = Patient(
            patient_code="PT-000500", full_name="Package Patient", mobile="9876500500"
        )
        db.add(patient)
        db.flush()
        db.add(
            TreatmentPackage(
                patient_id=patient.id,
                clinic_id=clinics[0].id,
                sessions_registered=10,
                sessions_taken=4,
                price_per_session=Decimal("500.00"),
            )
        )
        db.commit()

        body = client.get(
            f"/api/patients/{patient.id}/profile", headers=auth("superadmin")
        ).json()
        assert body["total_sessions_registered"] == 10
        assert body["total_sessions_taken"] == 4
        assert body["total_sessions_remaining"] == 6
        assert body["packages"][0]["total_amount"] == "5000.00"

    def test_clinic_user_cannot_read_another_clinics_profile(
        self, client, superadmin, hsr_user, sample_clinical_data, auth
    ):
        btm_patient = sample_clinical_data["patients"][2]
        response = client.get(
            f"/api/patients/{btm_patient.id}/profile", headers=auth("hsr.reception")
        )
        assert response.status_code == 403


class TestAuditTrail:
    def test_registration_and_edits_are_recorded(self, client, superadmin, auth):
        from app.db.database import SessionLocal
        from app.models import AuditLog

        headers = auth("superadmin")
        created = client.post("/api/patients", headers=headers, json=patient_payload()).json()
        client.put(
            f"/api/patients/{created['id']}", headers=headers, json={"diagnosis": "Lumbar strain"}
        )
        client.post(f"/api/patients/{created['id']}/archive", headers=headers)

        with SessionLocal() as session:
            entries = {
                (log.entity_type, log.action.value) for log in session.query(AuditLog).all()
            }
        assert ("patient", "CREATED") in entries
        assert ("patient", "UPDATED") in entries
        assert ("patient", "DISABLED") in entries


class TestDerivedProfileCompleteness:
    """Regression: a client could previously declare a half-filled profile
    complete, which hid it from the "needs completing" work queue."""

    def test_registering_without_required_fields_is_incomplete(
        self, client, superadmin, clinics, auth
    ):
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json={"full_name": "Shankar", "mobile": "9871234567", "primary_clinic_id": clinics[0].id},
        )
        assert response.status_code == 201
        assert response.json()["is_profile_complete"] is False

    def test_registering_with_all_required_fields_is_complete(
        self, client, superadmin, clinics, sources, auth
    ):
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json={
                "full_name": "Complete Patient",
                "mobile": "9871234568",
                "gender": "FEMALE",
                "address": "HSR Layout",
                "source_id": sources["Google"].id,
                "primary_clinic_id": clinics[0].id,
            },
        )
        assert response.json()["is_profile_complete"] is True

    @pytest.mark.parametrize("omit", ["gender", "address", "source_id"])
    def test_each_required_field_is_load_bearing(
        self, client, superadmin, clinics, sources, auth, omit
    ):
        body = {
            "full_name": f"Partial {omit}",
            "mobile": "9871234569",
            "gender": "MALE",
            "address": "Somewhere",
            "source_id": sources["Google"].id,
            "primary_clinic_id": clinics[0].id,
        }
        body.pop(omit)
        response = client.post("/api/patients", headers=auth("superadmin"), json=body)
        assert response.json()["is_profile_complete"] is False, f"missing {omit} should block"

    def test_a_client_cannot_declare_completeness(
        self, client, superadmin, clinics, auth
    ):
        """The field does not exist on the schema; sending it must not take effect."""
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json={
                "full_name": "Sneaky Complete",
                "mobile": "9871234570",
                "is_profile_complete": True,
                "primary_clinic_id": clinics[0].id,
            },
        )
        assert response.status_code == 201
        assert response.json()["is_profile_complete"] is False

    def test_clearing_a_required_field_demotes_the_profile(
        self, client, superadmin, clinics, sources, auth
    ):
        headers = auth("superadmin")
        created = client.post(
            "/api/patients",
            headers=headers,
            json={
                "full_name": "Demote Me",
                "mobile": "9871234571",
                "gender": "MALE",
                "address": "HSR",
                "source_id": sources["Google"].id,
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        assert created["is_profile_complete"] is True

        response = client.put(
            f"/api/patients/{created['id']}", headers=headers, json={"address": None}
        )
        assert response.json()["is_profile_complete"] is False


class TestWhatsAppNumber:
    def test_blank_whatsapp_falls_back_to_mobile(self, client, superadmin, auth):
        response = client.post(
            "/api/patients", headers=auth("superadmin"), json=patient_payload()
        )
        body = response.json()
        assert body["whatsapp_number"] is None
        assert body["whatsapp_contact"] == body["mobile"]

    def test_a_different_whatsapp_number_is_kept(self, client, superadmin, auth):
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(whatsapp_number="+91 90000 11111"),
        )
        body = response.json()
        assert body["whatsapp_number"] == "9000011111"      # normalised
        assert body["whatsapp_contact"] == "9000011111"
        assert body["mobile"] == "9876543210"

    def test_whatsapp_may_equal_the_mobile_number(self, client, superadmin, auth):
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(whatsapp_number="9876543210"),
        )
        assert response.json()["whatsapp_number"] == "9876543210"

    def test_invalid_whatsapp_number_is_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(whatsapp_number="12345"),
        )
        assert response.status_code == 422

    def test_whatsapp_can_be_added_and_cleared_later(self, client, superadmin, auth):
        headers = auth("superadmin")
        created = client.post("/api/patients", headers=headers, json=patient_payload()).json()

        added = client.put(
            f"/api/patients/{created['id']}",
            headers=headers,
            json={"whatsapp_number": "9000022222"},
        )
        assert added.json()["whatsapp_contact"] == "9000022222"

        cleared = client.put(
            f"/api/patients/{created['id']}", headers=headers, json={"whatsapp_number": None}
        )
        assert cleared.json()["whatsapp_number"] is None
        assert cleared.json()["whatsapp_contact"] == created["mobile"]

    def test_quick_create_accepts_whatsapp(self, client, admin, clinics, auth):
        response = client.post(
            "/api/patients/quick",
            headers=auth("admin"),
            json={
                "full_name": "Lead From Ad",
                "mobile": "9876500111",
                "whatsapp_number": "9876500222",
                "primary_clinic_id": clinics[0].id,
            },
        )
        assert response.status_code == 201
        assert response.json()["whatsapp_contact"] == "9876500222"
        assert response.json()["is_profile_complete"] is False

    def test_whatsapp_is_not_used_for_duplicate_detection(self, client, superadmin, auth):
        """Two people may share a WhatsApp number; identity is name + mobile."""
        headers = auth("superadmin")
        client.post(
            "/api/patients",
            headers=headers,
            json=patient_payload(whatsapp_number="9000033333"),
        )
        second = client.post(
            "/api/patients",
            headers=headers,
            json=patient_payload(
                full_name="Other Person", mobile="9876543219", whatsapp_number="9000033333"
            ),
        )
        assert second.status_code == 201


class TestChainWideNameLookup:
    """Reception normally has a name, not a Patient ID. Refusing name search
    across clinics just pushes staff into creating the duplicate that Section 38
    forbids -- so it is allowed, with guards."""

    @pytest.fixture
    def hsr_patient(self, client, superadmin, clinics, auth):
        return client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(
                full_name="Anita Desai", mobile="9876543211", primary_clinic_id=clinics[0].id
            ),
        ).json()

    def test_btm_user_cannot_browse_an_hsr_patient(
        self, client, superadmin, btm_user, hsr_patient, auth
    ):
        response = client.get("/api/patients?search=Anita", headers=auth("btm.reception"))
        assert response.json()["total"] == 0

    def test_but_can_find_them_by_name_across_clinics(
        self, client, superadmin, btm_user, hsr_patient, auth
    ):
        """The exact scenario reported: Anita Desai at HSR, searched from BTM."""
        response = client.get(
            "/api/patients/lookup?name=Anita", headers=auth("btm.reception")
        )
        assert response.status_code == 200
        found = response.json()
        assert [p["full_name"] for p in found] == ["Anita Desai"]
        assert found[0]["patient_code"] == hsr_patient["patient_code"]
        assert found[0]["primary_clinic_name"] == "HSR Layout"
        assert found[0]["in_your_scope"] is False

    def test_name_lookup_is_case_insensitive_and_partial(
        self, client, superadmin, btm_user, hsr_patient, auth
    ):
        headers = auth("btm.reception")
        # All four are substrings of "anita desai" -- including "nita des",
        # which spans the space.
        for term in ("anita", "ANITA", "desai", "nita des"):
            response = client.get(f"/api/patients/lookup?name={term}", headers=headers)
            assert len(response.json()) == 1, term

        # Matching is substring, not fuzzy: a typo finds nothing.
        assert client.get("/api/patients/lookup?name=anitta", headers=headers).json() == []

    def test_name_lookup_returns_identity_fields_only(
        self, client, superadmin, btm_user, hsr_patient, auth
    ):
        found = client.get(
            "/api/patients/lookup?name=Anita", headers=auth("btm.reception")
        ).json()[0]
        for clinical in ("diagnosis", "address", "chief_complaint", "email", "date_of_birth"):
            assert clinical not in found, f"{clinical} must not cross clinic boundaries"

    def test_short_fragments_are_refused(self, client, btm_user, auth):
        """Two characters would match a large slice of the patient book."""
        response = client.get("/api/patients/lookup?name=an", headers=auth("btm.reception"))
        assert response.status_code == 400
        assert "at least 3 characters" in response.json()["error"]["message"]

    def test_lookup_still_needs_some_criterion(self, client, btm_user, auth):
        response = client.get("/api/patients/lookup", headers=auth("btm.reception"))
        assert response.status_code == 400
        assert "Patient ID" in response.json()["error"]["message"]

    def test_results_are_capped(self, client, superadmin, btm_user, clinics, auth):
        """A hundred rows means fishing, not looking someone up."""
        from app.services.patient_service import MAX_LOOKUP_RESULTS

        headers = auth("superadmin")
        for index in range(MAX_LOOKUP_RESULTS + 5):
            client.post(
                "/api/patients",
                headers=headers,
                json=patient_payload(
                    full_name=f"Common Name {index}",
                    mobile=f"90000{index:05d}",
                    primary_clinic_id=clinics[0].id,
                ),
            )
        response = client.get(
            "/api/patients/lookup?name=Common Name", headers=auth("btm.reception")
        )
        assert len(response.json()) == MAX_LOOKUP_RESULTS

    def test_opening_the_record_is_still_refused(
        self, client, superadmin, btm_user, hsr_patient, auth
    ):
        """Finding someone must not become reading their chart."""
        response = client.get(
            f"/api/patients/{hsr_patient['id']}", headers=auth("btm.reception")
        )
        assert response.status_code == 403

    def test_a_clinic_users_own_patients_are_marked_in_scope(
        self, client, superadmin, btm_user, clinics, auth
    ):
        client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json=patient_payload(
                full_name="Meera Krishnan",
                mobile="9876543213",
                primary_clinic_id=clinics[1].id,
            ),
        )
        found = client.get(
            "/api/patients/lookup?name=Meera", headers=auth("btm.reception")
        ).json()
        assert found[0]["in_your_scope"] is True

    def test_admin_and_superadmin_can_also_use_it(
        self, client, superadmin, admin, hsr_patient, auth
    ):
        for username in ("superadmin", "admin"):
            response = client.get(
                "/api/patients/lookup?name=Anita", headers=auth(username)
            )
            assert response.status_code == 200
            assert len(response.json()) == 1
