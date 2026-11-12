"""Regression tests for QA-72, QA-73, QA-97, QA-98, QA-100."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

import database as db
import payroll_extensions as pe


class QaBatchFutureJoinAndPayslip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name
        self.org = "ORG_QA72100"
        self.branch = "BR_QA72100"
        self._db_patch = mock.patch.object(db, "DB_PATH", self.db_path)
        self._db_patch.start()
        db.init_db()

    def tearDown(self):
        self._db_patch.stop()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _shift(self) -> int:
        with db.sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT id FROM shifts ORDER BY id LIMIT 1").fetchone()
            if row:
                return int(row[0])
            cur = conn.execute(
                "INSERT INTO shifts (name, start_time, end_time, working_days) VALUES ('General','09:00','18:00','1,2,3,4,5')"
            )
            conn.commit()
            return int(cur.lastrowid)

    def _emp(self, name: str, basic: float, join_date: str, allowances: float = 0.0) -> int:
        uid = db.add_user(
            name=name,
            email=f"{name.replace(' ', '').lower()}@example.com",
            department="QA",
            salary=basic,
            join_date=join_date,
            shift_id=self._shift(),
            organization_id=self.org,
            branch_id=self.branch,
        )
        db.set_salary_config(uid, basic_salary=basic, allowances=allowances, deductions=0, ot_rate=0)
        return uid

    def test_qa_73_impossible_date_refused(self):
        with self.assertRaises(ValueError):
            db.validate_calendar_date("2026-11-31", "Joining date")
        self.assertEqual(db.validate_calendar_date("2026-11-30", "Joining date"), "2026-11-30")

    def test_qa_97_basic_salary_rules(self):
        with self.assertRaises(ValueError):
            db.parse_salary_amount("0.001", default=None, min_amount=db.BASIC_SALARY_MIN, require_two_decimals=True)
        with self.assertRaises(ValueError):
            db.parse_salary_amount("0", default=None, min_amount=db.BASIC_SALARY_MIN, require_two_decimals=True)
        with self.assertRaises(ValueError):
            db.parse_salary_amount("-1", default=None, min_amount=db.BASIC_SALARY_MIN, require_two_decimals=True)
        with self.assertRaises(ValueError):
            db.parse_salary_amount("1e-7", default=None, min_amount=db.BASIC_SALARY_MIN, require_two_decimals=True)
        with self.assertRaises(ValueError):
            db.parse_salary_amount("99999999", default=None, min_amount=db.BASIC_SALARY_MIN, require_two_decimals=True)
        with self.assertRaises(ValueError):
            db.parse_salary_amount("0.01", default=None, min_amount=db.BASIC_SALARY_MIN, require_two_decimals=True)
        self.assertEqual(
            db.parse_salary_amount("500000", default=None, min_amount=db.BASIC_SALARY_MIN, require_two_decimals=True),
            500000.0,
        )
        self.assertEqual(
            db.parse_salary_amount("20000.12", default=None, min_amount=db.BASIC_SALARY_MIN, require_two_decimals=True),
            20000.12,
        )
        with self.assertRaises(ValueError):
            db.parse_salary_amount("20000.123", default=None, min_amount=db.BASIC_SALARY_MIN, require_two_decimals=True)

    def test_qa_72_join_marker_uses_saved_join_date_not_today(self):
        today = date.today()
        month = today.strftime("%Y-%m")
        # Join later this month (or skip if already near month end).
        try:
            join = today.replace(day=min(28, today.day + 10))
        except ValueError:
            self.skipTest("Cannot build a future join date in-month")
        if join.strftime("%Y-%m") != month or join <= today:
            self.skipTest("Need a future join date still inside the current month")
        uid = self._emp("Join Marker Staff", 60000, join_date=join.isoformat())
        preview = db.calculate_monthly_payroll(month, organization_id=self.org, branch_id=self.branch)
        row = next(r for r in preview["records"] if int(r["employee_id"]) == int(uid))
        self.assertEqual(row.get("join_date"), join.isoformat())
        self.assertIn(join.strftime("%d %b"), row.get("join_marker") or "")
        self.assertNotIn(today.strftime("%d %b"), row.get("join_marker") or "")
        self.assertEqual(float(row.get("absent_days") or 0), 0.0)
        self.assertEqual(float(row.get("absence_deduction") or 0), 0.0)

    def test_qa_72_future_join_no_absence_and_marker(self):
        today = date.today()
        month = today.strftime("%Y-%m")
        join = (today + timedelta(days=10)).isoformat()
        if join[:7] != month:
            # Keep join inside the current month for this assertion.
            join = today.replace(day=min(28, today.day + 1)).isoformat()
            if join <= today.isoformat():
                self.skipTest("Need a future join date still inside the current month")
        uid = self._emp("Future Joiner", 60000, join_date=join, allowances=0)
        preview = db.calculate_monthly_payroll(month, organization_id=self.org, branch_id=self.branch)
        row = next(r for r in preview["records"] if int(r["employee_id"]) == int(uid))
        self.assertTrue(row.get("join_marker"))
        self.assertIn("Joins", row["join_marker"])
        self.assertEqual(float(row.get("absent_days") or 0), 0.0)
        self.assertEqual(float(row.get("absence_deduction") or 0), 0.0)
        self.assertEqual(float(row.get("attendance_shortfall") or 0), 0.0)
        self.assertTrue(row.get("not_yet_started") or row.get("is_prorated"))

    def test_qa_98_cannot_reject_approved_overtime(self):
        uid = self._emp("OT Staff", 50000, join_date="2026-01-01")
        with db.sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO overtime (user_id, user_name, ot_date, hours, reason, status, organization_id, branch_id)
                   VALUES (?, 'OT Staff', '2026-03-10', 2, 'QA-98', 'pending', ?, ?)""",
                (uid, self.org, self.branch),
            )
            conn.commit()
            ot_id = int(cur.lastrowid)
        self.assertTrue(db.update_overtime_status(ot_id, status="approved", approved_by="Admin"))
        self.assertFalse(db.update_overtime_status(ot_id, status="rejected", approved_by="Admin"))

    def test_qa_100_payslip_shows_package_allowance_with_named_lines(self):
        payslip = {
            "currency_code": "PKR",
            "decimal_places": 2,
            "gross_salary": 76100,
            "net_salary": 60350,
            "deductions": {"total": 15750, "absence": 0, "unpaid_leave": 0, "half_leave": 0, "attendance_shortfall": 0, "other": 15750},
            "employee": {
                "basic_salary": 35000,
                "package_allowances": 15000,
                "allowances": 41100,
                "named_earnings_total": 26100,
                "pay_lines": [
                    {"name": "House Rent", "kind": "earning", "amount": 14000},
                    {"name": "Medical", "kind": "earning", "amount": 3500},
                    {"name": "Utility", "kind": "earning", "amount": 3500},
                    {"name": "Conveyance", "kind": "earning", "amount": 5000},
                    {"name": "Assing to all", "kind": "earning", "amount": 100},
                ],
                "overtime_amount": 0,
                "bonus_amount": 0,
                "off_day_extra_pay": 0,
                "adjustment_amount": 0,
                "arrears_amount": 0,
            },
        }
        items = pe._payslip_invoice_items(payslip)
        earning_total = sum(
            float(i["amount"])
            for i in items
            if not str(i["description"]).lower().startswith(("missed", "unpaid", "half-day", "days not"))
            and float(i["amount"]) > 0
        )
        # Basic + package lump + named lines
        self.assertAlmostEqual(earning_total, 76100.0, places=2)
        names = [i["description"] for i in items]
        self.assertTrue(any("Monthly Allowance" in n for n in names))
        self.assertTrue(any("House Rent" in n for n in names))


if __name__ == "__main__":
    unittest.main()
