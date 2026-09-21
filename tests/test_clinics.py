"""Clinic access control (Section 39: Clinic User access restriction).

The scenario named explicitly in Section 23 -- a Clinic User calling
`GET /clinics` -- is covered here at the API boundary.
"""


class TestClinicVisibility:
    def test_superadmin_sees_every_clinic(self, client, superadmin, clinics, auth):
        response = client.get("/api/clinics", headers=auth("superadmin"))
        assert response.status_code == 200
        assert len(response.json()) == len(clinics)

    def test_admin_sees_every_clinic(self, client, admin, clinics, auth):
        response = client.get("/api/clinics", headers=auth("admin"))
        assert {c["code"] for c in response.json()} == {"HSR", "BTM", "WHF"}

    def test_clinic_user_sees_only_their_own(self, client, hsr_user, clinics, auth):
        response = client.get("/api/clinics", headers=auth("hsr.reception"))
        assert response.status_code == 200
        assert [c["code"] for c in response.json()] == ["HSR"]

    def test_unassigned_clinic_user_sees_nothing(self, client, unassigned_user, clinics, auth):
        """An empty assignment must mean zero clinics, never all clinics."""
        response = client.get("/api/clinics", headers=auth("orphan.user"))
        assert response.status_code == 200
        assert response.json() == []

    def test_anonymous_access_is_refused(self, client, clinics):
        assert client.get("/api/clinics").status_code == 401

    def test_status_filter(self, client, superadmin, clinics, auth):
        response = client.get("/api/clinics?status=ACTIVE", headers=auth("superadmin"))
        assert {c["code"] for c in response.json()} == {"HSR", "BTM"}

    def test_search_filter(self, client, superadmin, clinics, auth):
        response = client.get("/api/clinics?search=btm", headers=auth("superadmin"))
        assert [c["code"] for c in response.json()] == ["BTM"]


class TestClinicDetail:
    def test_clinic_user_can_read_their_clinic(self, client, hsr_user, clinics, auth):
        response = client.get(f"/api/clinics/{clinics[0].id}", headers=auth("hsr.reception"))
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "HSR Layout"
        assert body["capacity_per_slot"] == 3
        assert body["slot_duration_minutes"] == 30
        # Monday-Saturday with a morning and an evening shift each.
        assert len(body["working_hours"]) == 12
        assert body["assigned_user_count"] == 1

    def test_clinic_user_cannot_read_another_clinic(self, client, hsr_user, clinics, auth):
        response = client.get(f"/api/clinics/{clinics[1].id}", headers=auth("hsr.reception"))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "clinic_access_denied"

    def test_admin_can_read_any_clinic(self, client, admin, clinics, auth):
        for clinic in clinics:
            response = client.get(f"/api/clinics/{clinic.id}", headers=auth("admin"))
            assert response.status_code == 200

    def test_unknown_clinic_is_404_for_privileged_roles(self, client, superadmin, auth):
        response = client.get("/api/clinics/9999", headers=auth("superadmin"))
        assert response.status_code == 404

    def test_nonexistent_clinic_is_not_distinguishable_for_clinic_users(
        self, client, hsr_user, clinics, auth
    ):
        """A restricted user gets 403 either way, so IDs cannot be probed."""
        other = client.get(f"/api/clinics/{clinics[1].id}", headers=auth("hsr.reception"))
        missing = client.get("/api/clinics/9999", headers=auth("hsr.reception"))
        assert other.status_code == 403
        assert missing.status_code == 404  # clinic genuinely absent
        assert other.json()["error"]["code"] == "clinic_access_denied"


class TestClinicWriteEndpoints:
    """Phase 2 exposed these. Detailed coverage lives in test_clinic_management.py;
    what matters here is that the read-focused scoping rules still hold alongside
    the new write endpoints."""

    def test_clinic_creation_is_exposed_to_superadmin(self, client, superadmin, auth):
        response = client.post(
            "/api/clinics", headers=auth("superadmin"), json={"name": "Jayanagar"}
        )
        assert response.status_code == 201

    def test_clinic_creation_requires_a_payload(self, client, superadmin, auth):
        response = client.post("/api/clinics", headers=auth("superadmin"), json={})
        assert response.status_code == 422

    def test_reads_are_unaffected_by_the_new_write_endpoints(
        self, client, hsr_user, clinics, auth
    ):
        response = client.get("/api/clinics", headers=auth("hsr.reception"))
        assert [c["code"] for c in response.json()] == ["HSR"]
