import os
import sys
import uuid
import unittest
from datetime import datetime, date

import database as db

class TestOvertimeRealtimePrecision(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.org_id = f"test-org-{uuid.uuid4().hex[:8]}"
        cls.branch_id = f"test-branch-{uuid.uuid4().hex[:8]}"
        cls.test_date = "2026-10-15"

    def _create_employee(self, shift_id=1, hourly_rate=250.0):
        uid_rand = uuid.uuid4().hex[:6]
        name = f"Emp_Precision_{uid_rand}"
        email = f"emp_{uid_rand}@example.com"
        user_id = db.add_user(
            name=name,
            email=email,
            phone="1234567890",
            department="Engineering",
            shift_id=shift_id,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            salary=60000.0,
        )
        db.set_salary_config(
            user_id=user_id,
            basic_salary=60000.0,
            allowances=0.0,
            deductions=0.0,
            ot_rate=hourly_rate,
        )
        return user_id, name

    def test_01_no_grace_period_one_sec_late_creates_overtime(self):
        """No grace period: checking out even 1 second after shift end creates pending overtime request."""
        shift_id = db.add_shift(
            name=f"Shift_Strict_1700_{uuid.uuid4().hex[:4]}",
            start_time="09:00",
            end_time="17:00",
            grace_period_minutes=15,
            checkout_grace_period_minutes=0,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        user_id, name = self._create_employee(shift_id=shift_id)

        # Checkin at 09:00:00
        cin_dt = datetime.strptime(f"{self.test_date} 09:00:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="camera", bypass_shift_check=True, attendance_time=cin_dt)

        # Checkout at 17:00:01 (1 second after 17:00 shift end)
        cout_dt = datetime.strptime(f"{self.test_date} 17:00:01", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="camera",
            checkout_camera_id="CAM_EXIT",
            checkout_location="Main Exit",
            attendance_time=cout_dt,
        )

        # Overtime request must be created for approval
        ot_records = db.get_overtime(
            organization_id=self.org_id,
            branch_id=self.branch_id,
            user_id=user_id,
            ot_date=self.test_date,
        )
        self.assertEqual(len(ot_records), 1)
        rec = ot_records[0]
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["ot_date"], self.test_date)
        self.assertGreaterEqual(float(rec["hours"]), 0.01)
        self.assertIn("17:00:01", rec["reason"])
        self.assertIn("Shift: 09:00 - 17:00", rec["reason"])

    def test_02_no_grace_period_one_min_late_creates_overtime(self):
        """No grace period: checking out 1 minute after shift end calculates real-time overtime."""
        test_day = "2026-10-16"
        shift_id = db.add_shift(
            name=f"Shift_Strict_1700_B_{uuid.uuid4().hex[:4]}",
            start_time="09:00",
            end_time="17:00",
            grace_period_minutes=15,
            checkout_grace_period_minutes=0,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        user_id, name = self._create_employee(shift_id=shift_id)

        # Checkin at 09:00:00
        cin_dt = datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="camera", bypass_shift_check=True, attendance_time=cin_dt)

        # Checkout at 17:01:00 (1 minute after 17:00 shift end)
        cout_dt = datetime.strptime(f"{test_day} 17:01:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="camera",
            checkout_camera_id="CAM_EXIT",
            checkout_location="Main Exit",
            attendance_time=cout_dt,
        )

        ot_records = db.get_overtime(
            organization_id=self.org_id,
            branch_id=self.branch_id,
            user_id=user_id,
            ot_date=test_day,
        )
        self.assertEqual(len(ot_records), 1)
        rec = ot_records[0]
        self.assertEqual(rec["status"], "pending")
        self.assertAlmostEqual(float(rec["hours"]), 0.02, places=2)
        self.assertIn("17:01:00", rec["reason"])

    def test_03_with_grace_period_exceeded_creates_overtime(self):
        """With 15-min grace period: checking out at 17:16:00 (1 min past grace) creates overtime."""
        test_day = "2026-10-17"
        shift_id = db.add_shift(
            name=f"Shift_Grace_15m_{uuid.uuid4().hex[:4]}",
            start_time="09:00",
            end_time="17:00",
            grace_period_minutes=15,
            checkout_grace_period_minutes=15,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        user_id, name = self._create_employee(shift_id=shift_id)

        # Checkin at 09:00:00
        cin_dt = datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="camera", bypass_shift_check=True, attendance_time=cin_dt)

        # Checkout within grace: 17:14:00 -> 0 OT
        cout_grace_dt = datetime.strptime(f"{test_day} 17:14:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="camera",
            checkout_camera_id="CAM_EXIT",
            checkout_location="Main Exit",
            attendance_time=cout_grace_dt,
        )
        ot_records = db.get_overtime(organization_id=self.org_id, branch_id=self.branch_id, user_id=user_id, ot_date=test_day)
        self.assertEqual(len(ot_records), 0)

        # Employee stays later and checks out at 17:16:00 (1 min past 17:15 threshold)
        cout_late_dt = datetime.strptime(f"{test_day} 17:16:00", "%Y-%m-%d %H:%M:%S")
        db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=0.99,
            source="camera",
            checkout_camera_id="CAM_EXIT",
            checkout_location="Main Exit",
            attendance_time=cout_late_dt,
        )
        ot_records = db.get_overtime(organization_id=self.org_id, branch_id=self.branch_id, user_id=user_id, ot_date=test_day)
        self.assertEqual(len(ot_records), 1)
        rec = ot_records[0]
        self.assertEqual(rec["status"], "pending")
        self.assertAlmostEqual(float(rec["hours"]), 0.02, places=2)
        self.assertIn("17:16:00", rec["reason"])

    def test_04_overtime_ongoing_detection_updates_pending_request(self):
        """As employee works late, subsequent detections update the pending overtime hours in real-time."""
        test_day = "2026-10-18"
        shift_id = db.add_shift(
            name=f"Shift_Dynamic_OT_{uuid.uuid4().hex[:4]}",
            start_time="09:00",
            end_time="17:00",
            grace_period_minutes=15,
            checkout_grace_period_minutes=0,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        user_id, name = self._create_employee(shift_id=shift_id)

        # Checkin at 09:00:00
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="camera", bypass_shift_check=True, attendance_time=datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S"))

        # Detection 1 at 17:30:00 (0.5 hrs OT)
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="camera", checkout_camera_id="CAM_EXIT", checkout_location="Exit", attendance_time=datetime.strptime(f"{test_day} 17:30:00", "%Y-%m-%d %H:%M:%S"))
        ot1 = db.get_overtime(organization_id=self.org_id, branch_id=self.branch_id, user_id=user_id, ot_date=test_day)
        self.assertEqual(len(ot1), 1)
        self.assertAlmostEqual(float(ot1[0]["hours"]), 0.5, places=2)

        # Detection 2 at 18:30:00 (1.5 hrs OT) -> updates existing pending record
        db.log_attendance(user_id=user_id, detected_name=name, confidence=0.99, source="camera", checkout_camera_id="CAM_EXIT", checkout_location="Exit", attendance_time=datetime.strptime(f"{test_day} 18:30:00", "%Y-%m-%d %H:%M:%S"))
        ot2 = db.get_overtime(organization_id=self.org_id, branch_id=self.branch_id, user_id=user_id, ot_date=test_day)
        self.assertEqual(len(ot2), 1)
        self.assertAlmostEqual(float(ot2[0]["hours"]), 1.5, places=2)
        self.assertEqual(ot2[0]["id"], ot1[0]["id"])
        self.assertIn("18:30:00", ot2[0]["reason"])

if __name__ == "__main__":
    unittest.main()
