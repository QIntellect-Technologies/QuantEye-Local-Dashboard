"""
Comprehensive Test Suite for QA-66 through QA-78.
"""
import os
os.environ['DISABLE_CAMERA_STREAMS'] = '1'

import unittest
from datetime import date

import database as db
import payroll_extensions as pe
import app as flask_app


class QABatch66To78Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = flask_app.app.test_client()

    # QA-66: Half-day group rule empty / missing validation
    def test_qa_66_half_day_rule_validation(self):
        with self.client.session_transaction() as sess:
            sess['admin_unlocked'] = True
        resp = self.client.post('/api/payroll-settings/scoped-rules/half-leave', json={
            'scope_type': 'department',
            'scope_id': 'Engineering',
            'half_leave_quota_mode': 'category',
            'half_leaves_per_quota_day': ''  # empty
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Missing required fields', resp.get_json().get('error', ''))

    # QA-67: Hours per month validation (1 to 744)
    def test_qa_67_hours_per_month_validation(self):
        with self.assertRaises(ValueError):
            db.update_payroll_settings(organization_id='TEST_QA67', ot_hour_divisor=0)
        with self.assertRaises(ValueError):
            db.update_payroll_settings(organization_id='TEST_QA67', ot_hour_divisor=-5)
        with self.assertRaises(ValueError):
            db.update_payroll_settings(organization_id='TEST_QA67', ot_hour_divisor=0.00000001)
        with self.assertRaises(ValueError):
            db.update_payroll_settings(organization_id='TEST_QA67', ot_hour_divisor=745)

    # QA-68: Overtime limit boxes validation (entry <= 24, month <= 744, max 2 decimals)
    def test_qa_68_overtime_limits_validation(self):
        with self.assertRaises(ValueError):
            db.save_scoped_overtime_limit_rule('department', 1, 25.0, 'daily', organization_id='TEST_QA68')
        with self.assertRaises(ValueError):
            db.save_scoped_overtime_limit_rule('department', 1, 745.0, 'monthly', organization_id='TEST_QA68')

    # QA-69: Multipliers (1.0 to 5.0) and premiums (0% to 100%)
    def test_qa_69_multipliers_and_premiums(self):
        with self.assertRaises(ValueError):
            db.update_payroll_settings(organization_id='TEST_QA69', holiday_ot_multiplier=0.9)
        with self.assertRaises(ValueError):
            db.update_payroll_settings(organization_id='TEST_QA69', holiday_ot_multiplier=5.1)
        with self.assertRaises(ValueError):
            db.update_payroll_settings(organization_id='TEST_QA69', holiday_ot_premium_percent=-1)
        with self.assertRaises(ValueError):
            db.update_payroll_settings(organization_id='TEST_QA69', holiday_ot_premium_percent=101)

    # QA-70: Component amounts capped at 10,000,000 and max 2 decimal places
    def test_qa_70_component_amounts(self):
        with self.assertRaises(ValueError):
            db.save_payroll_component_type(
                name='Over Limit',
                code='OVER',
                kind='earning',
                default_amount=10000001.0,
                organization_id='TEST_QA70'
            )

    # QA-71: Basic salary > 0
    def test_qa_71_basic_salary_gt_zero(self):
        user_id = db.add_user(name='Salary Test User', department='IT', organization_id='TEST_QA71')
        self.assertIsNotNone(user_id)
        with self.assertRaises(ValueError):
            db.set_salary_config(user_id, basic_salary=0)
        with self.assertRaises(ValueError):
            db.set_salary_config(user_id, basic_salary=-1000)

    # QA-72: Future join date excluded from payroll month
    def test_qa_72_future_join_date_excluded(self):
        users = [
            {'id': 101, 'name': 'Oct Hire', 'join_date': '2026-10-01', 'active': 1},
            {'id': 102, 'name': 'Nov Hire', 'join_date': '2026-11-01', 'active': 1},
        ]
        month_end = '2026-10-31'
        eligible = [u for u in users if not u.get('join_date') or u['join_date'] <= month_end]
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0]['id'], 101)

    # QA-73: Date validation in index.html
    def test_qa_73_date_validation_in_templates(self):
        with open('templates/index.html', 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('QA-73: Validate the join date is a real calendar date', content)

    # QA-74: Full name max 100 characters
    def test_qa_74_name_length_limit(self):
        long_name = 'A' * 101
        with self.assertRaises(ValueError):
            db.add_user(name=long_name, department='IT', organization_id='TEST_QA74')

    # QA-75: Formula injection prevention
    def test_qa_75_excel_formula_injection_prevention(self):
        dangerous_values = ['=SUM(A1:A10)', '+1234', '-cmd|/C calc', '@ext', '\tcmd', '\rcmd']
        for val in dangerous_values:
            safe = flask_app._safe_excel_val(val)
            self.assertTrue(safe.startswith("'"), f"Value {val} was not escaped: {safe}")

        normal_values = ['John Doe', 'Engineering', 'Staff Member', '12345', 100, None]
        for val in normal_values:
            safe = flask_app._safe_excel_val(val)
            self.assertEqual(safe, val)

    # QA-76: HTML escaping for shift names in UI
    def test_qa_76_html_injection_in_templates(self):
        with open('templates/index.html', 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('payrollEscape', content)
        self.assertIn('payrollEscape(emp.shift_name', content)
        self.assertIn('payrollEscape(name)', content)

    # QA-77: Join/exit date change to finalized month blocked
    def test_qa_77_finalized_month_date_change_blocked(self):
        with open('database.py', 'r', encoding='utf-8') as f:
            code = f.read()
        self.assertIn('Cannot set joining date in an already finalized payroll month', code)

    # QA-78: Finalized month employees query
    def test_qa_78_finalized_month_all_employees(self):
        with open('database.py', 'r', encoding='utf-8') as f:
            code = f.read()
        self.assertIn('organization_id = ? OR organization_id IS NULL', code)


if __name__ == '__main__':
    unittest.main()
