"""Dashboard scoping and aggregation (Section 22)."""

from decimal import Decimal


class TestDashboardScoping:
    def test_superadmin_sees_system_wide_counters(
        self, client, superadmin, sample_clinical_data, auth
    ):
        body = client.get("/api/dashboard/summary", headers=auth("superadmin")).json()
        counters = body["counters"]
        assert body["role"] == "SUPERADMIN"
        assert body["scope_label"] == "All clinics (system-wide)"
        assert counters["total_clinics"] == 3
        assert counters["active_clinics"] == 2
        assert counters["total_patients"] == 3
        assert counters["appointments_today"] == 3
        assert counters["completed_today"] == 1

    def test_clinic_user_counters_cover_only_their_clinic(
        self, client, hsr_user, sample_clinical_data, auth
    ):
        body = client.get("/api/dashboard/summary", headers=auth("hsr.reception")).json()
        counters = body["counters"]
        assert body["scope_label"] == "Your clinic: HSR Layout"
        assert counters["total_patients"] == 2       # 3rd patient belongs to BTM
        assert counters["appointments_today"] == 2
        assert counters["completed_today"] == 1

    def test_clinic_user_gets_no_system_or_revenue_figures(
        self, client, hsr_user, sample_clinical_data, auth
    ):
        counters = client.get(
            "/api/dashboard/summary", headers=auth("hsr.reception")
        ).json()["counters"]
        assert counters["total_clinics"] is None
        assert counters["total_users"] is None
        assert counters["revenue_total"] is None
        assert counters["outstanding_amount"] is None

    def test_admin_sees_revenue_but_not_system_footprint(
        self, client, admin, sample_clinical_data, auth
    ):
        counters = client.get("/api/dashboard/summary", headers=auth("admin")).json()["counters"]
        assert counters["revenue_total"] is not None
        assert counters["total_clinics"] is None

    def test_unassigned_clinic_user_sees_zeroes_not_everything(
        self, client, unassigned_user, sample_clinical_data, auth
    ):
        counters = client.get(
            "/api/dashboard/summary", headers=auth("orphan.user")
        ).json()["counters"]
        assert counters["total_patients"] == 0
        assert counters["appointments_today"] == 0

    def test_anonymous_access_is_refused(self, client):
        assert client.get("/api/dashboard/summary").status_code == 401


class TestRevenueAggregation:
    """Fixture totals: HSR billed 5000/paid 5000, BTM billed 4000/paid 1000,
    plus a cancelled 9999 bill that must be ignored everywhere."""

    def test_totals_are_exact(self, client, superadmin, sample_clinical_data, auth):
        counters = client.get(
            "/api/dashboard/summary", headers=auth("superadmin")
        ).json()["counters"]
        assert Decimal(counters["revenue_total"]) == Decimal("6000.00")
        assert Decimal(counters["outstanding_amount"]) == Decimal("3000.00")

    def test_cancelled_bills_are_excluded(self, client, superadmin, sample_clinical_data, auth):
        counters = client.get(
            "/api/dashboard/summary", headers=auth("superadmin")
        ).json()["counters"]
        # 9999 would show up in either figure if cancelled bills leaked in.
        assert Decimal(counters["revenue_total"]) + Decimal(
            counters["outstanding_amount"]
        ) == Decimal("9000.00")

    def test_per_clinic_breakdown_splits_correctly(
        self, client, superadmin, sample_clinical_data, auth
    ):
        body = client.get("/api/dashboard/summary", headers=auth("superadmin")).json()
        by_name = {row["clinic_name"]: row for row in body["clinic_performance"]}

        assert Decimal(by_name["HSR Layout"]["revenue"]) == Decimal("5000.00")
        assert Decimal(by_name["HSR Layout"]["outstanding"]) == Decimal("0.00")
        assert by_name["HSR Layout"]["total_patients"] == 2
        assert by_name["HSR Layout"]["appointments_today"] == 2

        assert Decimal(by_name["BTM Layout"]["revenue"]) == Decimal("1000.00")
        assert Decimal(by_name["BTM Layout"]["outstanding"]) == Decimal("3000.00")

    def test_breakdown_sums_to_the_headline_number(
        self, client, superadmin, sample_clinical_data, auth
    ):
        """Guards against the cross-join class of aggregation bug."""
        body = client.get("/api/dashboard/summary", headers=auth("superadmin")).json()
        total = sum(Decimal(row["revenue"]) for row in body["clinic_performance"])
        assert total == Decimal(body["counters"]["revenue_total"])

    def test_clinic_user_gets_no_breakdown(
        self, client, hsr_user, sample_clinical_data, auth
    ):
        body = client.get("/api/dashboard/summary", headers=auth("hsr.reception")).json()
        assert body["clinic_performance"] == []
