"""Authentication and token handling (Section 39: Authentication)."""

import jwt
import pytest

from app.auth.security import (
    TokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.config import settings
from app.models import AuditAction, AuditLog
from tests.conftest import TEST_PASSWORD


class TestLogin:
    def test_login_succeeds_and_returns_profile(self, client, superadmin):
        response = client.post(
            "/api/auth/login", json={"username": "superadmin", "password": TEST_PASSWORD}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"] and body["refresh_token"]
        assert body["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        assert body["user"]["username"] == "superadmin"
        assert body["user"]["role"] == "SUPERADMIN"
        # The password hash must never appear in an API response.
        assert "hashed_password" not in body["user"]
        assert "password" not in body["user"]

    def test_login_is_case_insensitive_on_username(self, client, superadmin):
        response = client.post(
            "/api/auth/login", json={"username": "SuperAdmin", "password": TEST_PASSWORD}
        )
        assert response.status_code == 200

    def test_wrong_password_is_rejected(self, client, superadmin):
        response = client.post(
            "/api/auth/login", json={"username": "superadmin", "password": "wrong-password"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_failed"

    def test_unknown_user_gives_identical_error(self, client, superadmin):
        """Same message as a wrong password, so usernames cannot be enumerated."""
        unknown = client.post(
            "/api/auth/login", json={"username": "ghost", "password": TEST_PASSWORD}
        )
        wrong = client.post(
            "/api/auth/login", json={"username": "superadmin", "password": "nope"}
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["error"] == wrong.json()["error"]

    def test_disabled_account_cannot_sign_in(self, client, db, admin):
        admin.is_active = False
        db.commit()
        response = client.post(
            "/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "user_disabled"

    def test_empty_credentials_fail_validation(self, client):
        response = client.post("/api/auth/login", json={"username": "", "password": ""})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    def test_login_records_audit_entries(self, client, db, superadmin):
        client.post("/api/auth/login", json={"username": "superadmin", "password": TEST_PASSWORD})
        client.post("/api/auth/login", json={"username": "superadmin", "password": "bad"})

        actions = {log.action for log in db.query(AuditLog).all()}
        assert AuditAction.LOGIN in actions
        assert AuditAction.LOGIN_FAILED in actions

    def test_login_updates_last_login_timestamp(self, client, db, superadmin):
        assert superadmin.last_login_at is None
        client.post("/api/auth/login", json={"username": "superadmin", "password": TEST_PASSWORD})
        db.refresh(superadmin)
        assert superadmin.last_login_at is not None

    def test_oauth2_form_login_works_for_docs(self, client, superadmin):
        response = client.post(
            "/api/auth/token", data={"username": "superadmin", "password": TEST_PASSWORD}
        )
        assert response.status_code == 200
        assert response.json()["access_token"]


class TestProtectedRoutes:
    def test_me_requires_a_token(self, client):
        assert client.get("/api/auth/me").status_code == 401

    def test_me_returns_the_caller(self, client, hsr_user, auth, clinics):
        response = client.get("/api/auth/me", headers=auth("hsr.reception"))
        assert response.status_code == 200
        body = response.json()
        assert body["role"] == "CLINIC_USER"
        assert [c["clinic_code"] for c in body["clinics"]] == ["HSR"]

    def test_garbage_token_is_rejected(self, client, superadmin):
        response = client.get(
            "/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401

    def test_token_signed_with_another_key_is_rejected(self, client, superadmin):
        forged = jwt.encode(
            {"sub": str(superadmin.id), "type": "access", "exp": 9999999999},
            "attacker-key",
            algorithm="HS256",
        )
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
        assert response.status_code == 401

    def test_refresh_token_cannot_be_used_as_access_token(self, client, superadmin):
        refresh = create_refresh_token(superadmin.id)
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh}"})
        assert response.status_code == 401

    def test_disabling_a_user_invalidates_their_live_token(self, client, db, admin, auth):
        headers = auth("admin")
        assert client.get("/api/auth/me", headers=headers).status_code == 200

        admin.is_active = False
        db.commit()

        # Permissions are re-read per request, so the still-valid JWT stops working.
        assert client.get("/api/auth/me", headers=headers).status_code == 403


class TestRefresh:
    def test_refresh_returns_a_new_pair(self, client, superadmin):
        login = client.post(
            "/api/auth/login", json={"username": "superadmin", "password": TEST_PASSWORD}
        ).json()
        response = client.post(
            "/api/auth/refresh", json={"refresh_token": login["refresh_token"]}
        )
        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_access_token_cannot_be_used_to_refresh(self, client, superadmin):
        access = create_access_token(superadmin.id, "SUPERADMIN")
        response = client.post("/api/auth/refresh", json={"refresh_token": access})
        assert response.status_code == 401


class TestChangePassword:
    def test_password_can_be_changed_and_old_one_stops_working(self, client, admin, auth):
        response = client.post(
            "/api/auth/change-password",
            headers=auth("admin"),
            json={"current_password": TEST_PASSWORD, "new_password": "BrandNew@456"},
        )
        assert response.status_code == 200

        old = client.post("/api/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
        new = client.post(
            "/api/auth/login", json={"username": "admin", "password": "BrandNew@456"}
        )
        assert old.status_code == 401
        assert new.status_code == 200

    def test_wrong_current_password_is_rejected(self, client, admin, auth):
        response = client.post(
            "/api/auth/change-password",
            headers=auth("admin"),
            json={"current_password": "not-it", "new_password": "BrandNew@456"},
        )
        assert response.status_code == 401

    def test_reusing_the_same_password_is_rejected(self, client, admin, auth):
        response = client.post(
            "/api/auth/change-password",
            headers=auth("admin"),
            json={"current_password": TEST_PASSWORD, "new_password": TEST_PASSWORD},
        )
        assert response.status_code == 400

    def test_short_password_fails_validation(self, client, admin, auth):
        response = client.post(
            "/api/auth/change-password",
            headers=auth("admin"),
            json={"current_password": TEST_PASSWORD, "new_password": "short"},
        )
        assert response.status_code == 422


class TestPasswordHashing:
    def test_hash_is_not_the_plaintext(self):
        hashed = hash_password("Secret@123")
        assert hashed != "Secret@123"
        assert hashed.startswith("$argon2")

    def test_verification_round_trip(self):
        hashed = hash_password("Secret@123")
        assert verify_password("Secret@123", hashed)
        assert not verify_password("Secret@124", hashed)

    def test_same_password_gets_different_hashes(self):
        """Distinct salts, so identical passwords are not identifiable in a dump."""
        assert hash_password("Secret@123") != hash_password("Secret@123")

    def test_verify_does_not_raise_on_a_corrupt_hash(self):
        assert verify_password("anything", "not-a-hash") is False


class TestTokenDecoding:
    def test_expired_token_raises(self, superadmin, monkeypatch):
        monkeypatch.setattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", -1)
        token = create_access_token(superadmin.id, "SUPERADMIN")
        with pytest.raises(TokenError, match="expired"):
            decode_token(token, expected_type=TokenType.ACCESS)

    def test_wrong_type_raises(self, superadmin):
        token = create_refresh_token(superadmin.id)
        with pytest.raises(TokenError):
            decode_token(token, expected_type=TokenType.ACCESS)
