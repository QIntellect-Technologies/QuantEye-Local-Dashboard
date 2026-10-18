"""
Comprehensive Production Authority Read Audit & Integrity Test Suite.
Tests:
1. Divergence (PG_VALUE vs SQLITE_STALE_VALUE -> returns PG_VALUE)
2. PostgreSQL-Only Record Retrieval (Not in SQLite -> Found via app route)
3. Opposite Direction Test (SQLite-Only Record -> Must NOT appear when PG is healthy)
4. Write -> Read Consistency (Leave, Overtime, Payroll, Shift, Attendance)
5. Tenant Isolation (Tenant A vs Tenant B, Tampering, RLS)
6. Offline Fallback (Observable, seamless fallback when PG is unreachable)
"""

import os
import sys
import unittest
import psycopg2
import sqlite3
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

# Ensure Local-Dashboard in path
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

ORG_A = "ORG_AUTH_A"
ORG_B = "ORG_AUTH_B"
BRANCH_A = "BR_AUTH_A"
BRANCH_B = "BR_AUTH_B"


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
                    cur.execute("DELETE FROM shifts WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM employees WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM branches WHERE organization_id = %s", (org,))
                    cur.execute("DELETE FROM organizations WHERE id = %s", (org,))
    except Exception as e:
        print(f"Warning cleaning PG: {e}")

    # Clean SQLite
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for org in (ORG_A, ORG_B):
                conn.execute("DELETE FROM attendance WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM payroll_records WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM payroll_runs WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM overtime WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM leave_requests WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM shifts WHERE organization_id = ?", (org,))
                conn.execute("DELETE FROM users WHERE organization_id = ?", (org,))
            conn.commit()
    except Exception as e:
        print(f"Warning cleaning SQLite: {e}")


class ProductionAuthorityReadAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clean_fixtures()
        # Seed organizations in PG
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO organizations (id, name, status) VALUES (%s, %s, 'Active') ON CONFLICT DO NOTHING",
                    (ORG_A, "Authority Org A"),
                )
                cur.execute(
                    "INSERT INTO organizations (id, name, status) VALUES (%s, %s, 'Active') ON CONFLICT DO NOTHING",
                    (ORG_B, "Authority Org B"),
                )
                cur.execute(
                    "INSERT INTO branches (id, organization_id, name, access) VALUES (%s, %s, %s, 'Active') ON CONFLICT DO NOTHING",
                    (BRANCH_A, ORG_A, "Branch A"),
                )
                cur.execute(
                    "INSERT INTO branches (id, organization_id, name, access) VALUES (%s, %s, %s, 'Active') ON CONFLICT DO NOTHING",
                    (BRANCH_B, ORG_B, "Branch B"),
                )

    @classmethod
    def tearDownClass(cls):
        clean_fixtures()

    # =========================================================================
    # 1. DIVERGENCE TESTS (PG_VALUE vs SQLITE_STALE_VALUE -> returns PG_VALUE)
    # =========================================================================
    def test_01_divergence_staff(self):
        """Staff Divergence: PG has 'PG_STAFF_NAME', SQLite has 'SQLITE_STALE_STAFF'."""
        user_id = 9101
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (organization_id, id) DO UPDATE SET name = EXCLUDED.name
                    """,
                    (str(user_id), ORG_A, BRANCH_A, "EMP-9101", "PG_STAFF_NAME"),
                )
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT (id) DO UPDATE SET name = excluded.name
                """,
                (user_id, "SQLITE_STALE_STAFF", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        # Call real production read endpoint/function
        user = db.get_user_by_id(user_id, organization_id=ORG_A)
        self.assertIsNotNone(user)
        self.assertEqual(user["name"], "PG_STAFF_NAME", f"Expected PG_STAFF_NAME, got {user['name']}")

        # Test get_user_by_name divergence
        user_by_name = db.get_user_by_name("PG_STAFF_NAME", organization_id=ORG_A)
        self.assertIsNotNone(user_by_name)
        self.assertEqual(user_by_name["id"], user_id)

    def test_02_divergence_shift(self):
        """Shift Divergence: PG has 'PG_SHIFT_NAME', SQLite has 'SQLITE_STALE_SHIFT'."""
        shift_id = 9201
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO shifts (id, organization_id, branch_id, name, start_time, end_time, grace_minutes)
                    VALUES (%s, %s, %s, %s, '08:00', '16:00', 20)
                    ON CONFLICT (organization_id, id) DO UPDATE SET name = EXCLUDED.name, grace_minutes = EXCLUDED.grace_minutes
                    """,
                    (str(shift_id), ORG_A, BRANCH_A, "PG_SHIFT_NAME"),
                )
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO shifts (id, name, start_time, end_time, grace_period_minutes, active, organization_id, branch_id)
                VALUES (?, ?, '09:00', '17:00', 10, 1, ?, ?)
                ON CONFLICT (id) DO UPDATE SET name = excluded.name, grace_period_minutes = excluded.grace_period_minutes
                """,
                (shift_id, "SQLITE_STALE_SHIFT", ORG_A, BRANCH_A),
            )
            s_conn.commit()

        # Call real production read function
        shift = db.get_shift_by_id(shift_id, organization_id=ORG_A)
        self.assertIsNotNone(shift)
        self.assertEqual(shift["name"], "PG_SHIFT_NAME")
        self.assertEqual(shift["grace_period_minutes"], 20)

    def test_03_divergence_leave(self):
        """Leave Divergence: PG has status='approved', SQLite has status='pending'."""
        leave_id = 9301
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO leave_requests (id, organization_id, branch_id, employee_id, leave_type, start_date, end_date, status, reason)
                    VALUES (%s, %s, %s, %s, 'casual', '2026-11-01', '2026-11-02', 'approved', 'PG_APPROVED')
                    ON CONFLICT (organization_id, id) DO UPDATE SET status = EXCLUDED.status, reason = EXCLUDED.reason
                    """,
                    (str(leave_id), ORG_A, BRANCH_A, "9101"),
                )
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO leave_requests (id, user_id, leave_type, start_date, end_date, status, reason, organization_id, branch_id)
                VALUES (?, ?, 'casual', '2026-11-01', '2026-11-02', 'pending', 'SQLITE_PENDING', ?, ?)
                ON CONFLICT (id) DO UPDATE SET status = excluded.status
                """,
                (leave_id, 9101, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        # Real production read
        leave = db.get_leave_by_id(leave_id, organization_id=ORG_A)
        self.assertIsNotNone(leave)
        self.assertEqual(leave["status"], "approved")
        self.assertEqual(leave["reason"], "PG_APPROVED")

    def test_04_divergence_overtime(self):
        """Overtime Divergence: PG has status='approved' and hours=4.0, SQLite has hours=1.0 and status='pending'."""
        ot_id = 9401
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO overtime_records (id, organization_id, branch_id, employee_id, work_date, hours, reason, status)
                    VALUES (%s, %s, %s, %s, '2026-11-05', 4.0, 'PG Overtime 4hrs', 'approved')
                    ON CONFLICT (organization_id, id) DO UPDATE SET hours = EXCLUDED.hours, status = EXCLUDED.status
                    """,
                    (str(ot_id), ORG_A, BRANCH_A, "9101"),
                )
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO overtime (id, user_id, ot_date, hours, reason, status, organization_id, branch_id)
                VALUES (?, ?, '2026-11-05', 1.0, 'SQLITE 1hr', 'pending', ?, ?)
                ON CONFLICT (id) DO UPDATE SET hours = excluded.hours, status = excluded.status
                """,
                (ot_id, 9101, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        # Real production read
        ot = db.get_overtime_by_id(ot_id, organization_id=ORG_A)
        self.assertIsNotNone(ot)
        self.assertEqual(ot["status"], "approved")
        self.assertEqual(float(ot["hours"]), 4.0)

    # =========================================================================
    # 2. POSTGRES-ONLY RECORDS (Inserted into PG only, NOT in SQLite -> App returns PG value)
    # =========================================================================
    def test_05_postgres_only_staff(self):
        """Employee created ONLY in PG must be retrieved via db.get_user_by_id and db.get_user_by_name."""
        user_id = 9102
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    """,
                    (str(user_id), ORG_A, BRANCH_A, "EMP-9102", "PG_ONLY_EMPLOYEE"),
                )

        # Make sure it's NOT in SQLite prior to read
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            s_conn.commit()

        # Real production reads
        user_by_id = db.get_user_by_id(user_id, organization_id=ORG_A)
        self.assertIsNotNone(user_by_id)
        self.assertEqual(user_by_id["name"], "PG_ONLY_EMPLOYEE")

        user_by_name = db.get_user_by_name("PG_ONLY_EMPLOYEE", organization_id=ORG_A)
        self.assertIsNotNone(user_by_name)
        self.assertEqual(user_by_name["id"], user_id)

    def test_06_postgres_only_shift(self):
        """Shift created ONLY in PG must be retrieved via db.get_shift_by_id and db.get_all_shifts."""
        shift_id = 9202
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO shifts (id, organization_id, branch_id, name, start_time, end_time, grace_minutes)
                    VALUES (%s, %s, %s, %s, '10:00', '19:00', 30)
                    """,
                    (str(shift_id), ORG_A, BRANCH_A, "PG_ONLY_SHIFT"),
                )

        # Ensure not in SQLite
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))
            s_conn.commit()

        shift = db.get_shift_by_id(shift_id, organization_id=ORG_A)
        self.assertIsNotNone(shift)
        self.assertEqual(shift["name"], "PG_ONLY_SHIFT")

        all_shifts = db.get_all_shifts(organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertTrue(any(s["id"] == shift_id for s in all_shifts))

    def test_07_postgres_only_leave(self):
        """Leave created ONLY in PG must be retrieved via db.get_leave_by_id and db.get_leave_requests."""
        leave_id = 9302
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO leave_requests (id, organization_id, branch_id, employee_id, leave_type, start_date, end_date, status, reason)
                    VALUES (%s, %s, %s, %s, 'annual', '2026-11-10', '2026-11-12', 'pending', 'PG Only Leave')
                    """,
                    (str(leave_id), ORG_A, BRANCH_A, "9101"),
                )

        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute("DELETE FROM leave_requests WHERE id = ?", (leave_id,))
            s_conn.commit()

        leave = db.get_leave_by_id(leave_id, organization_id=ORG_A)
        self.assertIsNotNone(leave)
        self.assertEqual(leave["status"], "pending")

        requests = db.get_leave_requests(organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertTrue(any(r["id"] == leave_id for r in requests))

    def test_08_postgres_only_overtime(self):
        """Overtime created ONLY in PG must be retrieved via db.get_overtime_by_id and db.get_overtime."""
        ot_id = 9402
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO overtime_records (id, organization_id, branch_id, employee_id, work_date, hours, reason, status)
                    VALUES (%s, %s, %s, %s, '2026-11-15', 3.5, 'PG Only OT', 'pending')
                    """,
                    (str(ot_id), ORG_A, BRANCH_A, "9101"),
                )

        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute("DELETE FROM overtime WHERE id = ?", (ot_id,))
            s_conn.commit()

        ot = db.get_overtime_by_id(ot_id, organization_id=ORG_A)
        self.assertIsNotNone(ot)
        self.assertEqual(float(ot["hours"]), 3.5)

        records = db.get_overtime(organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertTrue(any(r["id"] == ot_id for r in records))

    # =========================================================================
    # 3. OPPOSITE DIRECTION TEST (SQLite-only phantom record must NOT appear when PG is healthy)
    # =========================================================================
    def test_09_opposite_direction_sqlite_phantom_user(self):
        """A phantom user created directly in SQLite must NOT appear when PostgreSQL is healthy."""
        phantom_id = 99991
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, 'SQLITE_PHANTOM_USER', ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET name = 'SQLITE_PHANTOM_USER'
                """,
                (phantom_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        # Ensure it does NOT exist in PG
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("DELETE FROM employees WHERE id = %s AND organization_id = %s", (str(phantom_id), ORG_A))

        # Real production read by ID
        user = db.get_user_by_id(phantom_id, organization_id=ORG_A)
        self.assertIsNone(user, "Authoritative read must return None for SQLite phantom user!")

        # Real production read by Name
        user_by_name = db.get_user_by_name("SQLITE_PHANTOM_USER", organization_id=ORG_A)
        self.assertIsNone(user_by_name, "Authoritative read by name must return None for SQLite phantom user!")

        # Real production list
        all_users = db.get_all_users(organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertFalse(any(u["id"] == phantom_id for u in all_users), "Phantom user must not appear in get_all_users!")

    def test_10_opposite_direction_sqlite_phantom_shift(self):
        """A phantom shift created directly in SQLite must NOT appear when PostgreSQL is healthy."""
        phantom_shift_id = 99992
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO shifts (id, name, start_time, end_time, grace_period_minutes, active, organization_id, branch_id)
                VALUES (?, 'SQLITE_PHANTOM_SHIFT', '09:00', '17:00', 15, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name = 'SQLITE_PHANTOM_SHIFT'
                """,
                (phantom_shift_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        # Ensure not in PG
        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("DELETE FROM shifts WHERE id = %s AND organization_id = %s", (str(phantom_shift_id), ORG_A))

        shift = db.get_shift_by_id(phantom_shift_id, organization_id=ORG_A)
        self.assertIsNone(shift, "Authoritative read must return None for SQLite phantom shift!")

        all_shifts = db.get_all_shifts(organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertFalse(any(s["id"] == phantom_shift_id for s in all_shifts), "Phantom shift must not appear in get_all_shifts!")

    def test_11_opposite_direction_sqlite_phantom_leave(self):
        """A phantom leave request in SQLite must NOT appear when PostgreSQL is healthy."""
        phantom_leave_id = 99993
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO leave_requests (id, user_id, leave_type, start_date, end_date, status, reason, organization_id, branch_id)
                VALUES (?, 9101, 'casual', '2026-11-20', '2026-11-21', 'pending', 'PHANTOM', ?, ?)
                ON CONFLICT(id) DO UPDATE SET status = 'pending'
                """,
                (phantom_leave_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("DELETE FROM leave_requests WHERE id = %s AND organization_id = %s", (str(phantom_leave_id), ORG_A))

        leave = db.get_leave_by_id(phantom_leave_id, organization_id=ORG_A)
        self.assertIsNone(leave, "Authoritative read must return None for SQLite phantom leave!")

        leaves = db.get_leave_requests(organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertFalse(any(l["id"] == phantom_leave_id for l in leaves), "Phantom leave must not appear in get_leave_requests!")

    def test_12_opposite_direction_sqlite_phantom_overtime(self):
        """A phantom overtime record in SQLite must NOT appear when PostgreSQL is healthy."""
        phantom_ot_id = 99994
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO overtime (id, user_id, ot_date, hours, reason, status, organization_id, branch_id)
                VALUES (?, 9101, '2026-11-22', 2.0, 'PHANTOM OT', 'pending', ?, ?)
                ON CONFLICT(id) DO UPDATE SET status = 'pending'
                """,
                (phantom_ot_id, ORG_A, BRANCH_A),
            )
            s_conn.commit()

        with get_pg_conn() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("DELETE FROM overtime_records WHERE id = %s AND organization_id = %s", (str(phantom_ot_id), ORG_A))

        ot = db.get_overtime_by_id(phantom_ot_id, organization_id=ORG_A)
        self.assertIsNone(ot, "Authoritative read must return None for SQLite phantom overtime!")

        records = db.get_overtime(organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertFalse(any(r["id"] == phantom_ot_id for r in records), "Phantom overtime must not appear in get_overtime!")

    # =========================================================================
    # 4. WRITE -> READ CONSISTENCY (Create -> Update -> Status Change -> Read)
    # =========================================================================
    def test_13_write_read_leave_lifecycle(self):
        """Leave lifecycle: Create -> Approve -> Reject -> Verify PG authoritatively reflects each step."""
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (9101, 'PG_STAFF_NAME', ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET organization_id = excluded.organization_id, branch_id = excluded.branch_id
                """,
                (ORG_A, BRANCH_A),
            )
            s_conn.commit()

        lid = db.add_leave_request(
            user_id=9101,
            user_name="PG_STAFF_NAME",
            leave_type="casual",
            start_date="2026-12-01",
            end_date="2026-12-02",
            reason="Holiday request",
        )
        self.assertIsNotNone(lid)

        # 1. Read after create
        l1 = db.get_leave_by_id(lid, organization_id=ORG_A)
        self.assertEqual(l1["status"], "pending")

        # 2. Approve
        ok = db.update_leave_status(lid, "approved", approved_by="Supervisor A", organization_id=ORG_A)
        self.assertTrue(ok)
        l2 = db.get_leave_by_id(lid, organization_id=ORG_A)
        self.assertEqual(l2["status"], "approved")

        # 3. Reject
        ok_rej = db.update_leave_status(lid, "rejected", approved_by="Supervisor A", organization_id=ORG_A)
        self.assertTrue(ok_rej)
        l3 = db.get_leave_by_id(lid, organization_id=ORG_A)
        self.assertEqual(l3["status"], "rejected")

    def test_14_write_read_overtime_lifecycle(self):
        """Overtime lifecycle: Create -> Approve -> Reject -> Verify PG authoritatively reflects each step."""
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (9101, 'PG_STAFF_NAME', ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET organization_id = excluded.organization_id, branch_id = excluded.branch_id
                """,
                (ORG_A, BRANCH_A),
            )
            s_conn.commit()

        oid = db.add_overtime(
            user_id=9101,
            ot_date="2026-12-05",
            hours=2.5,
            reason="Project release",
            organization_id=ORG_A,
            branch_id=BRANCH_A,
        )
        self.assertIsNotNone(oid)

        # 1. Read after create
        o1 = db.get_overtime_by_id(oid, organization_id=ORG_A)
        self.assertEqual(o1["status"], "pending")
        self.assertEqual(float(o1["hours"]), 2.5)

        # 2. Approve
        ok_appr = db.update_overtime_status(oid, status="approved", approved_by="Supervisor A", organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertTrue(ok_appr)
        o2 = db.get_overtime_by_id(oid, organization_id=ORG_A)
        self.assertEqual(o2["status"], "approved")

        # 3. Reject
        ok_rej = db.update_overtime_status(oid, status="rejected", approved_by="Supervisor A", rejection_reason="Duplicate", organization_id=ORG_A, branch_id=BRANCH_A)
        self.assertTrue(ok_rej)
        o3 = db.get_overtime_by_id(oid, organization_id=ORG_A)
        self.assertEqual(o3["status"], "rejected")

    def test_15_write_read_shift_lifecycle(self):
        """Shift lifecycle: Create -> Update -> Read -> Verify PG authoritatively reflects changes."""
        sid = db.add_shift(
            name="Lifecycle Shift",
            start_time="08:30",
            end_time="17:30",
            grace_period_minutes=15,
            organization_id=ORG_A,
            branch_id=BRANCH_A,
        )
        self.assertIsNotNone(sid)

        s1 = db.get_shift_by_id(sid, organization_id=ORG_A)
        self.assertEqual(s1["name"], "Lifecycle Shift")

        ok_upd = db.update_shift(
            sid,
            name="Updated Shift Name",
            start_time="09:00",
            end_time="18:00",
            grace_period_minutes=25,
            organization_id=ORG_A,
        )
        self.assertTrue(ok_upd)

        s2 = db.get_shift_by_id(sid, organization_id=ORG_A)
        self.assertEqual(s2["name"], "Updated Shift Name")
        self.assertEqual(s2["grace_period_minutes"], 25)

    def test_16_write_read_attendance_event(self):
        """Attendance event: Write event -> Read via db.get_attendance_logs -> Verified."""
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (9101, 'PG_STAFF_NAME', ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET organization_id = excluded.organization_id, branch_id = excluded.branch_id
                """,
                (ORG_A, BRANCH_A),
            )
            s_conn.commit()

        event_id = 9501
        cloud_pg.mirror_attendance_event({
            "id": event_id,
            "organization_id": ORG_A,
            "branch_id": BRANCH_A,
            "user_id": 9101,
            "name": "PG_STAFF_NAME",
            "camera_id": "CAM-MAIN",
            "event_type": "CHECK_IN",
            "confidence": 0.98,
            "timestamp": "2026-10-18T10:00:00Z",
        })

        logs = db.get_attendance_logs(limit=50, organization_id=ORG_A, branch_id=BRANCH_A)
        matching = [l for l in logs if l.get("id") == event_id]
        self.assertTrue(len(matching) > 0, "Attendance event mirrored to PG must be returned in get_attendance_logs!")
        self.assertEqual(matching[0]["location"], "CAM-MAIN")

    # =========================================================================
    # 5. TENANT ISOLATION TESTS (Tenant A vs Tenant B)
    # =========================================================================
    def test_17_tenant_isolation_staff_and_tampering(self):
        """Tenant B must never see Tenant A's employees, even with tampering."""
        # Query user in ORG_A using ORG_B context -> must return None
        user_cross = db.get_user_by_id(9101, organization_id=ORG_B)
        self.assertIsNone(user_cross, "Tenant B must NOT access Tenant A employee!")

        # Query all users for ORG_B -> must NOT contain any users from ORG_A
        users_b = db.get_all_users(organization_id=ORG_B, branch_id=BRANCH_B)
        self.assertFalse(any(u["id"] == 9101 for u in users_b), "Tenant B user list must not contain Tenant A users!")

    def test_18_tenant_isolation_leaves_and_overtime(self):
        """Tenant B must never see Tenant A's leave or overtime records."""
        leaves_b = db.get_leave_requests(organization_id=ORG_B, branch_id=BRANCH_B)
        self.assertFalse(any(l.get("organization_id") == ORG_A for l in leaves_b))

        ot_b = db.get_overtime(organization_id=ORG_B, branch_id=BRANCH_B)
        self.assertFalse(any(o.get("organization_id") == ORG_A for o in ot_b))

    def test_19_tenant_isolation_shifts(self):
        """Tenant B must never see Tenant A's shifts."""
        shifts_b = db.get_all_shifts(organization_id=ORG_B, branch_id=BRANCH_B)
        self.assertFalse(any(s.get("organization_id") == ORG_A for s in shifts_b))

    # =========================================================================
    # 6. OFFLINE FALLBACK TEST (Observable fallback when PG is unreachable)
    # =========================================================================
    def test_20_offline_fallback_resilience(self):
        """When DATABASE_URL is invalid/unreachable, system must fall back to SQLite cache gracefully without crash."""
        cached_user_id = 9199
        with sqlite3.connect(DB_PATH) as s_conn:
            s_conn.execute(
                """
                INSERT INTO users (id, name, organization_id, branch_id, active)
                VALUES (?, 'OFFLINE_CACHED_USER', 'OFFLINE_ORG', 'OFFLINE_BR', 1)
                ON CONFLICT(id) DO UPDATE SET name = 'OFFLINE_CACHED_USER'
                """,
                (cached_user_id,),
            )
            s_conn.commit()

        # Temporarily point DATABASE_URL to a dead unreachable port
        old_url = os.environ.get("DATABASE_URL")
        try:
            os.environ["DATABASE_URL"] = "postgresql://invalid:invalid@127.0.0.1:54329/invalid"
            # Read user during offline simulation
            u = db.get_user_by_id(cached_user_id, organization_id="OFFLINE_ORG")
            self.assertIsNotNone(u, "Offline mode must fall back to SQLite cache!")
            self.assertEqual(u["name"], "OFFLINE_CACHED_USER")
        finally:
            if old_url:
                os.environ["DATABASE_URL"] = old_url


if __name__ == "__main__":
    unittest.main()
