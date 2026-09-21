"""Phase 9: audit-log viewer and login throttling.

The audit log is the record of who did what — including password resets and
every financial action — so the tests here are about it being *complete* and
*unreachable by the wrong role*, not merely present.
"""

from datetime import date, timedelta

import pytest

from app.auth.rate_limit import login_limiter
from app.config import settings


@pytest.fixture(autouse=True)
def _clear_limiter():
    """Throttle state is process-global, so it must not leak between tests."""
    login_limiter.reset()
    yield
    login_limiter.reset()


# --------------------------------------------------------------------------- #
class TestAuditLog:
    def test_actions_are_recorded_and_readable(self, client, superadmin, auth):
        headers = auth("superadmin")  # the login itself is auditable
        response = client.get("/api/audit-logs", headers=headers)
        assert response.status_code == 200

        body = response.json()
        assert body["total"] >= 1
        entry = body["items"][0]
        assert entry["action"] == "LOGIN"
        assert entry["username"] == "superadmin"
        assert entry["created_at"]

    def test_newest_first(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        client.post(
            "/api/patients",
            headers=headers,
            json={
                "full_name": "Rahul Sharma",
                "mobile": "9876500001",
                "primary_clinic_id": clinics[0].id,
            },
        )
        items = client.get("/api/audit-logs", headers=headers).json()["items"]
        assert items[0]["entity_type"] == "patient"
        assert [item["id"] for item in items] == sorted(
            (item["id"] for item in items), reverse=True
        )

    def test_a_failed_login_is_recorded(self, client, superadmin, auth):
        client.post(
            "/api/auth/login", json={"username": "superadmin", "password": "wrong"}
        )
        items = client.get("/api/audit-logs", headers=auth("superadmin")).json()["items"]
        assert any(item["action"] == "LOGIN_FAILED" for item in items)

    def test_filter_by_action_and_search(self, client, superadmin, clinics, auth):
        headers = auth("superadmin")
        client.post(
            "/api/patients",
            headers=headers,
            json={
                "full_name": "Meera Iyer",
                "mobile": "9876500002",
                "primary_clinic_id": clinics[0].id,
            },
        )

        created = client.get(
            "/api/audit-logs", headers=headers, params={"action": "CREATED"}
        ).json()
        assert created["total"] >= 1
        assert all(item["action"] == "CREATED" for item in created["items"])

        found = client.get(
            "/api/audit-logs", headers=headers, params={"search": "Meera"}
        ).json()
        assert found["total"] >= 1

    def test_date_range_filter(self, client, superadmin, auth):
        headers = auth("superadmin")
        past = (date.today() - timedelta(days=30)).isoformat()
        old = client.get(
            "/api/audit-logs", headers=headers, params={"to": past}
        ).json()
        assert old["total"] == 0

        today = client.get(
            "/api/audit-logs", headers=headers, params={"from": date.today().isoformat()}
        ).json()
        assert today["total"] >= 1

    def test_admin_and_clinic_users_are_refused(
        self, client, superadmin, admin, hsr_user, auth
    ):
        """The log spans every clinic and includes password resets."""
        for username in ("admin", "hsr.reception"):
            response = client.get("/api/audit-logs", headers=auth(username))
            assert response.status_code == 403, username

    def test_there_is_no_way_to_edit_or_delete_an_entry(self, client, superadmin, auth):
        """An audit trail that can be edited is not an audit trail.

        404 rather than 405 is the stronger result: the route does not exist at
        all, so there is no handler to reach even by accident.
        """
        headers = auth("superadmin")
        entry_id = client.get("/api/audit-logs", headers=headers).json()["items"][0]["id"]

        assert client.delete(f"/api/audit-logs/{entry_id}", headers=headers).status_code == 404
        assert (
            client.put(
                f"/api/audit-logs/{entry_id}", headers=headers, json={"description": "x"}
            ).status_code
            == 404
        )

    def test_known_actions_are_listed_for_the_filter(self, client, superadmin, auth):
        actions = client.get("/api/audit-logs/actions", headers=auth("superadmin")).json()
        assert "LOGIN" in actions["actions"]
        assert "PAYMENT_RECORDED" in actions["actions"]


# --------------------------------------------------------------------------- #
class TestLoginThrottling:
    def _fail(self, client, username="superadmin"):
        return client.post(
            "/api/auth/login", json={"username": username, "password": "definitely-wrong"}
        )

    def test_repeated_failures_are_eventually_blocked(self, client, superadmin):
        """Argon2 makes each attempt costly; unlimited attempts undo that."""
        limit = settings.LOGIN_MAX_ATTEMPTS
        for attempt in range(limit - 1):
            assert self._fail(client).status_code == 401, attempt

        # The attempt that trips the limit still reports 401...
        assert self._fail(client).status_code == 401
        # ...and the next one is refused outright, with a distinguishable code.
        blocked = self._fail(client)
        assert blocked.status_code == 429
        assert "Too many failed" in blocked.json()["error"]["message"]

    def test_a_lockout_blocks_the_correct_password_too(self, client, superadmin):
        """Otherwise the limit is trivially bypassed by guessing on."""
        for _ in range(settings.LOGIN_MAX_ATTEMPTS):
            self._fail(client)

        response = client.post(
            "/api/auth/login",
            json={"username": "superadmin", "password": "TestPass@123"},
        )
        assert response.status_code == 429

    def test_a_correct_password_clears_the_count(self, client, superadmin):
        for _ in range(settings.LOGIN_MAX_ATTEMPTS - 2):
            self._fail(client)

        good = client.post(
            "/api/auth/login",
            json={"username": "superadmin", "password": "TestPass@123"},
        )
        assert good.status_code == 200

        # Back to a full allowance rather than one attempt from a lockout.
        for _ in range(settings.LOGIN_MAX_ATTEMPTS - 1):
            assert self._fail(client).status_code == 401

    def test_one_account_lockout_does_not_affect_another(
        self, client, superadmin, admin
    ):
        """A colleague mistyping their password must not lock out the clinic."""
        for _ in range(settings.LOGIN_MAX_ATTEMPTS + 1):
            self._fail(client, "superadmin")
        assert self._fail(client, "superadmin").status_code == 429

        other = client.post(
            "/api/auth/login", json={"username": "admin", "password": "TestPass@123"}
        )
        assert other.status_code == 200

    def test_a_blocked_attempt_is_audited(self, client, superadmin, auth):
        for _ in range(settings.LOGIN_MAX_ATTEMPTS + 1):
            self._fail(client)
        login_limiter.reset()

        items = client.get("/api/audit-logs", headers=auth("superadmin")).json()["items"]
        assert any("rate limit" in (item["description"] or "") for item in items)
