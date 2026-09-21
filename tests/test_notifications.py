"""Phase 6: clinic notifications and patient messaging (Sections 15 and 16).

The properties under test:

* A notification is created in the **same transaction** as the appointment.
* A clinic is told only when someone *outside* it acted.
* Acknowledgement is per-user, idempotent, and clinic-scoped.
* Patient WhatsApp goes out on book/reschedule/cancel and is logged.
* **A provider failure never loses an appointment** — the one guarantee the
  spec states outright.
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app.integrations.base import MessageResult
from app.models.enums import MessageChannel


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


def book(client, headers, patient_id, clinic_id, **overrides):
    body = {
        "patient_id": patient_id,
        "clinic_id": clinic_id,
        "appointment_date": next_weekday(MONDAY).isoformat(),
        "start_time": "10:00",
        **overrides,
    }
    return client.post("/api/appointments", headers=headers, json=body)


def booked(client, headers, patient_id, clinic_id, **overrides):
    """The appointment itself, unwrapped from the BookingResult envelope."""
    response = book(client, headers, patient_id, clinic_id, **overrides)
    assert response.status_code == 201, response.text
    return response.json()["appointment"]


# --------------------------------------------------------------------------- #
class TestNotificationCreation:
    def test_admin_booking_notifies_the_clinic(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        booked = book(client, auth("admin"), patient["id"], clinics[0].id)
        assert booked.status_code == 201

        feed = client.get("/api/notifications", headers=auth("hsr.reception")).json()
        assert feed["total"] == 1
        item = feed["items"][0]
        assert item["title"] == "NEW APPOINTMENT"
        assert item["notification_type"] == "NEW_APPOINTMENT"
        assert item["payload"]["patient_name"] == "Rahul Sharma"
        assert item["payload"]["time"] == "10:00 AM"
        assert item["payload"]["clinic_name"] == "HSR Layout"
        assert item["payload"]["booked_by"] == admin.full_name
        assert item["is_acknowledged"] is False
        assert item["requires_sound_alert"] is True

    def test_clinic_user_booking_at_their_own_clinic_notifies_nobody(
        self, client, superadmin, hsr_user, patient, clinics, auth
    ):
        """Reception already knows what reception just booked."""
        booked = book(client, auth("hsr.reception"), patient["id"], clinics[0].id)
        assert booked.status_code == 201

        feed = client.get("/api/notifications", headers=auth("hsr.reception")).json()
        assert feed["total"] == 0

    def test_rescheduling_notifies_with_the_new_time(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        appointment = booked(client, auth("admin"), patient["id"], clinics[0].id)
        client.post(
            f"/api/appointments/{appointment['id']}/reschedule",
            headers=auth("admin"),
            json={
                "appointment_date": next_weekday(MONDAY, 2).isoformat(),
                "start_time": "16:30",
                "reason": "Patient request",
            },
        )

        feed = client.get(
            "/api/notifications",
            headers=auth("hsr.reception"),
            params={"unacknowledged_only": True},
        ).json()
        titles = [item["title"] for item in feed["items"]]
        assert "RESCHEDULED APPOINTMENT" in titles

        moved = next(i for i in feed["items"] if i["title"] == "RESCHEDULED APPOINTMENT")
        assert moved["payload"]["time"] == "4:30 PM"
        assert moved["payload"]["reason"] == "Patient request"

    def test_cancelling_notifies(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        appointment = booked(client, auth("admin"), patient["id"], clinics[0].id)
        client.post(
            f"/api/appointments/{appointment['id']}/cancel",
            headers=auth("admin"),
            json={"reason": "Patient unwell"},
        )

        feed = client.get("/api/notifications", headers=auth("hsr.reception")).json()
        titles = [item["title"] for item in feed["items"]]
        assert titles.count("CANCELLED APPOINTMENT") == 1

    def test_routine_desk_actions_do_not_notify(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        """Confirm/check-in/complete are things the clinic does itself."""
        appointment = booked(client, auth("admin"), patient["id"], clinics[0].id)
        before = client.get("/api/notifications", headers=auth("hsr.reception")).json()["total"]

        for action in ("confirm", "check-in"):
            client.post(
                f"/api/appointments/{appointment['id']}/{action}",
                headers=auth("hsr.reception"),
                json={},
            )

        after = client.get("/api/notifications", headers=auth("hsr.reception")).json()["total"]
        assert after == before


# --------------------------------------------------------------------------- #
class TestScoping:
    def test_a_clinic_only_sees_its_own(
        self, client, superadmin, admin, hsr_user, btm_user, patient, clinics, auth
    ):
        book(client, auth("admin"), patient["id"], clinics[0].id)

        assert client.get("/api/notifications", headers=auth("hsr.reception")).json()["total"] == 1
        assert client.get("/api/notifications", headers=auth("btm.reception")).json()["total"] == 0

    def test_admin_sees_the_whole_chain_but_gets_no_badge(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        book(client, auth("admin"), patient["id"], clinics[0].id)

        feed = client.get("/api/notifications", headers=auth("admin")).json()
        assert feed["total"] == 1

        counters = client.get("/api/notifications/counters", headers=auth("admin")).json()
        # The feed is for oversight; the alert belongs to the clinic floor.
        assert counters["alerts_enabled"] is False
        clinic_counters = client.get(
            "/api/notifications/counters", headers=auth("hsr.reception")
        ).json()
        assert clinic_counters["alerts_enabled"] is True
        assert clinic_counters["unacknowledged"] == 1

    def test_unassigned_clinic_user_sees_nothing(
        self, client, superadmin, admin, unassigned_user, patient, clinics, auth
    ):
        book(client, auth("admin"), patient["id"], clinics[0].id)
        feed = client.get("/api/notifications", headers=auth("orphan.user")).json()
        assert feed["total"] == 0

    def test_acknowledging_another_clinics_notification_is_denied(
        self, client, superadmin, admin, hsr_user, btm_user, patient, clinics, auth
    ):
        book(client, auth("admin"), patient["id"], clinics[0].id)
        target = client.get("/api/notifications", headers=auth("hsr.reception")).json()["items"][0]

        response = client.post(
            f"/api/notifications/{target['id']}/acknowledge", headers=auth("btm.reception")
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------- #
class TestAcknowledgement:
    def test_acknowledge_marks_it_and_clears_the_badge(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        book(client, auth("admin"), patient["id"], clinics[0].id)
        target = client.get("/api/notifications", headers=auth("hsr.reception")).json()["items"][0]

        acked = client.post(
            f"/api/notifications/{target['id']}/acknowledge", headers=auth("hsr.reception")
        )
        assert acked.status_code == 200
        assert acked.json()["is_acknowledged"] is True
        assert acked.json()["acknowledged_by"] == [hsr_user.full_name]

        counters = client.get(
            "/api/notifications/counters", headers=auth("hsr.reception")
        ).json()
        assert counters["unacknowledged"] == 0

    def test_acknowledging_twice_is_a_no_op(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        """Two receptionists clicking at once is normal, not a conflict."""
        book(client, auth("admin"), patient["id"], clinics[0].id)
        target = client.get("/api/notifications", headers=auth("hsr.reception")).json()["items"][0]

        first = client.post(
            f"/api/notifications/{target['id']}/acknowledge", headers=auth("hsr.reception")
        )
        second = client.post(
            f"/api/notifications/{target['id']}/acknowledge", headers=auth("hsr.reception")
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert len(second.json()["acknowledged_by"]) == 1

    def test_acknowledge_all(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        for hour in ("10:00", "11:00", "12:00"):
            book(client, auth("admin"), patient["id"], clinics[0].id, start_time=hour)

        result = client.post(
            "/api/notifications/acknowledge-all", headers=auth("hsr.reception")
        )
        assert result.status_code == 200
        assert "3" in result.json()["message"]

        counters = client.get(
            "/api/notifications/counters", headers=auth("hsr.reception")
        ).json()
        assert counters["unacknowledged"] == 0

    def test_unacknowledged_sort_to_the_top(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        for hour in ("10:00", "11:00"):
            book(client, auth("admin"), patient["id"], clinics[0].id, start_time=hour)

        items = client.get("/api/notifications", headers=auth("hsr.reception")).json()["items"]
        client.post(
            f"/api/notifications/{items[0]['id']}/acknowledge", headers=auth("hsr.reception")
        )

        after = client.get("/api/notifications", headers=auth("hsr.reception")).json()["items"]
        assert after[0]["is_acknowledged"] is False
        assert after[-1]["is_acknowledged"] is True


# --------------------------------------------------------------------------- #
class TestPatientMessaging:
    def _messages(self, db):
        from app.models import OutboundMessage

        return db.query(OutboundMessage).order_by(OutboundMessage.id).all()

    def test_booking_sends_the_patient_a_whatsapp(
        self, client, superadmin, admin, patient, clinics, auth, db
    ):
        book(client, auth("admin"), patient["id"], clinics[0].id)

        messages = self._messages(db)
        assert len(messages) == 1
        message = messages[0]
        assert message.channel == MessageChannel.WHATSAPP
        assert message.recipient == "9876543210"
        assert message.template_key == "appointment_booked"
        assert message.status.value == "SENT"
        assert "has been booked" in message.body
        assert "HSR Layout" in message.body

    def test_reschedule_and_cancel_send_their_own_messages(
        self, client, superadmin, admin, patient, clinics, auth, db
    ):
        appointment = booked(client, auth("admin"), patient["id"], clinics[0].id)
        client.post(
            f"/api/appointments/{appointment['id']}/cancel",
            headers=auth("admin"),
            json={"reason": "unwell"},
        )

        keys = [m.template_key for m in self._messages(db)]
        assert keys == ["appointment_booked", "appointment_cancelled"]

    def test_the_dedicated_whatsapp_number_wins_over_the_mobile(
        self, client, superadmin, admin, clinics, auth, db
    ):
        other = client.post(
            "/api/patients",
            headers=auth("superadmin"),
            json={
                "full_name": "Anita Desai",
                "mobile": "9876500123",
                "whatsapp_number": "9812345678",
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        book(client, auth("admin"), other["id"], clinics[0].id)

        assert self._messages(db)[0].recipient == "9812345678"

    def test_a_provider_failure_never_loses_the_appointment(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth, db
    ):
        """The guarantee Section 15 states outright."""
        boom = MessageResult(
            success=False,
            provider="mock-whatsapp",
            channel=MessageChannel.WHATSAPP,
            recipient="9876543210",
            error="provider unreachable",
        )
        with patch(
            "app.integrations.mock_providers.MockWhatsAppService.send_text", return_value=boom
        ):
            response = book(client, auth("admin"), patient["id"], clinics[0].id)

        assert response.status_code == 201
        # The failure is recorded rather than swallowed...
        message = self._messages(db)[0]
        assert message.status.value == "FAILED"
        assert message.error_message == "provider unreachable"
        # ...and the clinic was still told.
        assert client.get("/api/notifications", headers=auth("hsr.reception")).json()["total"] == 1

    def test_a_provider_raising_still_never_loses_the_appointment(
        self, client, superadmin, admin, patient, clinics, auth, db
    ):
        with patch(
            "app.integrations.mock_providers.MockWhatsAppService.send_text",
            side_effect=RuntimeError("connection reset"),
        ):
            response = book(client, auth("admin"), patient["id"], clinics[0].id)

        assert response.status_code == 201
        assert self._messages(db)[0].status.value == "FAILED"

    def test_a_failed_booking_leaves_no_notification_behind(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        """Notification and appointment share a transaction, so neither survives alone."""
        # A slot outside working hours is refused before anything is written.
        rejected = book(
            client, auth("admin"), patient["id"], clinics[0].id, start_time="03:00"
        )
        assert rejected.status_code >= 400

        feed = client.get("/api/notifications", headers=auth("hsr.reception")).json()
        assert feed["total"] == 0


# --------------------------------------------------------------------------- #
class TestCounters:
    def test_latest_id_tracks_the_newest_visible_notification(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        empty = client.get(
            "/api/notifications/counters", headers=auth("hsr.reception")
        ).json()
        assert empty["latest_id"] is None
        assert empty["unacknowledged"] == 0

        book(client, auth("admin"), patient["id"], clinics[0].id)
        after = client.get(
            "/api/notifications/counters", headers=auth("hsr.reception")
        ).json()
        assert after["latest_id"] is not None
        assert after["unacknowledged"] == 1
        assert after["total"] == 1

    def test_counters_carry_the_newest_item_for_the_floating_card(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        """The card renders straight from the poll, so no second request."""
        empty = client.get(
            "/api/notifications/counters", headers=auth("hsr.reception")
        ).json()
        assert empty["latest"] is None

        book(client, auth("admin"), patient["id"], clinics[0].id, start_time="10:00")
        book(client, auth("admin"), patient["id"], clinics[0].id, start_time="11:00")

        counters = client.get(
            "/api/notifications/counters", headers=auth("hsr.reception")
        ).json()
        assert counters["unacknowledged"] == 2
        latest = counters["latest"]
        # Newest first: the 11:00 booking, not the 10:00 one.
        assert latest["time"] == "11:00 AM"
        assert latest["patient_name"] == "Rahul Sharma"
        assert latest["clinic_name"] == "HSR Layout"
        assert latest["title"] == "NEW APPOINTMENT"

    def test_latest_clears_once_everything_is_acknowledged(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        """`latest` going null is what makes the floating card disappear."""
        book(client, auth("admin"), patient["id"], clinics[0].id)
        client.post("/api/notifications/acknowledge-all", headers=auth("hsr.reception"))

        counters = client.get(
            "/api/notifications/counters", headers=auth("hsr.reception")
        ).json()
        assert counters["unacknowledged"] == 0
        assert counters["latest"] is None

    def test_admin_gets_no_card_data_pushed_at_them(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        """Admins can browse the feed, but nothing pops up over their screen."""
        book(client, auth("admin"), patient["id"], clinics[0].id)
        counters = client.get("/api/notifications/counters", headers=auth("admin")).json()
        assert counters["alerts_enabled"] is False


# --------------------------------------------------------------------------- #
class TestNoShowAndFallback:
    def _messages(self, db):
        from app.models import OutboundMessage

        return db.query(OutboundMessage).order_by(OutboundMessage.id).all()

    def test_a_no_show_messages_the_patient(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth, db
    ):
        """The moment a patient is most likely to drift away."""
        appointment = booked(client, auth("admin"), patient["id"], clinics[0].id)
        before = len(self._messages(db))

        response = client.post(
            f"/api/appointments/{appointment['id']}/no-show",
            headers=auth("hsr.reception"),
            json={},
        )
        assert response.status_code == 200

        messages = self._messages(db)
        assert len(messages) == before + 1
        latest = messages[-1]
        assert latest.template_key == "appointment_missed"
        # An invitation to rebook, not a reprimand.
        assert "We missed you" in latest.body
        assert "another slot" in latest.body

    def test_a_no_show_also_tells_the_clinic(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth
    ):
        appointment = booked(client, auth("admin"), patient["id"], clinics[0].id)
        client.post(
            f"/api/appointments/{appointment['id']}/no-show",
            headers=auth("admin"),
            json={},
        )
        feed = client.get("/api/notifications", headers=auth("hsr.reception")).json()
        assert any(item["title"] == "MISSED APPOINTMENT" for item in feed["items"])

    def test_routine_desk_actions_still_message_nobody(
        self, client, superadmin, admin, hsr_user, patient, clinics, auth, db
    ):
        """Confirm and check-in must stay silent."""
        appointment = booked(client, auth("admin"), patient["id"], clinics[0].id)
        before = len(self._messages(db))
        for action in ("confirm", "check-in"):
            client.post(
                f"/api/appointments/{appointment['id']}/{action}",
                headers=auth("hsr.reception"),
                json={},
            )
        assert len(self._messages(db)) == before

    def test_a_failed_whatsapp_falls_back_to_sms_when_enabled(
        self, client, superadmin, admin, patient, clinics, auth, db
    ):
        """A fallback, recorded as its own row so the sequence stays legible."""
        from app.integrations.base import MessageResult
        from app.models.enums import MessageChannel
        from app.services import notification_service

        broken = MessageResult(
            success=False,
            provider="mock-whatsapp",
            channel=MessageChannel.WHATSAPP,
            recipient="9876543210",
            error="number not on WhatsApp",
        )
        with patch.object(notification_service.settings, "SMS_FALLBACK_ENABLED", True), patch(
            "app.integrations.mock_providers.MockWhatsAppService.send_text",
            return_value=broken,
        ):
            response = book(client, auth("admin"), patient["id"], clinics[0].id)
        assert response.status_code == 201

        messages = self._messages(db)
        whatsapp = [m for m in messages if m.channel == MessageChannel.WHATSAPP]
        sms = [m for m in messages if m.channel == MessageChannel.SMS]

        assert whatsapp[-1].status.value == "FAILED"
        assert len(sms) == 1
        assert sms[0].status.value == "SENT"
        assert sms[0].recipient == whatsapp[-1].recipient
        # Same body, so the patient gets the same information either way.
        assert sms[0].body == whatsapp[-1].body

    def test_no_sms_is_sent_when_the_fallback_is_off(
        self, client, superadmin, admin, patient, clinics, auth, db
    ):
        """Off by default: it costs money and needs DLT registration."""
        from app.integrations.base import MessageResult
        from app.models.enums import MessageChannel

        broken = MessageResult(
            success=False,
            provider="mock-whatsapp",
            channel=MessageChannel.WHATSAPP,
            recipient="9876543210",
            error="number not on WhatsApp",
        )
        with patch(
            "app.integrations.mock_providers.MockWhatsAppService.send_text",
            return_value=broken,
        ):
            book(client, auth("admin"), patient["id"], clinics[0].id)

        assert [m for m in self._messages(db) if m.channel == MessageChannel.SMS] == []

    def test_a_successful_whatsapp_never_triggers_an_sms(
        self, client, superadmin, admin, patient, clinics, auth, db
    ):
        """Sending both every time doubles the cost and trains patients to
        ignore one of them."""
        from app.models.enums import MessageChannel
        from app.services import notification_service

        with patch.object(notification_service.settings, "SMS_FALLBACK_ENABLED", True):
            book(client, auth("admin"), patient["id"], clinics[0].id)

        assert [m for m in self._messages(db) if m.channel == MessageChannel.SMS] == []
