import os
import sys
import uuid
import unittest
from datetime import datetime, date

import database as db
import app as flask_app

class TestRep04AttendanceReportFilters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.org_id = f"test-org-{uuid.uuid4().hex[:8]}"
        cls.branch_id = f"test-branch-{uuid.uuid4().hex[:8]}"
        cls.test_date = "2026-10-15"
        cls.client = flask_app.app.test_client()

    def setUp(self):
        # Configure app session for tenant isolation
        with self.client.session_transaction() as sess:
            sess["organization_id"] = self.org_id
            sess["branch_id"] = self.branch_id

    def _create_employee(self, dept="Engineering", shift_id=1):
        uid_rand = uuid.uuid4().hex[:6]
        name = f"Emp_REP04_{uid_rand}"
        email = f"emp_{uid_rand}@example.com"
        user_id = db.add_user(
            name=name,
            email=email,
            phone="1234567890",
            department=dept,
            shift_id=shift_id,
            organization_id=self.org_id,
            branch_id=self.branch_id,
            salary=50000.0,
        )
        return user_id, name

    def test_01_individual_filters_department_shift_status(self):
        """1. Individual filters work accurately: department, shift, and status."""
        shift1 = db.add_shift("Morning Shift", "08:00", "16:00", organization_id=self.org_id, branch_id=self.branch_id)
        shift2 = db.add_shift("Evening Shift", "16:00", "00:00", organization_id=self.org_id, branch_id=self.branch_id)

        u1, n1 = self._create_employee(dept="Engineering", shift_id=shift1)
        u2, n2 = self._create_employee(dept="Marketing", shift_id=shift2)

        # Log attendance for u1 only on test_date
        db.log_attendance(user_id=u1, detected_name=n1, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=datetime.strptime(f"{self.test_date} 08:00:00", "%Y-%m-%d %H:%M:%S"))

        # Filter by department = Engineering
        rep_dept = db.get_detailed_attendance_report(
            start_date=self.test_date, end_date=self.test_date,
            department="Engineering", organization_id=self.org_id, branch_id=self.branch_id
        )
        dept_uids = [r["user_id"] for r in rep_dept["records"]]
        self.assertIn(u1, dept_uids)
        self.assertNotIn(u2, dept_uids)

        # Filter by shift = shift2
        rep_shift = db.get_detailed_attendance_report(
            start_date=self.test_date, end_date=self.test_date,
            shift_id=shift2, organization_id=self.org_id, branch_id=self.branch_id
        )
        shift_uids = [r["user_id"] for r in rep_shift["records"]]
        self.assertIn(u2, shift_uids)
        self.assertNotIn(u1, shift_uids)

        # Filter by status = present
        rep_pres = db.get_detailed_attendance_report(
            start_date=self.test_date, end_date=self.test_date,
            status_filter="present", organization_id=self.org_id, branch_id=self.branch_id
        )
        pres_uids = [r["user_id"] for r in rep_pres["records"]]
        self.assertIn(u1, pres_uids)
        self.assertNotIn(u2, pres_uids)

    def test_02_reapply_filter_A_to_B_to_A_produces_identical_state(self):
        """2. Reapplying A -> B -> A produces identical results to initial A with no stale state."""
        u_ops1, n_ops1 = self._create_employee(dept="Operations")
        u_ops2, n_ops2 = self._create_employee(dept="Operations")
        u_fin, n_fin = self._create_employee(dept="Finance")

        test_day = "2026-10-14"
        db.log_attendance(user_id=u_ops1, detected_name=n_ops1, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S"))
        db.log_attendance(user_id=u_fin, detected_name=n_fin, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S"))

        # Step 1: Apply Filter A (Department = Operations)
        res_A1 = db.get_detailed_attendance_report(
            start_date=test_day, end_date=test_day,
            department="Operations", organization_id=self.org_id, branch_id=self.branch_id
        )
        uids_A1 = sorted([r["user_id"] for r in res_A1["records"]])
        self.assertIn(u_ops1, uids_A1)
        self.assertIn(u_ops2, uids_A1)
        self.assertNotIn(u_fin, uids_A1)

        # Step 2: Apply Filter B (Department = Finance)
        res_B = db.get_detailed_attendance_report(
            start_date=test_day, end_date=test_day,
            department="Finance", organization_id=self.org_id, branch_id=self.branch_id
        )
        uids_B = sorted([r["user_id"] for r in res_B["records"]])
        self.assertEqual(uids_B, [u_fin])

        # Step 3: Reapply Filter A (Department = Operations)
        res_A2 = db.get_detailed_attendance_report(
            start_date=test_day, end_date=test_day,
            department="Operations", organization_id=self.org_id, branch_id=self.branch_id
        )
        uids_A2 = sorted([r["user_id"] for r in res_A2["records"]])

        # Must be exactly identical to initial A call
        self.assertEqual(uids_A1, uids_A2)
        self.assertEqual(len(res_A1["records"]), len(res_A2["records"]))

    def test_03_combined_filter_and_semantics(self):
        """3. Combined filters enforce strict AND semantics across department, date, and status."""
        u_hr_pres, n1 = self._create_employee(dept="HR")
        u_hr_abs, n2 = self._create_employee(dept="HR")
        u_it_pres, n3 = self._create_employee(dept="IT")

        test_day = "2026-10-13"
        db.log_attendance(user_id=u_hr_pres, detected_name=n1, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S"))
        db.log_attendance(user_id=u_it_pres, detected_name=n3, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S"))

        # Combined query: Department = HR AND Status = Present
        rep = db.get_detailed_attendance_report(
            start_date=test_day, end_date=test_day,
            department="HR",
            status_filter="present",
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        uids = [r["user_id"] for r in rep["records"]]
        self.assertIn(u_hr_pres, uids)
        self.assertNotIn(u_hr_abs, uids) # Excluded because status is absent
        self.assertNotIn(u_it_pres, uids) # Excluded because dept is IT

    def test_04_reset_and_clear_filters_restores_default_state(self):
        """4. Reset/Clear restores baseline unfiltered report with all employees."""
        u_a, _ = self._create_employee(dept="DeptA")
        u_b, _ = self._create_employee(dept="DeptB")
        test_day = "2026-10-12"

        # Baseline unfiltered
        base_rep = db.get_detailed_attendance_report(
            start_date=test_day, end_date=test_day,
            organization_id=self.org_id, branch_id=self.branch_id
        )
        base_uids = sorted([r["user_id"] for r in base_rep["records"]])

        # Apply filter
        filtered_rep = db.get_detailed_attendance_report(
            start_date=test_day, end_date=test_day,
            department="DeptA",
            organization_id=self.org_id, branch_id=self.branch_id
        )
        self.assertNotEqual(len(filtered_rep["records"]), len(base_rep["records"]))

        # Reset / clear (department=None, status_filter=None, shift_id=None)
        reset_rep = db.get_detailed_attendance_report(
            start_date=test_day, end_date=test_day,
            department=None,
            status_filter=None,
            shift_id=None,
            organization_id=self.org_id,
            branch_id=self.branch_id,
        )
        reset_uids = sorted([r["user_id"] for r in reset_rep["records"]])
        self.assertEqual(base_uids, reset_uids)

    def test_05_export_csv_and_excel_matches_filtered_dataset(self):
        """5. Export endpoints (CSV & Excel) strictly match filtered criteria without exporting master dataset."""
        u_target, n_target = self._create_employee(dept="ExportDept")
        u_other, n_other = self._create_employee(dept="OtherDept")
        test_day = "2026-10-11"

        db.log_attendance(user_id=u_target, detected_name=n_target, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S"))
        db.log_attendance(user_id=u_other, detected_name=n_other, confidence=0.99, source="manual", bypass_shift_check=True, attendance_time=datetime.strptime(f"{test_day} 09:00:00", "%Y-%m-%d %H:%M:%S"))

        # Test CSV export with filter
        csv_resp = self.client.get(
            f"/api/reports/export/csv?date={test_day}&department=ExportDept"
        )
        self.assertEqual(csv_resp.status_code, 200)
        csv_text = csv_resp.data.decode("utf-8")
        self.assertIn(n_target, csv_text)
        self.assertNotIn(n_other, csv_text)

        # Test Excel export with filter
        excel_resp = self.client.get(
            f"/api/reports/export/excel?date={test_day}&department=ExportDept"
        )
        self.assertEqual(excel_resp.status_code, 200)
        self.assertIn("spreadsheetml", excel_resp.content_type)
        # Content length > 100 bytes
        self.assertGreater(len(excel_resp.data), 100)

if __name__ == "__main__":
    unittest.main()
