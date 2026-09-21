"""Phase 8: reporting.

The property these tests exist for: **every total reconciles with its own
breakdown.** A cross-join aggregation bug reported revenue as 4x reality in
Phase 1, and the only reliable guard is asserting the sum of the rows equals the
headline figure rather than trusting either in isolation.

Also covered: clinic scoping per role, CSV shape, and empty ranges — a report
over a quiet week must return zeros, not blow up on an empty aggregate.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest


def next_weekday(target: int = 0, weeks_ahead: int = 1) -> date:
    today = date.today()
    ahead = (target - today.weekday()) % 7
    return today + timedelta(days=ahead + 7 * weeks_ahead)


MONDAY = 0
WIDE = {"from": (date.today() - timedelta(days=90)).isoformat(),
        "to": (date.today() + timedelta(days=90)).isoformat()}


def dec(value) -> Decimal:
    return Decimal(str(value))


@pytest.fixture
def activity(client, superadmin, admin, hsr_user, btm_user, clinics, auth):
    """A known set of facts across both clinics, so the numbers are checkable.

    HSR: 2 patients, one 10-session package at 500 (5,000, 2,000 paid) plus a
    500 consultation; 2 sessions logged.
    BTM: 1 patient, one 4-session package at 300 (1,200, fully paid).
    """
    headers = auth("superadmin")

    def make_patient(name, mobile, clinic):
        return client.post(
            "/api/patients",
            headers=headers,
            json={"full_name": name, "mobile": mobile, "primary_clinic_id": clinic.id},
        ).json()

    hsr_one = make_patient("Rahul Sharma", "9876500001", clinics[0])
    hsr_two = make_patient("Meera Iyer", "9876500002", clinics[0])
    btm_one = make_patient("Arun Nair", "9876500003", clinics[1])

    package = client.post(
        f"/api/patients/{hsr_one['id']}/packages",
        headers=headers,
        json={
            "sessions_registered": 10,
            "price_per_session": "500.00",
            "clinic_id": clinics[0].id,
            "additional_charges": [
                {"description": "Initial consultation", "unit_price": "500.00"}
            ],
            "payment": {"amount": "2000.00", "payment_method": "UPI"},
        },
    ).json()

    btm_package = client.post(
        f"/api/patients/{btm_one['id']}/packages",
        headers=headers,
        json={
            "sessions_registered": 4,
            "price_per_session": "300.00",
            "clinic_id": clinics[1].id,
            "payment": {"amount": "1200.00", "payment_method": "CASH"},
        },
    ).json()

    for _ in range(2):
        client.post(
            f"/api/patients/{hsr_one['id']}/sessions",
            headers=headers,
            json={"package_id": package["id"]},
        )

    # One booking so the appointment report has something to count.
    client.post(
        "/api/appointments",
        headers=headers,
        json={
            "patient_id": hsr_one["id"],
            "clinic_id": clinics[0].id,
            "appointment_date": next_weekday(MONDAY).isoformat(),
            "start_time": "10:00",
        },
    )

    return {
        "patients": [hsr_one, hsr_two, btm_one],
        "package": package,
        "btm_package": btm_package,
    }


def run(client, headers, name, **params):
    response = client.get(f"/api/reports/{name}", headers=headers, params={**WIDE, **params})
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
class TestClinicReport:
    def test_totals_reconcile_with_the_rows(self, client, activity, auth):
        """The guard against the cross-join class of bug."""
        report = run(client, auth("superadmin"), "clinics")
        rows, totals = report["rows"], report["totals"]

        assert len(rows) >= 2
        for field in ("appointments", "sessions", "new_patients", "bills"):
            assert totals[field] == sum(row[field] for row in rows), field
        for field in ("billed", "collected"):
            assert dec(totals[field]) == sum(dec(row[field]) for row in rows), field

    def test_the_numbers_match_what_was_created(self, client, activity, clinics, auth):
        report = run(client, auth("superadmin"), "clinics")
        by_name = {row["clinic_name"]: row for row in report["rows"]}

        hsr = by_name["HSR Layout"]
        assert hsr["sessions"] == 2
        assert hsr["new_patients"] == 2
        # 10 x 500 + 500 consultation
        assert dec(hsr["billed"]) == dec("5500.00")
        assert dec(hsr["collected"]) == dec("2000.00")
        assert dec(hsr["outstanding"]) == dec("3500.00")

        btm = by_name["BTM Layout"]
        assert dec(btm["billed"]) == dec("1200.00")
        assert dec(btm["collected"]) == dec("1200.00")
        assert dec(btm["outstanding"]) == dec("0.00")

    def test_money_crosses_the_wire_as_strings(self, client, activity, auth):
        """Floats would reintroduce binary-fraction error at the edge."""
        report = run(client, auth("superadmin"), "clinics")
        assert isinstance(report["totals"]["billed"], str)
        assert isinstance(report["rows"][0]["collected"], str)

    def test_clinic_user_sees_only_their_own_clinic(self, client, activity, auth):
        report = run(client, auth("hsr.reception"), "clinics")
        assert [row["clinic_name"] for row in report["rows"]] == ["HSR Layout"]
        assert dec(report["totals"]["billed"]) == dec("5500.00")

    def test_unassigned_user_sees_zeros_not_everything(self, client, activity, unassigned_user, auth):
        report = run(client, auth("orphan.user"), "clinics")
        assert report["rows"] == []
        assert dec(report["totals"]["billed"]) == dec("0.00")

    def test_narrowing_to_one_clinic(self, client, activity, clinics, auth):
        report = run(client, auth("superadmin"), "clinics", clinic_id=clinics[1].id)
        assert [row["clinic_name"] for row in report["rows"]] == ["BTM Layout"]

    def test_another_clinics_report_is_denied(self, client, activity, clinics, auth):
        response = client.get(
            "/api/reports/clinics",
            headers=auth("hsr.reception"),
            params={**WIDE, "clinic_id": clinics[1].id},
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------- #
class TestAppointmentReport:
    def test_status_mix_sums_to_the_total(self, client, activity, auth):
        report = run(client, auth("superadmin"), "appointments")
        assert report["total"] == sum(report["by_status"].values())
        assert report["total"] == sum(row["appointments"] for row in report["daily"])

    def test_rates_are_percentages_of_the_total(self, client, activity, auth):
        report = run(client, auth("superadmin"), "appointments")
        for key in ("completion_rate", "no_show_rate", "cancellation_rate"):
            assert 0.0 <= report[key] <= 100.0


# --------------------------------------------------------------------------- #
class TestSourceReport:
    def test_shares_and_totals_agree(self, client, activity, auth):
        report = run(client, auth("superadmin"), "patient-sources")
        totals = report["totals"]
        assert totals["new_patients"] == sum(row["new_patients"] for row in report["rows"])
        assert dec(totals["billed"]) == sum(dec(row["billed"]) for row in report["rows"])
        if totals["new_patients"]:
            assert abs(sum(row["share_percent"] for row in report["rows"]) - 100) < 1.0

    def test_patients_without_a_source_are_still_counted(self, client, activity, auth):
        """Dropping them would make the shares add up to more than reality."""
        report = run(client, auth("superadmin"), "patient-sources")
        assert any(row["source_name"] == "Not recorded" for row in report["rows"])


# --------------------------------------------------------------------------- #
class TestRevenueReport:
    def test_daily_and_clinic_breakdowns_both_reconcile(self, client, activity, auth):
        report = run(client, auth("superadmin"), "revenue")
        assert dec(report["billed"]) == sum(dec(row["billed"]) for row in report["daily"])
        assert dec(report["collected"]) == sum(dec(row["collected"]) for row in report["daily"])
        assert dec(report["billed"]) == sum(dec(row["billed"]) for row in report["by_clinic"])

    def test_collected_equals_the_sum_of_payments(self, client, activity, auth):
        """Two independent tables must agree: bills.amount_paid and payments."""
        report = run(client, auth("superadmin"), "revenue")
        assert dec(report["collected"]) == sum(
            dec(row["amount"]) for row in report["by_payment_method"]
        )

    def test_line_items_sum_to_the_billed_total(self, client, activity, auth):
        report = run(client, auth("superadmin"), "revenue")
        assert dec(report["billed"]) == sum(dec(row["amount"]) for row in report["by_service"])

    def test_outstanding_is_billed_minus_collected(self, client, activity, auth):
        report = run(client, auth("superadmin"), "revenue")
        assert dec(report["outstanding"]) == dec(report["billed"]) - dec(report["collected"])

    def test_a_named_package_keeps_its_session_count_on_the_line(
        self, client, superadmin, clinics, auth
    ):
        """A package called "21" must not become an invoice line reading "21"."""
        headers = auth("superadmin")
        patient = client.post(
            "/api/patients",
            headers=headers,
            json={
                "full_name": "Kavya Rao",
                "mobile": "9876500009",
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 21,
                "price_per_session": "600.00",
                "package_name": "21",
                "clinic_id": clinics[0].id,
            },
        )
        report = run(client, headers, "revenue")
        descriptions = [row["description"] for row in report["by_service"]]
        assert "21" not in descriptions
        assert "21 (21 sessions)" in descriptions


# --------------------------------------------------------------------------- #
class TestSessionReport:
    def test_breakdowns_reconcile(self, client, activity, auth):
        report = run(client, auth("superadmin"), "sessions")
        assert report["total"] == sum(row["sessions"] for row in report["by_therapist"])
        assert report["total"] == sum(row["sessions"] for row in report["by_clinic"])
        assert report["total"] == sum(row["sessions"] for row in report["daily"])

    def test_voided_sessions_are_counted_separately_not_in_the_total(
        self, client, activity, auth
    ):
        headers = auth("superadmin")
        patient_id = activity["patients"][0]["id"]
        sessions = client.get(f"/api/patients/{patient_id}/sessions", headers=headers).json()
        client.post(
            f"/api/sessions/{sessions[0]['id']}/void",
            headers=headers,
            json={"reason": "Wrong patient"},
        )

        report = run(client, headers, "sessions")
        assert report["total"] == 1
        assert report["voided"] == 1

    def test_utilisation_compares_used_against_purchased(self, client, activity, auth):
        report = run(client, auth("superadmin"), "sessions")
        assert report["sessions_purchased"] == 14  # 10 + 4
        assert 0.0 <= report["utilisation_rate"] <= 100.0


# --------------------------------------------------------------------------- #
class TestRangesAndExport:
    def test_an_empty_range_returns_zeros_not_an_error(self, client, activity, auth):
        """A quiet week is a normal answer, not an exception."""
        long_ago = {"from": "2020-01-01", "to": "2020-01-31"}
        for name in ("clinics", "appointments", "patient-sources", "revenue", "sessions"):
            response = client.get(
                f"/api/reports/{name}", headers=auth("superadmin"), params=long_ago
            )
            assert response.status_code == 200, name
            body = response.json()
            if "total" in body:
                assert body["total"] == 0, name
            if "billed" in body:
                assert dec(body["billed"]) == dec("0.00"), name

    def test_backwards_range_is_refused(self, client, activity, auth):
        response = client.get(
            "/api/reports/revenue",
            headers=auth("superadmin"),
            params={"from": "2026-09-01", "to": "2026-08-01"},
        )
        assert response.status_code == 400

    def test_an_absurd_range_is_refused(self, client, activity, auth):
        response = client.get(
            "/api/reports/revenue",
            headers=auth("superadmin"),
            params={"from": "2000-01-01", "to": "2030-01-01"},
        )
        assert response.status_code == 400
        assert "days" in response.json()["error"]["message"]

    def test_default_range_is_the_last_30_days(self, client, activity, auth):
        report = client.get("/api/reports/revenue", headers=auth("superadmin")).json()
        assert report["days"] == 30
        assert report["date_to"] == date.today().isoformat()

    def test_unknown_report_is_rejected(self, client, activity, auth):
        response = client.get("/api/reports/nonsense", headers=auth("superadmin"))
        assert response.status_code == 422

    def test_csv_export_is_a_spreadsheet_not_json(self, client, activity, auth):
        response = client.get(
            "/api/reports/clinics",
            headers=auth("superadmin"),
            params={**WIDE, "format": "csv"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        assert ".csv" in response.headers["content-disposition"]

        lines = response.text.strip().splitlines()
        assert lines[0].startswith("Clinic,Appointments")
        assert len(lines) >= 3  # header + both clinics
        # Money must be plain so a spreadsheet can sum the column.
        assert "Rs." not in response.text
        assert "5500.00" in response.text

    def test_every_report_exports(self, client, activity, auth):
        for name in ("clinics", "appointments", "patient-sources", "revenue", "sessions"):
            response = client.get(
                f"/api/reports/{name}",
                headers=auth("superadmin"),
                params={**WIDE, "format": "csv"},
            )
            assert response.status_code == 200, name
            assert response.text.count("\n") >= 1, name


# --------------------------------------------------------------------------- #
class TestCollectedIsDatedByPayment:
    """`collected` must follow the money, not the invoice.

    `bills.amount_paid` is a running total with no date of its own. Dating it by
    `bill_date` credits a later payment to the month the bill was raised, which
    silently corrupts any month-on-month comparison — the exact report these
    tests sit next to.
    """

    def _bill_now_pay_later(self, client, headers, patient_id, clinics):
        bill = client.post(
            f"/api/patients/{patient_id}/bills",
            headers=headers,
            json={
                "items": [{"description": "Consultation", "unit_price": "1000.00"}],
                "clinic_id": clinics[0].id,
                "bill_date": "2026-06-10",
            },
        ).json()
        client.post(
            f"/api/bills/{bill['id']}/payments",
            headers=headers,
            json={
                "amount": "1000.00",
                "payment_method": "CASH",
                "payment_date": "2026-07-05",
            },
        )
        return bill

    def test_a_later_payment_counts_in_its_own_month(
        self, client, superadmin, activity, clinics, auth
    ):
        headers = auth("superadmin")
        self._bill_now_pay_later(client, headers, activity["patients"][0]["id"], clinics)

        june = run(client, headers, "revenue", **{"from": "2026-06-01", "to": "2026-06-30"})
        july = run(client, headers, "revenue", **{"from": "2026-07-01", "to": "2026-07-31"})

        # The bill belongs to June...
        assert dec(june["billed"]) == dec("1000.00")
        # ...but the money arrived in July, and that is where it must be counted.
        assert dec(june["collected"]) == dec("0.00")
        assert dec(july["collected"]) == dec("1000.00")
        assert dec(july["billed"]) == dec("0.00")

    def test_outstanding_belongs_to_the_bills_month(
        self, client, superadmin, activity, clinics, auth
    ):
        """June shows the debt even though June collected nothing."""
        headers = auth("superadmin")
        client.post(
            f"/api/patients/{activity['patients'][0]['id']}/bills",
            headers=headers,
            json={
                "items": [{"description": "Consultation", "unit_price": "1000.00"}],
                "clinic_id": clinics[0].id,
                "bill_date": "2026-06-10",
            },
        )
        june = run(client, headers, "revenue", **{"from": "2026-06-01", "to": "2026-06-30"})
        assert dec(june["outstanding"]) == dec("1000.00")


# --------------------------------------------------------------------------- #
class TestMonthComparison:
    def get(self, client, headers, **params):
        response = client.get(
            "/api/reports/month-comparison", headers=headers, params=params
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_every_period_is_cut_at_the_same_day(self, client, superadmin, activity, auth):
        """Comparing a part-month against whole months is how you conclude the
        business is collapsing on the 3rd."""
        report = self.get(client, auth("superadmin"), as_of="2026-08-24", months=4)

        assert report["day_of_month"] == 24
        assert [row["label"] for row in report["periods"]] == [
            "Aug 2026", "Jul 2026", "Jun 2026", "May 2026"
        ]
        for row in report["periods"]:
            assert row["period_end"].endswith("-24"), row["label"]
            assert row["days"] == 24, row["label"]

    def test_short_months_are_clamped_not_overflowed(self, client, superadmin, activity, auth):
        """31 January must compare against 28 February, not crash or roll over."""
        report = self.get(client, auth("superadmin"), as_of="2026-03-31", months=3)
        ends = {row["label"]: row["period_end"] for row in report["periods"]}
        assert ends["Mar 2026"] == "2026-03-31"
        assert ends["Feb 2026"] == "2026-02-28"   # 2026 is not a leap year
        assert ends["Jan 2026"] == "2026-01-31"

    def test_it_defaults_to_yesterday(self, client, superadmin, activity, auth):
        """Today is still in progress; including it flatters nothing and
        misleads the comparison."""
        report = self.get(client, auth("superadmin"))
        assert report["as_of"] == (date.today() - timedelta(days=1)).isoformat()

    def test_money_lands_in_the_right_period(self, client, superadmin, activity, clinics, auth):
        headers = auth("superadmin")
        for when, amount in (("2026-06-15", "500.00"), ("2026-07-15", "900.00")):
            bill = client.post(
                f"/api/patients/{activity['patients'][0]['id']}/bills",
                headers=headers,
                json={
                    "items": [{"description": "Consultation", "unit_price": amount}],
                    "clinic_id": clinics[0].id,
                    "bill_date": when,
                    "payment": {
                        "amount": amount, "payment_method": "CASH", "payment_date": when
                    },
                },
            )
            assert bill.status_code == 201

        report = self.get(client, headers, as_of="2026-07-20", months=3)
        by_label = {row["label"]: row for row in report["periods"]}
        assert dec(by_label["Jul 2026"]["collected"]) == dec("900.00")
        assert dec(by_label["Jun 2026"]["collected"]) == dec("500.00")
        # +80% on the previous month.
        assert report["change_vs_last_month_percent"] == 80.0

    def test_no_baseline_reports_none_rather_than_a_made_up_percentage(
        self, client, superadmin, activity, auth
    ):
        report = self.get(client, auth("superadmin"), as_of="2026-08-24", months=3)
        # Nothing was collected in Jun/Jul, so a percentage change is undefined.
        assert report["change_vs_last_month_percent"] is None

    def test_scoping_and_validation(self, client, superadmin, hsr_user, activity, clinics, auth):
        scoped = self.get(client, auth("hsr.reception"), as_of="2026-08-24")
        assert scoped["periods"][0]["label"] == "Aug 2026"

        denied = client.get(
            "/api/reports/month-comparison",
            headers=auth("hsr.reception"),
            params={"clinic_id": clinics[1].id},
        )
        assert denied.status_code == 403

        too_many = client.get(
            "/api/reports/month-comparison", headers=auth("superadmin"), params={"months": 24}
        )
        assert too_many.status_code == 422

    def test_csv_export(self, client, superadmin, activity, auth):
        response = client.get(
            "/api/reports/month-comparison",
            headers=auth("superadmin"),
            params={"as_of": "2026-08-24", "format": "csv"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert response.text.splitlines()[0].startswith("Month,Up to")


# --------------------------------------------------------------------------- #
class TestNewVsReturning:
    def test_appointments_split_by_first_ever_visit(
        self, client, superadmin, activity, clinics, auth
    ):
        headers = auth("superadmin")
        report = run(client, headers, "appointments")

        assert (
            report["new_patient_appointments"] + report["returning_patient_appointments"]
            == report["total"]
        )
        # The fixture books one appointment for a patient with no prior history.
        assert report["new_patients"] >= 1

    def test_a_second_appointment_makes_the_patient_returning(
        self, client, superadmin, activity, clinics, auth
    ):
        """The patient is new *once*; the split is by patient, not by booking."""
        headers = auth("superadmin")
        patient_id = activity["patients"][0]["id"]
        before = run(client, headers, "appointments")

        client.post(
            "/api/appointments",
            headers=headers,
            json={
                "patient_id": patient_id,
                "clinic_id": clinics[0].id,
                "appointment_date": next_weekday(MONDAY, 2).isoformat(),
                "start_time": "11:00",
            },
        )
        after = run(client, headers, "appointments")

        assert after["total"] == before["total"] + 1
        # Still the same patient, whose first visit is inside the window, so the
        # extra booking counts as new-patient activity but adds no new patient.
        assert after["new_patients"] == before["new_patients"]

    def test_weekday_and_hour_distribution_reconcile(self, client, superadmin, activity, auth):
        report = run(client, auth("superadmin"), "appointments")
        assert sum(row["appointments"] for row in report["by_weekday"]) == report["total"]
        assert sum(row["appointments"] for row in report["by_hour"]) == report["total"]
        assert len(report["by_weekday"]) == 7  # quiet days still listed


# --------------------------------------------------------------------------- #
class TestRetention:
    def test_a_stale_package_with_sessions_left_is_a_dropout(
        self, client, superadmin, clinics, auth, db
    ):
        """Money taken for treatment not delivered — the clearest leak."""
        from datetime import timedelta as td

        from app.models import TreatmentPackage

        headers = auth("superadmin")
        patient = client.post(
            "/api/patients",
            headers=headers,
            json={
                "full_name": "Lapsed Patient",
                "mobile": "9876500055",
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        created = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 10,
                "price_per_session": "500.00",
                "clinic_id": clinics[0].id,
                "skip_billing": True,
            },
        ).json()

        # Backdate the package so it reads as abandoned rather than brand new.
        package = db.get(TreatmentPackage, created["id"])
        package.start_date = date.today() - td(days=120)
        db.commit()

        report = run(client, headers, "retention")
        assert report["dropped_out"] == 1
        row = report["rows"][0]
        assert row["patient_name"] == "Lapsed Patient"
        assert row["sessions_remaining"] == 10
        assert dec(row["value_at_risk"]) == dec("5000.00")
        assert row["days_since"] >= 60
        assert dec(report["value_at_risk"]) == dec("5000.00")
        assert report["sessions_owed"] == 10

    def test_a_recent_package_is_not_a_dropout(self, client, superadmin, activity, auth):
        report = run(client, auth("superadmin"), "retention")
        assert report["dropped_out"] == 0
        assert dec(report["value_at_risk"]) == dec("0.00")

    def test_a_finished_package_is_never_a_dropout(
        self, client, superadmin, clinics, auth, db
    ):
        """Nothing is owed, so there is nothing at risk."""
        from datetime import timedelta as td

        from app.models import TreatmentPackage

        headers = auth("superadmin")
        patient = client.post(
            "/api/patients",
            headers=headers,
            json={
                "full_name": "Finished Course",
                "mobile": "9876500056",
                "primary_clinic_id": clinics[0].id,
            },
        ).json()
        created = client.post(
            f"/api/patients/{patient['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 2,
                "price_per_session": "500.00",
                "sessions_taken": 2,
                "clinic_id": clinics[0].id,
                "skip_billing": True,
            },
        ).json()
        package = db.get(TreatmentPackage, created["id"])
        package.start_date = date.today() - td(days=200)
        db.commit()

        assert run(client, headers, "retention")["dropped_out"] == 0

    def test_repeat_rate_counts_patients_with_more_than_one_package(
        self, client, superadmin, activity, clinics, auth
    ):
        headers = auth("superadmin")
        before = run(client, headers, "retention")["repeat_patients"]
        assert before["repeat_patients"] == 0

        client.post(
            f"/api/patients/{activity['patients'][0]['id']}/packages",
            headers=headers,
            json={
                "sessions_registered": 5,
                "price_per_session": "500.00",
                "clinic_id": clinics[0].id,
                "skip_billing": True,
            },
        )
        after = run(client, headers, "retention")["repeat_patients"]
        assert after["repeat_patients"] == 1
        assert after["repeat_rate"] > 0

    def test_dropouts_are_clinic_scoped(self, client, superadmin, btm_user, activity, auth):
        report = run(client, auth("btm.reception"), "retention")
        assert all(row["clinic_name"] == "BTM Layout" for row in report["rows"])

    def test_csv_export_gives_a_call_list(self, client, superadmin, activity, auth):
        response = client.get(
            "/api/reports/retention",
            headers=auth("superadmin"),
            params={**WIDE, "format": "csv"},
        )
        assert response.status_code == 200
        # A mobile number is what makes this actionable rather than merely informative.
        assert response.text.splitlines()[0].startswith("Patient,Patient ID,Mobile")
