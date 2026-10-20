import os
import sqlite3
import sys
import tempfile
import unittest

_LOCAL_DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _LOCAL_DASHBOARD_DIR)

import database as db

class ShiftsCamerasTenantMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="quanteye-shifts-cameras-")
        self.db_path = os.path.join(self.temp_dir.name, "test_sc.db")
        self.original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_legacy_shifts_migration_scopes_uniqueness_to_organization(self):
        # 1. Create a legacy single-tenant shifts table with UNIQUE(name)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    grace_period_minutes INTEGER DEFAULT 15,
                    active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    organization_id TEXT DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT DEFAULT 'DEFAULT_BRANCH'
                )
            """)
            conn.execute("INSERT INTO shifts (id, name, start_time, end_time, organization_id) VALUES (1, 'Morning Shift', '09:00', '17:00', 'ORG-1')")
            conn.commit()

        # 2. Run init_db() to trigger automatic migration
        self.assertTrue(db.init_db())

        # 3. Verify that another organization can now create 'Morning Shift'
        shift2_id = db.add_shift(
            name='Morning Shift',
            start_time='09:00',
            end_time='17:00',
            organization_id='ORG-2',
            branch_id='BR-2'
        )
        self.assertIsNotNone(shift2_id)
        self.assertNotEqual(shift2_id, 1)

        # 4. Verify that duplicate within the SAME organization is rejected
        shift3_id = db.add_shift(
            name='Morning Shift',
            start_time='09:00',
            end_time='17:00',
            organization_id='ORG-2',
            branch_id='BR-2'
        )
        self.assertIsNone(shift3_id)

    def test_legacy_cameras_migration_scopes_uniqueness_to_organization(self):
        # 1. Create a legacy single-tenant cameras table with UNIQUE(camera_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE cameras (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    room_name TEXT NOT NULL,
                    camera_type TEXT NOT NULL,
                    rtsp_url TEXT NOT NULL,
                    active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    organization_id TEXT DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT DEFAULT 'DEFAULT_BRANCH'
                )
            """)
            conn.execute("INSERT INTO cameras (id, camera_id, name, room_name, camera_type, rtsp_url, organization_id) VALUES (1, 'CAM-01', 'Front Gate', 'Main', 'ip_camera', '', 'ORG-1')")
            conn.commit()

        # 2. Run init_db() to trigger automatic migration
        self.assertTrue(db.init_db())

        # 3. Verify that another organization can create a camera with camera_id 'CAM-01'
        cam2_id = db.add_camera(
            camera_id='CAM-01',
            name='Reception Camera',
            room_name='Lobby',
            organization_id='ORG-2'
        )
        self.assertIsNotNone(cam2_id)
        self.assertNotEqual(cam2_id, 1)

        # 4. Verify that duplicate camera_id within the SAME organization is rejected
        cam3_id = db.add_camera(
            camera_id='CAM-01',
            name='Second Reception',
            room_name='Lobby',
            organization_id='ORG-2'
        )
        self.assertIsNone(cam3_id)

    def test_payroll_records_has_deductions_column(self):
        self.assertTrue(db.init_db())
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(payroll_records)")
            cols = [c[1] for c in cur.fetchall()]
            self.assertIn('deductions', cols)
            self.assertIn('deduction_amount', cols)

if __name__ == '__main__':
    unittest.main()
