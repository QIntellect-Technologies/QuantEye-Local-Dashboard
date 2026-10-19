import os
import sys
import uuid
import unittest
from datetime import datetime, date

import database as db

class TestOt02LateCheckoutOvertime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.org_id = f"test-org-{uuid.uuid4().hex[:8]}"
        cls.branch_id = f"test-branch-{uuid.uuid4().hex[:8]}"
        cls.test_date = "2026-10-15"

    def _create_employee(self, shift_id=1, hourly_rate=200.0):
        uid_rand = uuid.uuid4().hex[:6]
        name = f"Emp_OT02_{uid_rand}"
        email = f"emp_{uid_rand}@example.com"
        user_id = db.add_user(
            name=name,
            email=email,
            phone="1234567890",
            department="Operations",
            shift_id=shift_id,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            salary=50000.0,
        )
        db.set_salary_config(
            user_id=user_id,
            basic_salary=50000.0,
            allowances=0.0,
            deductions=0.0,
            ot_rate=hourly_rate,
        )
        return user_id, name

    def test_01_eligible_late_checkout_creates_overtime(self):
        """1. Checkout at 18:00 when scheduled shift ends at 17:00 creates 1.0 hr overtime request."""
        # Create a dedicated shift 09:00 to 17:00
        shift_id = db.add_shift(
            name=f"Shift_9_to_5_{uuid.uuid4().hex[:4]}",
            start_time="09:00",
            end_time="17:00",
            grace_period_minutes=15,
            checkout_grace_period_minutes=0,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        user_id, name = self._create_employee(shift_id=shift_id)

        # Log checkin at 09:00
        checkin_dt = datetime.strptime(f"{self.test_date} 09:00:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="manual",
            bypass_shift_check=True,
            attendance_time=checkin_dt,
        )

        # Log checkout at 18:00 (1 hour after 17:00 end time)
        checkout_dt = datetime.strptime(f"{self.test_date} 18:00:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="manual",
            bypass_shift_check=True,
            attendance_time=checkout_dt,
        )

        # Verify overtime record was automatically recorded
        ot_records = db.get_overtime(
            organization_id=self.org_id,
            branch_id=self.branch_id,
            user_id=user_id,
            ot_date=self.test_date,
        )
        self.assertGreaterEqual(len(ot_records), 1)
        rec = ot_records[0]
        self.assertEqual(rec["ot_date"], self.test_date)
        self.assertAlmostEqual(float(rec["hours"]), 1.0, places=1)
        self.assertEqual(rec["status"], "pending")

    def test_02_below_threshold_checkout_does_not_create_overtime(self):
        """2. Checkout at 17:15 with 30-min minimum OT threshold generates 0 OT."""
        test_day = "2026-10-16"
        shift_id = db.add_shift(
            name=f"Shift_9_to_5_{uuid.uuid4().hex[:4]}",
            start_time="09:00",
            end_time="17:00",
            grace_period_minutes=15,
            checkout_grace_period_minutes=30,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        user_id, name = self._create_employee(shift_id=shift_id)

        # Checkin at 09:00
        cin_dt = datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="manual",
            bypass_shift_check=True,
            attendance_time=cin_dt,
        )

        # Checkout at 17:15 (only 15 mins late; below 30-min threshold)
        cout_dt = datetime.strptime(f"{test_day} 17:15:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="manual",
            bypass_shift_check=True,
            attendance_time=cout_dt,
        )

        # Overtime records should NOT have any record for this date
        ot_records = db.get_overtime(
            organization_id=self.org_id,
            branch_id=self.branch_id,
            user_id=user_id,
            ot_date=test_day,
        )
        self.assertEqual(len(ot_records), 0)

    def test_03_overtime_approval_and_rejection_payroll_impact(self):
        """3. Approved late-checkout OT contributes to payroll; rejected OT is excluded."""
        test_day = date.today().isoformat()
        curr_month = date.today().strftime("%Y-%m")
        user_id, name = self._create_employee(hourly_rate=300.0)

        # Mark attendance on test_day so employee is present
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="manual",
            bypass_shift_check=True,
            attendance_time=datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S"),
        )

        # Add 2.0 hrs OT directly as pending
        ot_id = db.add_overtime(
            user_id=user_id,
            user_name=name,
            ot_date=test_day,
            hours=2.0,
            reason="Late checkout verified",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )

        # Pending OT must NOT contribute to payroll
        pay_pending = db.calculate_monthly_payroll(
            payroll_month=curr_month,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            force_recalculate=True,
        )
        emp_pending = next((r for r in pay_pending["records"] if r["user_id"] == user_id), None)
        self.assertIsNotNone(emp_pending)
        self.assertAlmostEqual(float(emp_pending.get("overtime_amount", 0.0)), 0.0, places=2)

        # Approve OT
        db.update_overtime_status(ot_id, "approved")
        pay_approved = db.calculate_monthly_payroll(
            payroll_month="2026-10",
            organization_id=self.org_id,
            branch_id=self.branch_id,
            force_recalculate=True,
        )
        emp_approved = next((r for r in pay_approved["records"] if r["user_id"] == user_id), None)
        self.assertAlmostEqual(float(emp_approved.get("overtime_amount", 0.0)), 600.0, places=2)

        # Reject OT
        db.update_overtime_status(ot_id, "rejected")
        pay_rejected = db.calculate_monthly_payroll(
            payroll_month="2026-10",
            organization_id=self.org_id,
            branch_id=self.branch_id,
            force_recalculate=True,
        )
        emp_rejected = next((r for r in pay_rejected["records"] if r["user_id"] == user_id), None)
        self.assertAlmostEqual(float(emp_rejected.get("overtime_amount", 0.0)), 0.0, places=2)

    def test_04_attendance_marked_absent_cancels_overtime(self):
        """4. When attendance on OT date is marked absent, OT transitions to cancelled_absent and is excluded."""
        test_day = "2026-10-18"
        user_id, name = self._create_employee(hourly_rate=250.0)

        # Checkin and checkout with OT
        cin_dt = datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S")
        cout_dt = datetime.strptime(f"{test_day} 18:30:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=cin_dt)
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=cout_dt)

        ot_records = db.get_overtime(organization_id=self.org_id, branch_id=self.branch_id, user_id=user_id, ot_date=test_day)
        if not ot_records:
            ot_id = db.add_overtime(user_id=user_id, user_name=name, ot_date=test_day, hours=1.5, organization_id=self.org_id, branch_id=self.branch_id)
        else:
            ot_id = ot_records[0]["id"]
        db.update_overtime_status(ot_id, "approved")

        # Now mark user absent for that date
        db.mark_user_absent_today(user_id=user_id, target_date=test_day, organization_id=self.org_id)

        # Verify OT status changed to cancelled_absent
        ot_rec = db.get_overtime_by_id(ot_id)
        self.assertEqual(ot_rec["status"], "cancelled_absent")

        # Verify payroll excludes this OT
        payroll = db.calculate_monthly_payroll(
            payroll_month="2026-10",
            organization_id=self.org_id,
            branch_id=self.branch_id,
            force_recalculate=True,
        )
        emp_pay = next((r for r in payroll["records"] if r["user_id"] == user_id), None)
        self.assertAlmostEqual(float(emp_pay.get("overtime_amount", 0.0)), 0.0, places=2)

    def test_05_duplicate_checkout_does_not_duplicate_overtime(self):
        """5. Multiple checkouts on same day update checkout time rather than creating duplicate OT records."""
        test_day = "2026-10-19"
        shift_id = db.add_shift(
            name=f"Shift_9_to_5_{uuid.uuid4().hex[:4]}",
            start_time="09:00",
            end_time="17:00",
            grace_period_minutes=15,
            checkout_grace_period_minutes=0,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        user_id, name = self._create_employee(shift_id=shift_id)

        # Checkin at 09:00
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S"))

        # Checkout 1 at 18:00
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=datetime.strptime(f"{test_day} 18:00:00", "%Y-%m-%d %H:%M:%S"))

        # Checkout 2 at 18:30 (e.g. employee lingered or punched again)
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=datetime.strptime(f"{test_day} 18:30:00", "%Y-%m-%d %H:%M:%S"))

        ot_records = db.get_overtime(
            organization_id=self.org_id,
            branch_id=self.branch_id,
            user_id=user_id,
            ot_date=test_day,
        )
        # Must be exactly 1 record for this date/employee, not 2
        self.assertEqual(len(ot_records), 1)

if __name__ == "__main__":
    unittest.main()
