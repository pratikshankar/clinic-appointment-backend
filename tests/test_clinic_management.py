"""Phase 2: clinic CRUD, configuration, holidays, staff and patient sources.

The emphasis is on the validation rules, because those are what protect Phase 4's
slot engine from configuration that cannot produce sane slots.
"""

from datetime import date, timedelta

import pytest

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def shift(day, open_time="09:00:00", close_time="13:00:00", label=None):
    return {
        "day_of_week": day,
        "open_time": open_time,
        "close_time": close_time,
        "is_closed": False,
        "label": label,
    }


SPLIT_WEEK = [shift(d, "09:00:00", "13:00:00", "Morning") for d in range(6)] + [
    shift(d, "16:00:00", "20:00:00", "Evening") for d in range(6)
]


class TestCreateClinic:
    def test_create_with_minimal_payload(self, client, superadmin, auth):
        response = client.post(
            "/api/clinics",
            headers=auth("superadmin"),
            json={"name": "Koramangala", "city": "Bengaluru"},
        )
        assert response.status_code == 201
        clinic = response.json()["clinic"]
        assert clinic["name"] == "Koramangala"
        assert clinic["status"] == "ACTIVE"
        # Defaults applied, and Mon-Sat split shifts seeded.
        assert clinic["slot_duration_minutes"] == 30
        assert clinic["capacity_per_slot"] == 3
        assert len(clinic["working_hours"]) == 12

    def test_code_is_derived_from_the_name(self, client, superadmin, auth):
        response = client.post(
            "/api/clinics", headers=auth("superadmin"), json={"name": "Whitefield"}
        )
        assert response.json()["clinic"]["code"] == "WHIT"

    def test_derived_code_is_deduplicated(self, client, superadmin, clinics, auth):
        # "HSR Extension" -> initials "HE" are too short, so it falls back to the
        # first word "HSR", which the fixture already uses -> suffixed.
        response = client.post(
            "/api/clinics", headers=auth("superadmin"), json={"name": "HSR Extension"}
        )
        assert response.status_code == 201
        assert response.json()["clinic"]["code"] == "HSR2"

    def test_create_with_full_configuration(self, client, superadmin, auth):
        response = client.post(
            "/api/clinics",
            headers=auth("superadmin"),
            json={
                "name": "Indiranagar",
                "address": "100 Feet Road",
                "location": "Indiranagar",
                "city": "Bengaluru",
                "state": "Karnataka",
                "pin_code": "560038",
                "phone": "+91 98765 12345",
                "email": "indiranagar@clinic.example.com",
                "slot_duration_minutes": 20,
                "capacity_per_slot": 2,
                "working_hours": SPLIT_WEEK,
                "breaks": [
                    {
                        "day_of_week": None,
                        "start_time": "11:00:00",
                        "end_time": "11:20:00",
                        "label": "Tea break",
                    }
                ],
            },
        )
        assert response.status_code == 201
        clinic = response.json()["clinic"]
        assert clinic["phone"] == "9876512345"  # normalised
        assert clinic["slot_duration_minutes"] == 20
        assert len(clinic["breaks"]) == 1
        # 8 working hours a day at 20 minutes = 24 slots, minus the tea break.
        assert clinic["weekly_slot_counts"]["0"] == 23

    def test_duplicate_name_conflicts(self, client, superadmin, clinics, auth):
        response = client.post(
            "/api/clinics", headers=auth("superadmin"), json={"name": "HSR Layout"}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "duplicate_resource"

    def test_explicit_duplicate_code_conflicts(self, client, superadmin, clinics, auth):
        response = client.post(
            "/api/clinics",
            headers=auth("superadmin"),
            json={"name": "Another Clinic", "code": "HSR"},
        )
        assert response.status_code == 409

    def test_overlapping_shifts_are_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/clinics",
            headers=auth("superadmin"),
            json={
                "name": "Overlap Clinic",
                "working_hours": [
                    shift(MON, "09:00:00", "13:00:00"),
                    shift(MON, "12:00:00", "18:00:00"),
                ],
            },
        )
        assert response.status_code == 400
        assert "overlap" in response.json()["error"]["message"].lower()

    def test_touching_shifts_are_allowed(self, client, superadmin, auth):
        """13:00-13:00 is a boundary, not an overlap."""
        response = client.post(
            "/api/clinics",
            headers=auth("superadmin"),
            json={
                "name": "Back To Back",
                "working_hours": [
                    shift(MON, "09:00:00", "13:00:00"),
                    shift(MON, "13:00:00", "18:00:00"),
                ],
            },
        )
        assert response.status_code == 201

    def test_break_outside_working_hours_is_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/clinics",
            headers=auth("superadmin"),
            json={
                "name": "Bad Break Clinic",
                "working_hours": [shift(MON, "09:00:00", "13:00:00")],
                "breaks": [
                    {"start_time": "14:00:00", "end_time": "14:30:00", "label": "Lunch"}
                ],
            },
        )
        assert response.status_code == 400
        assert "does not fit" in response.json()["error"]["message"]

    def test_invalid_pin_code_is_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/clinics",
            headers=auth("superadmin"),
            json={"name": "Bad Pin", "pin_code": "12"},
        )
        assert response.status_code == 422

    def test_capacity_must_be_positive(self, client, superadmin, auth):
        response = client.post(
            "/api/clinics",
            headers=auth("superadmin"),
            json={"name": "Zero Capacity", "capacity_per_slot": 0},
        )
        assert response.status_code == 422


class TestWriteAuthorisation:
    """Every write must be refused for Admin and Clinic User (Section 6)."""

    @pytest.fixture
    def endpoints(self, clinics):
        cid = clinics[0].id
        return [
            ("post", "/api/clinics", {"name": "Nope"}),
            ("put", f"/api/clinics/{cid}", {"name": "Renamed"}),
            ("post", f"/api/clinics/{cid}/activate", None),
            ("post", f"/api/clinics/{cid}/deactivate", None),
            ("put", f"/api/clinics/{cid}/working-hours", {"working_hours": []}),
            (
                "post",
                f"/api/clinics/{cid}/breaks",
                {"start_time": "11:00:00", "end_time": "11:30:00", "label": "X"},
            ),
            ("delete", f"/api/clinics/{cid}/breaks/1", None),
            ("post", f"/api/clinics/{cid}/users", {"user_id": 1}),
            ("delete", f"/api/clinics/{cid}/users/1", None),
            ("post", "/api/holidays", {"holiday_date": "2026-12-25"}),
            ("delete", "/api/holidays/1", None),
            ("post", "/api/patient-sources", {"name": "Billboard"}),
            ("put", "/api/patient-sources/1", {"name": "Renamed"}),
        ]

    def test_admin_is_refused_every_write(self, client, admin, endpoints, auth):
        headers = auth("admin")
        for method, url, body in endpoints:
            response = getattr(client, method)(
                url, headers=headers, **({"json": body} if body is not None else {})
            )
            assert response.status_code == 403, f"{method.upper()} {url} returned {response.status_code}"

    def test_clinic_user_is_refused_every_write(self, client, hsr_user, endpoints, auth):
        headers = auth("hsr.reception")
        for method, url, body in endpoints:
            response = getattr(client, method)(
                url, headers=headers, **({"json": body} if body is not None else {})
            )
            assert response.status_code == 403, f"{method.upper()} {url} returned {response.status_code}"

    def test_reads_stay_available_to_all_roles(self, client, admin, hsr_user, clinics, auth):
        cid = clinics[0].id
        for username in ("admin", "hsr.reception"):
            for url in (
                f"/api/clinics/{cid}",
                f"/api/clinics/{cid}/working-hours",
                f"/api/clinics/{cid}/breaks",
                f"/api/clinics/{cid}/schedule-preview",
                f"/api/clinics/{cid}/users",
                "/api/patient-sources",
            ):
                assert client.get(url, headers=auth(username)).status_code == 200, url


class TestUpdateClinic:
    def test_edit_basic_details(self, client, superadmin, clinics, auth):
        response = client.put(
            f"/api/clinics/{clinics[0].id}",
            headers=auth("superadmin"),
            json={"name": "HSR Layout Main", "phone": "9876511111", "city": "Bangalore"},
        )
        assert response.status_code == 200
        clinic = response.json()["clinic"]
        assert clinic["name"] == "HSR Layout Main"
        assert clinic["phone"] == "9876511111"

    def test_code_cannot_be_changed(self, client, superadmin, clinics, auth):
        """Immutable by decision: the code appears in historical records."""
        response = client.put(
            f"/api/clinics/{clinics[0].id}",
            headers=auth("superadmin"),
            json={"code": "NEW"},
        )
        assert response.status_code == 422
        fields = {d["field"] for d in response.json()["error"]["details"]}
        assert "code" in fields

    def test_renaming_to_an_existing_name_conflicts(self, client, superadmin, clinics, auth):
        response = client.put(
            f"/api/clinics/{clinics[0].id}",
            headers=auth("superadmin"),
            json={"name": "BTM Layout"},
        )
        assert response.status_code == 409

    def test_capacity_change_is_applied(self, client, superadmin, clinics, auth):
        response = client.put(
            f"/api/clinics/{clinics[0].id}",
            headers=auth("superadmin"),
            json={"capacity_per_slot": 5},
        )
        assert response.json()["clinic"]["capacity_per_slot"] == 5

    def test_slot_duration_change_recomputes_slot_counts(
        self, client, superadmin, clinics, auth
    ):
        before = client.get(
            f"/api/clinics/{clinics[0].id}", headers=auth("superadmin")
        ).json()["weekly_slot_counts"]["0"]
        response = client.put(
            f"/api/clinics/{clinics[0].id}",
            headers=auth("superadmin"),
            json={"slot_duration_minutes": 60},
        )
        after = response.json()["clinic"]["weekly_slot_counts"]["0"]
        assert before == 16 and after == 8  # 8 hours: 30-min vs 60-min slots

    def test_slot_duration_with_remainder_warns_but_saves(
        self, client, superadmin, clinics, auth
    ):
        response = client.put(
            f"/api/clinics/{clinics[0].id}",
            headers=auth("superadmin"),
            json={"slot_duration_minutes": 50},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["clinic"]["slot_duration_minutes"] == 50
        assert any("unusable" in w for w in body["warnings"])

    def test_unknown_clinic_is_404(self, client, superadmin, auth):
        response = client.put(
            "/api/clinics/9999", headers=auth("superadmin"), json={"name": "Ghost"}
        )
        assert response.status_code == 404


class TestCapacityReductionKeepsBookings:
    """Decision: existing appointments are never invalidated by a config change."""

    def test_reduction_below_bookings_warns_and_reports_slots(
        self, client, db, superadmin, clinics, sample_clinical_data, auth
    ):
        from datetime import time

        from app.models import Appointment, AppointmentStatus

        target = date.today() + timedelta(days=3)
        for index in range(3):
            db.add(
                Appointment(
                    appointment_code=f"APT-CAP-{index:04d}",
                    patient_id=sample_clinical_data["patients"][0].id,
                    clinic_id=clinics[0].id,
                    appointment_date=target,
                    start_time=time(17, 30),
                    end_time=time(18, 0),
                    duration_minutes=30,
                    status=AppointmentStatus.BOOKED,
                )
            )
        db.commit()

        response = client.put(
            f"/api/clinics/{clinics[0].id}",
            headers=auth("superadmin"),
            json={"capacity_per_slot": 1},
        )
        assert response.status_code == 200, response.text
        body = response.json()

        # Saved, not blocked.
        assert body["clinic"]["capacity_per_slot"] == 1
        # And the affected slot is reported.
        assert len(body["overbooked_slots"]) == 1
        assert body["overbooked_slots"][0]["booked"] == 3
        assert body["overbooked_slots"][0]["time"] == "17:30"
        assert any("Existing bookings are kept" in w for w in body["warnings"])

    def test_past_slots_are_not_reported(
        self, client, db, superadmin, clinics, sample_clinical_data, auth
    ):
        from datetime import time

        from app.models import Appointment, AppointmentStatus

        past = date.today() - timedelta(days=5)
        for index in range(3):
            db.add(
                Appointment(
                    appointment_code=f"APT-OLD-{index:04d}",
                    patient_id=sample_clinical_data["patients"][0].id,
                    clinic_id=clinics[0].id,
                    appointment_date=past,
                    start_time=time(10, 0),
                    end_time=time(10, 30),
                    duration_minutes=30,
                    status=AppointmentStatus.COMPLETED,
                )
            )
        db.commit()

        response = client.put(
            f"/api/clinics/{clinics[0].id}",
            headers=auth("superadmin"),
            json={"capacity_per_slot": 1},
        )
        assert response.json()["overbooked_slots"] == []


class TestActivation:
    def test_deactivate_then_activate(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        off = client.post(f"/api/clinics/{clinics[0].id}/deactivate", headers=headers)
        assert off.status_code == 200
        assert off.json()["clinic"]["status"] == "INACTIVE"

        on = client.post(f"/api/clinics/{clinics[0].id}/activate", headers=headers)
        assert on.json()["clinic"]["status"] == "ACTIVE"

    def test_deactivation_warns_about_future_appointments_but_succeeds(
        self, client, superadmin, clinics, sample_clinical_data, db, auth
    ):
        from datetime import time

        from app.models import Appointment, AppointmentStatus

        db.add(
            Appointment(
                appointment_code="APT-FUT-0001",
                patient_id=sample_clinical_data["patients"][0].id,
                clinic_id=clinics[0].id,
                appointment_date=date.today() + timedelta(days=7),
                start_time=time(10, 0),
                end_time=time(10, 30),
                duration_minutes=30,
                status=AppointmentStatus.BOOKED,
            )
        )
        db.commit()

        response = client.post(
            f"/api/clinics/{clinics[0].id}/deactivate", headers=auth("superadmin")
        )
        assert response.status_code == 200
        body = response.json()
        assert body["clinic"]["status"] == "INACTIVE"
        assert body["future_appointment_count"] >= 1
        assert any("future appointment" in w for w in body["warnings"])

    def test_deactivated_clinic_disappears_from_the_active_filter(
        self, client, superadmin, clinics, auth
    ):
        headers = auth("superadmin")
        client.post(f"/api/clinics/{clinics[0].id}/deactivate", headers=headers)
        active = client.get("/api/clinics?status=ACTIVE", headers=headers).json()
        assert clinics[0].code not in {c["code"] for c in active}


class TestWorkingHours:
    def test_replace_round_trips_split_shifts(self, client, superadmin, clinics, auth):
        """The exit criterion: save split shifts, reload, get identical data."""
        headers = auth("superadmin")
        payload = {
            "working_hours": [
                shift(MON, "09:00:00", "13:00:00", "Morning"),
                shift(MON, "16:00:00", "20:00:00", "Evening"),
                shift(TUE, "10:00:00", "14:00:00", "Single"),
            ]
        }
        response = client.put(
            f"/api/clinics/{clinics[0].id}/working-hours", headers=headers, json=payload
        )
        assert response.status_code == 200

        reloaded = client.get(
            f"/api/clinics/{clinics[0].id}/working-hours", headers=headers
        ).json()
        assert len(reloaded) == 3
        got = sorted((h["day_of_week"], h["open_time"], h["close_time"]) for h in reloaded)
        assert got == [
            (MON, "09:00:00", "13:00:00"),
            (MON, "16:00:00", "20:00:00"),
            (TUE, "10:00:00", "14:00:00"),
        ]

    def test_overlapping_replacement_is_rejected(self, client, superadmin, clinics, auth):
        response = client.put(
            f"/api/clinics/{clinics[0].id}/working-hours",
            headers=auth("superadmin"),
            json={
                "working_hours": [
                    shift(WED, "09:00:00", "14:00:00"),
                    shift(WED, "13:30:00", "18:00:00"),
                ]
            },
        )
        assert response.status_code == 400
        assert "Wednesday" in response.json()["error"]["message"]

    def test_backwards_shift_is_rejected(self, client, superadmin, clinics, auth):
        response = client.put(
            f"/api/clinics/{clinics[0].id}/working-hours",
            headers=auth("superadmin"),
            json={"working_hours": [shift(MON, "18:00:00", "09:00:00")]},
        )
        assert response.status_code in (400, 422)

    def test_duplicate_shift_is_rejected(self, client, superadmin, clinics, auth):
        response = client.put(
            f"/api/clinics/{clinics[0].id}/working-hours",
            headers=auth("superadmin"),
            json={
                "working_hours": [
                    shift(MON, "09:00:00", "13:00:00"),
                    shift(MON, "09:00:00", "13:00:00"),
                ]
            },
        )
        assert response.status_code == 422

    def test_replacement_that_orphans_a_break_is_rejected(
        self, client, superadmin, clinics, auth
    ):
        """A surviving break must still fall inside the new hours."""
        headers = auth("superadmin")
        created = client.post(
            f"/api/clinics/{clinics[0].id}/breaks",
            headers=headers,
            json={
                "day_of_week": MON,
                "start_time": "11:00:00",
                "end_time": "11:30:00",
                "label": "Tea",
            },
        )
        assert created.status_code == 201

        response = client.put(
            f"/api/clinics/{clinics[0].id}/working-hours",
            headers=headers,
            json={"working_hours": [shift(MON, "14:00:00", "18:00:00")]},
        )
        assert response.status_code == 400
        assert "does not fit" in response.json()["error"]["message"]

    def test_empty_replacement_closes_the_clinic_schedule(
        self, client, superadmin, clinics, auth
    ):
        headers = auth("superadmin")
        # Remove the fixture's every-day break first, or it would block the change.
        response = client.put(
            f"/api/clinics/{clinics[1].id}/working-hours",
            headers=headers,
            json={"working_hours": []},
        )
        assert response.status_code == 200
        assert response.json()["clinic"]["working_hours"] == []
        assert any(
            "no bookable slots" in w for w in response.json()["clinic"]["configuration_warnings"]
        )


class TestBreaks:
    def test_add_and_remove(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        created = client.post(
            f"/api/clinics/{clinics[0].id}/breaks",
            headers=headers,
            json={
                "day_of_week": None,
                "start_time": "11:00:00",
                "end_time": "11:30:00",
                "label": "Tea break",
            },
        )
        assert created.status_code == 201
        break_id = created.json()["id"]

        listed = client.get(f"/api/clinics/{clinics[0].id}/breaks", headers=headers).json()
        assert len(listed) == 1

        removed = client.delete(
            f"/api/clinics/{clinics[0].id}/breaks/{break_id}", headers=headers
        )
        assert removed.status_code == 200
        assert client.get(f"/api/clinics/{clinics[0].id}/breaks", headers=headers).json() == []

    def test_every_day_break_must_fit_every_working_day(
        self, client, superadmin, clinics, auth
    ):
        """Sunday is closed in the fixture, so a break must fit Mon-Sat."""
        headers = auth("superadmin")
        client.put(
            f"/api/clinics/{clinics[0].id}/working-hours",
            headers=headers,
            json={
                "working_hours": [
                    shift(MON, "09:00:00", "13:00:00"),
                    shift(TUE, "16:00:00", "20:00:00"),
                ]
            },
        )
        response = client.post(
            f"/api/clinics/{clinics[0].id}/breaks",
            headers=headers,
            json={"start_time": "11:00:00", "end_time": "11:30:00", "label": "Tea"},
        )
        assert response.status_code == 400
        assert "every working day" in response.json()["error"]["message"]

    def test_duplicate_break_conflicts(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        body = {
            "day_of_week": MON,
            "start_time": "11:00:00",
            "end_time": "11:30:00",
            "label": "Tea",
        }
        assert (
            client.post(
                f"/api/clinics/{clinics[0].id}/breaks", headers=headers, json=body
            ).status_code
            == 201
        )
        assert (
            client.post(
                f"/api/clinics/{clinics[0].id}/breaks", headers=headers, json=body
            ).status_code
            == 409
        )

    def test_removing_another_clinics_break_is_404(
        self, client, superadmin, clinics, auth
    ):
        headers = auth("superadmin")
        created = client.post(
            f"/api/clinics/{clinics[0].id}/breaks",
            headers=headers,
            json={"day_of_week": MON, "start_time": "11:00:00", "end_time": "11:30:00"},
        )
        break_id = created.json()["id"]
        response = client.delete(
            f"/api/clinics/{clinics[1].id}/breaks/{break_id}", headers=headers
        )
        assert response.status_code == 404


class TestHolidays:
    def test_add_clinic_holiday(self, client, superadmin, clinics, auth):
        response = client.post(
            "/api/holidays",
            headers=auth("superadmin"),
            json={
                "clinic_id": clinics[0].id,
                "holiday_date": "2026-10-02",
                "reason": "Gandhi Jayanti",
            },
        )
        assert response.status_code == 201
        assert response.json()["clinic_name"] == "HSR Layout"

    def test_chain_wide_holiday_has_no_clinic(self, client, superadmin, clinics, auth):
        response = client.post(
            "/api/holidays",
            headers=auth("superadmin"),
            json={"holiday_date": "2026-11-08", "reason": "Diwali"},
        )
        assert response.status_code == 201
        assert response.json()["clinic_id"] is None
        assert response.json()["clinic_name"] is None

    def test_clinic_listing_includes_chain_wide_holidays(
        self, client, superadmin, clinics, auth
    ):
        headers = auth("superadmin")
        client.post(
            "/api/holidays", headers=headers, json={"holiday_date": "2026-11-08", "reason": "Diwali"}
        )
        client.post(
            "/api/holidays",
            headers=headers,
            json={"clinic_id": clinics[0].id, "holiday_date": "2026-10-02", "reason": "Local"},
        )
        listed = client.get(f"/api/holidays?clinic_id={clinics[0].id}", headers=headers).json()
        dates = {h["holiday_date"] for h in listed}
        assert dates == {"2026-11-08", "2026-10-02"}

        # The other clinic sees only the chain-wide one.
        other = client.get(f"/api/holidays?clinic_id={clinics[1].id}", headers=headers).json()
        assert {h["holiday_date"] for h in other} == {"2026-11-08"}

    def test_duplicate_holiday_is_rejected(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        body = {"clinic_id": clinics[0].id, "holiday_date": "2026-10-02"}
        assert client.post("/api/holidays", headers=headers, json=body).status_code == 201
        duplicate = client.post("/api/holidays", headers=headers, json=body)
        assert duplicate.status_code == 400
        assert "already exists" in duplicate.json()["error"]["message"]

    def test_remove_holiday(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        created = client.post(
            "/api/holidays", headers=headers, json={"holiday_date": "2026-12-25"}
        )
        holiday_id = created.json()["id"]
        assert client.delete(f"/api/holidays/{holiday_id}", headers=headers).status_code == 200
        assert client.get("/api/holidays", headers=headers).json() == []

    def test_clinic_user_sees_only_their_own_and_chain_wide(
        self, client, superadmin, hsr_user, clinics, auth
    ):
        admin_headers = auth("superadmin")
        client.post(
            "/api/holidays",
            headers=admin_headers,
            json={"clinic_id": clinics[1].id, "holiday_date": "2026-10-02", "reason": "BTM only"},
        )
        client.post(
            "/api/holidays", headers=admin_headers, json={"holiday_date": "2026-11-08"}
        )
        visible = client.get("/api/holidays", headers=auth("hsr.reception")).json()
        assert {h["holiday_date"] for h in visible} == {"2026-11-08"}

    def test_holiday_for_unknown_clinic_is_404(self, client, superadmin, auth):
        response = client.post(
            "/api/holidays",
            headers=auth("superadmin"),
            json={"clinic_id": 9999, "holiday_date": "2026-10-02"},
        )
        assert response.status_code == 404


class TestSchedulePreview:
    def test_preview_reflects_configuration(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        # Next Monday.
        today = date.today()
        monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        response = client.get(
            f"/api/clinics/{clinics[0].id}/schedule-preview?date={monday.isoformat()}",
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["is_open"] is True
        assert body["day_name"] == "Monday"
        # 09:00-13:00 and 16:00-20:00 at 30 minutes = 16 slots.
        assert body["total_slots"] == 16
        assert body["total_capacity"] == 48  # 16 slots x capacity 3
        assert body["slots"][0]["start_time"] == "09:00:00"
        assert body["slots"][-1]["end_time"] == "20:00:00"

    def test_breaks_remove_slots(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        today = date.today()
        monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        before = client.get(
            f"/api/clinics/{clinics[0].id}/schedule-preview?date={monday.isoformat()}",
            headers=headers,
        ).json()["total_slots"]

        client.post(
            f"/api/clinics/{clinics[0].id}/breaks",
            headers=headers,
            json={"day_of_week": MON, "start_time": "11:00:00", "end_time": "11:30:00"},
        )
        after = client.get(
            f"/api/clinics/{clinics[0].id}/schedule-preview?date={monday.isoformat()}",
            headers=headers,
        ).json()
        assert after["total_slots"] == before - 1
        assert "11:00:00" not in {s["start_time"] for s in after["slots"]}

    def test_holiday_closes_the_day(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        today = date.today()
        monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
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
            f"/api/clinics/{clinics[0].id}/schedule-preview?date={monday.isoformat()}",
            headers=headers,
        ).json()
        assert body["is_open"] is False
        assert body["total_slots"] == 0
        assert "Deep clean" in body["closed_reason"]

    def test_chain_wide_holiday_closes_every_clinic(
        self, client, superadmin, clinics, auth
    ):
        headers = auth("superadmin")
        today = date.today()
        monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        client.post(
            "/api/holidays",
            headers=headers,
            json={"holiday_date": monday.isoformat(), "reason": "Diwali"},
        )
        for clinic in clinics[:2]:
            body = client.get(
                f"/api/clinics/{clinic.id}/schedule-preview?date={monday.isoformat()}",
                headers=headers,
            ).json()
            assert body["is_open"] is False
            assert "chain-wide" in body["closed_reason"]

    def test_sunday_is_closed_by_configuration(self, client, superadmin, clinics, auth):
        today = date.today()
        sunday = today + timedelta(days=(6 - today.weekday()) % 7 or 7)
        body = client.get(
            f"/api/clinics/{clinics[0].id}/schedule-preview?date={sunday.isoformat()}",
            headers=auth("superadmin"),
        ).json()
        assert body["is_open"] is False
        assert "No working hours" in body["closed_reason"]

    def test_clinic_user_cannot_preview_another_clinic(
        self, client, hsr_user, clinics, auth
    ):
        response = client.get(
            f"/api/clinics/{clinics[1].id}/schedule-preview", headers=auth("hsr.reception")
        )
        assert response.status_code == 403


class TestStaffAssignment:
    def test_list_staff(self, client, superadmin, hsr_user, clinics, auth):
        response = client.get(
            f"/api/clinics/{clinics[0].id}/users", headers=auth("superadmin")
        )
        assert response.status_code == 200
        staff = response.json()
        assert len(staff) == 1
        assert staff[0]["username"] == "hsr.reception"
        assert staff[0]["is_primary"] is True

    def test_assign_an_additional_clinic(self, client, superadmin, hsr_user, clinics, auth):
        headers = auth("superadmin")
        response = client.post(
            f"/api/clinics/{clinics[1].id}/users",
            headers=headers,
            json={"user_id": hsr_user.id, "designation": "Relief cover"},
        )
        assert response.status_code == 201
        assert response.json()["designation"] == "Relief cover"

        # The user can now see both clinics.
        visible = client.get("/api/clinics", headers=auth("hsr.reception")).json()
        assert {c["code"] for c in visible} == {"HSR", "BTM"}

    def test_admin_cannot_be_assigned_to_a_clinic(
        self, client, superadmin, admin, clinics, auth
    ):
        response = client.post(
            f"/api/clinics/{clinics[0].id}/users",
            headers=auth("superadmin"),
            json={"user_id": admin.id},
        )
        assert response.status_code == 400
        assert "every clinic" in response.json()["error"]["message"]

    def test_duplicate_assignment_conflicts(self, client, superadmin, hsr_user, clinics, auth):
        response = client.post(
            f"/api/clinics/{clinics[0].id}/users",
            headers=auth("superadmin"),
            json={"user_id": hsr_user.id},
        )
        assert response.status_code == 409

    def test_unassign_when_the_user_has_two_clinics(
        self, client, superadmin, hsr_user, clinics, auth
    ):
        headers = auth("superadmin")
        client.post(
            f"/api/clinics/{clinics[1].id}/users",
            headers=headers,
            json={"user_id": hsr_user.id},
        )
        response = client.delete(
            f"/api/clinics/{clinics[1].id}/users/{hsr_user.id}", headers=headers
        )
        assert response.status_code == 200
        visible = client.get("/api/clinics", headers=auth("hsr.reception")).json()
        assert {c["code"] for c in visible} == {"HSR"}

    def test_cannot_unassign_the_last_clinic(self, client, superadmin, hsr_user, clinics, auth):
        """A Clinic User with no clinic can see nothing -- disable instead."""
        response = client.delete(
            f"/api/clinics/{clinics[0].id}/users/{hsr_user.id}", headers=auth("superadmin")
        )
        assert response.status_code == 400
        assert "without any clinic" in response.json()["error"]["message"]

    def test_unassigning_a_user_who_is_not_assigned_is_404(
        self, client, superadmin, hsr_user, clinics, auth
    ):
        response = client.delete(
            f"/api/clinics/{clinics[1].id}/users/{hsr_user.id}", headers=auth("superadmin")
        )
        assert response.status_code == 404


class TestPatientSources:
    @pytest.fixture
    def sources(self, db):
        from app.models import PatientSource

        for index, name in enumerate(["Google", "Instagram", "Walk-in"]):
            db.add(PatientSource(name=name, sort_order=index))
        db.commit()

    def test_list_with_patient_counts(self, client, superadmin, sources, auth):
        response = client.get("/api/patient-sources", headers=auth("superadmin"))
        assert response.status_code == 200
        body = response.json()
        assert [s["name"] for s in body] == ["Google", "Instagram", "Walk-in"]
        assert all(s["patient_count"] == 0 for s in body)

    def test_create(self, client, superadmin, sources, auth):
        response = client.post(
            "/api/patient-sources",
            headers=auth("superadmin"),
            json={"name": "Billboard", "sort_order": 5},
        )
        assert response.status_code == 201
        assert response.json()["name"] == "Billboard"

    def test_duplicate_name_conflicts(self, client, superadmin, sources, auth):
        response = client.post(
            "/api/patient-sources", headers=auth("superadmin"), json={"name": "google"}
        )
        assert response.status_code == 409

    def test_rename_and_deactivate(self, client, superadmin, sources, db, auth):
        from app.models import PatientSource

        source_id = db.query(PatientSource).filter_by(name="Instagram").one().id
        headers = auth("superadmin")

        renamed = client.put(
            f"/api/patient-sources/{source_id}", headers=headers, json={"name": "Instagram Ads"}
        )
        assert renamed.json()["name"] == "Instagram Ads"

        deactivated = client.put(
            f"/api/patient-sources/{source_id}", headers=headers, json={"is_active": False}
        )
        assert deactivated.json()["is_active"] is False

        active_only = client.get(
            "/api/patient-sources?include_inactive=false", headers=headers
        ).json()
        assert "Instagram Ads" not in {s["name"] for s in active_only}

    def test_cannot_deactivate_the_last_active_source(self, client, superadmin, db, auth):
        from app.models import PatientSource

        source = PatientSource(name="Only One", sort_order=0)
        db.add(source)
        db.commit()

        response = client.put(
            f"/api/patient-sources/{source.id}",
            headers=auth("superadmin"),
            json={"is_active": False},
        )
        assert response.status_code == 400
        assert "at least one" in response.json()["error"]["message"].lower()

    def test_patient_count_is_reported(self, client, superadmin, sources, db, auth):
        from app.models import Patient, PatientSource

        source = db.query(PatientSource).filter_by(name="Google").one()
        db.add(
            Patient(
                patient_code="PT-000900",
                full_name="Counted Patient",
                mobile="9876543299",
                source_id=source.id,
            )
        )
        db.commit()

        body = client.get("/api/patient-sources", headers=auth("superadmin")).json()
        google = next(s for s in body if s["name"] == "Google")
        assert google["patient_count"] == 1


class TestAuditTrail:
    def test_every_mutation_is_recorded(self, client, superadmin, clinics, auth):
        from app.models import AuditLog

        headers = auth("superadmin")
        created = client.post(
            "/api/clinics", headers=headers, json={"name": "Audited Clinic"}
        )
        clinic_id = created.json()["clinic"]["id"]
        client.put(f"/api/clinics/{clinic_id}", headers=headers, json={"city": "Mysuru"})
        client.post(f"/api/clinics/{clinic_id}/deactivate", headers=headers)
        client.post(
            "/api/holidays", headers=headers, json={"clinic_id": clinic_id, "holiday_date": "2026-12-25"}
        )

        from app.db.database import SessionLocal

        with SessionLocal() as session:
            entries = [
                (log.entity_type, log.action.value)
                for log in session.query(AuditLog).all()
            ]
        assert ("clinic", "CREATED") in entries
        assert ("clinic", "UPDATED") in entries
        assert ("clinic", "DISABLED") in entries
        assert ("clinic_holiday", "CREATED") in entries
