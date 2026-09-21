"""User management and role authorisation (Section 39: Authentication/Clinics)."""

from tests.conftest import TEST_PASSWORD


class TestRoleAuthorisation:
    def test_only_superadmin_may_list_users(self, client, superadmin, admin, hsr_user, auth):
        assert client.get("/api/users", headers=auth("superadmin")).status_code == 200
        assert client.get("/api/users", headers=auth("admin")).status_code == 403
        assert client.get("/api/users", headers=auth("hsr.reception")).status_code == 403

    def test_admin_cannot_create_users(self, client, admin, auth):
        response = client.post(
            "/api/users",
            headers=auth("admin"),
            json={
                "username": "new.admin",
                "password": "Secret@123",
                "full_name": "New Admin",
                "role": "ADMIN",
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    def test_clinic_user_cannot_create_users(self, client, hsr_user, auth):
        response = client.post(
            "/api/users",
            headers=auth("hsr.reception"),
            json={
                "username": "sneaky",
                "password": "Secret@123",
                "full_name": "Sneaky User",
                "role": "CLINIC_USER",
                "clinic_ids": [1],
            },
        )
        assert response.status_code == 403

    def test_superadmin_cannot_mint_another_superadmin(self, client, superadmin, auth):
        """SUPERADMIN is not in the assignable set, so escalation needs DB access."""
        response = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "second.super",
                "password": "Secret@123",
                "full_name": "Second Super",
                "role": "SUPERADMIN",
            },
        )
        assert response.status_code == 403


class TestCreateUser:
    def test_create_admin(self, client, superadmin, auth):
        response = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "ops.admin",
                "password": "Secret@123",
                "full_name": "Ops Admin",
                "email": "ops@clinic.example.com",
                "phone": "9876500999",
                "role": "ADMIN",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["role"] == "ADMIN"
        assert body["is_active"] is True
        assert body["clinics"] == []

    def test_created_user_can_sign_in(self, client, superadmin, auth):
        client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "ops.admin",
                "password": "Secret@123",
                "full_name": "Ops Admin",
                "role": "ADMIN",
            },
        )
        response = client.post(
            "/api/auth/login", json={"username": "ops.admin", "password": "Secret@123"}
        )
        assert response.status_code == 200

    def test_create_clinic_user_with_assignment(self, client, superadmin, clinics, auth):
        response = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "hsr.physio2",
                "password": "Secret@123",
                "full_name": "Second Physio",
                "role": "CLINIC_USER",
                "clinic_ids": [clinics[0].id],
                "designation": "Physiotherapist",
            },
        )
        assert response.status_code == 201
        assignments = response.json()["clinics"]
        assert len(assignments) == 1
        assert assignments[0]["clinic_code"] == "HSR"
        assert assignments[0]["is_primary"] is True
        assert assignments[0]["designation"] == "Physiotherapist"

    def test_clinic_user_requires_a_clinic(self, client, superadmin, auth):
        response = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "floating.user",
                "password": "Secret@123",
                "full_name": "Floating User",
                "role": "CLINIC_USER",
                "clinic_ids": [],
            },
        )
        assert response.status_code == 422

    def test_admin_cannot_be_pinned_to_a_clinic(self, client, superadmin, clinics, auth):
        """Admins are chain-wide by definition (Section 38)."""
        response = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "weird.admin",
                "password": "Secret@123",
                "full_name": "Weird Admin",
                "role": "ADMIN",
                "clinic_ids": [clinics[0].id],
            },
        )
        assert response.status_code == 422

    def test_unknown_clinic_is_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "ghost.clinic",
                "password": "Secret@123",
                "full_name": "Ghost Clinic User",
                "role": "CLINIC_USER",
                "clinic_ids": [9999],
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"

    def test_duplicate_username_conflicts(self, client, superadmin, admin, auth):
        response = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "admin",
                "password": "Secret@123",
                "full_name": "Duplicate",
                "role": "ADMIN",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "duplicate_resource"

    def test_duplicate_email_conflicts(self, client, superadmin, admin, auth):
        response = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "another.admin",
                "password": "Secret@123",
                "full_name": "Another Admin",
                "email": "admin@clinic.example.com",
                "role": "ADMIN",
            },
        )
        assert response.status_code == 409

    def test_invalid_phone_is_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "bad.phone",
                "password": "Secret@123",
                "full_name": "Bad Phone",
                "phone": "12345",
                "role": "ADMIN",
            },
        )
        assert response.status_code == 422

    def test_short_password_is_rejected(self, client, superadmin, auth):
        response = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "weak.pass",
                "password": "abc",
                "full_name": "Weak Password",
                "role": "ADMIN",
            },
        )
        assert response.status_code == 422


class TestUpdateUser:
    def test_edit_details(self, client, superadmin, admin, auth):
        response = client.put(
            f"/api/users/{admin.id}",
            headers=auth("superadmin"),
            json={"full_name": "Renamed Admin", "phone": "9876511111"},
        )
        assert response.status_code == 200
        assert response.json()["full_name"] == "Renamed Admin"
        assert response.json()["phone"] == "9876511111"

    def test_reassign_clinic_user_to_another_clinic(
        self, client, superadmin, hsr_user, clinics, auth
    ):
        response = client.put(
            f"/api/users/{hsr_user.id}",
            headers=auth("superadmin"),
            json={"clinic_ids": [clinics[1].id]},
        )
        assert response.status_code == 200
        assert [c["clinic_code"] for c in response.json()["clinics"]] == ["BTM"]

        # And the new scope is what the API now enforces.
        visible = client.get("/api/clinics", headers=auth("hsr.reception")).json()
        assert [c["code"] for c in visible] == ["BTM"]

    def test_clinic_user_cannot_be_left_with_no_clinic(
        self, client, superadmin, hsr_user, auth
    ):
        response = client.put(
            f"/api/users/{hsr_user.id}", headers=auth("superadmin"), json={"clinic_ids": []}
        )
        assert response.status_code == 400

    def test_unknown_user_is_404(self, client, superadmin, auth):
        response = client.put(
            "/api/users/9999", headers=auth("superadmin"), json={"full_name": "Nobody"}
        )
        assert response.status_code == 404


class TestEnableDisable:
    def test_disable_then_enable(self, client, superadmin, admin, auth):
        headers = auth("superadmin")

        disabled = client.post(f"/api/users/{admin.id}/disable", headers=headers)
        assert disabled.status_code == 200
        assert disabled.json()["is_active"] is False
        assert (
            client.post(
                "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
            ).status_code
            == 403
        )

        enabled = client.post(f"/api/users/{admin.id}/enable", headers=headers)
        assert enabled.json()["is_active"] is True
        assert (
            client.post(
                "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
            ).status_code
            == 200
        )

    def test_cannot_disable_own_account(self, client, superadmin, auth):
        response = client.post(
            f"/api/users/{superadmin.id}/disable", headers=auth("superadmin")
        )
        assert response.status_code == 400

    def test_superadmin_account_cannot_be_disabled(self, client, db, roles, superadmin, auth):
        from tests.conftest import _make_user
        from app.models import RoleName

        other = _make_user(db, roles, "other.super", RoleName.SUPERADMIN)
        response = client.post(f"/api/users/{other.id}/disable", headers=auth("superadmin"))
        assert response.status_code == 403


class TestPasswordReset:
    def test_superadmin_resets_a_password(self, client, superadmin, admin, auth):
        response = client.post(
            f"/api/users/{admin.id}/reset-password",
            headers=auth("superadmin"),
            json={"new_password": "Rotated@789"},
        )
        assert response.status_code == 200
        assert (
            client.post(
                "/api/auth/login", json={"username": "admin", "password": "Rotated@789"}
            ).status_code
            == 200
        )


class TestListing:
    def test_filter_by_role(self, client, superadmin, admin, hsr_user, btm_user, auth):
        response = client.get("/api/users?role=CLINIC_USER", headers=auth("superadmin"))
        assert response.status_code == 200
        assert response.json()["total"] == 2

    def test_filter_by_clinic(self, client, superadmin, hsr_user, btm_user, clinics, auth):
        response = client.get(
            f"/api/users?clinic_id={clinics[0].id}", headers=auth("superadmin")
        )
        assert [u["username"] for u in response.json()["items"]] == ["hsr.reception"]

    def test_search_by_name(self, client, superadmin, admin, auth):
        response = client.get("/api/users?search=admin", headers=auth("superadmin"))
        usernames = {u["username"] for u in response.json()["items"]}
        assert "admin" in usernames

    def test_pagination(self, client, superadmin, admin, hsr_user, btm_user, auth):
        response = client.get("/api/users?page=1&page_size=2", headers=auth("superadmin"))
        body = response.json()
        assert body["total"] == 4
        assert len(body["items"]) == 2


class TestStaffIdentifiers:
    """Internal employee number and professional registration."""

    def _create(self, client, auth, clinics, **extra):
        return client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "swati",
                "full_name": "Dr Swati",
                "password": "TestPass@123",
                "role": "CLINIC_USER",
                "clinic_ids": [clinics[0].id],
                **extra,
            },
        )

    def test_both_are_saved_on_creation(self, client, superadmin, clinics, auth):
        """The same omission that silently dropped clinic branding."""
        response = self._create(
            self and client,
            auth,
            clinics,
            employee_id="EMP-014",
            registration_number="KSPC/PT/2019/4471",
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["employee_id"] == "EMP-014"
        assert body["registration_number"] == "KSPC/PT/2019/4471"

    def test_both_are_optional(self, client, superadmin, clinics, auth):
        """A chain that does not use staff numbers must not be forced to invent them."""
        response = self._create(client, auth, clinics)
        assert response.status_code == 201
        assert response.json()["employee_id"] is None
        assert response.json()["registration_number"] is None

    def test_employee_id_must_be_unique(self, client, superadmin, clinics, auth):
        self._create(client, auth, clinics, employee_id="EMP-014")
        clash = client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": "arjun",
                "full_name": "Dr Arjun",
                "password": "TestPass@123",
                "role": "CLINIC_USER",
                "clinic_ids": [clinics[0].id],
                "employee_id": "EMP-014",
            },
        )
        assert clash.status_code == 409
        assert "EMP-014" in clash.json()["error"]["message"]

    def test_they_can_be_edited_later(self, client, superadmin, clinics, auth):
        created = self._create(client, auth, clinics).json()
        updated = client.put(
            f"/api/users/{created['id']}",
            headers=auth("superadmin"),
            json={"employee_id": "EMP-020", "registration_number": "KSPC/PT/2020/9910"},
        )
        assert updated.status_code == 200
        assert updated.json()["employee_id"] == "EMP-020"
        assert updated.json()["registration_number"] == "KSPC/PT/2020/9910"


class TestDeletingUsers:
    """Deletion is for an account created by mistake, not for a leaver.

    Every foreign key pointing at `users` is `ON DELETE SET NULL`, so removing
    someone who has worked would blank them out of sessions, bills and payments.
    """

    def _make(self, client, auth, clinics, username="temp.user"):
        return client.post(
            "/api/users",
            headers=auth("superadmin"),
            json={
                "username": username,
                "full_name": "Temp User",
                "password": "TestPass@123",
                "role": "CLINIC_USER",
                "clinic_ids": [clinics[0].id],
            },
        ).json()

    def test_an_unused_account_can_be_deleted(self, client, superadmin, clinics, auth):
        created = self._make(client, auth, clinics)
        response = client.delete(
            f"/api/users/{created['id']}", headers=auth("superadmin")
        )
        assert response.status_code == 204

        gone = client.get(f"/api/users/{created['id']}", headers=auth("superadmin"))
        assert gone.status_code == 404

    def test_a_user_with_history_is_refused(self, client, superadmin, clinics, auth):
        """Deleting them would leave a session with no therapist."""
        headers = auth("superadmin")
        created = self._make(client, auth, clinics)

        patient = client.post(
            "/api/patients",
            headers=headers,
            json={
                "full_name": "Rahul Sharma",
                "mobile": "9876500031",
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        package = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 4,
                "price_per_session": "500.00",
                "clinic_id": clinics[0].id,
                "skip_billing": True,
            },
        ).json()
        client.post(
            f"/api/patients/{patient['id']}/sessions",
            headers=headers,
            json={"package_id": package["id"], "therapist_user_id": created["id"]},
        )

        response = client.delete(f"/api/users/{created['id']}", headers=headers)
        assert response.status_code == 400
        message = response.json()["error"]["message"]
        assert "1 session(s) delivered" in message
        # The message must point at the action that actually fits.
        assert "Disable" in message

    def test_a_disabled_user_with_history_still_cannot_be_deleted(
        self, client, superadmin, clinics, auth
    ):
        """Disabling first is not a way round the guard."""
        headers = auth("superadmin")
        created = self._make(client, auth, clinics)
        patient = client.post(
            "/api/patients",
            headers=headers,
            json={
                "full_name": "Meera Iyer",
                "mobile": "9876500032",
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 2,
                "price_per_session": "500.00",
                "clinic_id": clinics[0].id,
                "payment": {"amount": "1000.00", "payment_method": "CASH"},
            },
        )
        # The bill and payment were recorded by the superadmin, so give the
        # target user some history of their own.
        client.post(
            f"/api/patients/{patient['id']}/sessions",
            headers=headers,
            json={"no_package": True, "therapist_user_id": created["id"]},
        )
        client.post(f"/api/users/{created['id']}/disable", headers=headers)

        assert client.delete(f"/api/users/{created['id']}", headers=headers).status_code == 400

    def test_you_cannot_delete_yourself(self, client, superadmin, auth):
        response = client.delete(
            f"/api/users/{superadmin.id}", headers=auth("superadmin")
        )
        assert response.status_code in (400, 403)

    def test_a_superadmin_cannot_be_deleted(self, client, superadmin, db, roles, auth):
        from app.auth.security import hash_password
        from app.models import RoleName, User

        other = User(
            username="second.super",
            full_name="Second Superadmin",
            hashed_password=hash_password("TestPass@123"),
            role_id=roles[RoleName.SUPERADMIN].id,
            is_active=True,
        )
        db.add(other)
        db.commit()

        response = client.delete(f"/api/users/{other.id}", headers=auth("superadmin"))
        assert response.status_code == 403

    def test_admins_and_clinic_users_cannot_delete_anyone(
        self, client, superadmin, admin, hsr_user, clinics, auth
    ):
        created = self._make(client, auth, clinics)
        for username in ("admin", "hsr.reception"):
            response = client.delete(
                f"/api/users/{created['id']}", headers=auth(username)
            )
            assert response.status_code == 403, username

    def test_the_deletion_is_audited_and_survives_the_account(
        self, client, superadmin, clinics, auth
    ):
        """The audit trail keeps the username as free text for exactly this."""
        headers = auth("superadmin")
        created = self._make(client, auth, clinics, username="mistake.user")
        client.delete(f"/api/users/{created['id']}", headers=headers)

        entries = client.get(
            "/api/audit-logs", headers=headers, params={"action": "DELETED"}
        ).json()
        assert entries["total"] >= 1
        assert any("mistake.user" in (e["description"] or "") for e in entries["items"])
