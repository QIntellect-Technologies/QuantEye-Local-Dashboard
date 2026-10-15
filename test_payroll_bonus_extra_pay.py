"""Excel bonuses and configurable rest-day extra pay (not a hardcoded Sunday)."""
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
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
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="quanteye-payroll-bonus-")
_TEST_DB_PATH = os.path.join(_TEST_DB_DIR.name, "attendance.db")
os.environ["QINTELLECT_DB_PATH"] = _TEST_DB_PATH

import config as db_config
import app as app_module
import database as db
from payroll_policy_schema import off_day_attendance_extra, overtime_entry_pay

db_config.DB_PATH = _TEST_DB_PATH
db.DB_PATH = _TEST_DB_PATH

ORG = "ORG-BONUS"
BRANCH = "BR-BONUS"


class PayrollBonusExtraPayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not db.init_db():
            raise RuntimeError("Could not initialize bonus extra-pay database")
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()
        cls.user_id = cls._seed()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = os.environ.get("QINTELLECT_DB_PATH", _TEST_DB_PATH)
        if _ORIGINAL_DB_PATH_ENV is None:
            os.environ.pop("QINTELLECT_DB_PATH", None)
        else:
            os.environ["QINTELLECT_DB_PATH"] = _ORIGINAL_DB_PATH_ENV
        _TEST_DB_DIR.cleanup()

    @classmethod
    def _seed(cls):
        db.update_payroll_settings(False, "category", 2, 0, 0, organization_id=ORG, branch_id=BRANCH)
        db.save_payroll_rule(1, None, 5, True, None, "percentage", 0, "absence", organization_id=ORG, branch_id=BRANCH)
        user_id = db.add_user(
            name="Rest Day Worker",
            email="rest@bonus.test",
            department="Ops",
            salary=100000,
            join_date="2026-09-01",
            shift_id=1,
            organization_id=ORG,
            branch_id=BRANCH,
        )
        db.set_salary_config(user_id, 100000, 0, 0, 200)
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "UPDATE users SET organization_id = ?, branch_id = ? WHERE id = ?",
                (ORG, BRANCH, user_id),
            )
            conn.execute(
                """INSERT OR IGNORE INTO employee_shift_history (
                       user_id, shift_id, effective_from, shift_name, start_time, end_time,
                       grace_period_minutes, checkout_grace_period_minutes, working_days, history_source
                   ) VALUES (?, 1, '2020-01-01', 'Morning Shift', '09:00', '17:00', 15, 0, '[0,1,2,3,4,5]', 'test')""",
                (user_id,),
            )
            day = date(2026, 9, 1)
            while day <= date(2026, 9, 30):
                if day.weekday() <= 5:
                    conn.execute(
                        """INSERT INTO attendance (
                               user_id, detected_name, confidence, source, timestamp, organization_id, branch_id
                           ) VALUES (?, ?, 0.99, 'manual', ?, ?, ?)""",
                        (user_id, "Rest Day Worker", f"{day.isoformat()} 09:00:00", ORG, BRANCH),
                    )
                day = date.fromordinal(day.toordinal() + 1)
            conn.execute(
                """INSERT INTO attendance (
                       user_id, detected_name, confidence, source, timestamp, organization_id, branch_id
                   ) VALUES (?, ?, 0.99, 'manual', ?, ?, ?)""",
                (user_id, "Rest Day Worker", "2026-09-06 10:00:00", ORG, BRANCH),
            )
        return user_id

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
            session["organization_id"] = ORG
            session["branch_id"] = BRANCH
            session["licensed_modules"] = ["attendance", "shifts", "leave", "overtime", "payroll"]

    def tearDown(self):
        self.config_patch.stop()
        db.replace_payroll_bonuses(2026, 9, [], organization_id=ORG, branch_id=BRANCH)
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DELETE FROM overtime WHERE user_id = ?", (self.user_id,))
            conn.execute("DELETE FROM payroll_records")
            conn.execute("DELETE FROM payroll_runs")
        db.update_payroll_settings(
            False, "category", 2, 0, 0,
            organization_id=ORG, branch_id=BRANCH,
            holiday_ot_pay_mode="percent",
            non_working_day_ot_pay_mode="percent",
            off_day_attendance_pay_active=False,
            attendance_pay_mode="full_salary",
        )

    def test_multiplier_and_fixed_helpers_are_configurable(self):
        rate, amount, extra = overtime_entry_pay(
            200, 4, "non_working_day",
            {"non_working_day_ot_premium_active": 1, "non_working_day_ot_pay_mode": "multiplier", "non_working_day_ot_multiplier": 1.5},
        )
        self.assertEqual(rate, 300.0)
        self.assertEqual(amount, 1200.0)
        self.assertEqual(extra, 50.0)
        rate, amount, _ = overtime_entry_pay(
            200, 2, "holiday",
            {"holiday_ot_premium_active": 1, "holiday_ot_pay_mode": "fixed_hour", "holiday_ot_fixed_amount": 50},
        )
        self.assertEqual(rate, 250.0)
        self.assertEqual(amount, 500.0)
        extra_pay = off_day_attendance_extra(
            1000, 2,
            {"off_day_attendance_pay_active": 1, "off_day_attendance_pay_mode": "fixed_day", "off_day_attendance_pay_fixed_amount": 750},
        )
        self.assertEqual(extra_pay, 1500.0)

    def test_excel_bonus_is_added_to_gross(self):
        import openpyxl
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Employee ID", "Employee Name", "Email", "Amount", "Note"])
        sheet.append([self.user_id, "Rest Day Worker", "rest@bonus.test", 4000, "Spot bonus"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        buffer.name = "bonuses.xlsx"
        uploaded = self.client.post(
            "/api/payroll/bonuses/upload?month=2026-09",
            data={"file": (buffer, "bonuses.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.get_data(as_text=True))
        body = uploaded.get_json()
        self.assertEqual(body["imported"], 1)
        payroll = db.calculate_monthly_payroll("2026-09", organization_id=ORG, branch_id=BRANCH)
        row = payroll["records"][0]
        self.assertEqual(row["bonus_amount"], 4000.0)
        self.assertEqual(row["gross_salary"], round(100000 + 4000, 2))
        self.assertEqual(row["off_day_extra_pay"], 0)

    def test_rest_day_attendance_uses_shift_off_days_not_hardcoded_sunday(self):
        db.update_payroll_settings(
            False, "category", 2, 0, 0,
            organization_id=ORG, branch_id=BRANCH,
            off_day_attendance_pay_active=True,
            off_day_attendance_pay_mode="multiplier",
            off_day_attendance_pay_multiplier=1.5,
        )
        payroll = db.calculate_monthly_payroll("2026-09", organization_id=ORG, branch_id=BRANCH)
        row = payroll["records"][0]
        daily = round(100000 / row["scheduled_workdays"], 2)
        expected_extra = round(daily * 1.5, 2)
        self.assertEqual(row["off_days_worked"], 1)
        self.assertEqual(row["off_day_extra_pay"], expected_extra)
        self.assertEqual(row["gross_salary"], round(100000 + expected_extra, 2))

        db.update_payroll_settings(
            False, "category", 2, 0, 0,
            organization_id=ORG, branch_id=BRANCH,
            off_day_attendance_pay_active=True,
            off_day_attendance_pay_mode="fixed_day",
            off_day_attendance_pay_fixed_amount=800,
        )
        payroll = db.calculate_monthly_payroll("2026-09", organization_id=ORG, branch_id=BRANCH)
        row = payroll["records"][0]
        self.assertEqual(row["off_day_extra_pay"], 800.0)

    def test_rest_day_extra_is_skipped_when_approved_ot_exists(self):
        ot_id = db.add_overtime(self.user_id, "Rest Day Worker", "2026-09-06", 3.0, "Cover", ORG, BRANCH)
        db.update_overtime_status(ot_id, "approved", "Admin", ORG, BRANCH)
        db.update_payroll_settings(
            False, "category", 2, 0, 0,
            organization_id=ORG, branch_id=BRANCH,
            non_working_day_ot_pay_mode="multiplier",
            non_working_day_ot_multiplier=1.5,
            off_day_attendance_pay_active=True,
            off_day_attendance_pay_mode="multiplier",
            off_day_attendance_pay_multiplier=1.5,
        )
        payroll = db.calculate_monthly_payroll("2026-09", organization_id=ORG, branch_id=BRANCH)
        row = payroll["records"][0]
        self.assertEqual(row["off_day_extra_pay"], 0)
        self.assertEqual(row["overtime_hours"], 3.0)
        self.assertEqual(row["overtime_amount"], 900.0)

    def test_payable_days_mode_scales_salary_to_attendance(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "DELETE FROM attendance WHERE user_id = ? AND date(timestamp) > '2026-09-10'",
                (self.user_id,),
            )
        db.update_payroll_settings(
            False, "category", 2, 0, 0,
            organization_id=ORG, branch_id=BRANCH,
            attendance_pay_mode="payable_days",
        )
        payroll = db.calculate_monthly_payroll("2026-09", organization_id=ORG, branch_id=BRANCH)
        row = payroll["records"][0]
        self.assertGreater(row["month_scheduled_workdays"], row["payable_days"])
        expected_net = round(100000 * row["payable_days"] / row["month_scheduled_workdays"], 2)
        self.assertEqual(row["net_salary"], expected_net)
        self.assertGreater(row["attendance_shortfall"], 0)
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DELETE FROM attendance WHERE user_id = ?", (self.user_id,))
            day = date(2026, 9, 1)
            while day <= date(2026, 9, 30):
                if day.weekday() <= 5:
                    conn.execute(
                        """INSERT INTO attendance (
                               user_id, detected_name, confidence, source, timestamp, organization_id, branch_id
                           ) VALUES (?, ?, 0.99, 'manual', ?, ?, ?)""",
                        (self.user_id, "Rest Day Worker", f"{day.isoformat()} 09:00:00", ORG, BRANCH),
                    )
                day = date.fromordinal(day.toordinal() + 1)
            conn.execute(
                """INSERT INTO attendance (
                       user_id, detected_name, confidence, source, timestamp, organization_id, branch_id
                   ) VALUES (?, ?, 0.99, 'manual', ?, ?, ?)""",
                (self.user_id, "Rest Day Worker", "2026-09-06 10:00:00", ORG, BRANCH),
            )

    def test_payable_days_uses_full_month_working_days_as_divisor(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 15, 12, 0, 0)

        db.update_payroll_settings(
            False, "category", 2, 0, 0,
            organization_id=ORG, branch_id=BRANCH,
            attendance_pay_mode="payable_days",
        )
        with patch("database.datetime", FrozenDateTime):
            payroll = db.calculate_monthly_payroll("2026-09", organization_id=ORG, branch_id=BRANCH)
        row = payroll["records"][0]
        self.assertLess(row["payable_days"], row["month_scheduled_workdays"])
        expected_net = round(100000 * row["payable_days"] / row["month_scheduled_workdays"], 2)
        self.assertEqual(row["net_salary"], expected_net)
        self.assertLess(row["net_salary"], 100000)

    def test_finalize_blocked_until_month_ends(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 15, 12, 0, 0)

        with patch("database.datetime", FrozenDateTime):
            preview = db.calculate_monthly_payroll("2026-09", organization_id=ORG, branch_id=BRANCH)
            self.assertFalse(preview["month_complete"])
            with self.assertRaises(ValueError):
                db.finalize_monthly_payroll(preview, organization_id=ORG, branch_id=BRANCH)

    def test_bonus_template_downloads(self):
        response = self.client.get("/api/payroll/bonuses/template?month=2026-09")
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheet", response.mimetype)

    def test_payroll_settings_put_saves_rest_day_amount(self):
        response = self.client.put(
            "/api/settings/payroll",
            json={
                "deduct_unpaid_leave": False,
                "currency_code": "PKR",
                "decimal_places": 2,
                "proration_basis": "working_days",
                "attendance_pay_mode": "payable_days",
                "off_day_attendance_pay_active": True,
                "off_day_attendance_pay_mode": "fixed_day",
                "off_day_attendance_pay_fixed_amount": 1500,
                "enable_named_components": True,
                "enable_bonuses": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["settings"]["attendance_pay_mode"], "payable_days")
        self.assertEqual(body["settings"]["off_day_attendance_pay_mode"], "fixed_day")
        self.assertEqual(float(body["settings"]["off_day_attendance_pay_fixed_amount"]), 1500.0)
        self.assertTrue(body["settings"]["enable_named_components"])

    def test_group_overtime_extra_saves_and_overrides_company_rest_day_rate(self):
        db.save_overtime_premium_rule("non_working_day", 25.0, organization_id=ORG, branch_id=BRANCH)
        created = self.client.post(
            "/api/settings/payroll/scoped-rules/overtime-premium",
            json={
                "scope_type": "employee",
                "scope_id": self.user_id,
                "day_type": "non_working_day",
                "premium_percent": 80,
            },
        )
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        listed = self.client.get("/api/settings/payroll/scoped-rules/overtime-premium")
        self.assertEqual(listed.status_code, 200)
        rows = listed.get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scope_type"], "employee")
        self.assertEqual(float(rows[0]["overtime_premium_percent"]), 80.0)
        preview = db.get_overtime_rate_preview(self.user_id, "2026-09-06", ORG, BRANCH)
        self.assertIsNotNone(preview)
        self.assertEqual(preview["day_type"], "non_working_day")
        self.assertEqual(preview["premium_percent"], 80.0)
        deleted = self.client.delete(
            f"/api/settings/payroll/scoped-rules/overtime-premium/{rows[0]['id']}"
        )
        self.assertEqual(deleted.status_code, 200, deleted.get_data(as_text=True))
        after = db.get_overtime_rate_preview(self.user_id, "2026-09-06", ORG, BRANCH)
        self.assertEqual(after["premium_percent"], 25.0)


if __name__ == "__main__":
    unittest.main()
