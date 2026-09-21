"""Phase 5: treatment packages and session logging.

The invariant under test throughout: `sessions_taken` moves only by logging or
voiding, `remaining = registered - taken`, and it never leaves 0..registered.
"""

from datetime import date, time, timedelta
from decimal import Decimal

import pytest


def next_weekday(target: int = 0, weeks_ahead: int = 1) -> date:
    today = date.today()
    ahead = (target - today.weekday()) % 7
    return today + timedelta(days=ahead + 7 * weeks_ahead)


MONDAY = 0


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
def package(client, superadmin, patient, clinics, auth):
    return client.post(
        f"/api/patients/{patient['id']}/packages",
        headers=auth("superadmin"),
        json={
            "sessions_registered": 10,
            "price_per_session": "500.00",
            "clinic_id": clinics[0].id,
        },
    ).json()


def log(client, headers, patient_id, **body):
    return client.post(f"/api/patients/{patient_id}/sessions", headers=headers, json=body)


class TestPackages:
    def test_register_a_package(self, client, superadmin, patient, clinics, auth):
        response = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=auth("superadmin"),
            json={
                "sessions_registered": 10,
                "price_per_session": "500.00",
                "clinic_id": clinics[0].id,
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["sessions_registered"] == 10
        assert body["sessions_taken"] == 0
        assert body["sessions_remaining"] == 10
        assert body["total_amount"] == "5000.00"
        assert body["status"] == "ACTIVE"
        assert body["package_name"] == "10-session physiotherapy package"

    def test_zero_sessions_is_rejected(self, client, superadmin, patient, auth):
        response = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=auth("superadmin"),
            json={"sessions_registered": 0},
        )
        assert response.status_code == 422

    def test_a_mid_course_package_can_record_sessions_already_taken(
        self, client, superadmin, patient, clinics, auth
    ):
        response = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=auth("superadmin"),
            json={
                "sessions_registered": 10,
                "sessions_taken": 4,
                "clinic_id": clinics[0].id,
            },
        )
        assert response.status_code == 201
        assert response.json()["sessions_remaining"] == 6

    def test_taken_cannot_exceed_registered_on_creation(
        self, client, superadmin, patient, auth
    ):
        response = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=auth("superadmin"),
            json={"sessions_registered": 5, "sessions_taken": 6},
        )
        assert response.status_code == 422

    def test_a_fully_delivered_package_is_created_closed(
        self, client, superadmin, patient, clinics, auth
    ):
        response = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=auth("superadmin"),
            json={
                "sessions_registered": 5,
                "sessions_taken": 5,
                "clinic_id": clinics[0].id,
            },
        )
        assert response.json()["status"] == "COMPLETED"

    def test_sessions_taken_cannot_be_set_by_update(
        self, client, superadmin, package, auth
    ):
        """It moves only by logging or voiding a session."""
        response = client.put(
            f"/api/packages/{package['id']}",
            headers=auth("superadmin"),
            json={"sessions_taken": 99},
        )
        assert response.status_code == 422

    def test_a_package_cannot_shrink_below_what_is_logged(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        for _ in range(3):
            log(client, headers, patient["id"])
        response = client.put(
            f"/api/packages/{package['id']}", headers=headers, json={"sessions_registered": 2}
        )
        assert response.status_code == 400
        assert "already has 3 session(s) logged" in response.json()["error"]["message"]

    def test_cancel_a_package(self, client, superadmin, package, auth):
        response = client.post(
            f"/api/packages/{package['id']}/cancel",
            headers=auth("superadmin"),
            json={"reason": "Patient moved city"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"

    def test_a_cancelled_package_cannot_be_used(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        client.post(f"/api/packages/{package['id']}/cancel", headers=headers, json={})
        response = log(client, headers, patient["id"], package_id=package["id"])
        assert response.status_code == 400
        assert "cancelled" in response.json()["error"]["message"]

    def test_a_cancelled_package_has_nothing_remaining(
        self, client, superadmin, patient, package, auth
    ):
        """It cannot be used, so it must not read as usable either."""
        headers = auth("superadmin")
        body = client.post(
            f"/api/packages/{package['id']}/cancel", headers=headers, json={}
        ).json()

        # The registration is kept as history...
        assert body["sessions_registered"] == 10
        # ...but nothing is available from it.
        assert body["sessions_remaining"] == 0

    def test_cancelled_packages_are_left_out_of_the_profile_totals(
        self, client, superadmin, patient, package, auth
    ):
        """A patient who cancelled a course does not still 'have' its sessions.

        The row stays in `packages` -- deleting history is never the answer --
        but reception reads the headline numbers, and those must describe what
        the patient can actually use today.
        """
        headers = auth("superadmin")
        before = client.get(f"/api/patients/{patient['id']}/profile", headers=headers).json()
        assert before["total_sessions_registered"] == 10
        assert before["total_sessions_remaining"] == 10
        assert before["cancelled_package_count"] == 0

        client.post(f"/api/packages/{package['id']}/cancel", headers=headers, json={})

        after = client.get(f"/api/patients/{patient['id']}/profile", headers=headers).json()
        assert after["total_sessions_registered"] == 0
        assert after["total_sessions_taken"] == 0
        assert after["total_sessions_remaining"] == 0
        assert after["cancelled_package_count"] == 1
        # Still listed, still showing what was originally registered.
        assert len(after["packages"]) == 1
        assert after["packages"][0]["status"] == "CANCELLED"
        assert after["packages"][0]["sessions_registered"] == 10

    def test_cancelling_one_package_leaves_the_others_counted(
        self, client, superadmin, patient, package, clinics, auth
    ):
        headers = auth("superadmin")
        second = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={"sessions_registered": 4, "clinic_id": clinics[0].id, "skip_billing": True},
        ).json()

        client.post(f"/api/packages/{package['id']}/cancel", headers=headers, json={})

        profile = client.get(f"/api/patients/{patient['id']}/profile", headers=headers).json()
        assert profile["total_sessions_registered"] == 4
        assert profile["total_sessions_remaining"] == 4
        assert profile["cancelled_package_count"] == 1
        assert second["sessions_registered"] == 4

    def test_session_context_totals_ignore_cancelled_packages(
        self, client, superadmin, patient, package, auth
    ):
        """The session dialog header must agree with the profile."""
        headers = auth("superadmin")
        client.post(f"/api/packages/{package['id']}/cancel", headers=headers, json={})

        context = client.get(
            f"/api/patients/{patient['id']}/session-context", headers=headers
        ).json()
        assert context["total_sessions_registered"] == 0
        assert context["total_sessions_remaining"] == 0
        assert context["suggested_package_id"] is None
        assert any("no active treatment package" in w for w in context["warnings"])

    def test_delivered_sessions_survive_cancelling_the_package(
        self, client, superadmin, patient, package, auth
    ):
        """Cancelling forfeits what is left, not what was already delivered."""
        headers = auth("superadmin")
        log(client, headers, patient["id"], package_id=package["id"])
        log(client, headers, patient["id"], package_id=package["id"])
        client.post(f"/api/packages/{package['id']}/cancel", headers=headers, json={})

        profile = client.get(f"/api/patients/{patient['id']}/profile", headers=headers).json()
        # The two visits happened and stay on the record...
        assert len(profile["sessions"]) == 2
        assert profile["packages"][0]["sessions_taken"] == 2
        # ...while the eight unused sessions are no longer claimable.
        assert profile["total_sessions_remaining"] == 0

    def test_clinic_user_cannot_read_another_clinics_package(
        self, client, superadmin, btm_user, patient, package, auth
    ):
        response = client.get(
            f"/api/patients/{patient['id']}/packages", headers=auth("btm.reception")
        )
        assert response.status_code == 403


class TestLoggingSessions:
    def test_log_a_session_and_increment_the_counter(
        self, client, superadmin, patient, package, auth
    ):
        response = log(
            client,
            auth("superadmin"),
            patient["id"],
            treatment_provided="Manual therapy + exercises",
            notes="Reported reduced pain",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["session"]["session_number"] == 1
        assert body["session"]["treatment_provided"] == "Manual therapy + exercises"
        assert body["session"]["package_progress"] == "1 of 10"
        assert body["package"]["sessions_taken"] == 1
        assert body["package"]["sessions_remaining"] == 9

    def test_session_numbers_increment_per_package(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        numbers = [
            log(client, headers, patient["id"]).json()["session"]["session_number"]
            for _ in range(3)
        ]
        assert numbers == [1, 2, 3]

    def test_the_therapist_defaults_to_the_recording_user(
        self, client, superadmin, hsr_user, patient, package, auth
    ):
        response = log(client, auth("hsr.reception"), patient["id"])
        assert response.json()["session"]["therapist_name"] == "Hsr Reception"

    def test_the_therapist_can_be_someone_else(
        self, client, superadmin, hsr_user, patient, package, db, auth
    ):
        from tests.conftest import _make_user
        from app.models import RoleName, Role

        roles = {r.name: r for r in db.query(Role).all()}
        physio = _make_user(
            db, roles, "hsr.physio", RoleName.CLINIC_USER, clinic=None
        )
        response = log(
            client, auth("hsr.reception"), patient["id"], therapist_user_id=physio.id
        )
        assert response.json()["session"]["therapist_name"] == "Hsr Physio"

    def test_the_last_session_closes_the_package(
        self, client, superadmin, patient, clinics, auth
    ):
        headers = auth("superadmin")
        small = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={"sessions_registered": 2, "clinic_id": clinics[0].id},
        ).json()

        log(client, headers, patient["id"], package_id=small["id"])
        final = log(client, headers, patient["id"], package_id=small["id"])

        assert final.json()["package"]["status"] == "COMPLETED"
        assert final.json()["package"]["sessions_remaining"] == 0
        assert any("last session" in w for w in final.json()["warnings"])

    def test_a_full_package_cannot_take_another_session(
        self, client, superadmin, patient, clinics, auth
    ):
        headers = auth("superadmin")
        small = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={"sessions_registered": 1, "clinic_id": clinics[0].id},
        ).json()
        log(client, headers, patient["id"], package_id=small["id"])

        response = log(client, headers, patient["id"], package_id=small["id"])
        assert response.status_code == 400
        assert "fully used" in response.json()["error"]["message"]

    def test_low_balance_is_warned_about(self, client, superadmin, patient, clinics, auth):
        headers = auth("superadmin")
        small = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={"sessions_registered": 3, "clinic_id": clinics[0].id},
        ).json()
        response = log(client, headers, patient["id"], package_id=small["id"])
        assert any("2 session(s) remaining" in w for w in response.json()["warnings"])

    def test_a_future_dated_session_is_rejected(
        self, client, superadmin, patient, package, auth
    ):
        response = log(
            client,
            auth("superadmin"),
            patient["id"],
            session_date=(date.today() + timedelta(days=1)).isoformat(),
        )
        assert response.status_code == 422

    def test_a_package_from_another_patient_is_rejected(
        self, client, superadmin, patient, package, clinics, auth
    ):
        headers = auth("superadmin")
        other = client.post(
            "/api/patients",
            headers=headers,
            json={"full_name": "Someone Else", "mobile": "9876500123"},
        ).json()
        response = log(client, headers, other["id"], package_id=package["id"])
        assert response.status_code == 400
        assert "different patient" in response.json()["error"]["message"]


class TestSessionsWithoutAPackage:
    def test_an_assessment_can_be_logged_without_a_package(
        self, client, superadmin, patient, auth
    ):
        response = log(
            client,
            auth("superadmin"),
            patient["id"],
            no_package=True,
            treatment_provided="Initial assessment",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["session"]["package_id"] is None
        assert body["session"]["package_progress"] is None
        assert body["package"] is None

    def test_no_package_does_not_touch_an_existing_package(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        log(client, headers, patient["id"], no_package=True)
        packages = client.get(
            f"/api/patients/{patient['id']}/packages", headers=headers
        ).json()
        assert packages[0]["sessions_taken"] == 0

    def test_package_less_sessions_are_numbered_per_patient(
        self, client, superadmin, patient, auth
    ):
        headers = auth("superadmin")
        numbers = [
            log(client, headers, patient["id"], no_package=True).json()["session"][
                "session_number"
            ]
            for _ in range(3)
        ]
        assert numbers == [1, 2, 3]

    def test_logging_with_no_active_package_warns_and_still_records(
        self, client, superadmin, patient, auth
    ):
        response = log(client, auth("superadmin"), patient["id"])
        assert response.status_code == 201
        assert response.json()["session"]["package_id"] is None
        assert any("without consuming one" in w for w in response.json()["warnings"])

    def test_package_and_no_package_together_is_rejected(
        self, client, superadmin, patient, package, auth
    ):
        response = log(
            client,
            auth("superadmin"),
            patient["id"],
            package_id=package["id"],
            no_package=True,
        )
        assert response.status_code == 422


class TestMultiplePackages:
    """Decision: multiple active packages allowed; the oldest with room is used."""

    @pytest.fixture
    def two_packages(self, client, superadmin, patient, clinics, auth):
        headers = auth("superadmin")
        first = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 2,
                "clinic_id": clinics[0].id,
                "start_date": (date.today() - timedelta(days=30)).isoformat(),
                "package_name": "Older",
            },
        ).json()
        second = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 5,
                "clinic_id": clinics[0].id,
                "start_date": date.today().isoformat(),
                "package_name": "Newer",
            },
        ).json()
        return first, second

    def test_the_oldest_package_is_consumed_first(
        self, client, superadmin, patient, two_packages, auth
    ):
        older, _ = two_packages
        response = log(client, auth("superadmin"), patient["id"])
        assert response.json()["package"]["id"] == older["id"]
        assert response.json()["package"]["package_name"] == "Older"

    def test_consumption_moves_on_when_the_oldest_fills_up(
        self, client, superadmin, patient, two_packages, auth
    ):
        older, newer = two_packages
        headers = auth("superadmin")
        # The older package holds 2.
        log(client, headers, patient["id"])
        log(client, headers, patient["id"])
        third = log(client, headers, patient["id"])
        assert third.json()["package"]["id"] == newer["id"]
        assert third.json()["package"]["sessions_taken"] == 1

    def test_a_specific_package_can_be_chosen(
        self, client, superadmin, patient, two_packages, auth
    ):
        _, newer = two_packages
        response = log(client, auth("superadmin"), patient["id"], package_id=newer["id"])
        assert response.json()["package"]["id"] == newer["id"]

    def test_the_context_endpoint_suggests_the_oldest_and_warns(
        self, client, superadmin, patient, two_packages, auth
    ):
        older, _ = two_packages
        body = client.get(
            f"/api/patients/{patient['id']}/session-context", headers=auth("superadmin")
        ).json()
        assert body["suggested_package_id"] == older["id"]
        assert body["next_session_number"] == 1
        assert len(body["packages"]) == 2
        assert any("2 active packages" in w for w in body["warnings"])
        assert body["total_sessions_registered"] == 7
        assert body["total_sessions_remaining"] == 7


class TestVoiding:
    """Decision: a mis-logged session is voided, not deleted."""

    def test_voiding_returns_the_place_to_the_package(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        logged = log(client, headers, patient["id"]).json()
        assert logged["package"]["sessions_taken"] == 1

        response = client.post(
            f"/api/sessions/{logged['session']['id']}/void",
            headers=headers,
            json={"reason": "Logged against the wrong patient"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["session"]["is_voided"] is True
        assert body["session"]["void_reason"] == "Logged against the wrong patient"
        assert body["session"]["voided_by"]
        assert body["package"]["sessions_taken"] == 0
        assert body["package"]["sessions_remaining"] == 10

    def test_a_voided_session_is_kept_not_deleted(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        logged = log(client, headers, patient["id"]).json()
        client.post(
            f"/api/sessions/{logged['session']['id']}/void",
            headers=headers,
            json={"reason": "Mistake"},
        )
        # Hidden by default, visible on request.
        assert (
            client.get(f"/api/patients/{patient['id']}/sessions", headers=headers).json()
            == []
        )
        with_voided = client.get(
            f"/api/patients/{patient['id']}/sessions?include_voided=true", headers=headers
        ).json()
        assert len(with_voided) == 1
        assert with_voided[0]["is_voided"] is True

    def test_session_numbers_are_not_reused(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        first = log(client, headers, patient["id"]).json()
        client.post(
            f"/api/sessions/{first['session']['id']}/void",
            headers=headers,
            json={"reason": "Mistake"},
        )
        second = log(client, headers, patient["id"]).json()
        assert second["session"]["session_number"] == 2, "the sequence must not rewind"
        assert second["package"]["sessions_taken"] == 1

    def test_voiding_reopens_a_completed_package(
        self, client, superadmin, patient, clinics, auth
    ):
        headers = auth("superadmin")
        small = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={"sessions_registered": 1, "clinic_id": clinics[0].id},
        ).json()
        logged = log(client, headers, patient["id"], package_id=small["id"]).json()
        assert logged["package"]["status"] == "COMPLETED"

        response = client.post(
            f"/api/sessions/{logged['session']['id']}/void",
            headers=headers,
            json={"reason": "Did not attend after all"},
        )
        assert response.json()["package"]["status"] == "ACTIVE"

    def test_voiding_twice_is_refused(self, client, superadmin, patient, package, auth):
        headers = auth("superadmin")
        logged = log(client, headers, patient["id"]).json()
        body = {"reason": "Mistake"}
        assert (
            client.post(
                f"/api/sessions/{logged['session']['id']}/void", headers=headers, json=body
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/sessions/{logged['session']['id']}/void", headers=headers, json=body
            ).status_code
            == 400
        )

    def test_a_reason_is_required(self, client, superadmin, patient, package, auth):
        headers = auth("superadmin")
        logged = log(client, headers, patient["id"]).json()
        response = client.post(
            f"/api/sessions/{logged['session']['id']}/void", headers=headers, json={"reason": ""}
        )
        assert response.status_code == 422

    def test_a_voided_session_cannot_be_edited(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        logged = log(client, headers, patient["id"]).json()
        client.post(
            f"/api/sessions/{logged['session']['id']}/void",
            headers=headers,
            json={"reason": "Mistake"},
        )
        response = client.put(
            f"/api/sessions/{logged['session']['id']}",
            headers=headers,
            json={"notes": "trying to edit"},
        )
        assert response.status_code == 400


class TestEditingNotes:
    def test_notes_can_be_corrected(self, client, superadmin, patient, package, auth):
        headers = auth("superadmin")
        logged = log(client, headers, patient["id"], notes="Typo hree").json()
        response = client.put(
            f"/api/sessions/{logged['session']['id']}",
            headers=headers,
            json={"notes": "Corrected note", "remarks": "Patient tolerated well"},
        )
        assert response.status_code == 200
        assert response.json()["notes"] == "Corrected note"
        assert response.json()["remarks"] == "Patient tolerated well"

    def test_the_date_cannot_be_changed(self, client, superadmin, patient, package, auth):
        """Moving a session between days would silently move it between counters."""
        headers = auth("superadmin")
        logged = log(client, headers, patient["id"]).json()
        response = client.put(
            f"/api/sessions/{logged['session']['id']}",
            headers=headers,
            json={"session_date": "2020-01-01"},
        )
        assert response.status_code == 422

    def test_the_package_cannot_be_changed(self, client, superadmin, patient, package, auth):
        headers = auth("superadmin")
        logged = log(client, headers, patient["id"]).json()
        response = client.put(
            f"/api/sessions/{logged['session']['id']}", headers=headers, json={"package_id": 99}
        )
        assert response.status_code == 422


class TestAppointmentIntegration:
    """Decision: completing an appointment and logging its session are one
    transaction, so the two counts cannot drift."""

    @pytest.fixture
    def appointment(self, client, superadmin, patient, clinics, auth):
        return client.post(
            "/api/appointments",
            headers=auth("superadmin"),
            json={
                "clinic_id": clinics[0].id,
                "patient_id": patient["id"],
                "appointment_date": next_weekday(MONDAY).isoformat(),
                "start_time": "09:30:00",
            },
        ).json()["appointment"]

    def test_logging_a_session_completes_the_appointment(
        self, client, superadmin, patient, package, appointment, auth
    ):
        headers = auth("superadmin")
        response = log(
            client,
            headers,
            patient["id"],
            appointment_id=appointment["id"],
            treatment_provided="Manual therapy",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["appointment_completed"] is True
        assert body["session"]["appointment_code"] == appointment["appointment_code"]
        assert body["package"]["sessions_taken"] == 1

        refreshed = client.get(
            f"/api/appointments/{appointment['id']}", headers=headers
        ).json()
        assert refreshed["status"] == "COMPLETED"
        assert refreshed["completed_at"]

    def test_the_appointment_history_records_the_reason(
        self, client, superadmin, patient, package, appointment, auth
    ):
        headers = auth("superadmin")
        log(client, headers, patient["id"], appointment_id=appointment["id"])
        history = client.get(
            f"/api/appointments/{appointment['id']}/history", headers=headers
        ).json()
        completed = next(e for e in history if e["action"] == "COMPLETED")
        assert completed["reason"] == "Session recorded"

    def test_an_already_completed_appointment_still_accepts_its_session(
        self, client, superadmin, patient, package, appointment, auth
    ):
        headers = auth("superadmin")
        client.post(f"/api/appointments/{appointment['id']}/complete", headers=headers, json={})
        response = log(client, headers, patient["id"], appointment_id=appointment["id"])
        assert response.status_code == 201
        assert response.json()["appointment_completed"] is False

    def test_two_sessions_for_one_appointment_are_refused(
        self, client, superadmin, patient, package, appointment, auth
    ):
        headers = auth("superadmin")
        log(client, headers, patient["id"], appointment_id=appointment["id"])
        response = log(client, headers, patient["id"], appointment_id=appointment["id"])
        assert response.status_code == 400
        assert "already recorded" in response.json()["error"]["message"]

    def test_voiding_frees_the_appointment_to_be_re_recorded(
        self, client, superadmin, patient, package, appointment, auth
    ):
        headers = auth("superadmin")
        first = log(client, headers, patient["id"], appointment_id=appointment["id"]).json()
        client.post(
            f"/api/sessions/{first['session']['id']}/void",
            headers=headers,
            json={"reason": "Wrong therapist recorded"},
        )
        again = log(client, headers, patient["id"], appointment_id=appointment["id"])
        assert again.status_code == 201

    def test_a_cancelled_appointment_cannot_have_a_session(
        self, client, superadmin, patient, package, appointment, auth
    ):
        headers = auth("superadmin")
        client.post(f"/api/appointments/{appointment['id']}/cancel", headers=headers, json={})
        response = log(client, headers, patient["id"], appointment_id=appointment["id"])
        assert response.status_code == 400
        assert "cannot be completed" in response.json()["error"]["message"]

    def test_an_appointment_for_another_patient_is_refused(
        self, client, superadmin, patient, package, appointment, auth
    ):
        headers = auth("superadmin")
        other = client.post(
            "/api/patients",
            headers=headers,
            json={"full_name": "Other Person", "mobile": "9876500999"},
        ).json()
        response = log(client, headers, other["id"], appointment_id=appointment["id"])
        assert response.status_code == 400
        assert "different patient" in response.json()["error"]["message"]

    def test_a_failed_session_does_not_complete_the_appointment(
        self, client, superadmin, patient, clinics, appointment, auth
    ):
        """One transaction: if the counter cannot move, nothing moves."""
        headers = auth("superadmin")
        full = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 1,
                "sessions_taken": 1,
                "clinic_id": clinics[0].id,
            },
        ).json()
        response = log(
            client,
            headers,
            patient["id"],
            appointment_id=appointment["id"],
            package_id=full["id"],
        )
        assert response.status_code == 400

        refreshed = client.get(
            f"/api/appointments/{appointment['id']}", headers=headers
        ).json()
        assert refreshed["status"] == "BOOKED", "the appointment must not have completed"


class TestListingAndScoping:
    def test_sessions_are_clinic_scoped(
        self, client, superadmin, hsr_user, btm_user, patient, package, auth
    ):
        headers = auth("superadmin")
        log(client, headers, patient["id"])

        assert client.get("/api/sessions", headers=auth("hsr.reception")).json()["total"] == 1
        assert client.get("/api/sessions", headers=auth("btm.reception")).json()["total"] == 0

    def test_filter_by_date_range(self, client, superadmin, patient, package, auth):
        headers = auth("superadmin")
        log(client, headers, patient["id"], session_date=(date.today() - timedelta(days=10)).isoformat())
        log(client, headers, patient["id"])

        recent = client.get(
            f"/api/sessions?from={date.today().isoformat()}", headers=headers
        ).json()
        assert recent["total"] == 1

    def test_search_by_patient(self, client, superadmin, patient, package, auth):
        headers = auth("superadmin")
        log(client, headers, patient["id"])
        assert client.get("/api/sessions?search=Rahul", headers=headers).json()["total"] == 1
        assert client.get("/api/sessions?search=Nobody", headers=headers).json()["total"] == 0

    def test_counters(self, client, superadmin, patient, package, auth):
        headers = auth("superadmin")
        log(client, headers, patient["id"])
        log(client, headers, patient["id"], session_date=(date.today() - timedelta(days=40)).isoformat())
        body = client.get("/api/sessions/counters", headers=headers).json()
        assert body["today"] == 1
        assert body["total"] == 2

    def test_voided_sessions_are_excluded_from_counters(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        logged = log(client, headers, patient["id"]).json()
        client.post(
            f"/api/sessions/{logged['session']['id']}/void",
            headers=headers,
            json={"reason": "Mistake"},
        )
        assert client.get("/api/sessions/counters", headers=headers).json()["total"] == 0

    def test_the_patient_profile_reflects_the_counters(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        for _ in range(4):
            log(client, headers, patient["id"])
        profile = client.get(f"/api/patients/{patient['id']}/profile", headers=headers).json()
        assert profile["total_sessions_registered"] == 10
        assert profile["total_sessions_taken"] == 4
        assert profile["total_sessions_remaining"] == 6
        assert len(profile["sessions"]) == 4


class TestDatabaseInvariant:
    def test_the_counter_can_never_exceed_the_registered_total(
        self, client, superadmin, patient, clinics, db, auth
    ):
        """Belt and braces: the API refuses, and the CHECK constraint would too."""
        from sqlalchemy.exc import IntegrityError

        from app.models import TreatmentPackage

        headers = auth("superadmin")
        created = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={"sessions_registered": 3, "clinic_id": clinics[0].id},
        ).json()

        package = db.get(TreatmentPackage, created["id"])
        package.sessions_taken = 4
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_remaining_is_always_registered_minus_taken(
        self, client, superadmin, patient, package, auth
    ):
        headers = auth("superadmin")
        for expected_taken in range(1, 6):
            body = log(client, headers, patient["id"]).json()["package"]
            assert body["sessions_taken"] == expected_taken
            assert (
                body["sessions_remaining"]
                == body["sessions_registered"] - body["sessions_taken"]
            )


class TestAuditTrail:
    def test_package_and_session_actions_are_audited(
        self, client, superadmin, patient, package, auth
    ):
        from app.db.database import SessionLocal
        from app.models import AuditLog

        headers = auth("superadmin")
        logged = log(client, headers, patient["id"]).json()
        client.post(
            f"/api/sessions/{logged['session']['id']}/void",
            headers=headers,
            json={"reason": "Mistake"},
        )

        with SessionLocal() as session:
            entries = {
                (log_row.entity_type, log_row.action.value)
                for log_row in session.query(AuditLog).all()
            }
        assert ("treatment_package", "CREATED") in entries
        assert ("patient_session", "SESSION_RECORDED") in entries
        assert ("patient_session", "UPDATED") in entries


class TestPackageGapVisibility:
    """Decision: flag patients being treated with no package, do not block them."""

    def test_the_flag_reflects_reality(self, client, superadmin, patient, auth):
        headers = auth("superadmin")
        before = client.get(f"/api/patients/{patient['id']}", headers=headers).json()
        assert before["has_active_package"] is False

        client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={"sessions_registered": 5},
        )
        after = client.get(f"/api/patients/{patient['id']}", headers=headers).json()
        assert after["has_active_package"] is True

    def test_a_used_up_package_no_longer_counts(
        self, client, superadmin, patient, clinics, auth
    ):
        headers = auth("superadmin")
        small = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={"sessions_registered": 1, "clinic_id": clinics[0].id},
        ).json()
        log(client, headers, patient["id"], package_id=small["id"])

        body = client.get(f"/api/patients/{patient['id']}", headers=headers).json()
        assert body["has_active_package"] is False, "a full package is not an active one"

    def test_the_filter_finds_the_gap(self, client, superadmin, patient, clinics, auth):
        headers = auth("superadmin")
        with_package = client.post(
            "/api/patients",
            headers=headers,
            json={
                "full_name": "Has Package",
                "mobile": "9876500321",
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        client.post(
            f"/api/patients/{with_package['id']}/packages",
            headers=headers,
            json={"sessions_registered": 5, "clinic_id": clinics[0].id},
        )

        without = client.get(
            "/api/patients?has_active_package=false&page_size=50", headers=headers
        ).json()
        with_ = client.get(
            "/api/patients?has_active_package=true&page_size=50", headers=headers
        ).json()

        assert patient["full_name"] in [p["full_name"] for p in without["items"]]
        assert "Has Package" in [p["full_name"] for p in with_["items"]]
        assert "Has Package" not in [p["full_name"] for p in without["items"]]

    def test_the_list_flag_is_populated_for_every_row(
        self, client, superadmin, patient, auth
    ):
        body = client.get("/api/patients?page_size=50", headers=auth("superadmin")).json()
        assert body["items"], "expected at least one patient"
        assert all(p["has_active_package"] is not None for p in body["items"])

    def test_a_session_is_still_allowed_without_a_package(
        self, client, superadmin, patient, auth
    ):
        """Flag, don't block: an assessment legitimately has no package."""
        response = log(client, auth("superadmin"), patient["id"], no_package=True)
        assert response.status_code == 201


class TestFutureStartDate:
    """A patient pays today and begins the course later.

    Everyday at the desk, and previously impossible from the appointment-complete
    flow: the inline panel sent no start date, so the package always began today
    and the session being completed silently consumed session 1 of a course the
    patient had not started.
    """

    def test_a_package_can_start_in_the_future(
        self, client, superadmin, patient, clinics, auth
    ):
        start = date.today() + timedelta(days=7)
        created = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=auth("superadmin"),
            json={
                "sessions_registered": 21,
                "price_per_session": "500.00",
                "start_date": start.isoformat(),
                "clinic_id": clinics[0].id,
                "payment": {"amount": "10500.00", "payment_method": "UPI"},
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()

        assert body["start_date"] == start.isoformat()
        # Nothing delivered yet: the whole course is still owed.
        assert body["sessions_taken"] == 0
        assert body["sessions_remaining"] == 21

    def test_the_invoice_is_dated_today_not_by_the_course_start(
        self, client, superadmin, patient, clinics, auth
    ):
        """The money changed hands today, whatever the treatment plan says."""
        start = date.today() + timedelta(days=30)
        body = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=auth("superadmin"),
            json={
                "sessions_registered": 10,
                "price_per_session": "500.00",
                "start_date": start.isoformat(),
                "clinic_id": clinics[0].id,
                "payment": {"amount": "5000.00", "payment_method": "CASH"},
            },
        ).json()

        assert body["bill"]["bill_date"] == date.today().isoformat()
        assert body["start_date"] == start.isoformat()

    def test_logging_a_session_before_the_course_starts_warns(
        self, client, superadmin, patient, clinics, auth
    ):
        """Reported, not refused: a genuine backdated visit must still be enterable."""
        headers = auth("superadmin")
        created = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 5,
                "price_per_session": "500.00",
                "start_date": (date.today() + timedelta(days=3)).isoformat(),
                "clinic_id": clinics[0].id,
                "skip_billing": True,
            },
        ).json()

        result = log(client, headers, patient["id"], package_id=created["id"])
        assert result.status_code == 201
        warnings = result.json()["warnings"]
        assert any("before the package starts" in w for w in warnings), warnings

    def test_a_session_on_or_after_the_start_date_does_not_warn(
        self, client, superadmin, patient, clinics, auth
    ):
        headers = auth("superadmin")
        created = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 5,
                "price_per_session": "500.00",
                "start_date": date.today().isoformat(),
                "clinic_id": clinics[0].id,
                "skip_billing": True,
            },
        ).json()

        result = log(client, headers, patient["id"], package_id=created["id"])
        assert result.status_code == 201
        assert not any(
            "before the package starts" in w for w in result.json()["warnings"]
        )

    def test_a_future_package_is_still_available_to_pick(
        self, client, superadmin, patient, clinics, auth
    ):
        """When the patient arrives next week it must be selectable."""
        headers = auth("superadmin")
        client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 8,
                "price_per_session": "400.00",
                "start_date": (date.today() + timedelta(days=5)).isoformat(),
                "clinic_id": clinics[0].id,
                "skip_billing": True,
            },
        )
        context = client.get(
            f"/api/patients/{patient['id']}/session-context", headers=headers
        ).json()
        assert any(p["sessions_remaining"] == 8 for p in context["packages"])
