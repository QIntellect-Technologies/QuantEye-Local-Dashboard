"""Named pay lines, exceptions, salary dates, arrears, documents, audit."""
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
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="quanteye-payroll-ext-")
_TEST_DB_PATH = os.path.join(_TEST_DB_DIR.name, "attendance.db")
os.environ["QINTELLECT_DB_PATH"] = _TEST_DB_PATH

import config as db_config
import app as app_module
import database as db

db_config.DB_PATH = _TEST_DB_PATH
db.DB_PATH = _TEST_DB_PATH

ORG = "ORG-PAY-EXT"
BRANCH = "BR-PAY-EXT"


class PayrollExtensionsE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not db.init_db():
            raise RuntimeError("Could not initialize extension payroll database")
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
            name="Catalog Worker",
            email="catalog@ext.test",
            department="Ops",
            salary=100000,
            join_date="2026-09-01",
            shift_id=1,
            organization_id=ORG,
            branch_id=BRANCH,
        )
        db.set_salary_config(user_id, 100000, 8000, 1000, 200)
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
                        (user_id, "Catalog Worker", f"{day.isoformat()} 09:00:00", ORG, BRANCH),
                    )
                day = date.fromordinal(day.toordinal() + 1)
            conn.execute(
                """INSERT INTO attendance (
                       user_id, detected_name, confidence, source, timestamp, organization_id, branch_id
                   ) VALUES (?, ?, 0.99, 'manual', ?, ?, ?)""",
                (user_id, "Catalog Worker", "2026-09-06 10:00:00", ORG, BRANCH),
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
        self._reset_extensions()

    def tearDown(self):
        self.config_patch.stop()
        self._reset_extensions()
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.executescript(
                """
                DROP TRIGGER IF EXISTS prevent_finalized_payroll_run_update;
                DROP TRIGGER IF EXISTS prevent_finalized_payroll_run_delete;
                DROP TRIGGER IF EXISTS prevent_finalized_payroll_record_insert;
                DROP TRIGGER IF EXISTS prevent_finalized_payroll_record_update;
                DROP TRIGGER IF EXISTS prevent_finalized_payroll_record_delete;
                DELETE FROM payroll_records;
                DELETE FROM payroll_runs;
                """
            )

    def _reset_extensions(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DELETE FROM payroll_employee_components")
            conn.execute("DELETE FROM payroll_component_types")
            conn.execute("DELETE FROM payroll_adjustments")
            conn.execute("DELETE FROM salary_revisions")
            conn.execute("DELETE FROM payroll_audit_log")
        db.set_salary_config(self.user_id, 100000, 8000, 1000, 200)
        db.update_payroll_settings(
            False, "category", 2, 0, 0,
            organization_id=ORG, branch_id=BRANCH,
            off_day_attendance_pay_active=False,
            feature_flags={
                "enable_named_components": 0,
                "enable_tax": 0,
                "enable_loans": 0,
                "enable_advances": 0,
                "enable_provident": 0,
                "enable_insurance": 0,
                "enable_bonuses": 1,
                "enable_individual_adjustments": 0,
                "enable_salary_revisions": 0,
            },
        )

    def _payroll(self):
        return db.calculate_monthly_payroll("2026-09", organization_id=ORG, branch_id=BRANCH)["records"][0]

    def test_flags_off_keeps_lump_allowance_even_if_catalog_exists(self):
        type_id = db.save_payroll_component_type(
            "HRA", "hra", "earning", "allowance", "fixed", 25000, True, None, ORG, BRANCH
        )
        db.save_employee_component(self.user_id, type_id, 25000, 0, "2026-09-01", None, ORG, BRANCH)
        row = self._payroll()
        self.assertEqual(row["allowances"], 8000.0)
        self.assertEqual(row["custom_deductions"], 1000.0)
        self.assertEqual(row["basic_salary"], 100000.0)
        self.assertEqual(row["adjustment_amount"], 0)

    def test_named_components_replace_lump_and_tax_is_optional(self):
        hra = db.save_payroll_component_type("HRA", "hra", "earning", "allowance", "fixed", 12000, True, None, ORG, BRANCH)
        tax = db.save_payroll_component_type("Income tax", "tax", "deduction", "tax", "percent_basic", 5, True, None, ORG, BRANCH)
        db.save_employee_component(self.user_id, hra, 12000, 0, "2026-09-01", None, ORG, BRANCH)
        db.save_employee_component(self.user_id, tax, 0, 5, "2026-09-01", None, ORG, BRANCH)
        db.update_payroll_settings(
            False, "category", 2, 0, 0, organization_id=ORG, branch_id=BRANCH,
            feature_flags={"enable_named_components": 1, "enable_tax": 0},
        )
        row = self._payroll()
        self.assertEqual(row["allowances"], 12000.0)
        self.assertEqual(row["custom_deductions"], 1000.0)
        db.update_payroll_settings(
            False, "category", 2, 0, 0, organization_id=ORG, branch_id=BRANCH,
            feature_flags={"enable_named_components": 1, "enable_tax": 1},
        )
        row = self._payroll()
        self.assertEqual(row["allowances"], 12000.0)
        self.assertEqual(row["custom_deductions"], 5000.0)
        self.assertTrue(any(item.get("category") == "tax" for item in row["pay_lines"]))

    def test_rest_day_exception_replaces_org_policy_for_that_date(self):
        db.update_payroll_settings(
            False, "category", 2, 0, 0, organization_id=ORG, branch_id=BRANCH,
            off_day_attendance_pay_active=True,
            off_day_attendance_pay_mode="multiplier",
            off_day_attendance_pay_multiplier=1.5,
            feature_flags={"enable_individual_adjustments": 1},
        )
        baseline = self._payroll()
        daily = round(100000 / baseline["scheduled_workdays"], 2)
        self.assertEqual(baseline["off_days_worked"], 1)
        db.save_payroll_adjustment(
            self.user_id, "2026-09", "rest_day", "multiplier", 0, 2.0,
            "2026-09-06", "Ali rest day", None, "Admin", ORG, BRANCH,
        )
        row = self._payroll()
        self.assertEqual(row["off_days_worked"], 0)
        self.assertEqual(row["off_day_extra_pay"], 0)
        self.assertEqual(row["adjustment_amount"], round(daily * 2.0, 2))
        self.assertEqual(row["gross_salary"], round(100000 + 8000 + row["adjustment_amount"], 2))

    def test_salary_revision_splits_the_month(self):
        db.update_payroll_settings(
            False, "category", 2, 0, 0, organization_id=ORG, branch_id=BRANCH,
            feature_flags={"enable_salary_revisions": 1},
        )
        self.assertTrue(db.set_salary_config(self.user_id, 100000, 0, 0, 200, "2026-09-01"))
        self.assertTrue(db.set_salary_config(self.user_id, 40000, 0, 0, 200, "2026-09-16"))
        row = self._payroll()
        self.assertEqual(row["basic_salary"], 70000.0)

    def test_arrears_and_finalize_audit_and_documents(self):
        db.update_payroll_settings(
            False, "category", 2, 0, 0, organization_id=ORG, branch_id=BRANCH,
            feature_flags={"enable_individual_adjustments": 1},
        )
        db.save_payroll_adjustment(
            self.user_id, "2026-09", "arrears", "fixed", 1500, 0,
            None, "August correction", "2026-08", "Admin", ORG, BRANCH,
        )
        preview = db.calculate_monthly_payroll("2026-09", organization_id=ORG, branch_id=BRANCH)
        row = preview["records"][0]
        self.assertEqual(row["arrears_amount"], 1500.0)
        self.assertEqual(row["adjustment_amount"], 1500.0)
        self.assertEqual(row["gross_salary"], round(100000 + 8000 + 1500, 2))
        saved = db.finalize_monthly_payroll(preview, ORG, BRANCH, "Tester")
        self.assertEqual(saved["status"], "Finalized")
        audit = db.list_payroll_audit("2026-09", ORG, BRANCH)
        self.assertTrue(any(item["action"] == "payroll_finalized" for item in audit))
        excel = self.client.get("/api/payroll/export.xlsx?month=2026-09")
        self.assertEqual(excel.status_code, 200)
        self.assertIn("spreadsheet", excel.mimetype)
        pdf = self.client.get(f"/api/payroll/payslip/{self.user_id}/pdf?month=2026-09")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
