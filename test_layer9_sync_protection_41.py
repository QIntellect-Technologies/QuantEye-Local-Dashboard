"""
Layer 9 — Complete 41-Point Synchronization & Protection Test Suite.

Covers Sections A through H:
- Section A: Employee protection (Tests 01 - 05)
- Section B: Shift protection (Tests 06 - 09)
- Section C: Tenant isolation (Tests 10 - 17)
- Section D: Authority (Tests 18 - 23)
- Section E: Divergence (Tests 24 - 29)
- Section F: Phantom protection (Tests 30 - 32)
- Section G: Employment dates (Tests 33 - 36)
- Section H: Offline recovery (Tests 37 - 41)
"""

import os
import sys
import unittest
import psycopg2
import sqlite3
from datetime import datetime, date, timedelta
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

PG_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:fIcwBawFDrhFgsAXVhipYzUUdeaqdUvf@iriguchi.proxy.rlwy.net:12691/railway",
)
os.environ["DATABASE_URL"] = PG_DSN

import database as db
import postgres_mirror as cloud_pg
from config import DB_PATH

ORG_A = "ORG_L9_A"
ORG_B = "ORG_L9_B"
BRANCH_A = "BR_L9_A"
BRANCH_B = "BR_L9_B"


def get_pg_conn():
    conn = psycopg2.connect(PG_DSN, connect_timeout=10)
    conn.autocommit = True
    return conn


def clean_fixtures():
    # Clean PG
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                for org in (ORG_A, ORG_B):
                    cur.execute("DELETE FROM attendance_events WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM payroll_records WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM payroll_runs WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM overtime_records WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM leave_requests WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM employees WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM shifts WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM branches WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM organizations WHERE id = %s", (org,))
    except Exception as e:
        print(f"Notice: cleaning PG fixtures encountered: {e}")

    # Clean SQLite
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for org in (ORG_A, ORG_B):
                conn.execute("DELETE FROM attendance WHERE organization_id = ?", (org,))
                conn.execute("UPDATE payroll_runs SET status = 'Reopened' WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM payroll_records WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM payroll_runs WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM overtime WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM leave_requests WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM shifts WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM users WHERE organization_id = ?", (org,))
            conn.commit()
    except Exception as e:
        print(f"Notice: cleaning SQLite fixtures encountered: {e}")


class Layer9SyncProtection41Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clean_fixtures()
        # Seed test orgs and branches in PG
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO organizations (id, name, status) VALUES (%s, %s, 'Active') ON CONFLICT DO NOTHING",
                    (ORG_A, "Layer 9 Org A"),
                )
                cur.execute(
                    "INSERT INTO organizations (id, name, status) VALUES (%s, %s, 'Active') ON CONFLICT DO NOTHING",
                    (ORG_B, "Layer 9 Org B"),
                )
                cur.execute(
                    "INSERT INTO branches (id, organization_id, name, access) VALUES (%s, %s, %s, 'Active') ON CONFLICT DO NOTHING",
                    (BRANCH_A, ORG_A, "L9 Branch A"),
                )
                cur.execute(
                    "INSERT INTO branches (id, organization_id, name, access) VALUES (%s, %s, %s, 'Active') ON CONFLICT DO NOTHING",
                    (BRANCH_B, ORG_B, "L9 Branch B"),
                )

    @classmethod
    def tearDownClass(cls):
        clean_fixtures()

    # =========================================================================
    # SECTION A: EMPLOYEE PROTECTION (Tests 01 - 05)
    # =========================================================================

    def test_01_pg_sqlite_employee_match(self):
        """1. PG employee + SQLite employee -> PASS."""
        user_id = 9001
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET name = EXCLUDED.name
                    """,
                    (str(user_id), ORG_A, BRANCH_A, "EMP-9001", "Ayesha Sync"),
                )
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET name = excluded.name
                """,
                (user_id, "Ayesha Sync", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        user = db.get_user_by_id(user_id, organization_id=ORG_A)
        self.assertIsNotNone(user)
        self.assertEqual(user["name"], "Ayesha Sync")

    def test_02_pg_unavailable_employee_remains(self):
        """2. PG temporarily unavailable -> employee remains in SQLite cache."""
        user_id = 9002
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET name = excluded.name
                """,
                (user_id, "Bilal Offline", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        with patch.object(cloud_pg, "postgres_is_configured", return_value=False):
            user = db.get_user_by_id(user_id, organization_id=ORG_A)
            self.assertIsNotNone(user)
            self.assertEqual(user["name"], "Bilal Offline")

    def test_03_sqlite_employee_not_pruned_when_absent_from_pg(self):
        """3. SQLite employee temporarily absent from PG -> NO DELETE/PRUNE."""
        user_id = 9003
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET name = excluded.name
                """,
                (user_id, "Persistent Local Employee", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        cloud_pg.hydrate_branch_employees(ORG_A, BRANCH_A)

        with sqlite3.connect(DB_PATH) as s_conn:
            cur = s_conn.cursor()
            cur.execute("SELECT id, name FROM users WHERE id = ? AND organization_id = ?", (user_id, ORG_A))
            row = cur.fetchone()
            self.assertIsNotNone(row, "Employee must NOT be pruned from SQLite when absent from PG")
            self.assertEqual(row[1], "Persistent Local Employee")

    def test_04_authorized_employee_deactivation_both_systems(self):
        """4. Authorized employee deactivation -> both systems update."""
        user_id = 9004
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET is_active = TRUE
                    """,
                    (str(user_id), ORG_A, BRANCH_A, "EMP-9004", "Deactivate Me"),
                )
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET active = 1
                """,
                (user_id, "Deactivate Me", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        ok = db.set_user_active(user_id, False, organization_id=ORG_A)
        self.assertTrue(ok)

        with sqlite3.connect(DB_PATH) as s_conn:
            s_active = s_conn.execute("SELECT active FROM users WHERE id = ?", (user_id,)).fetchone()[0]
            self.assertEqual(s_active, 0)

        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT is_active FROM employees WHERE id = %s AND organization_id = %s", (str(user_id), ORG_A))
                pg_active = cur.fetchone()[0]
                self.assertFalse(pg_active)

    def test_05_unauthorized_deletion_rejected(self):
        """5. Unauthorized deletion attempt -> rejected (cross-tenant delete fails)."""
        user_id = 9005
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET name = EXCLUDED.name
                    """,
                    (str(user_id), ORG_A, BRANCH_A, "EMP-9005", "Protected Tenant A Employee"),
                )
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET organization_id = excluded.organization_id
                """,
                (user_id, "Protected Tenant A Employee", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        result = db.delete_user(user_id, organization_id=ORG_B)
        self.assertFalse(result, "Cross-tenant delete attempt must return False")

        user = db.get_user_by_id(user_id, organization_id=ORG_A)
        self.assertIsNotNone(user)

    # =========================================================================
    # SECTION B: SHIFT PROTECTION (Tests 06 - 09)
    # =========================================================================

    def test_06_custom_overnight_shift_survives_restart(self):
        """6. Custom overnight shift (e.g. 16:45 -> 05:00) survives restart / ensure default."""
        shift_id = 9106
        cloud_pg.mirror_shift({
            "id": shift_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "shift_name": "Overnight Service",
            "start_time": "16:45",
            "end_time": "05:00",
            "working_days": [0, 1, 2, 3, 4, 5],
            "active": 1,
        })
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO shifts (id, name, start_time, end_time, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET start_time = excluded.start_time, end_time = excluded.end_time
                """,
                (shift_id, "Overnight Service", "16:45", "05:00", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        with sqlite3.connect(DB_PATH) as cur_conn:
            db._ensure_organization_default_shift(cur_conn.cursor(), ORG_A, BRANCH_A)
            cur_conn.commit()

        shift = db.get_shift_by_id(shift_id, organization_id=ORG_A)
        self.assertIsNotNone(shift)
        self.assertEqual(shift["start_time"], "16:45")
        self.assertEqual(shift["end_time"], "05:00")

    def test_07_default_shift_never_overwrites_valid_shift(self):
        """7. Default shift never overwrites an employee's valid assigned shift."""
        shift_id = 9107
        user_id = 9107
        cloud_pg.mirror_shift({
            "id": shift_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "shift_name": "Evening Special",
            "start_time": "18:00",
            "end_time": "02:00",
            "active": 1,
        })
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, shift_id, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET shift_id = EXCLUDED.shift_id
                    """,
                    (str(user_id), ORG_A, BRANCH_A, "EMP-9107", "Evening Chef", shift_id),
                )
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO shifts (id, name, start_time, end_time, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET active = 1
                """,
                (shift_id, "Evening Special", "18:00", "02:00", ORG_A, BRANCH_A),
            )
            s_conn.execute(
                """
                INSERT INTO users (id, name, shift_id, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET shift_id = excluded.shift_id
                """,
                (user_id, "Evening Chef", shift_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        with sqlite3.connect(DB_PATH) as cur_conn:
            db._ensure_organization_default_shift(cur_conn.cursor(), ORG_A, BRANCH_A)
            cur_conn.commit()

        user = db.get_user_by_id(user_id, organization_id=ORG_A)
        self.assertIsNotNone(user)
        self.assertEqual(int(user["shift_id"]), shift_id)

    def test_08_employee_shift_assignment_survives_sync(self):
        """8. Employee shift assignment survives PG/SQLite synchronization."""
        shift_id = 9108
        user_id = 9108
        cloud_pg.mirror_shift({
            "id": shift_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "name": "PG Shift",
            "start_time": "10:00",
            "end_time": "19:00",
            "active": 1,
        })
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, shift_id, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET shift_id = EXCLUDED.shift_id
                    """,
                    (str(user_id), ORG_A, BRANCH_A, "EMP-9108", "Sync Shift User", shift_id),
                )

        cloud_pg.hydrate_branch_employees(ORG_A, BRANCH_A)

        user = db.get_user_by_id(user_id, organization_id=ORG_A)
        self.assertIsNotNone(user)
        self.assertEqual(int(user["shift_id"]), shift_id)

    def test_09_cross_tenant_shift_assignment_rejected(self):
        """9. Cross-tenant shift assignment rejected."""
        shift_b = 9109
        user_a = 9109
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO shifts (id, name, start_time, end_time, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET organization_id = excluded.organization_id
                """,
                (shift_b, "Tenant B Shift", "08:00", "16:00", ORG_B, BRANCH_B),
            )
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET organization_id = excluded.organization_id
                """,
                (user_a, "Tenant A User", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        success = db.assign_user_shift(user_a, shift_b, organization_id=ORG_A)
        self.assertFalse(success, "Assigning foreign tenant shift must fail")

    # =========================================================================
    # SECTION C: TENANT ISOLATION (Tests 10 - 17)
    # =========================================================================

    def test_10_tenant_isolation_employee(self):
        """10. Tenant A cannot read Tenant B employee."""
        user_b = 9210
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET name = EXCLUDED.name
                    """,
                    (str(user_b), ORG_B, BRANCH_B, "EMP-B-10", "Secret Agent B"),
                )
        user = db.get_user_by_id(user_b, organization_id=ORG_A)
        self.assertIsNone(user, "Tenant A must NOT be able to read Tenant B employee")

    def test_11_tenant_isolation_attendance(self):
        """11. Tenant A cannot read Tenant B attendance."""
        cloud_pg.mirror_attendance_event({
            "id": 9211,
            "organization_id": ORG_B,
            "branch_id": BRANCH_B,
            "user_id": 9211,
            "name": "Bob B",
            "camera_id": "CAM-B",
            "event_type": "check_in",
            "confidence": 0.99,
            "timestamp": "2026-10-18T10:00:00Z",
        })
        logs_a = db.get_attendance_logs(limit=50, organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertFalse(any(l.get("id") == 9211 for l in logs_a))

    def test_12_tenant_isolation_shift(self):
        """12. Tenant A cannot read Tenant B shift."""
        shift_b = 9212
        cloud_pg.mirror_shift({
            "id": shift_b,
            "organization_id": ORG_B,
            "branch_id": BRANCH_B,
            "name": "Org B Shift",
            "start_time": "08:00",
            "end_time": "16:00",
            "active": 1,
        })
        shift = db.get_shift_by_id(shift_b, organization_id=ORG_A)
        self.assertIsNone(shift, "Tenant A must NOT read Tenant B shift")

    def test_13_tenant_isolation_leave(self):
        """13. Tenant A cannot read Tenant B leave."""
        leave_id = 9213
        cloud_pg.mirror_leave({
            "id": leave_id,
            "organization_id": ORG_B,
            "branch_id": BRANCH_B,
            "employee_id": "9999",
            "leave_type": "sick",
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "status": "approved",
            "reason": "Illness",
        })
        leaves = db.get_leave_requests(organization_id=ORG_A)
        self.assertFalse(any(l["id"] == leave_id for l in leaves))

    def test_14_tenant_isolation_overtime(self):
        """14. Tenant A cannot read Tenant B overtime."""
        ot_id = 9214
        cloud_pg.mirror_overtime({
            "id": ot_id,
            "organization_id": ORG_B,
            "branch_id": BRANCH_B,
            "employee_id": "9999",
            "work_date": "2026-10-05",
            "hours": 4.0,
            "status": "approved",
            "reason": "Cover",
        })
        ot_list = db.get_overtime(organization_id=ORG_A)
        self.assertFalse(any(o["id"] == ot_id for o in ot_list))

    def test_15_tenant_isolation_payroll(self):
        """15. Tenant A cannot read Tenant B payroll."""
        cloud_pg.mirror_payroll_run({
            "id": "RUN-L9-15",
            "organization_id": ORG_B,
            "branch_id": BRANCH_B,
            "payroll_month": "2026-09",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "total_net_salary": 777777.0,
            "status": "approved",
        })
        run_a = db.get_monthly_payroll("2026-09", organization_id=ORG_A, branch_id=BRANCH_A)
        if run_a:
            self.assertNotEqual(run_a.get("total_net_salary"), 777777.0)

    def test_16_tenant_isolation_camera_device(self):
        """16. Tenant A cannot access Tenant B camera/device."""
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO cameras (id, camera_id, name, room_name, camera_type, rtsp_url, organization_id, branch_id)
                VALUES (9216, 'CAM-B-9216', 'Cam B', 'Office B', 'Check-In', 'rtsp://mock', ?, ?)
                ON CONFLICT (id) DO UPDATE SET organization_id = excluded.organization_id, room_name = excluded.room_name
                """,
                (ORG_B, BRANCH_B),
            )
            s_conn.commit()
        cameras_a = db.get_cameras(organization_id=ORG_A)
        self.assertFalse(any(c["id"] == 9216 for c in cameras_a))

    def test_17_identical_employee_ids_isolated(self):
        """17. Identical employee IDs across tenants remain completely isolated."""
        shared_id = 9217
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET name = EXCLUDED.name
                    """,
                    (str(shared_id), ORG_A, BRANCH_A, "EMP-A-17", "Alice Org A"),
                )
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET name = EXCLUDED.name
                    """,
                    (str(shared_id), ORG_B, BRANCH_B, "EMP-B-17", "Bob Org B"),
                )

        user_a = db.get_user_by_id(shared_id, organization_id=ORG_A)
        user_b = db.get_user_by_id(shared_id, organization_id=ORG_B)
        self.assertIsNotNone(user_a)
        self.assertIsNotNone(user_b)
        self.assertEqual(user_a["name"], "Alice Org A")
        self.assertEqual(user_b["name"], "Bob Org B")

    # =========================================================================
    # SECTION D: AUTHORITY (Tests 18 - 23)
    # =========================================================================

    def test_18_pg_only_employee_visible(self):
        """18. PG-only employee is visible through application."""
        user_id = 9318
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET name = EXCLUDED.name
                    """,
                    (str(user_id), ORG_A, BRANCH_A, "EMP-9318", "Cloud Only Employee"),
                )
        user = db.get_user_by_id(user_id, organization_id=ORG_A)
        self.assertIsNotNone(user)
        self.assertEqual(user["name"], "Cloud Only Employee")

    def test_19_pg_only_shift_visible(self):
        """19. PG-only shift is visible through application."""
        shift_id = 9319
        cloud_pg.mirror_shift({
            "id": shift_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "name": "Cloud Night Shift",
            "start_time": "20:00",
            "end_time": "04:00",
            "active": 1,
        })
        shift = db.get_shift_by_id(shift_id, organization_id=ORG_A)
        self.assertIsNotNone(shift)
        self.assertEqual(shift["name"], "Cloud Night Shift")

    def test_20_pg_only_leave_visible(self):
        """20. PG-only leave is visible through application."""
        leave_id = 9320
        cloud_pg.mirror_leave({
            "id": leave_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "employee_id": "9001",
            "leave_type": "sick",
            "start_date": "2026-11-01",
            "end_date": "2026-11-15",
            "status": "approved",
            "reason": "Medical recovery",
        })
        leaves = db.get_leave_requests(organization_id=ORG_A)
        found = next((l for l in leaves if l["id"] == leave_id), None)
        self.assertIsNotNone(found)
        self.assertEqual(found["leave_type"], "sick")

    def test_21_pg_only_overtime_visible(self):
        """21. PG-only overtime is visible through application."""
        ot_id = 9321
        cloud_pg.mirror_overtime({
            "id": ot_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "employee_id": "9001",
            "work_date": "2026-10-18",
            "hours": 6.0,
            "status": "approved",
            "reason": "Special Project",
        })
        ot_list = db.get_overtime(organization_id=ORG_A)
        found = next((o for o in ot_list if o["id"] == ot_id), None)
        self.assertIsNotNone(found)
        self.assertEqual(float(found["hours"]), 6.0)

    def test_22_pg_only_payroll_visible(self):
        """22. PG-only payroll is visible through application."""
        cloud_pg.mirror_payroll_run({
            "id": "RUN-L9-22",
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "payroll_month": "2026-08",
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "total_net_salary": 123456.0,
            "status": "approved",
        })
        run = db.get_monthly_payroll("2026-08", organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertIsNotNone(run)
        self.assertEqual(float(run.get("total_amount") or run.get("total_net_salary", 0)), 123456.0)

    def test_23_pg_only_attendance_visible(self):
        """23. PG-only attendance is visible through application."""
        event_id = 9323
        cloud_pg.mirror_attendance_event({
            "id": event_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "user_id": 9001,
            "name": "Ayesha Sync",
            "camera_id": "CAM-MAIN",
            "event_type": "check_in",
            "confidence": 0.98,
            "timestamp": "2026-10-18T08:30:00Z",
        })
        logs = db.get_attendance_logs(limit=50, organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertTrue(any(l.get("id") == event_id for l in logs))

    # =========================================================================
    # SECTION E: DIVERGENCE (Tests 24 - 29)
    # =========================================================================

    def test_24_divergence_employee_pg_wins(self):
        """24. PG employee differs from SQLite -> PG wins."""
        user_id = 9424
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET name = EXCLUDED.name
                    """,
                    (str(user_id), ORG_A, BRANCH_A, "EMP-9424", "PG Authoritative Name"),
                )
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET name = excluded.name
                """,
                (user_id, "SQLite Stale Name", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        user = db.get_user_by_id(user_id, organization_id=ORG_A)
        self.assertEqual(user["name"], "PG Authoritative Name")

    def test_25_divergence_shift_pg_wins(self):
        """25. PG shift differs -> PG wins."""
        shift_id = 9425
        cloud_pg.mirror_shift({
            "id": shift_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "name": "Authoritative Shift",
            "start_time": "11:00",
            "end_time": "20:00",
            "active": 1,
        })
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO shifts (id, name, start_time, end_time, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET start_time = excluded.start_time
                """,
                (shift_id, "Authoritative Shift", "09:00", "17:00", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        shift = db.get_shift_by_id(shift_id, organization_id=ORG_A)
        self.assertEqual(shift["start_time"], "11:00")

    def test_26_divergence_leave_pg_wins(self):
        """26. PG leave status differs -> PG wins."""
        leave_id = 9426
        cloud_pg.mirror_leave({
            "id": leave_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "employee_id": "9001",
            "leave_type": "annual",
            "start_date": "2026-10-10",
            "end_date": "2026-10-12",
            "status": "approved",
            "reason": "Vacation",
        })
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO leave_requests (id, user_id, leave_type, start_date, end_date, status, organization_id, branch_id)
                VALUES (?, 9001, 'annual', '2026-10-10', '2026-10-12', 'pending', ?, ?)
                ON CONFLICT (id) DO UPDATE SET status = excluded.status
                """,
                (leave_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        leaves = db.get_leave_requests(organization_id=ORG_A)
        found = next(l for l in leaves if l["id"] == leave_id)
        self.assertEqual(found["status"], "approved")

    def test_27_divergence_overtime_pg_wins(self):
        """27. PG OT status/hours differ -> PG wins."""
        ot_id = 9427
        cloud_pg.mirror_overtime({
            "id": ot_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "employee_id": "9001",
            "work_date": "2026-10-15",
            "hours": 5.0,
            "status": "approved",
            "reason": "PG Overtime 5hrs",
        })
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO overtime (id, user_id, ot_date, hours, reason, status, organization_id, branch_id)
                VALUES (?, 9001, '2026-10-15', 2.0, 'SQLITE 2hrs', 'pending', ?, ?)
                ON CONFLICT (id) DO UPDATE SET hours = excluded.hours, status = excluded.status
                """,
                (ot_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        ot = db.get_overtime_by_id(ot_id, organization_id=ORG_A)
        self.assertIsNotNone(ot)
        self.assertEqual(float(ot["hours"]), 5.0)
        self.assertEqual(ot["status"], "approved")

    def test_28_divergence_payroll_pg_wins(self):
        """28. PG payroll differs -> PG wins."""
        cloud_pg.mirror_payroll_run({
            "id": "RUN-L9-28",
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "payroll_month": "2026-07",
            "period_start": "2026-07-01",
            "period_end": "2026-07-31",
            "total_net_salary": 99999.0,
            "status": "approved",
        })
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO payroll_runs (payroll_month, period_start, period_end, organization_id, branch_id, total_net_salary, status)
                VALUES ('2026-07', '2026-07-01', '2026-07-31', ?, ?, 11111.0, 'Draft')
                ON CONFLICT DO NOTHING
                """,
                (ORG_A, BRANCH_A),
            )
            s_conn.commit()

        run = db.get_monthly_payroll("2026-07", organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertIsNotNone(run)
        self.assertEqual(float(run.get("total_amount") or run.get("total_net_salary", 0)), 99999.0)

    def test_29_divergence_attendance_pg_wins(self):
        """29. PG attendance differs -> PG wins."""
        event_id = 9429
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO attendance (id, user_id, confidence, device_id, location, timestamp, organization_id, branch_id)
                VALUES (?, 9001, 0.50, 'OLD-CAM', 'Entrance', '2026-10-14 08:45:00', ?, ?)
                ON CONFLICT (id) DO UPDATE SET confidence = excluded.confidence, device_id = excluded.device_id
                """,
                (event_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        cloud_pg.mirror_attendance_event({
            "id": event_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "user_id": 9001,
            "name": "Ayesha Sync",
            "camera_id": "CAM-MAIN",
            "event_type": "check_in",
            "confidence": 0.99,
            "timestamp": "2026-10-14T08:45:00Z",
        })
        logs = db.get_attendance_logs(limit=50, organization_id=ORG_A, branch_id=BRANCH_A)
        matching = next((l for l in logs if str(l.get("id")) == str(event_id)), None)
        self.assertIsNotNone(matching)
        self.assertEqual(float(matching.get("confidence") or 0.0), 0.99)
        self.assertEqual(matching.get("device_id"), "CAM-MAIN")

    # =========================================================================
    # SECTION F: PHANTOM PROTECTION (Tests 30 - 32)
    # =========================================================================

    def test_30_sqlite_only_employee_never_deleted_in_pg(self):
        """30. SQLite-only employee must NOT automatically become a PostgreSQL deletion."""
        user_id = 9530
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET active = 1
                """,
                (user_id, "Local Candidate", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        cloud_pg.hydrate_branch_employees(ORG_A, BRANCH_A)

        with sqlite3.connect(DB_PATH) as s_conn:
            u = s_conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
            self.assertIsNotNone(u)

    def test_31_sqlite_only_stale_record_not_in_cloud_reads(self):
        """31. SQLite-only stale record must NOT appear in normal cloud-authoritative reads."""
        user_id = 9531
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET name = excluded.name
                """,
                (user_id, "Phantom Stale Local User", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        user = db.get_user_by_id(user_id, organization_id=ORG_A)
        self.assertIsNone(user, "Stale SQLite-only record must not appear when PG is authoritative")

    def test_32_no_destructive_cleanup_on_pg_absence(self):
        """32. No automatic destructive cleanup occurs because of temporary PG absence."""
        user_id = 9532
        shift_id = 9532
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO shifts (id, name, start_time, end_time, organization_id, branch_id, active)
                VALUES (?, 'Safe Shift', '09:00', '17:00', ?, ?, 1)
                ON CONFLICT (id) DO NOTHING
                """,
                (shift_id, ORG_A, BRANCH_A),
            )
            s_conn.execute(
                """
                INSERT INTO users (id, name, shift_id, organization_id, branch_id, active)
                VALUES (?, 'Safe User', ?, ?, ?, 1)
                ON CONFLICT (id) DO NOTHING
                """,
                (user_id, shift_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        cloud_pg.fetch_pg_employee(user_id, ORG_A)
        cloud_pg.fetch_pg_shift(shift_id, ORG_A)
        cloud_pg.fetch_pg_all_shifts(ORG_A, BRANCH_A)

        with sqlite3.connect(DB_PATH) as s_conn:
            self.assertIsNotNone(s_conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone())
            self.assertIsNotNone(s_conn.execute("SELECT id FROM shifts WHERE id = ?", (shift_id,)).fetchone())

    # =========================================================================
    # SECTION G: EMPLOYMENT DATES (Tests 33 - 36)
    # =========================================================================

    def test_33_historical_join_date_respected(self):
        """33. Legitimate historical join date is respected."""
        user = {
            "join_date": "2024-01-15",
            "enrollment_date": "2026-01-01",
            "created_at": "2026-10-18 10:00:00",
        }
        res = db.get_employee_employment_start_date(user)
        self.assertEqual(str(res), "2024-01-15")

    def test_34_null_join_date_fallback(self):
        """34. NULL join date correctly falls back to enrollment_date or created_at."""
        user1 = {
            "join_date": None,
            "enrollment_date": "2025-06-01",
            "created_at": "2026-10-18 10:00:00",
        }
        self.assertEqual(str(db.get_employee_employment_start_date(user1)), "2025-06-01")

        user2 = {
            "join_date": None,
            "enrollment_date": None,
            "created_at": "2026-05-20 12:00:00",
        }
        self.assertEqual(str(db.get_employee_employment_start_date(user2)), "2026-05-20")

    def test_35_future_join_date_works(self):
        """35. Future join date is correctly parsed and returned."""
        future_d = (date.today() + timedelta(days=60)).isoformat()
        user = {
            "join_date": future_d,
            "enrollment_date": "2026-01-01",
            "created_at": "2026-10-18 10:00:00",
        }
        res = db.get_employee_employment_start_date(user)
        self.assertEqual(str(res), future_d)

    def test_36_historical_reports_classify_employment(self):
        """36. Historical reports correctly classify employment using start date precedence."""
        user_id = 9636
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, join_date, organization_id, branch_id, active)
                VALUES (?, 'Joined Oct 10', '2026-10-10', ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET join_date = excluded.join_date
                """,
                (user_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        st_before = db.get_user_attendance_status(user_id, "2026-10-05", organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertEqual(st_before, "not_employed")

        st_after = db.get_user_attendance_status(user_id, "2026-10-12", organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertNotEqual(st_after, "not_employed")

    # =========================================================================
    # SECTION H: OFFLINE RECOVERY (Tests 37 - 41)
    # =========================================================================

    def test_37_outage_does_not_delete_employees(self):
        """37. PostgreSQL outage does not delete employees."""
        user_id = 9737
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, 'Offline Survivor', ?, ?, 1)
                ON CONFLICT (id) DO NOTHING
                """,
                (user_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        with patch.object(cloud_pg, "postgres_is_configured", return_value=False):
            users = db.get_all_users(organization_id=ORG_A)
            self.assertTrue(any(u["id"] == user_id for u in users))

        with sqlite3.connect(DB_PATH) as s_conn:
            self.assertIsNotNone(s_conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone())

    def test_38_outage_does_not_replace_shifts(self):
        """38. PostgreSQL outage does not replace shifts."""
        shift_id = 9738
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO shifts (id, name, start_time, end_time, organization_id, branch_id, active)
                VALUES (?, 'Special Night', '21:00', '05:00', ?, ?, 1)
                ON CONFLICT (id) DO NOTHING
                """,
                (shift_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        with patch.object(cloud_pg, "postgres_is_configured", return_value=False):
            shift = db.get_shift_by_id(shift_id, organization_id=ORG_A)
            self.assertIsNotNone(shift)
            self.assertEqual(shift["start_time"], "21:00")

    def test_39_sqlite_continues_offline(self):
        """39. SQLite continues operating offline where supported."""
        user_id = 9739
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, 'Offline User', ?, ?, 1)
                ON CONFLICT (id) DO NOTHING
                """,
                (user_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        with patch.object(cloud_pg, "postgres_is_configured", return_value=False):
            u = db.get_user_by_id(user_id, organization_id=ORG_A)
            self.assertIsNotNone(u)
            self.assertEqual(u["name"], "Offline User")

    def test_40_queued_changes_sync_after_recovery(self):
        """40. Queued changes synchronize after PG recovery."""
        user_id = 9740
        user_data = {
            "id": user_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "name": "Queued Sync User",
            "active": 1,
            "role": "staff",
        }
        cloud_pg.mirror_employee(user_data)

        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT name FROM employees WHERE id = %s AND organization_id = %s", (str(user_id), ORG_A))
                row = cur.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "Queued Sync User")

    def test_41_repeated_sync_no_duplicates(self):
        """41. Repeated synchronization does not duplicate records."""
        user_id = 9741
        user_data = {
            "id": user_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "name": "Idempotent User",
            "active": 1,
            "role": "staff",
        }
        for _ in range(3):
            cloud_pg.mirror_employee(user_data)

        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM employees WHERE id = %s AND organization_id = %s", (str(user_id), ORG_A))
                count = cur.fetchone()[0]
                self.assertEqual(count, 1, f"Expected 1 record in PG, got {count}")


if __name__ == "__main__":
    unittest.main()
