import unittest
import sqlite3
import os
import sys
from datetime import date

# Ensure Local-Dashboard is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database as db
import payroll_extensions as pe

def create_test_user(name, department, salary=2000, join_date="2026-01-01", org_id="QA_R4_TEST_ORG", branch_id="QA_R4_TEST_BRANCH"):
    with sqlite3.connect(db.DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users (name, department, active, salary, join_date, organization_id, branch_id)
               VALUES (?, ?, 1, ?, ?, ?, ?)""",
            (name, department, salary, join_date, org_id, branch_id)
        )
        conn.commit()
        return cur.lastrowid

class TestQARound4Tickets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        self.org_id = "QA_R4_TEST_ORG"
        self.branch_id = "QA_R4_TEST_BRANCH"
        
        # Clean up test tenant
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DELETE FROM leave_requests WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM leave_types WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM employee_leave_balances WHERE user_id IN (SELECT id FROM users WHERE organization_id = ?)", (self.org_id,))
            conn.execute("DELETE FROM payroll_scoped_rules WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM departments WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_records WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_runs WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_settings WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM users WHERE organization_id = ?", (self.org_id,))
            conn.commit()

        with sqlite3.connect(db.DB_PATH) as conn:
            db.ensure_payroll_settings_row(conn.cursor(), self.org_id, self.branch_id)
            conn.commit()
        db.update_payroll_settings(
            deduct_unpaid_leave=True,
            proration_basis='calendar_days',
            attendance_pay_mode='full_salary',
            half_leave_quota_mode='category',
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

    def tearDown(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DELETE FROM leave_requests WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM leave_types WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM employee_leave_balances WHERE user_id IN (SELECT id FROM users WHERE organization_id = ?)", (self.org_id,))
            conn.execute("DELETE FROM payroll_scoped_rules WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM departments WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_records WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_runs WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_settings WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM users WHERE organization_id = ?", (self.org_id,))
            conn.commit()

    def test_qa_39_half_leave_not_set_up_message(self):
        """QA-39: Half-leave error message is misleading when half leave is not set up
        Expected: A clear message such as 'Half leave is not set up for this department yet.'
        The 'allowance reached' wording should only appear when an allowance exists and has been used up.
        """
        # Create department with NO half-leave rule
        dept_id = db.add_department("Engineering", "ENG", "No half leave rule", self.org_id, self.branch_id)
        
        # Create employee in Engineering
        emp_id = create_test_user(
            name="QA39 Tester",
            department="Engineering",
            salary=3000,
            org_id=self.org_id,
            branch_id=self.branch_id,
        )

        # Create Casual Leave type where half_leave_allowance_count is 0 or unset
        lt_id = db.add_leave_type(
            name="Casual Leave",
            code="casual",
            yearly_allowance=10,
            half_leave_enabled=True,
            half_leave_mode="separate",
            half_leave_allowance_count=0,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        # Check quota for half day
        res = db.check_leave_quota(
            user_id=emp_id,
            leave_type_code="casual",
            start_date="2026-10-10",
            end_date="2026-10-10",
            leave_duration="half",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        self.assertFalse(res['allowed'])
        self.assertEqual(res['message'], "Half leave is not set up for this department yet.")
        self.assertNotIn("allowance reached", res['message'].lower())
        self.assertNotIn("(0 occurrence(s)", res['message'])

    def test_qa_40_half_leave_allowance_with_full_quota(self):
        """QA-40: Half-leave allowance shows 0 even when the person has full-day leave quota
        Expected: The half-leave allowance follows the configured rule (for example 2 per month).
        It does not read as 0 when the person has full-day quota.
        """
        # Create department with half-leave rule (2 per month)
        dept_id = db.add_department("Sales", "SALES", "Sales Dept", self.org_id, self.branch_id)
        
        # Save department rule: 2 half-leaves per month
        saved = db.save_scoped_half_leave_rule(
            scope_type="department",
            scope_id=dept_id,
            quota_mode="category",
            monthly_count=2,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(saved)

        # Create employee in Sales with full-day quota (10 days)
        emp_id = create_test_user(
            name="QA40 Tester",
            department="Sales",
            salary=2500,
            org_id=self.org_id,
            branch_id=self.branch_id,
        )

        # Casual leave with 10 days yearly quota
        lt_id = db.add_leave_type(
            name="Casual Leave",
            code="casual",
            yearly_allowance=10,
            half_leave_enabled=True,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        # Give employee full quota balance
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO employee_leave_balances (user_id, leave_type_id, year, allocated_days) VALUES (?, ?, ?, ?)",
                (emp_id, lt_id, 2026, 10.0)
            )
            conn.commit()

        # Check quota for half day
        res = db.check_leave_quota(
            user_id=emp_id,
            leave_type_code="casual",
            start_date="2026-10-12",
            end_date="2026-10-12",
            leave_duration="half",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        self.assertTrue(res['allowed'], f"Expected half leave to be allowed, got: {res}")
        self.assertEqual(res['remaining_days'], 2, "Expected remaining half-leave allowance to be 2, not 0")

        # Submit first half day
        lid1 = db.add_leave_request(
            user_id=emp_id,
            user_name="QA40 Tester",
            leave_type="casual",
            leave_duration="half",
            start_date="2026-10-12",
            end_date="2026-10-12",
            status="approved",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertIsNotNone(lid1)

        # Check quota for second half day: remaining should be 1
        res2 = db.check_leave_quota(
            user_id=emp_id,
            leave_type_code="casual",
            start_date="2026-10-15",
            end_date="2026-10-15",
            leave_duration="half",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(res2['allowed'])
        self.assertEqual(res2['remaining_days'], 1)

        # Submit second half day
        lid2 = db.add_leave_request(
            user_id=emp_id,
            user_name="QA40 Tester",
            leave_type="casual",
            leave_duration="half",
            start_date="2026-10-15",
            end_date="2026-10-15",
            status="approved",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertIsNotNone(lid2)

        # Check quota for third half day: allowance reached (2 per month)
        res3 = db.check_leave_quota(
            user_id=emp_id,
            leave_type_code="casual",
            start_date="2026-10-20",
            end_date="2026-10-20",
            leave_duration="half",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertFalse(res3['allowed'])
        self.assertIn("half-leave allowance reached (2 occurrence(s) per month)", res3['message'])

    def test_qa_41_payslip_proration_and_missed_day_explanation(self):
        """QA-41: Payslip does not explain a part-month salary
        Steps:
        5. Create an employee with basic 2,000 who joined mid-month (Join or leave mid-month is All days in the month).
        6. Calculate payroll and open the payslip.
        Expected: The payslip shows the full basic, the days paid and the method, for example:
        'Basic 2,000, paid 27 of 31 days = 1,741.94'
        Also for Missed-day cut: show number of days and daily rate.
        """
        # Create employee joining on 2026-10-05 (day 5 of 31-day October, so active days = 27)
        emp_id = create_test_user(
            name="QA41 MidMonth Joiner",
            department="Operations",
            salary=2000,
            join_date="2026-10-05",
            org_id=self.org_id,
            branch_id=self.branch_id,
        )

        # Calculate payroll for October 2026
        payroll = db.calculate_monthly_payroll(
            "2026-10",
            organization_id=self.org_id,
            branch_id=self.branch_id,
            force_recalculate=True,
        )

        record = next((r for r in payroll['records'] if r['employee_id'] == emp_id), None)
        self.assertIsNotNone(record)

        # Prorated basic salary check: 2000 * 27 / 31 = 1741.94
        self.assertEqual(record['basic_salary'], 1741.94)
        self.assertEqual(record['is_prorated'], 1)
        self.assertEqual(record['proration_days_paid'], 27)
        self.assertEqual(record['proration_method_days'], 31)
        expected_explanation = "Basic 2,000, paid 27 of 31 days = 1,741.94"
        self.assertEqual(record['proration_explanation'], expected_explanation)

        # Retrieve payslip
        payslip = db.get_employee_payslip("2026-10", emp_id, organization_id=self.org_id, branch_id=self.branch_id)
        self.assertIsNotNone(payslip)

        # Check invoice HTML rendering
        html = pe.payslip_invoice_html(payslip)
        self.assertIn("Basic 2,000, paid 27 of 31 days = 1,741.94", html)

        # Check PDF breakdown rows
        earnings, cuts = pe._pdf_rows_from_payslip(payslip)
        basic_row = next((r for r in earnings if "Basic" in r[0]), None)
        self.assertIsNotNone(basic_row)
        self.assertIn("Basic 2,000, paid 27 of 31 days = 1,741.94", basic_row[0])

        # Test Missed-day cut breakdown formatting
        payslip_with_absence = dict(payslip)
        payslip_with_absence['deductions'] = dict(payslip['deductions'])
        payslip_with_absence['deductions']['absence'] = 129.03
        payslip_with_absence['employee'] = dict(payslip['employee'])
        payslip_with_absence['employee']['absent_days'] = 2

        html_absence = pe.payslip_invoice_html(payslip_with_absence)
        self.assertIn("Missed-day cut (2 days @", html_absence)
        self.assertIn("/day)", html_absence)

        _, absence_cuts = pe._pdf_rows_from_payslip(payslip_with_absence)
        missed_day_row = next((r for r in absence_cuts if "Missed-day cut" in r[0]), None)
        self.assertIsNotNone(missed_day_row)
        self.assertIn("2 days @", missed_day_row[0])
        self.assertIn("/day", missed_day_row[0])

if __name__ == '__main__':
    unittest.main()
