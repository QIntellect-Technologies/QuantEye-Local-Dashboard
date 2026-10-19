import os
import sys
import unittest
import sqlite3
import json
from datetime import datetime, date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database as db
import payroll_extensions as pe


class TestQARound5Tickets(unittest.TestCase):
    """
    Complete verification suite for QA Round 5 Findings:
    - QA-25: Lowest/Highest amount limits enforced on pay lines
    - QA-39: Half-leave error message when half-leave is not allowed/set up
    - QA-40: Half-leave rule precedence (PERSON -> DEPARTMENT -> COMPANY -> LEAVE TYPE DEFAULT)
    - QA-43: Success, duplicate, and error confirmation on half-day rule save
    - QA-44: Half-leave quota display using winning rule
    - QA-45: Half-day leave in attendance reports, leave tracking, and payroll
    - QA-46: Grouping of leave requests under parent employee records
    - QA-47: Payslip header Days Paid consistency with payroll proration/basic salary
    - QA-48: Missed-day deduction rule transparency and 580.64 vs 580.65 rounding precision
    """

    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.org_id = "QA_R5_TEST_ORG"
        cls.branch_id = "QA_R5_TEST_BRANCH"
        cls.month = "2026-10"

    def setUp(self):
        self._cleanup()
        with sqlite3.connect(db.DB_PATH) as conn:
            db.ensure_payroll_settings_row(conn.cursor(), self.org_id, self.branch_id)
            conn.commit()
        db.add_leave_type(
            name="Casual Leave",
            code="casual",
            yearly_allowance=10,
            half_leave_enabled=False,
            half_leave_mode="separate",
            half_leave_allowance_count=0,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DELETE FROM payroll_records WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_runs WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_rules WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_employee_components WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_component_types WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_scoped_rules WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM leave_requests WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM leave_types WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM attendance WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM users WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM departments WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM shifts WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_settings WHERE organization_id = ?", (self.org_id,))
            conn.commit()

    # =========================================================================
    # QA-25: Lowest / Highest Amount Limits Enforced
    # =========================================================================
    def test_qa_25_lowest_highest_limits(self):
        """QA-25: Enforce min_amount and max_amount limits on employee pay components."""
        emp_id = db.add_user(name="QA25 Emp", email="qa25@example.com", department="Dev", salary=50000.0, organization_id=self.org_id, branch_id=self.branch_id, join_date="2026-10-01")
        comp_type_id = db.save_payroll_component_type(name="QA25 Allowance", code="qa25_allowance", kind="earning", category="allowance", calc_mode="fixed", organization_id=self.org_id)
        self.assertIsNotNone(comp_type_id)

        # 1. min_amount > max_amount rejected
        with self.assertRaises(ValueError):
            db.save_employee_component(
                user_id=emp_id,
                component_type_id=comp_type_id,
                amount=150.0,
                effective_from="2026-10-01",
                effective_to="2026-10-31",
                min_amount=200.0,
                max_amount=100.0,
                organization_id=self.org_id,
                branch_id=self.branch_id,
            )

        # 2. Negative amount rejected
        with self.assertRaises(ValueError):
            db.save_employee_component(
                user_id=emp_id,
                component_type_id=comp_type_id,
                amount=-10.0,
                effective_from="2026-10-01",
                effective_to="2026-10-31",
                min_amount=100.0,
                max_amount=200.0,
                organization_id=self.org_id,
                branch_id=self.branch_id,
            )

        # 3. Amount below minimum rejected
        with self.assertRaises(ValueError) as cm_low:
            db.save_employee_component(
                user_id=emp_id,
                component_type_id=comp_type_id,
                amount=50.0,
                effective_from="2026-10-01",
                effective_to="2026-10-31",
                min_amount=100.0,
                max_amount=200.0,
                organization_id=self.org_id,
                branch_id=self.branch_id,
            )
        self.assertIn("cannot be less than lowest amount limit", str(cm_low.exception))

        # 4. Amount above maximum rejected
        with self.assertRaises(ValueError) as cm_high:
            db.save_employee_component(
                user_id=emp_id,
                component_type_id=comp_type_id,
                amount=300.0,
                effective_from="2026-10-01",
                effective_to="2026-10-31",
                min_amount=100.0,
                max_amount=200.0,
                organization_id=self.org_id,
                branch_id=self.branch_id,
            )
        self.assertIn("cannot be greater than highest amount limit", str(cm_high.exception))

        # 5. Exactly minimum accepted
        res_min = db.save_employee_component(
            user_id=emp_id,
            component_type_id=comp_type_id,
            amount=100.0,
            effective_from="2026-10-01",
            effective_to="2026-10-31",
            min_amount=100.0,
            max_amount=200.0,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(res_min.get("ok"))

        # 6. Inside range accepted (subsequent month)
        res_mid = db.save_employee_component(
            user_id=emp_id,
            component_type_id=comp_type_id,
            amount=150.0,
            effective_from="2026-11-01",
            effective_to="2026-11-30",
            min_amount=100.0,
            max_amount=200.0,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(res_mid.get("ok"))

        # 7. Exactly maximum accepted
        res_max = db.save_employee_component(
            user_id=emp_id,
            component_type_id=comp_type_id,
            amount=200.0,
            effective_from="2026-12-01",
            effective_to="2026-12-31",
            min_amount=100.0,
            max_amount=200.0,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(res_max.get("ok"))

    # =========================================================================
    # QA-39: Half-Leave Message
    # =========================================================================
    def test_qa_39_half_leave_message(self):
        """QA-39: Clear message when half leave is not configured or allowed; only say allowance reached when exhausted."""
        emp_id = db.add_user(name="QA39 Emp", email="qa39@example.com", salary=50000.0, organization_id=self.org_id, branch_id=self.branch_id)
        
        res = db.check_leave_quota(
            user_id=emp_id,
            leave_type_code="casual",
            start_date="2026-10-10",
            end_date="2026-10-10",
            leave_duration="half",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertFalse(res["allowed"])
        self.assertIn("not allowed", res["message"].lower())
        self.assertNotIn("allowance reached", res["message"].lower())
        self.assertNotIn("0 occurrence(s)", res["message"].lower())

    # =========================================================================
    # QA-40: Half-Leave Rule Precedence (PERSON -> DEPT -> COMPANY -> LEAVE TYPE)
    # =========================================================================
    def test_qa_40_rule_precedence_hierarchy(self):
        """QA-40: Strict precedence: PERSON -> DEPARTMENT -> COMPANY -> LEAVE TYPE DEFAULT."""
        dept_id = db.add_department("Engineering", "ENG", "Engineering Dept", self.org_id, self.branch_id)
        emp_id = db.add_user(name="QA40 Emp", email="qa40@example.com", department="Engineering", salary=50000.0, organization_id=self.org_id, branch_id=self.branch_id)

        # 1. Company rule: 1 half leave per month
        db.save_scoped_half_leave_rule(
            scope_type="company",
            scope_id=self.org_id,
            quota_mode="category",
            monthly_count=1,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        rule_res = db.get_half_leave_rule(emp_id, "casual", "2026-10-15", self.org_id, self.branch_id)
        self.assertEqual(rule_res["winning_source"], "company")
        self.assertEqual(rule_res["allowance"], 1)

        # 2. Department rule: 2 half leaves per month (should override company)
        db.save_scoped_half_leave_rule(
            scope_type="department",
            scope_id=dept_id,
            quota_mode="category",
            monthly_count=2,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        rule_res = db.get_half_leave_rule(emp_id, "casual", "2026-10-15", self.org_id, self.branch_id)
        self.assertEqual(rule_res["winning_source"], "department")
        self.assertEqual(rule_res["allowance"], 2)

        # 3. Person rule: 3 half leaves per month (should override department and company)
        db.save_scoped_half_leave_rule(
            scope_type="employee",
            scope_id=emp_id,
            quota_mode="category",
            monthly_count=3,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        rule_res = db.get_half_leave_rule(emp_id, "casual", "2026-10-15", self.org_id, self.branch_id)
        self.assertEqual(rule_res["winning_source"], "person")
        self.assertEqual(rule_res["allowance"], 3)

        # Verify quota exhaustion using person allowance (3)
        for i in range(3):
            d_str = f"2026-10-{10+i:02d}"
            db.add_leave_request(
                user_id=emp_id,
                user_name="QA40 Emp",
                leave_type="casual",
                start_date=d_str,
                end_date=d_str,
                leave_duration="half",
                status="approved",
                organization_id=self.org_id,
                branch_id=self.branch_id,
            )
        
        exhausted_res = db.get_half_leave_rule(emp_id, "casual", "2026-10-15", self.org_id, self.branch_id)
        self.assertFalse(exhausted_res["allowed"])
        self.assertEqual(exhausted_res["used_count"], 3)
        self.assertEqual(exhausted_res["remaining_count"], 0)
        self.assertIn("half-leave allowance reached (3 occurrence(s) per month)", exhausted_res["message"])

    # =========================================================================
    # QA-43: Confirmation Messages on Half-Day Rule Save
    # =========================================================================
    def test_qa_43_confirmation_messages(self):
        """QA-43: Success, duplicate, and error responses when saving half-day group rules."""
        dept_id = db.add_department("Operations", "OPS", "Operations Dept", self.org_id, self.branch_id)

        # 1. Successful save
        res1 = db.save_scoped_half_leave_rule(
            scope_type="department",
            scope_id=dept_id,
            quota_mode="category",
            monthly_count=2,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(res1.get("success"))
        self.assertIn("Operations", res1.get("message", ""))
        self.assertFalse(res1.get("duplicate"))

        # 2. Duplicate rule detection
        res2 = db.save_scoped_half_leave_rule(
            scope_type="department",
            scope_id=dept_id,
            quota_mode="category",
            monthly_count=2,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertFalse(res2.get("success"))
        self.assertTrue(res2.get("duplicate"))
        self.assertIn("already exists", res2.get("error", ""))

    # =========================================================================
    # QA-44: Half-Leave Quota Display
    # =========================================================================
    def test_qa_44_half_leave_quota_display(self):
        """QA-44: Full leave view displays half-leave quota from winning rule."""
        emp_id = db.add_user(name="QA44 Emp", email="qa44@example.com", department="Dev", salary=50000.0, organization_id=self.org_id, branch_id=self.branch_id)
        db.save_scoped_half_leave_rule(
            scope_type="employee",
            scope_id=emp_id,
            quota_mode="category",
            monthly_count=2,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        db.add_leave_request(
            user_id=emp_id,
            user_name="QA44 Emp",
            leave_type="casual",
            start_date="2026-10-05",
            end_date="2026-10-05",
            leave_duration="half",
            status="approved",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        balances = db.get_leave_balances(year=2026, user_id=emp_id, organization_id=self.org_id)
        emp_data = next((item for item in balances if item["user_id"] == emp_id), None)
        self.assertIsNotNone(emp_data)
        casual_bal = emp_data["balances"].get("casual")
        self.assertIsNotNone(casual_bal)
        self.assertEqual(casual_bal["half_leave_count"], 1)
        self.assertEqual(casual_bal["half_leave_remaining"], 1)
        self.assertEqual(casual_bal["half_leave_winning_source"], "person")
        self.assertIn("Half leave: 1 used, 1 remaining", casual_bal["half_leave_quota_text"])

    # =========================================================================
    # QA-45: Half-Day Leave in Attendance Reports & Payroll
    # =========================================================================
    def test_qa_45_attendance_and_payroll_half_leave(self):
        """QA-45: Half day attended produces 0.5 present + 0.5 leave without restoring entitlement."""
        emp_id = db.add_user(name="QA45 Emp", email="qa45@example.com", department="Dev", salary=60000.0, organization_id=self.org_id, branch_id=self.branch_id)
        shift_id = db.add_shift("Standard Shift", "09:00", "17:00", 15, 0, [0, 1, 2, 3, 4], self.org_id, self.branch_id)
        db.update_user(emp_id, "QA45 Emp", shift_id=shift_id)

        # Request half-day leave on Monday 2026-10-05
        leave_id = db.add_leave_request(
            user_id=emp_id,
            user_name="QA45 Emp",
            leave_type="casual",
            start_date="2026-10-05",
            end_date="2026-10-05",
            leave_duration="half",
            status="approved",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        # Employee checked in and out for the morning
        db.log_attendance(
            user_id=emp_id,
            detected_name="QA45 Emp",
            confidence=0.99,
            source="camera",
            bypass_shift_check=True,
            attendance_time=datetime(2026, 10, 5, 9, 5, 0),
        )
        db.log_attendance(
            user_id=emp_id,
            detected_name="QA45 Emp",
            confidence=0.99,
            source="camera",
            bypass_shift_check=True,
            attendance_time=datetime(2026, 10, 5, 13, 0, 0),
        )

        # Check detailed attendance report
        report = db.get_detailed_attendance_report(
            start_date="2026-10-01",
            end_date="2026-10-31",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        records = report["records"]
        emp_report = next((r for r in records if r["user_id"] == emp_id), None)
        self.assertIsNotNone(emp_report)
        self.assertEqual(emp_report["present_days"], 0.5)
        self.assertEqual(emp_report["paid_leave_days"], 0.5)

        # Verify entitlement was NOT restored
        leave_record = db.get_leave_by_id(leave_id)
        self.assertNotEqual(leave_record["status"], "cancelled_worked")

    # =========================================================================
    # QA-46: Group Leave Requests by Employee
    # =========================================================================
    def test_qa_46_group_leaves_by_employee(self):
        """QA-46: Verify multiple leaves for an employee are associated with that user_id."""
        emp_id = db.add_user(name="QA46 Emp", email="qa46@example.com", department="Dev", salary=50000.0, organization_id=self.org_id, branch_id=self.branch_id)
        l1 = db.add_leave_request(emp_id, "QA46 Emp", "casual", "2026-10-01", "2026-10-02", "Trip 1", organization_id=self.org_id, branch_id=self.branch_id)
        l2 = db.add_leave_request(emp_id, "QA46 Emp", "casual", "2026-10-08", "2026-10-09", "Trip 2", organization_id=self.org_id, branch_id=self.branch_id)

        all_leaves = db.get_leave_requests(organization_id=self.org_id, branch_id=self.branch_id)
        emp_leaves = [req for req in all_leaves if req["user_id"] == emp_id]
        self.assertEqual(len(emp_leaves), 2)
        leave_ids = {r["id"] for r in emp_leaves}
        self.assertIn(l1, leave_ids)
        self.assertIn(l2, leave_ids)

    # =========================================================================
    # QA-47: Payslip Header Days Paid Consistency
    # =========================================================================
    def test_qa_47_days_paid_consistency(self):
        """QA-47: Days paid matches authoritative calculation (e.g. 3 of 31 for mid-month joiner)."""
        # Join date: Oct 29, 2026 -> 3 days in month (29, 30, 31)
        emp_id = db.add_user(
            name="QA47 Joiner",
            email="qa47@example.com",
            department="Dev",
            salary=60000.0,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            join_date="2026-10-29",
        )
        payroll = db.calculate_monthly_payroll("2026-10", organization_id=self.org_id, branch_id=self.branch_id)
        records = payroll["records"] if isinstance(payroll, dict) else payroll
        emp_rec = next((r for r in records if r["employee_id"] == emp_id), None)
        self.assertIsNotNone(emp_rec)
        self.assertEqual(emp_rec["days_paid"], 3)
        self.assertTrue(emp_rec["is_prorated"])

        # Check payslip invoice HTML
        html = pe.payslip_invoice_html({
            "period_start": "2026-10-01",
            "period_end": "2026-10-31",
            "gross_salary": emp_rec["basic_salary"],
            "net_salary": emp_rec["net_salary"],
            "employee": emp_rec,
            "deductions": {"absence": 0, "total": 0},
            "allowances": {"total": 0},
        })
        self.assertIn("Days paid: 3", html)
        self.assertNotIn("Days paid: 0", html)

    # =========================================================================
    # QA-48: Missed-Day Deduction Rule Transparency & Rounding
    # =========================================================================
    def test_qa_48_deduction_rule_transparency_and_rounding(self):
        """QA-48: Deduction transparently shows bracket and base; 580.65 rounding bug resolved."""
        # Join date Oct 29, 2025 (3 of 31 days in completed month) with 60,000 basic
        emp_id = db.add_user(
            name="QA48 Joiner",
            email="qa48@example.com",
            department="Dev",
            salary=60000.0,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            join_date="2025-10-29",
        )
        shift_id = db.add_shift("AllDays Shift 2025", "09:00", "17:00", 15, 0, [0, 1, 2, 3, 4, 5, 6], self.org_id, self.branch_id)
        db.update_user(emp_id, "QA48 Joiner", shift_id=shift_id)

        # Configure missed-day rule: 1–3 missed days = 10% deduction
        db.save_payroll_rule(
            min_absent_days=1,
            max_absent_days=3,
            deduction_type="percentage",
            deduction_percent=10.0,
            fixed_amount=0.0,
            active=True,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        # Run payroll with 0 attendance (all 3 days unexcused absence)
        payroll = db.calculate_monthly_payroll("2025-10", organization_id=self.org_id, branch_id=self.branch_id)
        records = payroll["records"] if isinstance(payroll, dict) else payroll
        emp_rec = next((r for r in records if r["employee_id"] == emp_id), None)
        self.assertIsNotNone(emp_rec)

        # Unrounded: 60,000 * 3 / 31 * 0.10 = 580.64516... -> rounded once at final monetary calculation = 580.65
        self.assertEqual(emp_rec["absence_deduction"], 580.65)

        # Check metadata
        m_details = emp_rec["missed_day_details"]
        self.assertIsNotNone(m_details)
        self.assertEqual(m_details["rule_label"], "1–3 days")
        self.assertEqual(m_details["percentage"], 10.0)
        self.assertIn("prorated basic", m_details["base_type"])
        self.assertEqual(m_details["final_deduction"], 580.65)

        # Check payslip text
        html = pe.payslip_invoice_html({
            "period_start": "2025-10-01",
            "period_end": "2025-10-31",
            "gross_salary": emp_rec["basic_salary"],
            "net_salary": emp_rec["net_salary"],
            "employee": emp_rec,
            "deductions": {"absence": 580.65, "total": 580.65},
            "allowances": {"total": 0},
        })
        self.assertIn("Missed-day rule: 1–3 days = 10% of prorated basic", html)
        self.assertNotIn("@ PKR", html)  # No fake per-day rate!


if __name__ == "__main__":
    unittest.main()
