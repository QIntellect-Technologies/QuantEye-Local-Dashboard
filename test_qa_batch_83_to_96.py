"""Focused regression checks for QA-83 through QA-96."""
import io
import os
import sys
import tempfile
import unittest
import uuid

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
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="quanteye-qa83-96-")
_TEST_DB_PATH = os.path.join(_TEST_DB_DIR.name, "attendance.db")
os.environ["QINTELLECT_DB_PATH"] = _TEST_DB_PATH

import types
sys.modules.setdefault("numpy", types.ModuleType("numpy"))
sys.modules.setdefault("cv2", types.ModuleType("cv2"))
sys.modules.setdefault("insightface", types.ModuleType("insightface"))
_fp = types.ModuleType("face_processor")
_fp.process_frame = lambda *a, **k: None
sys.modules.setdefault("face_processor", _fp)

import config as db_config
import database as db

db_config.DB_PATH = _TEST_DB_PATH
db.DB_PATH = _TEST_DB_PATH


class QaBatch83To96(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not db.init_db():
            raise RuntimeError("Could not initialize QA 83-96 database")
        cls.org = f"org-{uuid.uuid4().hex[:8]}"
        cls.branch = f"br-{uuid.uuid4().hex[:8]}"

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = os.environ.get("QINTELLECT_DB_PATH", _TEST_DB_PATH)
        if _ORIGINAL_DB_PATH_ENV is None:
            os.environ.pop("QINTELLECT_DB_PATH", None)
        else:
            os.environ["QINTELLECT_DB_PATH"] = _ORIGINAL_DB_PATH_ENV
        _TEST_DB_DIR.cleanup()

    def _emp(self, name=None, basic=20000.0, shift_id=1, join_date="2026-01-01", exit_date=None):
        uid = db.add_user(
            name=name or f"Emp {uuid.uuid4().hex[:6]}",
            email=f"e{uuid.uuid4().hex[:6]}@example.com",
            phone="03001234567",
            department="QA",
            shift_id=shift_id,
            organization_id=self.org,
            branch_id=self.branch,
            salary=basic,
            join_date=join_date or "",
        )
        db.set_salary_config(uid, basic_salary=basic, allowances=0, deductions=0, ot_rate=0)
        if exit_date:
            db.deactivate_user(uid, exit_date=exit_date, organization_id=self.org)
        return uid

    def test_qa83_overtime_24h_refused(self):
        uid = self._emp()
        db.mark_user_present_today(
            uid, target_date="2026-10-10", check_in_time="09:00", check_out_time="17:00",
            organization_id=self.org,
        )
        with self.assertRaises(ValueError) as ctx:
            db.add_overtime(
                uid, ot_date="2026-10-10", hours=24, reason="full day",
                organization_id=self.org, branch_id=self.branch,
            )
        self.assertIn("less than 24", str(ctx.exception).lower())
        with self.assertRaises(ValueError) as ctx25:
            db.add_overtime(
                uid, ot_date="2026-10-10", hours=25, reason="over",
                organization_id=self.org, branch_id=self.branch,
            )
        self.assertIn("less than 24", str(ctx25.exception).lower())
        # Under-24 is accepted by the hours rule (may still hit other attendance rules).
        try:
            db.add_overtime(
                uid, ot_date="2026-10-10", hours=3, reason="under",
                organization_id=self.org, branch_id=self.branch,
            )
        except ValueError as exc:
            self.assertNotIn("less than 24", str(exc).lower())

    def test_qa84_late_checkout_creates_pending_ot(self):
        shift_id = db.add_shift(
            name=f"S9to5_{uuid.uuid4().hex[:4]}",
            start_time="09:00",
            end_time="17:00",
            grace_period_minutes=15,
            checkout_grace_period_minutes=0,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            organization_id=self.org,
            branch_id=self.branch,
        )
        uid = self._emp(shift_id=shift_id)
        result = db.mark_user_present_today(
            uid,
            target_date="2026-10-14",
            check_in_time="09:00",
            check_out_time="20:00",
            shift_id=shift_id,
            organization_id=self.org,
        )
        self.assertTrue(result)
        self.assertTrue(result.get("overtime_created"))
        self.assertAlmostEqual(float(result.get("overtime_hours") or 0), 3.0, places=1)
        rows = db.get_overtime(
            organization_id=self.org, branch_id=self.branch, user_id=uid, ot_date="2026-10-14",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["status"]).lower(), "pending")
        self.assertAlmostEqual(float(rows[0]["hours"]), 3.0, places=1)

    def test_qa85_86_87_88_bonus_validation(self):
        active = self._emp(name="Active Bonus")
        left = self._emp(name="Left Bonus", exit_date="2026-09-15")
        dup = db.replace_payroll_bonuses(
            2026, 10,
            [
                {"employee_id": active, "amount": 100, "_row_number": 2},
                {"employee_id": active, "amount": 200, "_row_number": 3},
            ],
            organization_id=self.org, branch_id=self.branch, commit=True,
        )
        self.assertFalse(dup["committed"])
        self.assertTrue(any("twice" in e["error"].lower() for e in dup["errors"]))

        bad = db.replace_payroll_bonuses(
            2026, 10,
            [{"employee_id": active, "amount": "abc", "_row_number": 2}],
            organization_id=self.org, branch_id=self.branch, commit=True,
        )
        self.assertFalse(bad["committed"])
        self.assertTrue(any("not numeric" in e["error"].lower() for e in bad["errors"]))

        neg = db.replace_payroll_bonuses(
            2026, 10,
            [{"employee_id": active, "amount": -5, "_row_number": 2}],
            organization_id=self.org, branch_id=self.branch, commit=True,
        )
        self.assertFalse(neg["committed"])

        comma = db.replace_payroll_bonuses(
            2026, 10,
            [{"employee_id": active, "amount": "100,50", "_row_number": 2}],
            organization_id=self.org, branch_id=self.branch, commit=True,
        )
        self.assertFalse(comma["committed"])

        left_res = db.replace_payroll_bonuses(
            2026, 10,
            [{"employee_id": left, "amount": 200, "_row_number": 2}],
            organization_id=self.org, branch_id=self.branch, commit=True,
        )
        self.assertFalse(left_res["committed"])
        self.assertTrue(any("left on" in e["error"].lower() for e in left_res["errors"]))

        ok = db.replace_payroll_bonuses(
            2026, 10,
            [{"employee_id": active, "amount": 250, "_row_number": 2}],
            organization_id=self.org, branch_id=self.branch, commit=True,
        )
        self.assertTrue(ok["committed"])
        self.assertEqual(ok["imported"], 1)

        try:
            import openpyxl
            import app as app_module
        except ImportError as exc:
            self.skipTest(f"workbook parser deps missing: {exc}")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Employee ID", "Wrong Name", "Email", "Amount", "Note"])
        ws.append([active, "x", "a@b.com", 10, ""])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        with self.assertRaises(ValueError) as ctx:
            app_module._bonus_rows_from_workbook(buf)
        self.assertIn("Unrecognized header", str(ctx.exception))

    def test_qa89_90_91_92_payroll_math(self):
        shift_id = db.add_shift(
            name=f"WD_{uuid.uuid4().hex[:4]}",
            start_time="09:00",
            end_time="17:00",
            working_days=[0, 1, 2, 3, 4],
            organization_id=self.org,
            branch_id=self.branch,
        )
        join_mid = self._emp(name="joined mid", basic=20000, shift_id=shift_id, join_date="2026-10-22")
        left_mid = self._emp(
            name="Left mid", basic=20000, shift_id=shift_id,
            join_date="2026-10-01", exit_date="2026-10-15",
        )
        heavy = self._emp(name="Heavy Deduct", basic=500000, shift_id=shift_id, join_date="2026-01-01")
        db.set_salary_config(heavy, basic_salary=500000, allowances=0, deductions=9991499.90, ot_rate=0)

        db.update_payroll_settings(
            organization_id=self.org,
            branch_id=self.branch,
            proration_basis="working_days",
            decimal_places=3,
            attendance_pay_mode="full_salary",
        )
        preview = db.calculate_monthly_payroll("2026-10", organization_id=self.org, branch_id=self.branch)
        by_id = {int(r["employee_id"]): r for r in preview["records"]}

        j = by_id[join_mid]
        l = by_id[left_mid]
        self.assertTrue(j.get("is_prorated"))
        self.assertIn("paid", (j.get("proration_explanation") or "").lower())
        self.assertAlmostEqual(
            float(j["basic_salary"]),
            20000 * float(j["proration_days_paid"]) / float(j["proration_method_days"]),
            places=2,
        )
        self.assertAlmostEqual(
            float(l["basic_salary"]),
            20000 * float(l["proration_days_paid"]) / float(l["proration_method_days"]),
            places=2,
        )

        h = by_id[heavy]
        self.assertGreaterEqual(float(h["net_salary"]), 0.0)
        self.assertGreater(float(h.get("shortfall") or 0), 0.0)
        self.assertGreaterEqual(float(preview["total_net_salary"]), 0.0)
        self.assertAlmostEqual(
            float(preview["total_shortfall"]),
            sum(float(r.get("shortfall") or 0) for r in preview["records"]),
            places=2,
        )
        self.assertAlmostEqual(
            float(preview["total_deduction"]),
            sum(float(r.get("deduction_amount") or 0) for r in preview["records"]),
            places=2,
        )

        db.update_payroll_settings(organization_id=self.org, branch_id=self.branch, decimal_places=0)
        preview0 = db.calculate_monthly_payroll("2026-10", organization_id=self.org, branch_id=self.branch)
        by0 = {int(r["employee_id"]): r for r in preview0["records"]}
        self.assertAlmostEqual(float(h["net_salary"]), float(by0[heavy]["net_salary"]), places=2)

    def test_qa96_auto_ot_uses_shift(self):
        shift_id = db.add_shift(
            name=f"S4x9_{uuid.uuid4().hex[:4]}",
            start_time="09:00",
            end_time="18:00",
            working_days=[0, 1, 2, 3],
            organization_id=self.org,
            branch_id=self.branch,
        )
        uid = self._emp(basic=56000, shift_id=shift_id)
        result = db.auto_calculate_ot_rates(
            organization_id=self.org, branch_id=self.branch, overwrite_manual=True,
        )
        self.assertTrue(result["success"])
        self.assertGreaterEqual(result["updated"], 1)
        import sqlite3
        with sqlite3.connect(db.DB_PATH) as conn:
            rate = conn.execute("SELECT ot_rate FROM salary_configs WHERE user_id=?", (uid,)).fetchone()[0]
        self.assertAlmostEqual(float(rate), 358.97, places=2)


if __name__ == "__main__":
    unittest.main()
