import unittest
import sqlite3
import json
from datetime import datetime, date
import database as db
import app as flask_app
from payroll_extensions import tenant_ids

class TestQA63AllowanceAssignment(unittest.TestCase):
    def setUp(self):
        self.client = flask_app.app.test_client()
        self.org_id = 'ORG_QA63_TEST'
        self.branch_id = 'DEFAULT_BRANCH'
        
        # Seed test organization and user
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DELETE FROM payroll_employee_components WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_component_types WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_settings WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM users WHERE organization_id = ?", (self.org_id,))
            
            # Create user
            conn.execute("""
                INSERT INTO users (id, name, email, department, position, salary, active, organization_id, branch_id, join_date)
                VALUES (99001, 'QA63 Employee', 'qa63@example.com', 'Engineering', 'Developer', 50000, 1, ?, ?, '2026-01-01')
            """, (self.org_id, self.branch_id))
            
            # Create payroll_settings with enable_named_components initially 0
            conn.execute("""
                INSERT INTO payroll_settings (organization_id, branch_id, enable_named_components, attendance_pay_mode, ot_hour_divisor, proration_basis)
                VALUES (?, ?, 0, 'full_salary', 208, 'calendar_days')
            """, (self.org_id, self.branch_id))
            conn.commit()

        # Set up authenticated session
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'Admin'
            sess['admin_unlocked'] = True
            sess['organization_id'] = self.org_id
            sess['branch_id'] = self.branch_id

    def tearDown(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DELETE FROM payroll_employee_components WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_component_types WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM payroll_settings WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM users WHERE organization_id = ?", (self.org_id,))
            conn.commit()

    def test_qa63_create_and_assign_allowance_org_scope(self):
        """Test creating an allowance and assigning to org scope (Everyone)."""
        # 1. Create a new allowance component type
        res = self.client.post('/api/payroll/component-types', json={
            'name': 'Medical Allowance',
            'code': 'med_allw',
            'kind': 'earning',
            'category': 'allowance',
            'calc_mode': 'fixed',
            'default_amount': 3000,
        })
        self.assertEqual(res.status_code, 200)
        type_id = res.get_json()['id']

        # 2. Assign to org (Everyone) via /api/payroll/employee-components
        res_assign = self.client.post('/api/payroll/employee-components', json={
            'scope': 'org',
            'scope_id': '',
            'component_type_id': type_id,
            'amount': 3000,
            'effective_from': '2026-10-01',
        })
        self.assertEqual(res_assign.status_code, 200)
        assign_data = res_assign.get_json()
        self.assertTrue(assign_data.get('success'))
        assignment_id = assign_data.get('id')
        self.assertIsNotNone(assignment_id)

        # Verify assignment_id corresponds to the actual row in payroll_employee_components
        with sqlite3.connect(db.DB_PATH) as conn:
            row = conn.execute("SELECT * FROM payroll_employee_components WHERE id = ?", (assignment_id,)).fetchone()
            self.assertIsNotNone(row, "Assignment ID must match payroll_employee_components id")

            # Verify enable_named_components is turned on in payroll_settings
            setting_row = conn.execute("SELECT enable_named_components FROM payroll_settings WHERE organization_id = ?", (self.org_id,)).fetchone()
            self.assertEqual(setting_row[0], 1, "enable_named_components must be enabled after component assignment")

        # 3. Retry the same assignment -> must return 400 with 'already assigned' message, NOT 500!
        res_retry = self.client.post('/api/payroll/employee-components', json={
            'scope': 'org',
            'scope_id': '',
            'component_type_id': type_id,
            'amount': 3000,
            'effective_from': '2026-10-01',
        })
        self.assertEqual(res_retry.status_code, 400)
        retry_data = res_retry.get_json()
        self.assertIn('already assigned', retry_data.get('error', '').lower())

        # 4. Verify QA-61: named_lines_count reflects this org-level allowance for employee 99001
        configs = db.get_all_salary_configs(organization_id=self.org_id)
        emp_cfg = next((c for c in configs if c['user_id'] == 99001), None)
        self.assertIsNotNone(emp_cfg)
        self.assertEqual(emp_cfg['named_lines_count'], 1)

        single_cfg = db.get_salary_config(99001)
        self.assertIsNotNone(single_cfg)
        self.assertEqual(single_cfg['named_lines_count'], 1)

        # 5. Verify list_employee_components includes this org-level allowance
        components = db.list_employee_components(99001, organization_id=self.org_id, branch_id=self.branch_id)
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]['name'], 'Medical Allowance')
        self.assertEqual(components[0]['calculated_amount'], 3000.0)

        # 6. Verify calculate_monthly_payroll shows the allowance in payroll
        payroll = db.calculate_monthly_payroll('2026-10', organization_id=self.org_id, branch_id=self.branch_id)
        records = payroll.get('records', [])
        emp_rec = next((r for r in records if r['employee_id'] == 99001), None)
        self.assertIsNotNone(emp_rec)
        self.assertEqual(emp_rec.get('allowances'), 3000.0, "Allowance must appear in payroll calculation")
        pay_lines = emp_rec.get('pay_lines', [])
        self.assertTrue(any(p.get('name') == 'Medical Allowance' and p.get('amount') == 3000.0 for p in pay_lines))

    def test_qa63_failed_save_leaves_nothing_behind(self):
        """Test that if an assignment fails validation, nothing is saved and retry works."""
        # Create an allowance
        res = self.client.post('/api/payroll/component-types', json={
            'name': 'Fuel Allowance',
            'code': 'fuel_allw',
            'kind': 'earning',
            'category': 'allowance',
            'calc_mode': 'fixed',
            'default_amount': 5000,
        })
        type_id = res.get_json()['id']

        # Attempt invalid assignment: start date before employee joining date (2026-01-01)
        res_bad = self.client.post('/api/payroll/employee-components', json={
            'scope': 'employee',
            'user_id': 99001,
            'component_type_id': type_id,
            'amount': 5000,
            'effective_from': '2025-10-01',
        })
        self.assertEqual(res_bad.status_code, 400)
        self.assertIn('cannot precede employee joining date', res_bad.get_json()['error'])

        # Verify nothing was saved in database
        with sqlite3.connect(db.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM payroll_employee_components WHERE organization_id = ?", (self.org_id,)).fetchone()[0]
            self.assertEqual(count, 0, "No records should remain after failed assignment")

        # Retry with valid date -> should succeed completely
        res_good = self.client.post('/api/payroll/employee-components', json={
            'scope': 'employee',
            'user_id': 99001,
            'component_type_id': type_id,
            'amount': 5000,
            'effective_from': '2026-10-01',
        })
        self.assertEqual(res_good.status_code, 200)
        self.assertTrue(res_good.get_json()['success'])

if __name__ == '__main__':
    unittest.main()
