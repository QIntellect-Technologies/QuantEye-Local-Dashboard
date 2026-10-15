"""Two-organization payroll policy isolation tests."""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
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
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="quanteye-payroll-orgs-")
_TEST_DB_PATH = os.path.join(_TEST_DB_DIR.name, "attendance.db")
os.environ["QINTELLECT_DB_PATH"] = _TEST_DB_PATH

import config as db_config
import app as app_module
import database as db

db_config.DB_PATH = _TEST_DB_PATH
db.DB_PATH = _TEST_DB_PATH

ORG_A = "ORG-PAY-A"
ORG_B = "ORG-PAY-B"
BR_A = "BR-A"
BR_B = "BR-B"


class PayrollMultiOrgIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not db.init_db():
            raise RuntimeError("Could not initialize payroll isolation database")
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()
        cls._seed()

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
        db.update_payroll_settings(
            True, "category", 2, 40.0, 10.0,
            organization_id=ORG_A, branch_id=BR_A,
            ot_hour_divisor=200, proration_basis="calendar_days",
        )
        db.save_payroll_rule(1, None, 15, True, None, "percentage", 0, "absence", organization_id=ORG_A, branch_id=BR_A)
        db.add_leave_type("Org A Sabbatical", "sabbatical_a", 5, 1, "A only", organization_id=ORG_A, branch_id=BR_A)
        db.add_company_holiday("Org A Day", "2026-09-10", organization_id=ORG_A, branch_id=BR_A)
        db.save_special_date_override("2026-09-11", "holiday", "A override", organization_id=ORG_A, branch_id=BR_A)

        db.update_payroll_settings(
            False, "monthly_cap", 4, 100.0, 50.0,
            organization_id=ORG_B, branch_id=BR_B,
            ot_hour_divisor=160, proration_basis="30_day",
        )
        db.save_payroll_rule(1, None, 50, True, None, "percentage", 0, "absence", organization_id=ORG_B, branch_id=BR_B)
        db.add_leave_type("Org B Unpaid Extra", "unpaid_b", 0, 0, "B only", organization_id=ORG_B, branch_id=BR_B)
        db.add_company_holiday("Org B Day", "2026-09-10", organization_id=ORG_B, branch_id=BR_B)
        db.save_special_date_override("2026-09-12", "off_day", "B override", organization_id=ORG_B, branch_id=BR_B)

        for org, branch, name, email, salary in (
            (ORG_A, BR_A, "Amina A", "amina@a.test", 100000),
            (ORG_B, BR_B, "Bilal B", "bilal@b.test", 80000),
        ):
            user_id = db.add_user(
                name=name,
                email=email,
                department="Ops",
                salary=salary,
                join_date="2026-09-01",
                shift_id=1,
                organization_id=org,
                branch_id=branch,
            )
            db.set_salary_config(user_id, salary, 0, 0, 0)
            with sqlite3.connect(db.DB_PATH) as conn:
                conn.execute(
                    """UPDATE users SET organization_id = ?, branch_id = ? WHERE id = ?""",
                    (org, branch, user_id),
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
                    if day.weekday() <= 5 and day.day not in (2, 3):
                        conn.execute(
                            """INSERT INTO attendance (
                                   user_id, detected_name, confidence, source, timestamp, organization_id, branch_id
                               ) VALUES (?, ?, 0.99, 'manual', ?, ?, ?)""",
                            (user_id, name, f"{day.isoformat()} 09:00:00", org, branch),
                        )
                    day = date.fromordinal(day.toordinal() + 1)

    def _login(self, org, branch):
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
            session["organization_id"] = org
            session["branch_id"] = branch
            session["licensed_modules"] = ["attendance", "shifts", "leave", "overtime", "payroll"]

    def tearDown(self):
        if getattr(self, "config_patch", None):
            self.config_patch.stop()

    def test_settings_are_organization_specific(self):
        self._login(ORG_A, BR_A)
        settings_a = self.client.get("/api/settings/payroll").get_json()
        self.assertEqual(float(settings_a["ot_hour_divisor"]), 200)
        self.assertEqual(settings_a["proration_basis"], "calendar_days")
        self.assertEqual(float(settings_a["holiday_ot_premium_percent"]), 40)

        self._login(ORG_B, BR_B)
        settings_b = self.client.get("/api/settings/payroll").get_json()
        self.assertEqual(float(settings_b["ot_hour_divisor"]), 160)
        self.assertEqual(settings_b["proration_basis"], "30_day")
        self.assertEqual(float(settings_b["holiday_ot_premium_percent"]), 100)

    def test_changing_a_does_not_change_b(self):
        self._login(ORG_A, BR_A)
        saved = self.client.put(
            "/api/settings/payroll",
            json={
                "deduct_unpaid_leave": True,
                "half_leave_quota_mode": "category",
                "half_leaves_per_quota_day": 2,
                "holiday_ot_premium_percent": 12,
                "non_working_day_ot_premium_percent": 10,
                "ot_hour_divisor": 222,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self._login(ORG_B, BR_B)
        settings_b = self.client.get("/api/settings/payroll").get_json()
        self.assertEqual(float(settings_b["ot_hour_divisor"]), 160)
        self.assertEqual(float(settings_b["holiday_ot_premium_percent"]), 100)
        db.update_payroll_settings(
            True, "category", 2, 40.0, 10.0,
            organization_id=ORG_A, branch_id=BR_A,
            ot_hour_divisor=200, proration_basis="calendar_days",
        )

    def test_absence_rules_isolated(self):
        self._login(ORG_A, BR_A)
        rules_a = self.client.get("/api/payroll/rules").get_json()
        self.assertEqual({float(r["deduction_percent"]) for r in rules_a}, {15})
        self._login(ORG_B, BR_B)
        rules_b = self.client.get("/api/payroll/rules").get_json()
        self.assertEqual({float(r["deduction_percent"]) for r in rules_b}, {50})

    def test_leave_types_and_holidays_isolated(self):
        self._login(ORG_A, BR_A)
        types_a = {item["code"] for item in self.client.get("/api/leave-types").get_json()}
        holidays_a = {item["name"] for item in self.client.get("/api/holidays").get_json()}
        calendar_a = {item["note"] for item in self.client.get("/api/payroll/calendar").get_json()}
        self.assertIn("sabbatical_a", types_a)
        self.assertNotIn("unpaid_b", types_a)
        self.assertEqual(holidays_a, {"Org A Day"})
        self.assertEqual(calendar_a, {"A override"})

        self._login(ORG_B, BR_B)
        types_b = {item["code"] for item in self.client.get("/api/leave-types").get_json()}
        holidays_b = {item["name"] for item in self.client.get("/api/holidays").get_json()}
        calendar_b = {item["note"] for item in self.client.get("/api/payroll/calendar").get_json()}
        self.assertIn("unpaid_b", types_b)
        self.assertNotIn("sabbatical_a", types_b)
        self.assertEqual(holidays_b, {"Org B Day"})
        self.assertEqual(calendar_b, {"B override"})

    def test_auto_ot_rates_only_current_org(self):
        self._login(ORG_A, BR_A)
        result = self.client.post("/api/salary/auto-ot-rates", json={}).get_json()
        self.assertTrue(result["success"])
        self.assertEqual(result["updated_count"], 1)
        users = db.get_all_users(organization_id=ORG_B, branch_id=BR_B)
        b_user = next(user for user in users if user["email"] == "bilal@b.test")
        config = db.get_salary_config(b_user["id"])
        self.assertEqual(float(config.get("ot_rate") or 0), 0.0)

    def test_scope_options_only_current_org(self):
        self._login(ORG_A, BR_A)
        options = self.client.get("/api/payroll/scope-options").get_json()
        names = {item["name"] for item in options["employees"]}
        self.assertIn("Amina A", names)
        self.assertNotIn("Bilal B", names)

    def test_payroll_uses_own_absence_rule(self):
        payroll_a = db.calculate_monthly_payroll("2026-09", organization_id=ORG_A, branch_id=BR_A)
        payroll_b = db.calculate_monthly_payroll("2026-09", organization_id=ORG_B, branch_id=BR_B)
        amina = next(row for row in payroll_a["records"] if row["employee_name"] == "Amina A")
        bilal = next(row for row in payroll_b["records"] if row["employee_name"] == "Bilal B")
        self.assertEqual(amina["deduction_percent"], 15)
        self.assertEqual(bilal["deduction_percent"], 50)
        self.assertNotEqual(amina["net_salary"], bilal["net_salary"])

    def test_bonuses_are_organization_specific(self):
        amina = next(user for user in db.get_all_users(organization_id=ORG_A, branch_id=BR_A) if user["email"] == "amina@a.test")
        db.replace_payroll_bonuses(
            2026, 9,
            [{"employee_id": amina["id"], "amount": 2500, "note": "A only"}],
            organization_id=ORG_A, branch_id=BR_A,
        )
        self._login(ORG_A, BR_A)
        bonuses_a = self.client.get("/api/payroll/bonuses?month=2026-09").get_json()
        self.assertEqual(bonuses_a["count"], 1)
        self.assertEqual(float(bonuses_a["bonuses"][0]["amount"]), 2500)
        self._login(ORG_B, BR_B)
        bonuses_b = self.client.get("/api/payroll/bonuses?month=2026-09").get_json()
        self.assertEqual(bonuses_b["count"], 0)
        payroll_b = db.calculate_monthly_payroll("2026-09", organization_id=ORG_B, branch_id=BR_B)
        bilal = next(row for row in payroll_b["records"] if row["employee_name"] == "Bilal B")
        self.assertEqual(float(bilal.get("bonus_amount") or 0), 0)

    def test_named_pay_lines_are_organization_specific(self):
        saved = db.save_payroll_component_type(
            "HRA A", "hra_a", "earning", "allowance", "fixed", 1000, True, None, ORG_A, BR_A
        )
        self.assertIsNotNone(saved)
        self._login(ORG_A, BR_A)
        types_a = self.client.get("/api/payroll/component-types").get_json()["types"]
        self.assertTrue(any(item.get("code") == "hra_a" for item in types_a))
        self._login(ORG_B, BR_B)
        types_b = self.client.get("/api/payroll/component-types").get_json()["types"]
        self.assertFalse(any(item.get("code") == "hra_a" for item in types_b))

    def test_purge_removes_only_that_org_payroll(self):
        db.update_payroll_settings(True, "category", 2, 1, 1, organization_id="ORG-PURGE", branch_id="BR-PURGE")
        db.save_payroll_rule(1, None, 9, True, None, "percentage", 0, "absence", organization_id="ORG-PURGE", branch_id="BR-PURGE")
        self.assertTrue(db.purge_organization_data("ORG-PURGE"))
        remaining = db.get_payroll_settings(organization_id=ORG_B, branch_id=BR_B)
        self.assertEqual(float(remaining["ot_hour_divisor"]), 160)
        self.assertEqual(db.get_payroll_rules(organization_id="ORG-PURGE", branch_id="BR-PURGE"), [])


if __name__ == "__main__":
    unittest.main()
