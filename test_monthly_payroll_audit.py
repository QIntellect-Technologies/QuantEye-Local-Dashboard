"""Strict monthly payroll audit against the real Local payroll APIs.

Creates a licensed payroll tenant in the Flask session (the same contract a
client portal login uses after Main enables the Payroll module), seeds a
complete September 2026 month, independently recalculates every figure, and
compares it to GET /api/payroll.
"""
import calendar
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

_LOCAL_DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_LOCAL_DASHBOARD_DIR)
sys.path.insert(0, _LOCAL_DASHBOARD_DIR)

for _module_name, _module in list(sys.modules.items()):
    _module_file = getattr(_module, "__file__", None)
    if not _module_file:
        continue
    _module_path = os.path.normcase(os.path.abspath(_module_file))
    if not _module_path.startswith(os.path.normcase(_PROJECT_ROOT + os.sep)):
        continue
    if not _module_path.startswith(os.path.normcase(_LOCAL_DASHBOARD_DIR + os.sep)):
        del sys.modules[_module_name]

_ORIGINAL_DB_PATH_ENV = os.environ.get("QINTELLECT_DB_PATH")
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="quanteye-payroll-audit-")
_TEST_DB_PATH = os.path.join(_TEST_DB_DIR.name, "attendance.db")
os.environ["QINTELLECT_DB_PATH"] = _TEST_DB_PATH

import config as db_config
import app as app_module
import database as db

db_config.DB_PATH = _TEST_DB_PATH
db.DB_PATH = _TEST_DB_PATH

ORG_ID = "ORG-PAYROLL-AUDIT"
BRANCH_ID = "BR-PAY-01"
PAYROLL_MONTH = "2026-09"
MONTH_START = date(2026, 9, 1)
MONTH_END = date(2026, 9, 30)
WORKING_DAYS = {0, 1, 2, 3, 4, 5}


def _r2(value):
    return round(float(value), 2)


def _month_workdays(join_on=None, exit_on=None):
    join_on = join_on or MONTH_START
    exit_on = exit_on or MONTH_END
    days = []
    current = max(MONTH_START, join_on)
    last = min(MONTH_END, exit_on)
    while current <= last:
        if current.weekday() in WORKING_DAYS:
            days.append(current)
        current += timedelta(days=1)
    return days


def _proration(join_on, exit_on):
    eligible_start = max(MONTH_START, join_on)
    schedule_end = min(MONTH_END, exit_on)
    _, days_in_month = calendar.monthrange(2026, 9)
    active_days = max(0, (schedule_end - eligible_start).days + 1)
    if active_days >= days_in_month:
        return 1.0
    return active_days / days_in_month


def _expected(
    *,
    basic,
    allowances=0.0,
    custom=0.0,
    ot_rate=0.0,
    overtime=None,
    present=0,
    late=0,
    paid_leave=0,
    unpaid_leave=0,
    absences=0,
    half_leave=0,
    join_on=MONTH_START,
    exit_on=MONTH_END,
    deduct_unpaid=True,
    absence_rule=None,
    half_rule=None,
):
    overtime = overtime or []
    factor = _proration(join_on, exit_on)
    scheduled = len(_month_workdays(join_on, exit_on))
    basic_p = _r2(max(0.0, basic) * factor)
    allow_p = _r2(max(0.0, allowances) * factor)
    custom_p = _r2(max(0.0, custom) * factor)
    rate = _r2(ot_rate) if ot_rate > 0 else (_r2(basic_p / 208.0) if basic_p > 0 else 0.0)
    ot_amount = 0.0
    ot_hours = 0.0
    for hours, premium in overtime:
        ot_hours += hours
        ot_amount += _r2(hours * _r2(rate * (1 + premium / 100.0)))
    ot_amount = _r2(ot_amount)
    ot_hours = _r2(ot_hours)
    absence_ded = 0.0
    if absences and absence_rule:
        kind, value = absence_rule
        absence_ded = _r2(basic_p * value / 100.0 if kind == "percentage" else value)
    half_ded = 0.0
    if half_leave and half_rule:
        kind, value = half_rule
        half_ded = _r2(basic_p * value / 100.0 if kind == "percentage" else value)
    unpaid_ded = _r2(basic_p * unpaid_leave / scheduled) if deduct_unpaid and scheduled and unpaid_leave else 0.0
    deduction_amount = _r2(absence_ded + unpaid_ded + half_ded)
    gross = _r2(basic_p + allow_p + ot_amount)
    net = _r2(max(0.0, gross - deduction_amount - custom_p))
    payable = min(scheduled, present + late + paid_leave)
    return {
        "basic_salary": basic_p,
        "allowances": allow_p,
        "custom_deductions": custom_p,
        "overtime_hours": ot_hours,
        "overtime_rate": rate,
        "overtime_amount": ot_amount,
        "gross_salary": gross,
        "scheduled_workdays": scheduled,
        "present_days": present,
        "late_days": late,
        "paid_leave_days": paid_leave,
        "unpaid_leave_days": unpaid_leave,
        "absent_days": absences + unpaid_leave,
        "unexcused_absent_days": absences,
        "payable_days": payable,
        "absence_deduction": absence_ded,
        "unpaid_leave_deduction": unpaid_ded,
        "half_leave_deduction": half_ded,
        "deduction_amount": deduction_amount,
        "net_salary": net,
    }


class MonthlyPayrollAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not db.init_db():
            raise RuntimeError("Could not initialize payroll audit database")
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()
        cls.ids = cls._seed_company()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = os.environ.get("QINTELLECT_DB_PATH", _TEST_DB_PATH)
        if _ORIGINAL_DB_PATH_ENV is None:
            os.environ.pop("QINTELLECT_DB_PATH", None)
        else:
            os.environ["QINTELLECT_DB_PATH"] = _ORIGINAL_DB_PATH_ENV
        _TEST_DB_DIR.cleanup()

    def setUp(self):
        self.config_patch = patch.object(
            app_module,
            "load_integration_config",
            return_value={
                "linked": True,
                "organization_id": "ORG-HELLO-WORLD",
                "branch_id": "BR-HELLO",
                "licensed_modules": ["attendance"],
            },
        )
        self.config_patch.start()
        with self.client.session_transaction() as session:
            session.clear()
            session["client_user"] = True
            session["admin_unlocked"] = True
            session["organization_id"] = ORG_ID
            session["branch_id"] = BRANCH_ID
            session["licensed_modules"] = ["attendance", "shifts", "leave", "overtime", "payroll"]
            session["organization_name"] = "Northwind Payroll Co"

    def tearDown(self):
        self.config_patch.stop()

    @classmethod
    def _seed_company(cls):
        ids = {}
        db.update_payroll_settings(
            True, "category", 2, 50.0, 25.0,
            organization_id=ORG_ID, branch_id=BRANCH_ID,
        )
        db.save_overtime_premium_rule("holiday", 50.0, organization_id=ORG_ID, branch_id=BRANCH_ID)
        db.save_overtime_premium_rule("non_working_day", 25.0, organization_id=ORG_ID, branch_id=BRANCH_ID)
        db.save_payroll_rule(1, 2, 5, True, None, "percentage", 0, "absence", organization_id=ORG_ID, branch_id=BRANCH_ID)
        db.save_payroll_rule(3, 5, 10, True, None, "percentage", 0, "absence", organization_id=ORG_ID, branch_id=BRANCH_ID)
        db.save_payroll_rule(6, None, 20, True, None, "percentage", 0, "absence", organization_id=ORG_ID, branch_id=BRANCH_ID)
        db.save_payroll_rule(1, None, 2, True, None, "percentage", 0, "half_leave", organization_id=ORG_ID, branch_id=BRANCH_ID)

        people = [
            ("full_present", "Ayesha Khan", "Finance", 100000, 5000, 1500, 0, MONTH_START, None),
            ("absences", "Bilal Ahmed", "Ops", 80000, 0, 0, 0, MONTH_START, None),
            ("paid_leave", "Sana Malik", "HR", 90000, 0, 0, 0, MONTH_START, None),
            ("unpaid_leave", "Omar Sheikh", "Ops", 78000, 0, 0, 0, MONTH_START, None),
            ("overtime", "Hina Raza", "Support", 50000, 0, 0, 250, MONTH_START, None),
            ("mixed", "Usman Tariq", "Finance", 120000, 3000, 1000, 0, MONTH_START, None),
            ("mid_join", "Zara Iqbal", "Ops", 100000, 0, 0, 0, date(2026, 9, 16), None),
            ("user_salary", "Nadia Qureshi", "Admin", 70000, None, None, None, MONTH_START, None),
            ("mid_exit", "Farhan Ali", "Ops", 60000, 0, 0, 0, MONTH_START, date(2026, 9, 15)),
        ]
        for key, name, dept, basic, allow, custom, ot_rate, join_on, exit_on in people:
            user_id = db.add_user(
                name=name,
                email=f"{key}@northwind.test",
                department=dept,
                position="Staff",
                salary=basic if allow is None else 0,
                join_date=join_on.isoformat(),
                shift_id=1,
                organization_id=ORG_ID,
                branch_id=BRANCH_ID,
            )
            ids[key] = user_id
            if exit_on:
                with sqlite3.connect(db.DB_PATH) as conn:
                    conn.execute(
                        "UPDATE users SET exit_date = ?, active = 0 WHERE id = ?",
                        (exit_on.isoformat(), user_id),
                    )
            if allow is not None:
                db.set_salary_config(user_id, basic, allow, custom, ot_rate or 0)

        other = db.add_user(
            name="Other Org Employee",
            email="other@example.test",
            department="Ops",
            salary=999999,
            join_date="2026-09-01",
            organization_id="ORG-OTHER",
            branch_id="BR-OTHER",
        )
        ids["other_org"] = other
        db.set_salary_config(other, 999999, 0, 0, 0)

        cls._seed_attendance_and_exceptions(ids)
        return ids

    @classmethod
    def _insert_attendance(cls, user_id, name, day, checkin="09:00:00", checkout=None):
        org, branch = ORG_ID, BRANCH_ID
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                """INSERT INTO attendance (
                       user_id, detected_name, confidence, source, location, device_id,
                       timestamp, organization_id, branch_id
                   ) VALUES (?, ?, 0.99, 'manual', 'Office', 'cam-1', ?, ?, ?)""",
                (user_id, name, f"{day.isoformat()} {checkin}", org, branch),
            )
            if checkout:
                conn.execute(
                    """INSERT INTO attendance (
                           user_id, detected_name, confidence, source, location, device_id,
                           checkout_camera_id, checkout_location, timestamp, organization_id, branch_id
                       ) VALUES (?, ?, 0.99, 'manual', 'Office', 'cam-1', 'cam-1', 'Office', ?, ?, ?)""",
                    (user_id, name, f"{day.isoformat()} {checkout}", org, branch),
                )

    @classmethod
    def _seed_attendance_and_exceptions(cls, ids):
        full_days = _month_workdays()
        absence_days = {date(2026, 9, 2), date(2026, 9, 8), date(2026, 9, 14)}
        paid_days = {date(2026, 9, 3), date(2026, 9, 4)}
        unpaid_days = {date(2026, 9, 10), date(2026, 9, 11)}
        mixed_absent = {date(2026, 9, 1)}
        mixed_paid = {date(2026, 9, 7)}
        mixed_unpaid = {date(2026, 9, 8)}
        mixed_late = {date(2026, 9, 9), date(2026, 9, 15)}

        people = {
            "full_present": ("Ayesha Khan", MONTH_START, MONTH_END, set(), set(), set(), set()),
            "absences": ("Bilal Ahmed", MONTH_START, MONTH_END, absence_days, set(), set(), set()),
            "paid_leave": ("Sana Malik", MONTH_START, MONTH_END, set(), paid_days, set(), set()),
            "unpaid_leave": ("Omar Sheikh", MONTH_START, MONTH_END, set(), set(), unpaid_days, set()),
            "overtime": ("Hina Raza", MONTH_START, MONTH_END, set(), set(), set(), set()),
            "mixed": ("Usman Tariq", MONTH_START, MONTH_END, mixed_absent, mixed_paid, mixed_unpaid, mixed_late),
            "mid_join": ("Zara Iqbal", date(2026, 9, 16), MONTH_END, set(), set(), set(), set()),
            "user_salary": ("Nadia Qureshi", MONTH_START, MONTH_END, set(), set(), set(), set()),
            "mid_exit": ("Farhan Ali", MONTH_START, date(2026, 9, 15), set(), set(), set(), set()),
        }
        for key, (name, join_on, exit_on, absents, paid, unpaid, lates) in people.items():
            for day in _month_workdays(join_on, exit_on):
                if day in absents or day in paid or day in unpaid:
                    continue
                checkin = "09:30:00" if day in lates else "09:00:00"
                checkout = "18:00:00"
                cls._insert_attendance(ids[key], name, day, checkin, checkout)

        paid_id = db.add_leave_request(ids["paid_leave"], "Sana Malik", "annual", "2026-09-03", "2026-09-04", "Family")
        unpaid_id = db.add_leave_request(ids["unpaid_leave"], "Omar Sheikh", "unpaid", "2026-09-10", "2026-09-11", "LWP")
        mixed_paid_id = db.add_leave_request(ids["mixed"], "Usman Tariq", "sick", "2026-09-07", "2026-09-07", "Fever")
        mixed_unpaid_id = db.add_leave_request(ids["mixed"], "Usman Tariq", "unpaid", "2026-09-08", "2026-09-08", "LWP")
        for leave_id in (paid_id, unpaid_id, mixed_paid_id, mixed_unpaid_id):
            db.update_leave_status(leave_id, "approved", "Payroll Admin", ORG_ID)

        ot_regular = db.add_overtime(ids["overtime"], "Hina Raza", "2026-09-04", 8.0, "Month close", ORG_ID, BRANCH_ID)
        ot_sunday = db.add_overtime(ids["mixed"], "Usman Tariq", "2026-09-06", 4.0, "Sunday cover", ORG_ID, BRANCH_ID)
        ot_pending = db.add_overtime(ids["full_present"], "Ayesha Khan", "2026-09-04", 12.0, "Should not pay", ORG_ID, BRANCH_ID)
        db.update_overtime_status(ot_regular, "approved", "Payroll Admin", ORG_ID, BRANCH_ID)
        db.update_overtime_status(ot_sunday, "approved", "Payroll Admin", ORG_ID, BRANCH_ID)
        cls.pending_ot_id = ot_pending

    def _payroll(self, recalculate=True):
        query = f"/api/payroll?month={PAYROLL_MONTH}"
        if recalculate:
            query += "&recalculate=1"
        response = self.client.get(query)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        by_name = {row["employee_name"]: row for row in body["records"]}
        return body, by_name

    def test_payroll_module_is_required_for_the_payroll_page(self):
        with self.client.session_transaction() as session:
            session["licensed_modules"] = ["attendance", "shifts"]
        denied = self.client.get(f"/api/payroll?month={PAYROLL_MONTH}")
        self.assertEqual(denied.status_code, 403)

    def test_future_month_is_rejected(self):
        response = self.client.get("/api/payroll?month=2026-11")
        self.assertEqual(response.status_code, 400)

    def test_other_organization_staff_are_excluded(self):
        body, by_name = self._payroll()
        self.assertNotIn("Other Org Employee", by_name)
        self.assertEqual(body["payroll_month"], PAYROLL_MONTH)
        self.assertEqual(body["period_start"], "2026-09-01")
        self.assertEqual(body["period_end"], "2026-09-30")

    def test_full_month_attendance_uses_full_salary_plus_allowances_minus_custom_deductions(self):
        expected = _expected(basic=100000, allowances=5000, custom=1500, present=26)
        _, by_name = self._payroll()
        row = by_name["Ayesha Khan"]
        self.assertEqual(row["scheduled_workdays"], 26)
        self.assertEqual(row["present_days"], 26)
        self.assertEqual(row["absent_days"], 0)
        self.assertEqual(row["overtime_amount"], 0)
        self.assertEqual(row["basic_salary"], expected["basic_salary"])
        self.assertEqual(row["allowances"], expected["allowances"])
        self.assertEqual(row["custom_deductions"], expected["custom_deductions"])
        self.assertEqual(row["gross_salary"], expected["gross_salary"])
        self.assertEqual(row["net_salary"], expected["net_salary"])
        self.assertEqual(row["payable_days"], 26)

    def test_three_unexcused_absences_apply_the_10_percent_absence_rule(self):
        expected = _expected(basic=80000, present=23, absences=3, absence_rule=("percentage", 10))
        _, by_name = self._payroll()
        row = by_name["Bilal Ahmed"]
        self.assertEqual(row["present_days"], 23)
        self.assertEqual(row["absent_days"], 3)
        self.assertIn("3-5", row["matched_rule"])
        self.assertEqual(row["absence_deduction"], expected["absence_deduction"])
        self.assertEqual(row["net_salary"], expected["net_salary"])
        self.assertEqual(row["payable_days"], 23)

    def test_paid_leave_is_payable_and_does_not_reduce_salary(self):
        expected = _expected(basic=90000, present=24, paid_leave=2)
        _, by_name = self._payroll()
        row = by_name["Sana Malik"]
        self.assertEqual(row["paid_leave_days"], 2)
        self.assertEqual(row["unpaid_leave_days"], 0)
        self.assertEqual(row["absence_deduction"], 0)
        self.assertEqual(row["basic_salary"], expected["basic_salary"])
        self.assertEqual(row["net_salary"], expected["net_salary"])
        self.assertEqual(row["payable_days"], 26)

    def test_unpaid_leave_deducts_prorated_per_scheduled_workday(self):
        expected = _expected(basic=78000, present=24, unpaid_leave=2, deduct_unpaid=True)
        _, by_name = self._payroll()
        row = by_name["Omar Sheikh"]
        self.assertEqual(row["unpaid_leave_days"], 2)
        self.assertEqual(row["unpaid_leave_deduction"], expected["unpaid_leave_deduction"])
        self.assertEqual(row["net_salary"], expected["net_salary"])
        self.assertEqual(row["payable_days"], 24)

    def test_approved_overtime_uses_configured_hourly_rate(self):
        expected = _expected(basic=50000, present=26, ot_rate=250, overtime=[(8.0, 0.0)])
        _, by_name = self._payroll()
        row = by_name["Hina Raza"]
        self.assertEqual(row["overtime_hours"], 8.0)
        self.assertEqual(row["overtime_rate"], 250.0)
        self.assertEqual(row["overtime_amount"], 2000.0)
        self.assertEqual(row["gross_salary"], expected["gross_salary"])
        self.assertEqual(row["net_salary"], expected["net_salary"])

    def test_pending_overtime_is_excluded(self):
        _, by_name = self._payroll()
        self.assertEqual(by_name["Ayesha Khan"]["overtime_hours"], 0)

    def test_mixed_rules_late_days_unpaid_absence_and_sunday_ot_premium(self):
        expected = _expected(
            basic=120000,
            allowances=3000,
            custom=1000,
            present=21,
            late=2,
            paid_leave=1,
            unpaid_leave=1,
            absences=1,
            overtime=[(4.0, 25.0)],
            absence_rule=("percentage", 5),
        )
        _, by_name = self._payroll()
        row = by_name["Usman Tariq"]
        self.assertEqual(row["late_days"], 2)
        self.assertEqual(row["paid_leave_days"], 1)
        self.assertEqual(row["unpaid_leave_days"], 1)
        self.assertEqual(row["absent_days"], 2)
        self.assertEqual(row["overtime_hours"], 4.0)
        self.assertEqual(row["overtime_amount"], expected["overtime_amount"])
        self.assertEqual(row["absence_deduction"], expected["absence_deduction"])
        self.assertEqual(row["unpaid_leave_deduction"], expected["unpaid_leave_deduction"])
        self.assertEqual(row["custom_deductions"], 1000.0)
        self.assertEqual(row["gross_salary"], expected["gross_salary"])
        self.assertEqual(row["net_salary"], expected["net_salary"])
        self.assertEqual(row["payable_days"], expected["payable_days"])
        breakdown = row["overtime_breakdown"]
        if isinstance(breakdown, str):
            breakdown = json.loads(breakdown)
        self.assertEqual(breakdown[0]["premium_percent"], 25.0)
        self.assertEqual(breakdown[0]["day_type"], "non_working_day")

    def test_mid_month_joiner_is_calendar_prorated(self):
        expected = _expected(basic=100000, present=13, join_on=date(2026, 9, 16))
        _, by_name = self._payroll()
        row = by_name["Zara Iqbal"]
        self.assertEqual(row["basic_salary"], 50000.0)
        self.assertEqual(row["scheduled_workdays"], 13)
        self.assertEqual(row["present_days"], 13)
        self.assertEqual(row["net_salary"], expected["net_salary"])

    def test_mid_month_exit_is_calendar_prorated(self):
        expected = _expected(basic=60000, present=13, join_on=MONTH_START, exit_on=date(2026, 9, 15))
        _, by_name = self._payroll()
        row = by_name["Farhan Ali"]
        self.assertEqual(row["basic_salary"], 30000.0)
        self.assertEqual(row["scheduled_workdays"], 13)
        self.assertEqual(row["present_days"], 13)
        self.assertEqual(row["net_salary"], expected["net_salary"])

    def test_users_salary_is_used_when_salary_config_is_absent(self):
        expected = _expected(basic=70000, present=26)
        _, by_name = self._payroll()
        row = by_name["Nadia Qureshi"]
        self.assertEqual(row["basic_salary"], 70000.0)
        self.assertEqual(row["allowances"], 0)
        self.assertEqual(row["net_salary"], expected["net_salary"])

    def test_late_days_are_payable_and_have_no_separate_late_penalty(self):
        _, by_name = self._payroll()
        row = by_name["Usman Tariq"]
        self.assertGreater(row["late_days"], 0)
        self.assertEqual(row.get("late_deduction", 0), 0)
        self.assertEqual(row["payable_days"], row["present_days"] + row["late_days"] + row["paid_leave_days"])

    def test_totals_match_independent_sum_of_employee_nets(self):
        body, _ = self._payroll()
        self.assertEqual(body["total_basic_salary"], _r2(sum(r["basic_salary"] for r in body["records"])))
        self.assertEqual(body["total_allowances"], _r2(sum(r["allowances"] for r in body["records"])))
        self.assertEqual(body["total_overtime"], _r2(sum(r["overtime_amount"] for r in body["records"])))
        self.assertEqual(
            body["total_deduction"],
            _r2(sum(r["deduction_amount"] + r["custom_deductions"] for r in body["records"])),
        )
        self.assertEqual(body["total_net_salary"], _r2(sum(r["net_salary"] for r in body["records"])))

    def test_settings_premium_percent_is_marked_active_for_payroll(self):
        db.set_overtime_premium_rule_active("holiday", False)
        db.set_overtime_premium_rule_active("non_working_day", False)
        saved = self.client.put(
            "/api/settings/payroll",
            json={
                "deduct_unpaid_leave": True,
                "half_leave_quota_mode": "category",
                "half_leaves_per_quota_day": 2,
                "holiday_ot_premium_percent": 40,
                "non_working_day_ot_premium_percent": 10,
            },
        )
        self.assertEqual(saved.status_code, 200)
        body = saved.get_json()["settings"]
        self.assertEqual(body["holiday_ot_premium_percent"], 40)
        self.assertEqual(int(body.get("holiday_ot_premium_active") or 0), 1)
        self.assertEqual(int(body.get("non_working_day_ot_premium_active") or 0), 1)
        cleared = self.client.put(
            "/api/settings/payroll",
            json={
                "deduct_unpaid_leave": True,
                "half_leave_quota_mode": "category",
                "half_leaves_per_quota_day": 2,
                "holiday_ot_premium_percent": 0,
                "non_working_day_ot_premium_percent": 0,
            },
        )
        self.assertEqual(int(cleared.get_json()["settings"].get("holiday_ot_premium_active") or 0), 0)
        db.save_overtime_premium_rule("holiday", 50.0, organization_id=ORG_ID, branch_id=BRANCH_ID)
        db.save_overtime_premium_rule("non_working_day", 25.0, organization_id=ORG_ID, branch_id=BRANCH_ID)
        db.update_payroll_settings(True, "category", 2, 50.0, 25.0, organization_id=ORG_ID, branch_id=BRANCH_ID)

    def test_z_finalize_locks_the_monthly_run(self):
        preview, _ = self._payroll()
        locked = self.client.post("/api/payroll/finalize", json={"month": PAYROLL_MONTH})
        self.assertEqual(locked.status_code, 201, locked.get_data(as_text=True))
        body = locked.get_json()["payroll"]
        self.assertTrue(body.get("is_finalized") or body.get("status") == "Finalized")
        self.assertEqual(body["total_net_salary"], preview["total_net_salary"])
        again = self.client.post("/api/payroll/finalize", json={"month": PAYROLL_MONTH})
        self.assertEqual(again.status_code, 409)
        frozen = self.client.get(f"/api/payroll?month={PAYROLL_MONTH}&recalculate=1")
        self.assertEqual(frozen.status_code, 200)
        self.assertEqual(frozen.get_json()["total_net_salary"], preview["total_net_salary"])


if __name__ == "__main__":
    unittest.main()
