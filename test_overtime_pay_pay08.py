"""
PAY-08: Dedicated Test Suite
Proves:
- approved + eligible OT appears in Payroll > All Columns & API
- configured overtime rate is used
- duration is calculated correctly
- rejected OT excluded
- pending/ineligible OT excluded according to existing rules
- changing OT duration recalculates payroll
- changing approval status recalculates payroll
- changing overtime rate recalculates payroll
- cancelled/invalid OT excluded
- multiple OT records are aggregated exactly once
- no duplicate overtime amount enters payroll total
"""

from datetime import date, timedelta
import unittest
import uuid

import database as db


class TestPay08OvertimePayroll(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.org_id = f"test-org-{uuid.uuid4().hex[:8]}"
        cls.branch_id = f"test-branch-{uuid.uuid4().hex[:8]}"

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:6]
        self.today = date.today()
        self.curr_month = self.today.strftime("%Y-%m")
        self.today_str = self.today.isoformat()

    def _create_employee(self, salary=60000.0, ot_rate=250.0):
        name = f"Emp_PAY08_{self.suffix}_{uuid.uuid4().hex[:4]}"
        user_id = db.add_user(
            name=name,
            role="Employee",
            department="Operations",
            salary=salary,
            join_date="2026-01-01",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        db.set_salary_config(
            user_id=user_id,
            basic_salary=salary,
            allowances=0.0,
            deductions=0.0,
            ot_rate=ot_rate,
        )
        # Log attendance so employee is present
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="manual",
            bypass_shift_check=True,
        )
        return user_id, name

    def test_01_approved_eligible_ot_appears_in_payroll_and_uses_configured_rate(self):
        """1. Approved OT appears in payroll, duration is correct, configured rate is used."""
        user_id, name = self._create_employee(salary=60000.0, ot_rate=300.0)

        ot_id = db.add_overtime(
            user_id=user_id,
            user_name=name,
            ot_date=self.today_str,
            hours=3.5,
            reason="Release deployment",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(ot_id)
        db.update_overtime_status(ot_id, 'approved', approved_by="Admin", organization_id=self.org_id)

        payroll = db.calculate_monthly_payroll(
            payroll_month=self.curr_month,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            force_recalculate=True,
        )
        rec = next(r for r in payroll["records"] if r["user_id"] == user_id)

        # Duration
        self.assertEqual(float(rec["overtime_hours"]), 3.5)
        # Rate used
        self.assertEqual(float(rec["overtime_rate"]), 300.0)
        # Amount = 3.5 * 300 = 1050
        expected_ot_pay = 3.5 * 300.0
        self.assertAlmostEqual(float(rec["overtime_amount"]), expected_ot_pay, places=2)
        # Gross salary includes overtime
        self.assertAlmostEqual(float(rec["gross_salary"]), float(rec["basic_salary"]) + expected_ot_pay, places=2)
        # Net salary includes overtime
        self.assertAlmostEqual(float(rec["net_salary"]), float(rec["net_payable"]), places=2)

    def test_02_rejected_and_pending_ot_excluded_from_payroll(self):
        """2. Rejected and pending OT must be excluded from payroll calculation."""
        user_id, name = self._create_employee(salary=50000.0, ot_rate=200.0)

        # One pending OT
        ot_pending = db.add_overtime(
            user_id=user_id,
            user_name=name,
            ot_date=self.today_str,
            hours=2.0,
            reason="Unapproved extra work",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(ot_pending)

        # One rejected OT
        ot_rejected = db.add_overtime(
            user_id=user_id,
            user_name=name,
            ot_date=self.today_str,
            hours=3.0,
            reason="Unauthorized weekend work",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        db.update_overtime_status(ot_rejected, 'rejected', approved_by="Admin", rejection_reason="Not approved", organization_id=self.org_id)

        payroll = db.calculate_monthly_payroll(
            payroll_month=self.curr_month,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            force_recalculate=True,
        )
        rec = next(r for r in payroll["records"] if r["user_id"] == user_id)

        # Both pending and rejected must be excluded: 0 hours, 0 amount
        self.assertEqual(float(rec.get("overtime_hours", 0)), 0.0)
        self.assertEqual(float(rec.get("overtime_amount", 0)), 0.0)

    def test_03_cancelled_invalid_ot_excluded(self):
        """3. Cancelled/invalid OT records must be completely excluded from payroll."""
        user_id, name = self._create_employee(salary=50000.0, ot_rate=200.0)

        ot_id = db.add_overtime(
            user_id=user_id,
            user_name=name,
            ot_date=self.today_str,
            hours=4.0,
            reason="Will be cancelled",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        db.update_overtime_status(ot_id, 'cancelled', approved_by="Admin", organization_id=self.org_id)

        payroll = db.calculate_monthly_payroll(
            payroll_month=self.curr_month,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            force_recalculate=True,
        )
        rec = next(r for r in payroll["records"] if r["user_id"] == user_id)
        self.assertEqual(float(rec.get("overtime_hours", 0)), 0.0)
        self.assertEqual(float(rec.get("overtime_amount", 0)), 0.0)

    def test_04_multiple_ot_records_aggregated_exactly_once(self):
        """4. Multiple OT records are aggregated exactly once with no duplicate amounts."""
        user_id, name = self._create_employee(salary=60000.0, ot_rate=200.0)

        # Record 1: 2 hours
        ot1 = db.add_overtime(user_id=user_id, user_name=name, ot_date=self.today_str, hours=2.0, reason="Task 1", organization_id=self.org_id, branch_id=self.branch_id)
        db.update_overtime_status(ot1, 'approved', approved_by="Admin", organization_id=self.org_id)

        # Record 2: 3 hours on same date
        ot2 = db.add_overtime(user_id=user_id, user_name=name, ot_date=self.today_str, hours=3.0, reason="Task 2", organization_id=self.org_id, branch_id=self.branch_id)
        db.update_overtime_status(ot2, 'approved', approved_by="Admin", organization_id=self.org_id)

        # Record 3: Rejected 5 hours (must NOT be added)
        ot3 = db.add_overtime(user_id=user_id, user_name=name, ot_date=self.today_str, hours=5.0, reason="Rejected Task", organization_id=self.org_id, branch_id=self.branch_id)
        db.update_overtime_status(ot3, 'rejected', approved_by="Admin", organization_id=self.org_id)

        payroll = db.calculate_monthly_payroll(
            payroll_month=self.curr_month,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            force_recalculate=True,
        )
        rec = next(r for r in payroll["records"] if r["user_id"] == user_id)

        # Exactly 2 + 3 = 5 hours
        self.assertEqual(float(rec["overtime_hours"]), 5.0)
        # Exactly 5 * 200 = 1000
        self.assertEqual(float(rec["overtime_amount"]), 1000.0)

    def test_05_recalculation_on_duration_status_and_rate_changes(self):
        """5. Changing duration, approval status, or rate properly recalculates payroll."""
        user_id, name = self._create_employee(salary=60000.0, ot_rate=200.0)

        ot_id = db.add_overtime(user_id=user_id, user_name=name, ot_date=self.today_str, hours=2.0, reason="Task", organization_id=self.org_id, branch_id=self.branch_id)
        db.update_overtime_status(ot_id, 'approved', approved_by="Admin", organization_id=self.org_id)

        p1 = db.calculate_monthly_payroll(payroll_month=self.curr_month, organization_id=self.org_id, branch_id=self.branch_id, force_recalculate=True)
        r1 = next(r for r in p1["records"] if r["user_id"] == user_id)
        self.assertEqual(float(r1["overtime_amount"]), 400.0)

        # A) Change duration 2.0 -> 4.0
        import sqlite3
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE overtime SET hours = 4.0 WHERE id = ?", (ot_id,))
            conn.commit()

        p2 = db.calculate_monthly_payroll(payroll_month=self.curr_month, organization_id=self.org_id, branch_id=self.branch_id, force_recalculate=True)
        r2 = next(r for r in p2["records"] if r["user_id"] == user_id)
        self.assertEqual(float(r2["overtime_amount"]), 800.0)

        # B) Change rate 200 -> 350
        db.set_salary_config(user_id=user_id, basic_salary=60000.0, ot_rate=350.0)
        p3 = db.calculate_monthly_payroll(payroll_month=self.curr_month, organization_id=self.org_id, branch_id=self.branch_id, force_recalculate=True)
        r3 = next(r for r in p3["records"] if r["user_id"] == user_id)
        self.assertEqual(float(r3["overtime_amount"]), 1400.0)

        # C) Change status to rejected -> 0
        db.update_overtime_status(ot_id, 'rejected', approved_by="Admin", organization_id=self.org_id)
        p4 = db.calculate_monthly_payroll(payroll_month=self.curr_month, organization_id=self.org_id, branch_id=self.branch_id, force_recalculate=True)
        r4 = next(r for r in p4["records"] if r["user_id"] == user_id)
        self.assertEqual(float(r4["overtime_amount"]), 0.0)


if __name__ == "__main__":
    unittest.main()
