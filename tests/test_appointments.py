"""Phase 4: slot availability, booking, capacity, lifecycle and history.

The load-bearing test in this file is `TestConcurrentBooking`: two threads race
for one remaining place and exactly one must win. Everything else protects the
rules around it.
"""

import threading
from datetime import date, datetime, time, timedelta

import pytest


def next_weekday(target: int = 0, weeks_ahead: int = 1) -> date:
    """A future date falling on `target` weekday (0 = Monday).

    Always at least a week out, so "is it in the past?" never makes a test flaky
    depending on the hour it runs.
    """
    today = date.today()
    ahead = (target - today.weekday()) % 7
    return today + timedelta(days=ahead + 7 * weeks_ahead)


MONDAY = 0
SUNDAY = 6


@pytest.fixture
def patient(client, superadmin, clinics, auth):
    return client.post(
        "/api/patients",
        headers=auth("superadmin"),
        json={
            "full_name": "Rahul Sharma",
            "mobile": "9876543210",
            "primary_clinic_id": clinics[0].id,
        },
    ).json()


@pytest.fixture
def second_patient(client, superadmin, clinics, auth):
    return client.post(
        "/api/patients",
        headers=auth("superadmin"),
        json={
            "full_name": "Anita Desai",
            "mobile": "9876543211",
            "primary_clinic_id": clinics[0].id,
        },
    ).json()


def book(client, headers, clinic_id, patient_id, on_date, start="09:30:00", **extra):
    body = {
        "clinic_id": clinic_id,
        "patient_id": patient_id,
        "appointment_date": on_date.isoformat(),
        "start_time": start,
        **extra,
    }
    return client.post("/api/appointments", headers=headers, json=body)


class TestAvailability:
    def test_slots_match_the_clinic_configuration(self, client, superadmin, clinics, auth):
        monday = next_weekday(MONDAY)
        response = client.get(
            f"/api/appointments/available-slots?clinic_id={clinics[0].id}&date={monday}",
            headers=auth("superadmin"),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["is_open"] is True
        assert body["day_name"] == "Monday"
        # 09:00-13:00 and 16:00-20:00 at 30 minutes = 16 slots.
        assert len(body["slots"]) == 16
        assert body["capacity_per_slot"] == 3
        assert body["total_capacity"] == 48
        assert body["total_booked"] == 0
        assert body["slots"][0] == {
            "time": "09:00",
            "end_time": "09:30",
            "capacity": 3,
            "booked": 0,
            "available": 3,
            "shift_label": "Morning",
            "is_bookable": True,
            "unavailable_reason": None,
        }

    def test_booking_reduces_the_available_count(
        self, client, superadmin, clinics, patient, auth
    ):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        assert book(client, headers, clinics[0].id, patient["id"], monday).status_code == 201

        body = client.get(
            f"/api/appointments/available-slots?clinic_id={clinics[0].id}&date={monday}",
            headers=headers,
        ).json()
        slot = next(s for s in body["slots"] if s["time"] == "09:30")
        assert slot["booked"] == 1
        assert slot["available"] == 2
        assert body["total_booked"] == 1

    def test_a_full_slot_is_shown_as_unbookable(
        self, client, superadmin, clinics, patient, second_patient, db, auth
    ):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        # Capacity is 3; fill it with three different patients.
        third = client.post(
            "/api/patients",
            headers=headers,
            json={"full_name": "Third Patient", "mobile": "9876543212"},
        ).json()
        for person in (patient, second_patient, third):
            assert book(client, headers, clinics[0].id, person["id"], monday).status_code == 201

        body = client.get(
            f"/api/appointments/available-slots?clinic_id={clinics[0].id}&date={monday}",
            headers=headers,
        ).json()
        slot = next(s for s in body["slots"] if s["time"] == "09:30")
        assert slot["booked"] == 3
        assert slot["available"] == 0
        assert slot["is_bookable"] is False
        assert slot["unavailable_reason"] == "Fully booked"

    def test_breaks_remove_slots(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        before = len(
            client.get(
                f"/api/appointments/available-slots?clinic_id={clinics[0].id}&date={monday}",
                headers=headers,
            ).json()["slots"]
        )
        client.post(
            f"/api/clinics/{clinics[0].id}/breaks",
            headers=headers,
            json={"day_of_week": MONDAY, "start_time": "11:00:00", "end_time": "11:30:00"},
        )
        after = client.get(
            f"/api/appointments/available-slots?clinic_id={clinics[0].id}&date={monday}",
            headers=headers,
        ).json()
        assert len(after["slots"]) == before - 1
        assert "11:00" not in {s["time"] for s in after["slots"]}

    def test_a_holiday_closes_the_day(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        client.post(
            "/api/holidays",
            headers=headers,
            json={
                "clinic_id": clinics[0].id,
                "holiday_date": monday.isoformat(),
                "reason": "Deep clean",
            },
        )
        body = client.get(
            f"/api/appointments/available-slots?clinic_id={clinics[0].id}&date={monday}",
            headers=headers,
        ).json()
        assert body["is_open"] is False
        assert body["slots"] == []
        assert "Deep clean" in body["closed_reason"]

    def test_a_closed_weekday_has_no_slots(self, client, superadmin, clinics, auth):
        body = client.get(
            f"/api/appointments/available-slots?clinic_id={clinics[0].id}"
            f"&date={next_weekday(SUNDAY)}",
            headers=auth("superadmin"),
        ).json()
        assert body["is_open"] is False
        assert "No working hours" in body["closed_reason"]

    def test_clinic_user_cannot_read_another_clinics_availability(
        self, client, hsr_user, clinics, auth
    ):
        response = client.get(
            f"/api/appointments/available-slots?clinic_id={clinics[1].id}",
            headers=auth("hsr.reception"),
        )
        assert response.status_code == 403


class TestBookingExistingPatient:
    def test_book_an_appointment(self, client, superadmin, clinics, patient, auth):
        monday = next_weekday(MONDAY)
        response = book(
            client,
            auth("superadmin"),
            clinics[0].id,
            patient["id"],
            monday,
            chief_complaint="Lower back pain",
        )
        assert response.status_code == 201
        appointment = response.json()["appointment"]
        assert appointment["status"] == "BOOKED"
        assert appointment["start_time"] == "09:30:00"
        assert appointment["end_time"] == "10:00:00"
        assert appointment["duration_minutes"] == 30
        assert appointment["clinic_name"] == "HSR Layout"
        assert appointment["patient"]["patient_code"] == patient["patient_code"]
        # Clinic code is part of the reference: it scopes the sequence, so two
        # clinics booking simultaneously cannot compute the same code.
        assert appointment["appointment_code"].startswith("APT-HSR-")

    def test_capacity_exhaustion_returns_409(
        self, client, superadmin, clinics, patient, second_patient, auth
    ):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        people = [patient, second_patient]
        for index in range(2):
            extra = client.post(
                "/api/patients",
                headers=headers,
                json={"full_name": f"Filler {index}", "mobile": f"988877{index:04d}"},
            ).json()
            people.append(extra)

        # Capacity 3: the first three succeed, the fourth is refused.
        results = [
            book(client, headers, clinics[0].id, person["id"], monday).status_code
            for person in people
        ]
        assert results == [201, 201, 201, 409]

        refused = book(client, headers, clinics[0].id, people[3]["id"], monday)
        error = refused.json()["error"]
        assert error["code"] == "slot_unavailable"
        assert error["details"]["capacity"] == 3
        assert error["details"]["booked"] == 3

    def test_a_time_that_is_not_a_slot_is_refused(
        self, client, superadmin, clinics, patient, auth
    ):
        response = book(
            client,
            auth("superadmin"),
            clinics[0].id,
            patient["id"],
            next_weekday(MONDAY),
            start="09:17:00",
        )
        assert response.status_code == 400
        assert "not a bookable slot" in response.json()["error"]["message"]

    def test_booking_during_a_break_is_refused(
        self, client, superadmin, clinics, patient, auth
    ):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        client.post(
            f"/api/clinics/{clinics[0].id}/breaks",
            headers=headers,
            json={"day_of_week": MONDAY, "start_time": "11:00:00", "end_time": "11:30:00"},
        )
        response = book(
            client, headers, clinics[0].id, patient["id"], monday, start="11:00:00"
        )
        assert response.status_code == 400

    def test_booking_on_a_holiday_is_refused(
        self, client, superadmin, clinics, patient, auth
    ):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        client.post(
            "/api/holidays",
            headers=headers,
            json={"clinic_id": clinics[0].id, "holiday_date": monday.isoformat()},
        )
        response = book(client, headers, clinics[0].id, patient["id"], monday)
        assert response.status_code == 400
        assert "not open" in response.json()["error"]["message"]

    def test_booking_in_the_past_is_refused(self, client, superadmin, clinics, patient, auth):
        response = book(
            client,
            auth("superadmin"),
            clinics[0].id,
            patient["id"],
            date.today() - timedelta(days=1),
        )
        assert response.status_code == 400
        assert "in the past" in response.json()["error"]["message"]

    def test_booking_at_an_inactive_clinic_is_refused(
        self, client, superadmin, clinics, patient, auth
    ):
        headers = auth("superadmin")
        client.post(f"/api/clinics/{clinics[0].id}/deactivate", headers=headers)
        response = book(client, headers, clinics[0].id, patient["id"], next_weekday(MONDAY))
        assert response.status_code == 403
        assert "not accepting new bookings" in response.json()["error"]["message"]

    def test_same_day_second_appointment_warns_but_succeeds(
        self, client, superadmin, clinics, patient, auth
    ):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        book(client, headers, clinics[0].id, patient["id"], monday, start="09:30:00")
        response = book(
            client, headers, clinics[0].id, patient["id"], monday, start="17:30:00"
        )
        assert response.status_code == 201
        assert any("already has an appointment" in w for w in response.json()["warnings"])

    def test_archived_patients_cannot_be_booked(
        self, client, superadmin, clinics, patient, auth
    ):
        headers = auth("superadmin")
        client.post(f"/api/patients/{patient['id']}/archive", headers=headers)
        response = book(client, headers, clinics[0].id, patient["id"], next_weekday(MONDAY))
        assert response.status_code == 400
        assert "archived" in response.json()["error"]["message"]

    def test_clinic_user_cannot_book_at_another_clinic(
        self, client, superadmin, hsr_user, clinics, patient, auth
    ):
        response = book(
            client, auth("hsr.reception"), clinics[1].id, patient["id"], next_weekday(MONDAY)
        )
        assert response.status_code == 403

    def test_booking_grants_the_clinic_access_to_the_patient(
        self, client, superadmin, btm_user, clinics, patient, auth
    ):
        """An HSR patient booked at BTM becomes readable to BTM staff (Phase 3 rule)."""
        assert (
            client.get(f"/api/patients/{patient['id']}", headers=auth("btm.reception")).status_code
            == 403
        )
        book(
            client, auth("superadmin"), clinics[1].id, patient["id"], next_weekday(MONDAY)
        )
        assert (
            client.get(f"/api/patients/{patient['id']}", headers=auth("btm.reception")).status_code
            == 200
        )


class TestBookNewPatientInOneStep:
    """The user's requirement: a first-time caller is captured and booked on one
    screen, in one request."""

    def test_register_and_book_together(self, client, superadmin, clinics, auth):
        monday = next_weekday(MONDAY)
        response = client.post(
            "/api/appointments/book-new-patient",
            headers=auth("superadmin"),
            json={
                "clinic_id": clinics[0].id,
                "appointment_date": monday.isoformat(),
                "start_time": "10:00:00",
                "full_name": "New Lead",
                "mobile": "+91 90000 12345",
                "whatsapp_number": "9000067890",
                "chief_complaint": "Shoulder pain",
                "age": 41,
                "gender": "FEMALE",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["patient_created"] is True
        appointment = body["appointment"]
        assert appointment["status"] == "BOOKED"
        assert appointment["chief_complaint"] == "Shoulder pain"
        assert appointment["patient"]["full_name"] == "New Lead"
        assert appointment["patient"]["mobile"] == "9000012345"
        # Incomplete on purpose: the clinic finishes the profile on arrival.
        assert appointment["patient"]["is_profile_complete"] is False
        assert any("Complete their profile" in w for w in body["warnings"])

    def test_the_patient_is_findable_afterwards(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        client.post(
            "/api/appointments/book-new-patient",
            headers=headers,
            json={
                "clinic_id": clinics[0].id,
                "appointment_date": next_weekday(MONDAY).isoformat(),
                "start_time": "10:00:00",
                "full_name": "New Lead",
                "mobile": "9000012345",
            },
        )
        found = client.get("/api/patients?search=New Lead", headers=headers).json()
        assert found["total"] == 1
        assert found["items"][0]["whatsapp_contact"] == "9000012345"

    def test_an_exact_duplicate_is_still_refused(
        self, client, superadmin, clinics, patient, auth
    ):
        """Phase 3's duplicate rule applies to this path too."""
        response = client.post(
            "/api/appointments/book-new-patient",
            headers=auth("superadmin"),
            json={
                "clinic_id": clinics[0].id,
                "appointment_date": next_weekday(MONDAY).isoformat(),
                "start_time": "10:00:00",
                "full_name": "Rahul Sharma",
                "mobile": "9876543210",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "duplicate_resource"

    def test_a_full_slot_does_not_leave_an_orphan_patient(
        self, client, superadmin, clinics, patient, second_patient, auth
    ):
        """The slot is validated before the patient is created."""
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        third = client.post(
            "/api/patients",
            headers=headers,
            json={"full_name": "Third", "mobile": "9876500003"},
        ).json()
        for person in (patient, second_patient, third):
            book(client, headers, clinics[0].id, person["id"], monday, start="10:00:00")

        before = client.get("/api/patients?page_size=100", headers=headers).json()["total"]
        response = client.post(
            "/api/appointments/book-new-patient",
            headers=headers,
            json={
                "clinic_id": clinics[0].id,
                "appointment_date": monday.isoformat(),
                "start_time": "10:00:00",
                "full_name": "Unlucky Caller",
                "mobile": "9000099999",
            },
        )
        assert response.status_code == 409
        after = client.get("/api/patients?page_size=100", headers=headers).json()["total"]
        assert after == before, "a refused booking must not create a patient"

    def test_invalid_mobile_is_rejected(self, client, superadmin, clinics, auth):
        response = client.post(
            "/api/appointments/book-new-patient",
            headers=auth("superadmin"),
            json={
                "clinic_id": clinics[0].id,
                "appointment_date": next_weekday(MONDAY).isoformat(),
                "start_time": "10:00:00",
                "full_name": "Bad Number",
                "mobile": "12345",
            },
        )
        assert response.status_code == 422


class TestLifecycle:
    @pytest.fixture
    def appointment(self, client, superadmin, clinics, patient, auth):
        return book(
            client, auth("superadmin"), clinics[0].id, patient["id"], next_weekday(MONDAY)
        ).json()["appointment"]

    def test_the_happy_path(self, client, superadmin, appointment, auth):
        headers = auth("superadmin")
        aid = appointment["id"]
        for action, expected in [
            ("confirm", "CONFIRMED"),
            ("check-in", "CHECKED_IN"),
            ("complete", "COMPLETED"),
        ]:
            response = client.post(f"/api/appointments/{aid}/{action}", headers=headers, json={})
            assert response.status_code == 200, action
            assert response.json()["status"] == expected

        final = client.get(f"/api/appointments/{aid}", headers=headers).json()
        assert final["confirmed_at"] and final["checked_in_at"] and final["completed_at"]

    def test_cancel_frees_the_slot(self, client, superadmin, clinics, appointment, auth):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        before = client.get(
            f"/api/appointments/available-slots?clinic_id={clinics[0].id}&date={monday}",
            headers=headers,
        ).json()
        booked_before = next(s for s in before["slots"] if s["time"] == "09:30")["booked"]

        response = client.post(
            f"/api/appointments/{appointment['id']}/cancel",
            headers=headers,
            json={"reason": "Patient unwell"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"
        assert response.json()["cancellation_reason"] == "Patient unwell"

        after = client.get(
            f"/api/appointments/available-slots?clinic_id={clinics[0].id}&date={monday}",
            headers=headers,
        ).json()
        assert next(s for s in after["slots"] if s["time"] == "09:30")["booked"] == booked_before - 1

    def test_no_show(self, client, superadmin, appointment, auth):
        response = client.post(
            f"/api/appointments/{appointment['id']}/no-show", headers=auth("superadmin"), json={}
        )
        assert response.json()["status"] == "NO_SHOW"

    def test_completing_a_cancelled_appointment_is_refused(
        self, client, superadmin, appointment, auth
    ):
        headers = auth("superadmin")
        aid = appointment["id"]
        client.post(f"/api/appointments/{aid}/cancel", headers=headers, json={})
        response = client.post(f"/api/appointments/{aid}/complete", headers=headers, json={})
        assert response.status_code == 400
        assert "cannot be marked" in response.json()["error"]["message"]

    def test_checking_in_twice_is_refused(self, client, superadmin, appointment, auth):
        headers = auth("superadmin")
        aid = appointment["id"]
        assert client.post(f"/api/appointments/{aid}/check-in", headers=headers, json={}).status_code == 200
        assert client.post(f"/api/appointments/{aid}/check-in", headers=headers, json={}).status_code == 400

    def test_no_show_after_check_in_is_refused(self, client, superadmin, appointment, auth):
        """The patient demonstrably arrived."""
        headers = auth("superadmin")
        aid = appointment["id"]
        client.post(f"/api/appointments/{aid}/check-in", headers=headers, json={})
        assert client.post(f"/api/appointments/{aid}/no-show", headers=headers, json={}).status_code == 400

    def test_clinic_user_cannot_touch_another_clinics_appointment(
        self, client, superadmin, btm_user, appointment, auth
    ):
        response = client.post(
            f"/api/appointments/{appointment['id']}/confirm",
            headers=auth("btm.reception"),
            json={},
        )
        assert response.status_code == 403


class TestReschedule:
    @pytest.fixture
    def appointment(self, client, superadmin, clinics, patient, auth):
        return book(
            client, auth("superadmin"), clinics[0].id, patient["id"], next_weekday(MONDAY)
        ).json()["appointment"]

    def test_the_original_is_preserved_and_linked(
        self, client, superadmin, clinics, appointment, auth
    ):
        """Section 27: original -> rescheduled -> new stays reconstructable."""
        headers = auth("superadmin")
        new_monday = next_weekday(MONDAY, weeks_ahead=2)
        response = client.post(
            f"/api/appointments/{appointment['id']}/reschedule",
            headers=headers,
            json={
                "appointment_date": new_monday.isoformat(),
                "start_time": "17:30:00",
                "reason": "Patient travelling",
            },
        )
        assert response.status_code == 200
        replacement = response.json()["appointment"]
        assert replacement["id"] != appointment["id"]
        assert replacement["status"] == "BOOKED"
        assert replacement["start_time"] == "17:30:00"
        assert replacement["rescheduled_from_id"] == appointment["id"]

        original = client.get(f"/api/appointments/{appointment['id']}", headers=headers).json()
        assert original["status"] == "RESCHEDULED"
        assert original["start_time"] == "09:30:00"  # unchanged

    def test_both_rows_get_history(self, client, superadmin, appointment, auth):
        headers = auth("superadmin")
        new_monday = next_weekday(MONDAY, weeks_ahead=2)
        replacement = client.post(
            f"/api/appointments/{appointment['id']}/reschedule",
            headers=headers,
            json={
                "appointment_date": new_monday.isoformat(),
                "start_time": "17:30:00",
                "reason": "Patient travelling",
            },
        ).json()["appointment"]

        old_history = client.get(
            f"/api/appointments/{appointment['id']}/history", headers=headers
        ).json()
        actions = [entry["action"] for entry in old_history]
        assert "CREATED" in actions and "RESCHEDULED" in actions

        moved = next(e for e in old_history if e["action"] == "RESCHEDULED")
        assert moved["old_time"] == "09:30:00"
        assert moved["new_time"] == "17:30:00"
        assert moved["reason"] == "Patient travelling"
        assert moved["changed_by"] == "System Superadmin" or moved["changed_by"]

        new_history = client.get(
            f"/api/appointments/{replacement['id']}/history", headers=headers
        ).json()
        assert new_history[0]["action"] == "CREATED"
        assert new_history[0]["old_time"] == "09:30:00"

    def test_rescheduling_frees_the_old_slot(
        self, client, superadmin, clinics, appointment, auth
    ):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        client.post(
            f"/api/appointments/{appointment['id']}/reschedule",
            headers=headers,
            json={"appointment_date": monday.isoformat(), "start_time": "17:30:00"},
        )
        body = client.get(
            f"/api/appointments/available-slots?clinic_id={clinics[0].id}&date={monday}",
            headers=headers,
        ).json()
        assert next(s for s in body["slots"] if s["time"] == "09:30")["booked"] == 0
        assert next(s for s in body["slots"] if s["time"] == "17:30")["booked"] == 1

    def test_rescheduling_to_a_full_slot_is_refused(
        self, client, superadmin, clinics, patient, second_patient, appointment, auth
    ):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        others = [second_patient]
        for index in range(2):
            others.append(
                client.post(
                    "/api/patients",
                    headers=headers,
                    json={"full_name": f"Blocker {index}", "mobile": f"977766{index:04d}"},
                ).json()
            )
        for person in others:
            book(client, headers, clinics[0].id, person["id"], monday, start="17:30:00")

        response = client.post(
            f"/api/appointments/{appointment['id']}/reschedule",
            headers=headers,
            json={"appointment_date": monday.isoformat(), "start_time": "17:30:00"},
        )
        assert response.status_code == 409
        # And the original is untouched.
        original = client.get(f"/api/appointments/{appointment['id']}", headers=headers).json()
        assert original["status"] == "BOOKED"

    def test_rescheduling_to_the_same_slot_is_refused(
        self, client, superadmin, appointment, auth
    ):
        response = client.post(
            f"/api/appointments/{appointment['id']}/reschedule",
            headers=auth("superadmin"),
            json={
                "appointment_date": appointment["appointment_date"],
                "start_time": appointment["start_time"],
            },
        )
        assert response.status_code == 400
        assert "same as the current one" in response.json()["error"]["message"]

    def test_a_cancelled_appointment_cannot_be_rescheduled(
        self, client, superadmin, appointment, auth
    ):
        headers = auth("superadmin")
        client.post(f"/api/appointments/{appointment['id']}/cancel", headers=headers, json={})
        response = client.post(
            f"/api/appointments/{appointment['id']}/reschedule",
            headers=headers,
            json={
                "appointment_date": next_weekday(MONDAY, 2).isoformat(),
                "start_time": "17:30:00",
            },
        )
        assert response.status_code == 400

    def test_rescheduling_to_another_clinic(
        self, client, superadmin, clinics, appointment, auth
    ):
        response = client.post(
            f"/api/appointments/{appointment['id']}/reschedule",
            headers=auth("superadmin"),
            json={
                "clinic_id": clinics[1].id,
                "appointment_date": next_weekday(MONDAY, 2).isoformat(),
                "start_time": "09:30:00",
            },
        )
        assert response.status_code == 200
        assert response.json()["appointment"]["clinic_name"] == "BTM Layout"


class TestListingAndFilters:
    @pytest.fixture
    def spread(self, client, superadmin, clinics, patient, second_patient, auth):
        headers = auth("superadmin")
        today = date.today()
        made = {}
        # Today (may be a closed day, so insert directly for determinism).
        from app.db.database import SessionLocal
        from app.models import Appointment, AppointmentStatus

        with SessionLocal() as session:
            for index, (label, on_date, status) in enumerate(
                [
                    ("today", today, AppointmentStatus.BOOKED),
                    ("today_done", today, AppointmentStatus.COMPLETED),
                    ("past", today - timedelta(days=3), AppointmentStatus.COMPLETED),
                    ("future", today + timedelta(days=5), AppointmentStatus.BOOKED),
                ]
            ):
                row = Appointment(
                    appointment_code=f"APT-LIST-{index:04d}",
                    patient_id=patient["id"],
                    clinic_id=clinics[0].id,
                    appointment_date=on_date,
                    start_time=time(9 + index, 0),
                    end_time=time(9 + index, 30),
                    duration_minutes=30,
                    status=status,
                )
                session.add(row)
                session.flush()
                made[label] = row.id
            session.commit()
        return made

    def test_window_today(self, client, superadmin, spread, auth):
        body = client.get("/api/appointments?window=today", headers=auth("superadmin")).json()
        assert body["total"] == 2

    def test_window_upcoming(self, client, superadmin, spread, auth):
        body = client.get("/api/appointments?window=upcoming", headers=auth("superadmin")).json()
        assert body["total"] == 1

    def test_window_past(self, client, superadmin, spread, auth):
        body = client.get("/api/appointments?window=past", headers=auth("superadmin")).json()
        assert body["total"] == 1

    def test_status_filter(self, client, superadmin, spread, auth):
        body = client.get(
            "/api/appointments?status=COMPLETED", headers=auth("superadmin")
        ).json()
        assert body["total"] == 2

    def test_search_by_patient_name(self, client, superadmin, spread, auth):
        body = client.get(
            "/api/appointments?search=Rahul", headers=auth("superadmin")
        ).json()
        assert body["total"] == 4

    def test_search_by_appointment_code(self, client, superadmin, spread, auth):
        body = client.get(
            "/api/appointments?search=APT-LIST-0000", headers=auth("superadmin")
        ).json()
        assert body["total"] == 1

    def test_clinic_scoping(self, client, superadmin, btm_user, spread, auth):
        body = client.get("/api/appointments", headers=auth("btm.reception")).json()
        assert body["total"] == 0

    def test_counters(self, client, superadmin, spread, auth):
        body = client.get("/api/appointments/counters", headers=auth("superadmin")).json()
        assert body["today"] == 2
        assert body["completed_today"] == 1
        assert body["pending_today"] == 1
        assert body["upcoming"] == 1
        assert body["past"] == 1

    def test_patient_filter(self, client, superadmin, spread, patient, auth):
        body = client.get(
            f"/api/appointments?patient_id={patient['id']}", headers=auth("superadmin")
        ).json()
        assert body["total"] == 4


class TestConcurrentBooking:
    """The exit criterion for Phase 4.

    Two threads race for the last remaining place in a slot. Exactly one must get
    a 201 and the other a 409, and the database must end up with the configured
    capacity -- never one more.
    """

    def test_two_bookings_for_the_last_slot_produce_one_appointment(
        self, client, superadmin, clinics, db, auth
    ):
        from app.db.database import SessionLocal
        from app.models import Appointment, User
        from app.schemas.appointment import BookExistingPatient
        from app.services import appointment_service
        from app.utils.exceptions import SlotUnavailableError

        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        clinic_id = clinics[0].id

        # Capacity 3: fill two places, leaving exactly one.
        racers = []
        for index in range(4):
            person = client.post(
                "/api/patients",
                headers=headers,
                json={"full_name": f"Racer {index}", "mobile": f"9555500{index:03d}"},
            ).json()
            racers.append(person)
        for person in racers[:2]:
            assert book(client, headers, clinic_id, person["id"], monday).status_code == 201

        results: list[tuple[str, object]] = []
        barrier = threading.Barrier(2)

        def attempt(patient_id: int):
            # A separate session per thread, as two web workers would have.
            session = SessionLocal()
            try:
                actor = session.execute(
                    __import__("sqlalchemy").select(User).where(User.username == "superadmin")
                ).scalar_one()
                payload = BookExistingPatient(
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    appointment_date=monday,
                    start_time=time(9, 30),
                )
                barrier.wait(timeout=10)  # start together
                appointment, _ = appointment_service.book_existing_patient(
                    session, payload, actor
                )
                results.append(("created", appointment.appointment_code))
            except SlotUnavailableError as exc:
                results.append(("refused", str(exc)))
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                results.append(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                session.close()

        threads = [
            threading.Thread(target=attempt, args=(racers[2]["id"],)),
            threading.Thread(target=attempt, args=(racers[3]["id"],)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        outcomes = sorted(kind for kind, _ in results)
        assert outcomes == ["created", "refused"], f"expected one of each, got {results}"

        # The decisive assertion: the slot holds exactly its capacity.
        with SessionLocal() as session:
            from sqlalchemy import func, select

            from app.models import CAPACITY_CONSUMING_STATUSES

            total = session.execute(
                select(func.count())
                .select_from(Appointment)
                .where(
                    Appointment.clinic_id == clinic_id,
                    Appointment.appointment_date == monday,
                    Appointment.start_time == time(9, 30),
                    Appointment.status.in_(CAPACITY_CONSUMING_STATUSES),
                )
            ).scalar_one()
        assert total == 3, f"slot holds {total} appointments, capacity is 3"


class TestAuditTrail:
    def test_booking_and_changes_are_audited(
        self, client, superadmin, clinics, patient, auth
    ):
        from app.db.database import SessionLocal
        from app.models import AuditLog

        headers = auth("superadmin")
        appointment = book(
            client, headers, clinics[0].id, patient["id"], next_weekday(MONDAY)
        ).json()["appointment"]
        client.post(f"/api/appointments/{appointment['id']}/confirm", headers=headers, json={})
        client.post(
            f"/api/appointments/{appointment['id']}/cancel",
            headers=headers,
            json={"reason": "Changed plans"},
        )

        with SessionLocal() as session:
            actions = {log.action.value for log in session.query(AuditLog).all()}
        assert "APPOINTMENT_CREATED" in actions
        assert "APPOINTMENT_CANCELLED" in actions


class TestAppointmentCodeUniqueness:
    """Regression: the reference used to be sequenced per *date* across every
    clinic, so two branches booking at the same instant produced the same code
    and one hit the UNIQUE constraint -- a confusing conflict rather than a
    clean result. The clinic lock does not help there, because it only
    serialises bookings for the *same* clinic."""

    def test_codes_are_scoped_per_clinic(
        self, client, superadmin, clinics, patient, second_patient, auth
    ):
        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        hsr = book(client, headers, clinics[0].id, patient["id"], monday).json()["appointment"]
        btm = book(
            client, headers, clinics[1].id, second_patient["id"], monday
        ).json()["appointment"]

        # Same date, same slot, different clinics -> different, meaningful codes.
        assert hsr["appointment_code"].startswith("APT-HSR-")
        assert btm["appointment_code"].startswith("APT-BTM-")
        assert hsr["appointment_code"] != btm["appointment_code"]
        # Both are the first of the day at their own clinic.
        assert hsr["appointment_code"].endswith("-0001")
        assert btm["appointment_code"].endswith("-0001")

    def test_simultaneous_bookings_at_different_clinics_both_succeed(
        self, client, superadmin, clinics, auth
    ):
        """The exact race that previously raised an IntegrityError."""
        from datetime import time as time_of_day

        from app.db.database import SessionLocal
        from app.models import User
        from app.schemas.appointment import BookExistingPatient
        from app.services import appointment_service

        headers = auth("superadmin")
        monday = next_weekday(MONDAY)
        people = []
        for index in range(2):
            people.append(
                client.post(
                    "/api/patients",
                    headers=headers,
                    json={"full_name": f"Cross {index}", "mobile": f"9444400{index:03d}"},
                ).json()
            )

        results = []
        barrier = threading.Barrier(2)

        def attempt(patient_id, clinic_id):
            import sqlalchemy

            session = SessionLocal()
            try:
                actor = session.execute(
                    sqlalchemy.select(User).where(User.username == "superadmin")
                ).scalar_one()
                payload = BookExistingPatient(
                    clinic_id=clinic_id,
                    patient_id=patient_id,
                    appointment_date=monday,
                    start_time=time_of_day(9, 30),
                )
                barrier.wait(timeout=10)
                appointment, _ = appointment_service.book_existing_patient(
                    session, payload, actor
                )
                results.append(("created", appointment.appointment_code))
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                results.append(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                session.close()

        threads = [
            threading.Thread(target=attempt, args=(people[0]["id"], clinics[0].id)),
            threading.Thread(target=attempt, args=(people[1]["id"], clinics[1].id)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        kinds = sorted(kind for kind, _ in results)
        assert kinds == ["created", "created"], f"both should succeed, got {results}"
        codes = {code for _, code in results}
        assert len(codes) == 2, f"codes collided: {codes}"
