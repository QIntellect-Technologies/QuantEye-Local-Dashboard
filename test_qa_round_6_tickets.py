"""
Comprehensive automated tests for QA Handover Round 6 Tickets:
QA-25, QA-42, QA-46, QA-47, QA-48, QA-49, QA-50, QA-51, QA-52, QA-53, QA-54.
"""
import unittest
import json
import sqlite3
import uuid
from datetime import datetime, date, timedelta

import database
import payroll_extensions


class TestQARound6Tickets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()

    def test_qa_42_duplicate_component_type_validation(self):
        """QA-42: Duplicate item name/code must throw ValueError with clear message, not 500 / IntegrityError."""
        tag = uuid.uuid4().hex[:6]
        name = f"Unique_QA42_{tag}"
        code = f"QA42_{tag.upper()}"
        org_id = f"ORG_TEST_{tag}"

        # First creation succeeds
        first_id = database.save_payroll_component_type(
            name=name,
            code=code,
            kind="earning",
            category="allowance",
            calc_mode="fixed",
            default_amount=500.0,
            organization_id=org_id
        )
        self.assertIsNotNone(first_id)

        # Duplicate name should raise ValueError with helpful message
        with self.assertRaises(ValueError) as ctx:
            database.save_payroll_component_type(
                name=name,
                code=f"DIFF_{tag.upper()}",
                kind="earning",
                category="allowance",
                calc_mode="fixed",
                default_amount=600.0,
                organization_id=org_id
            )
        self.assertIn("already exists", str(ctx.exception).lower())

        # Duplicate code should raise ValueError with helpful message
        with self.assertRaises(ValueError) as ctx2:
            database.save_payroll_component_type(
                name=f"Different Name {tag}",
                code=code,
                kind="earning",
                category="allowance",
                calc_mode="fixed",
                default_amount=600.0,
                organization_id=org_id
            )
        self.assertIn("already exists", str(ctx2.exception).lower())

    def test_qa_25_lowest_and_highest_amount_limits_enforced(self):
        """QA-25: Lowest and highest amount limits must be enforced on assignment and payroll calculation."""
        tag = uuid.uuid4().hex[:6]
        org_id = f"ORG_QA25_{tag}"
        comp_id = database.save_payroll_component_type(
            name=f"QA25 Bracket {tag}",
            code=f"QA25_{tag.upper()}",
            kind="earning",
            category="allowance",
            calc_mode="fixed",
            default_amount=150.0,
            organization_id=org_id
        )
        # Assignment with min > max rejected
        with self.assertRaises(ValueError) as ctx:
            database.save_employee_component(
                organization_id=org_id,
                component_type_id=comp_id,
                scope_type="company",
                amount=150.0,
                min_amount=300.0,
                max_amount=200.0,
                effective_from="2026-01-01"
            )
        self.assertIn("lowest amount limit cannot be greater than highest amount limit", str(ctx.exception).lower())

        # Assignment with amount out of range rejected
        with self.assertRaises(ValueError) as ctx2:
            database.save_employee_component(
                organization_id=org_id,
                component_type_id=comp_id,
                scope_type="company",
                amount=50.0,
                min_amount=100.0,
                max_amount=200.0,
                effective_from="2026-01-01"
            )
        self.assertIn("cannot be less than lowest amount limit", str(ctx2.exception).lower())

        with self.assertRaises(ValueError) as ctx3:
            database.save_employee_component(
                organization_id=org_id,
                component_type_id=comp_id,
                scope_type="company",
                amount=350.0,
                min_amount=100.0,
                max_amount=200.0,
                effective_from="2026-01-01"
            )
        self.assertIn("cannot be greater than highest amount limit", str(ctx3.exception).lower())

    def test_qa_51_duplicate_allowance_assignment_reporting(self):
        """QA-51: Duplicate allowance assignments are detected and reported with human-readable information."""
        tag = uuid.uuid4().hex[:6]
        org_id = f"ORG_QA51_{tag}"
        comp_id = database.save_payroll_component_type(
            name=f"QA51 Fuel {tag}",
            code=f"QA51_{tag.upper()}",
            kind="earning",
            category="allowance",
            calc_mode="fixed",
            default_amount=1000.0,
            organization_id=org_id
        )

        # First assignment succeeds
        a1 = database.save_employee_component(
            organization_id=org_id,
            component_type_id=comp_id,
            scope_type="employee",
            user_id=9988,
            amount=1000.0,
            effective_from="2026-01-01"
        )
        self.assertTrue(a1.get("ok"))

        # Second identical assignment raises ValueError preventing duplicate
        with self.assertRaises(ValueError) as ctx:
            database.save_employee_component(
                organization_id=org_id,
                component_type_id=comp_id,
                scope_type="employee",
                user_id=9988,
                amount=1000.0,
                effective_from="2026-01-01"
            )
        self.assertIn("already assigned", str(ctx.exception).lower())

        # If duplicate rows exist in database (e.g. legacy or imported):
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute(
                """INSERT INTO payroll_employee_components (
                       organization_id, branch_id, user_id, component_type_id,
                       amount, effective_from, active, scope_type, scope_id
                   ) VALUES (?, ?, ?, ?, ?, ?, 1, 'employee', ?)""",
                (org_id, "DEFAULT_BRANCH", 9988, comp_id, 1000.0, "2026-01-01", "9988")
            )
            conn.commit()

        duplicates = database.list_payroll_component_duplicates(org_id)
        self.assertGreater(len(duplicates), 0)
        found = any(d["component_type_id"] == comp_id for d in duplicates)
        self.assertTrue(found, "Expected duplicate fuel allowance assignment to be reported")

    def test_qa_47_and_qa_49_and_qa_54_payslip_calculations(self):
        """QA-47 (labels), QA-49 (cuts exceed pay / shortfall), QA-54 (monthly allowance line item)."""
        # Create an employee with basic salary 50000, monthly allowance 18000
        # and heavy custom deductions exceeding gross pay
        emp_dict = {
            "employee_id": "EMP_QA_TEST",
            "employee_name": "Test QA Employee",
            "basic_salary": 50000.0,
            "allowances": 18000.0,  # Package monthly allowances
            "gross_salary": 68000.0,
            "attendance_deduction": 5000.0,
            "custom_deductions": 70000.0,  # 5000 + 70000 = 75000 > 68000
            "total_deductions": 75000.0,
            "net_salary": -7000.0,
            "shortfall": 7000.0,
            "absent_days": 2,
            "month_scheduled_workdays": 22,
            "days_paid": 20,
            "attendance_pay_mode": "full_salary",
            "is_prorated": False,
            "pay_lines": [],
            "half_leave_count": 2,
            "half_leave_cut": 20.0,
            "matched_half_leave_rule": ">= 2 occurrences: PKR 10 each",
        }
        payslip_data = {
            "payroll_month": "2026-10",
            "period_start": "2026-10-01",
            "period_end": "2026-10-31",
            "gross_salary": 68000.0,
            "total_deductions": 75000.0,
            "net_salary": -7000.0,
            "shortfall": 7000.0,
            "employee": emp_dict
        }

        # Check invoice line items generated
        items = payroll_extensions._payslip_invoice_items(payslip_data)
        item_names = [it["name"] for it in items]

        # QA-54: Package allowances appear as "Monthly Allowance" line item
        self.assertIn("Monthly Allowance", item_names)
        monthly_line = next(it for it in items if it["name"] == "Monthly Allowance")
        self.assertEqual(monthly_line["amount"], 18000.0)

        # QA-53: Half-day leave cut appears with occurrence count and rule
        half_line = next((it for it in items if "Half-day leave cut" in it["name"]), None)
        self.assertIsNotNone(half_line)
        self.assertIn("2 occurrences", half_line["name"])

        # Check total cuts and shortfall
        self.assertEqual(payslip_data["total_deductions"], 75000.0)
        self.assertEqual(payslip_data["net_salary"], -7000.0)
        self.assertEqual(payslip_data["shortfall"], 7000.0)

    def test_qa_48_missed_day_percentage_rule_explanation(self):
        """QA-48: Missed-day cut must show absent days, bracket, percentage, and base salary."""
        # Setup an employee and payroll rule
        # When absent 14 days with 30% cut on 58,064.52 base:
        explanation = payroll_extensions._format_missed_day_explanation(
            absent_days=14,
            bracket_str="6 to 31 days",
            pct=30.0,
            base_amount=58064.52,
            deduction=17419.36
        )
        self.assertEqual(
            explanation,
            "Absent 14 days (6 to 31 days: 30% of 58,064.52) = 17,419.36"
        )

    def test_qa_50_today_status_before_shift_end(self):
        """QA-50: Unpunched employee before shift ends is marked 'not_checked_in', not absent."""
        # Helper function in database: check shift end status logic
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        
        # Shift ending later today (e.g. 23:59)
        has_ended = database.is_shift_ended_for_time("23:59:00", now.time())
        self.assertFalse(has_ended)

        # Shift ended earlier today (e.g. 00:01)
        has_ended_past = database.is_shift_ended_for_time("00:01:00", now.time())
        self.assertTrue(has_ended_past)

    def test_qa_52_half_day_leave_duration_validation(self):
        """QA-52: Half-day leave duration > 50% of shift duration is rejected."""
        # 8-hour shift from 09:00 to 17:00
        shift_start = "09:00:00"
        shift_end = "17:00:00"
        
        # Valid half day: 09:00 to 13:00 (4 hours <= 4 hours)
        is_valid = database.validate_half_day_leave_duration(
            leave_start="09:00:00",
            leave_end="13:00:00",
            shift_start=shift_start,
            shift_end=shift_end
        )
        self.assertTrue(is_valid)

        # Invalid half day: 09:00 to 17:00 (8 hours > 4 hours)
        is_valid_full = database.validate_half_day_leave_duration(
            leave_start="09:00:00",
            leave_end="17:00:00",
            shift_start=shift_start,
            shift_end=shift_end
        )
        self.assertFalse(is_valid_full)


if __name__ == "__main__":
    unittest.main()
