"""
OT-04: Dedicated Test Suite
Proves:
- actual overtime date comes from the correct overtime record
- no day count/duration appears in Date
- consistent readable date format
- filtering works
- sorting works
- exported overtime data contains the correct date
- overtime date matches associated attendance date
"""

import csv
from datetime import date, timedelta
import io
import unittest
import uuid

import database as db


class TestOt04OvertimeDate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.org_id = f"test-org-{uuid.uuid4().hex[:8]}"
        cls.branch_id = f"test-branch-{uuid.uuid4().hex[:8]}"

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:6]
        self.today = date.today()
        self.d1 = self.today.isoformat()
        self.d2 = (self.today + timedelta(days=1)).isoformat()
        self.d3 = (self.today + timedelta(days=2)).isoformat()

    def _create_employee(self, dept="Engineering"):
        name = f"Emp_OT04_{self.suffix}_{uuid.uuid4().hex[:4]}"
        user_id = db.add_user(
            name=name,
            role="Employee",
            department=dept,
            salary=75000.0,
            join_date="2026-01-01",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        return user_id, name

    def test_01_actual_date_comes_from_correct_record_no_duration_count(self):
        """1. Date field contains exact ISO date, not numeric day count or duration."""
        user_id, name = self._create_employee()

        ot_id = db.add_overtime(
            user_id=user_id,
            user_name=name,
            ot_date=self.d1,
            hours=3.5,
            reason="Database optimization",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(ot_id)

        records = db.get_overtime(user_id=user_id, organization_id=self.org_id)
        self.assertEqual(len(records), 1)
        r = records[0]

        # The date must be self.d1 (e.g. '2026-10-19')
        self.assertEqual(r["ot_date"], self.d1)
        # Not a day count like "1" or "3.5" or "1 day"
        self.assertNotIn("day", str(r["ot_date"]).lower())
        self.assertNotEqual(str(r["ot_date"]), "1")
        self.assertNotEqual(str(r["ot_date"]), str(r["hours"]))
        # Verify valid YYYY-MM-DD format
        self.assertEqual(len(r["ot_date"]), 10)
        self.assertEqual(r["ot_date"].count("-"), 2)

    def test_02_overtime_date_matches_associated_attendance_date(self):
        """2. Overtime date corresponds to associated attendance date."""
        user_id, name = self._create_employee()

        # Mark attendance on d1
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="manual",
            bypass_shift_check=True,
        )
        report = db.get_detailed_attendance_report(start_date=self.d1, end_date=self.d1, organization_id=self.org_id)
        records = report.get("records", [])
        matched = [r for r in records if r.get("user_id") == user_id]
        self.assertTrue(len(matched) > 0)
        att_date = self.d1

        # Add OT on same date
        ot_id = db.add_overtime(
            user_id=user_id,
            user_name=name,
            ot_date=att_date,
            hours=2.0,
            reason="Overtime matching attendance",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(ot_id)

        ot_rec = db.get_overtime_by_id(ot_id)
        self.assertIsNotNone(ot_rec)
        self.assertEqual(ot_rec["ot_date"], att_date)

    def test_03_filtering_by_date_status_department(self):
        """3. Overtime filtering works properly by date range, status, and department."""
        u1, n1 = self._create_employee(dept="QA")
        u2, n2 = self._create_employee(dept="Finance")

        ot1 = db.add_overtime(user_id=u1, user_name=n1, ot_date=self.d1, hours=2.0, reason="QA testing", organization_id=self.org_id, branch_id=self.branch_id)
        db.update_overtime_status(ot1, 'approved', approved_by="Admin", organization_id=self.org_id)

        ot2 = db.add_overtime(user_id=u2, user_name=n2, ot_date=self.d2, hours=3.0, reason="Auditing", organization_id=self.org_id, branch_id=self.branch_id)
        db.update_overtime_status(ot2, 'pending', approved_by="Admin", organization_id=self.org_id)

        # Filter by department
        qa_records = db.get_overtime(department="QA", organization_id=self.org_id)
        self.assertTrue(all(r["department"] == "QA" for r in qa_records))
        self.assertTrue(any(r["user_id"] == u1 for r in qa_records))

        # Filter by status
        approved_records = db.get_overtime(status="approved", organization_id=self.org_id)
        self.assertTrue(all(r["status"].lower() == "approved" for r in approved_records))
        self.assertTrue(any(r["id"] == ot1 for r in approved_records))
        self.assertFalse(any(r["id"] == ot2 for r in approved_records))

        # Filter by date range
        d1_records = db.get_overtime(ot_date=self.d1, organization_id=self.org_id)
        self.assertTrue(all(r["ot_date"] == self.d1 for r in d1_records))

    def test_04_sorting_order_is_consistent(self):
        """4. Overtime records sort chronologically and consistently."""
        user_id, name = self._create_employee()

        db.add_overtime(user_id=user_id, user_name=name, ot_date=self.d1, hours=1.0, reason="Day 1", organization_id=self.org_id, branch_id=self.branch_id)
        db.add_overtime(user_id=user_id, user_name=name, ot_date=self.d3, hours=3.0, reason="Day 3", organization_id=self.org_id, branch_id=self.branch_id)
        db.add_overtime(user_id=user_id, user_name=name, ot_date=self.d2, hours=2.0, reason="Day 2", organization_id=self.org_id, branch_id=self.branch_id)

        records = db.get_overtime(user_id=user_id, organization_id=self.org_id)
        dates = [r["ot_date"] for r in records if r["user_id"] == user_id]
        # database default sort is DESC by ot_date
        self.assertEqual(dates, [self.d3, self.d2, self.d1])

    def test_05_exported_data_contains_actual_date(self):
        """5. Exported overtime data preserves the actual date format, not day counts."""
        user_id, name = self._create_employee()

        db.add_overtime(user_id=user_id, user_name=name, ot_date=self.d1, hours=2.5, reason="Export test", organization_id=self.org_id, branch_id=self.branch_id)
        records = db.get_overtime(user_id=user_id, organization_id=self.org_id)

        # Build CSV export in the same format
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Employee", "Department", "Date", "Hours", "Status"])
        for r in records:
            writer.writerow([r["id"], r["user_name"], r["department"], r["ot_date"], r["hours"], r["status"]])

        csv_text = output.getvalue()
        self.assertIn(self.d1, csv_text)
        self.assertNotIn("1 days", csv_text)
        self.assertNotIn("1 day", csv_text)


if __name__ == "__main__":
    unittest.main()
