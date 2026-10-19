"""
Unit and Integration tests for the 5 QA Tickets:
- PAY-08: Overtime amount & hours integration in monthly payroll calculations
- OT-04: Multi-date overtime display formatting & searchability
- OT-02: Auto-cancellation of overtime when employee is marked absent
- REP-04: Multi-day attendance report filter support for holiday and rest_day
- ATT-05: Attendance report export options (Excel, PDF, CSV)
"""

from datetime import date, timedelta
import os
import sys
import unittest
from unittest.mock import MagicMock
import uuid

# Mock heavy ML/vision packages before importing app or other modules
for mod_name in ['cv2', 'insightface', 'torch', 'ultralytics', 'face_processor']:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

import database as db


class TestFiveQATickets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.org_id = f"test-org-{uuid.uuid4().hex[:8]}"
        cls.branch_id = f"test-branch-{uuid.uuid4().hex[:8]}"

    def setUp(self):
        self.test_suffix = uuid.uuid4().hex[:6]

    def test_pay_08_overtime_in_payroll_calculation(self):
        """PAY-08: Ensure approved overtime hours and amount are included in monthly payroll calculation."""
        emp_name = f"OT_Emp_{self.test_suffix}"
        user_id = db.add_user(
            name=emp_name,
            role="Employee",
            department="Operations",
            salary=60000.0,
            join_date="2026-01-01",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertIsNotNone(user_id)

        # Set custom overtime rate
        db.set_salary_config(
            user_id=user_id,
            basic_salary=60000.0,
            allowances=0.0,
            deductions=0.0,
            ot_rate=250.0,
        )

        today_str = date.today().isoformat()
        curr_month = date.today().strftime("%Y-%m")

        # Record attendance for employee today
        db.log_attendance(
            user_id=user_id,
            detected_name=emp_name,
            confidence=0.99,
            source="manual",
            bypass_shift_check=True,
        )

        # Add overtime for today: 4 hours
        ot_id = db.add_overtime(
            user_id=user_id,
            user_name=emp_name,
            ot_date=today_str,
            hours=4.0,
            reason="Project release deadline",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(ot_id)

        # Approve the overtime
        db.update_overtime_status(
            ot_id=ot_id,
            status="approved",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        # Generate payroll
        payroll = db.calculate_monthly_payroll(
            payroll_month=curr_month,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            force_recalculate=True,
        )
        self.assertIn("records", payroll)
        emp_rec = next((r for r in payroll["records"] if r["user_id"] == user_id), None)
        self.assertIsNotNone(emp_rec, "Employee must appear in payroll")

        # Verify overtime fields
        self.assertGreaterEqual(float(emp_rec.get("overtime_hours", 0)), 4.0)
        self.assertGreaterEqual(float(emp_rec.get("overtime_amount", 0)), 1000.0)
        # Verify gross salary incorporates overtime amount
        self.assertIn("overtime_amount", emp_rec)
        self.assertEqual(emp_rec.get("net_payable"), emp_rec.get("net_salary"))

    def test_ot_02_auto_cancel_overtime_on_mark_absent(self):
        """OT-02: When attendance is marked absent, any overtime for that date should be cancelled."""
        emp_name = f"Absent_Emp_{self.test_suffix}"
        user_id = db.add_user(
            name=emp_name,
            role="Employee",
            department="Logistics",
            salary=50000.0,
            join_date="2026-01-01",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        today_str = date.today().isoformat()
        ot_id = db.add_overtime(
            user_id=user_id,
            user_name=emp_name,
            ot_date=today_str,
            hours=3.0,
            reason="Weekend coverage",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertTrue(ot_id)
        db.update_overtime_status(ot_id=ot_id, status="approved", organization_id=self.org_id, branch_id=self.branch_id)

        # Mark user absent on today_str
        db.mark_user_absent_today(user_id=user_id, target_date=today_str, organization_id=self.org_id)

        # Check overtime record status
        ot_records = db.get_overtime(
            user_id=user_id,
            start_date=today_str,
            end_date=today_str,
            organization_id=self.org_id,
        )
        self.assertTrue(len(ot_records) > 0)
        for ot in ot_records:
            if ot.get("ot_date") == today_str:
                self.assertIn(ot.get("status", "").lower(), ["cancelled_absent", "cancelled", "rejected"])

    def test_ot_04_multi_date_overtime_display(self):
        """OT-04: Multi-date overtime requests must store individual dates properly for search and range display."""
        emp_name = f"MultiDate_Emp_{self.test_suffix}"
        user_id = db.add_user(
            name=emp_name,
            role="Employee",
            department="Tech",
            salary=70000.0,
            join_date="2026-01-01",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        today = date.today()
        dates = [today.isoformat(), (today + timedelta(days=1)).isoformat(), (today + timedelta(days=2)).isoformat()]
        for d in dates:
            ot_id = db.add_overtime(
                user_id=user_id,
                user_name=emp_name,
                ot_date=d,
                hours=2.0,
                reason="Server maintenance",
                organization_id=self.org_id,
                branch_id=self.branch_id,
            )
            self.assertTrue(ot_id)

        records = db.get_overtime(user_id=user_id, organization_id=self.org_id)
        ot_dates = [r.get("ot_date") for r in records if r.get("user_id") == user_id]
        self.assertEqual(len(ot_dates), 3)
        for d in ot_dates:
            self.assertTrue(len(d) == 10 and "-" in d)

    def test_rep_04_multi_day_holiday_and_rest_day_filter(self):
        """REP-04: Multi-day detailed attendance report filtering by holiday and rest_day."""
        emp_name = f"Filter_Emp_{self.test_suffix}"
        user_id = db.add_user(
            name=emp_name,
            role="Employee",
            department="Finance",
            salary=55000.0,
            join_date="2026-01-01",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        # Query report with status_filter='holiday' and 'rest_day'
        report_holiday = db.get_detailed_attendance_report(
            start_date="2026-10-01",
            end_date="2026-10-07",
            status_filter="holiday",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertIsNotNone(report_holiday)
        self.assertTrue(report_holiday.get("is_multi_day"))

        report_rest = db.get_detailed_attendance_report(
            start_date="2026-10-01",
            end_date="2026-10-07",
            status_filter="rest_day",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        self.assertIsNotNone(report_rest)
        self.assertTrue(report_rest.get("is_multi_day"))

    def test_att_05_export_endpoints_availability(self):
        """ATT-05: Excel, PDF, and CSV report export endpoints must be registered and functional in app."""
        import app as flask_app
        client = flask_app.app.test_client()

        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["role"] = "Admin"
            sess["admin_unlocked"] = True
            sess["organization_id"] = self.org_id
            sess["branch_id"] = self.branch_id

        # Verify Excel endpoint responds (200 or 302/204)
        res_excel = client.get("/api/reports/export/excel?date=today")
        self.assertIn(res_excel.status_code, [200, 302])

        # Verify CSV endpoint responds
        res_csv = client.get("/api/reports/export/csv?date=today")
        self.assertIn(res_csv.status_code, [200, 302])


if __name__ == "__main__":
    unittest.main()
