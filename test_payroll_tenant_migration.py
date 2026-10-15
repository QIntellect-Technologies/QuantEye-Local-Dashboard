import os
import sqlite3
import sys
import tempfile
import unittest

_LOCAL_DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_LOCAL_DASHBOARD_DIR)
sys.path.insert(0, _LOCAL_DASHBOARD_DIR)

for _module_name, _module in list(sys.modules.items()):
    _module_file = getattr(_module, "__file__", None)
    if not _module_file:
        continue
    _module_path = os.path.normcase(os.path.abspath(_module_file))
    if not _module_path.startswith(os.path.normcase(_PROJECT_ROOT + os.sep)):
        continue
    if not _module_path.startswith(os.path.normcase(_LOCAL_DASHBOARD_DIR + os.sep)):
        del sys.modules[_module_name]

import database as db


class PayrollTenantMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="quanteye-payroll-migration-")
        self.db_path = os.path.join(self.temp_dir.name, "legacy.db")
        self.original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE payroll_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payroll_month TEXT NOT NULL UNIQUE,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    employee_count INTEGER NOT NULL DEFAULT 0,
                    total_basic_salary REAL NOT NULL DEFAULT 0,
                    total_deduction REAL NOT NULL DEFAULT 0,
                    total_net_salary REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'Finalized',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finalized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_allowances REAL NOT NULL DEFAULT 0,
                    total_overtime REAL NOT NULL DEFAULT 0,
                    organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH'
                );
                CREATE TABLE payroll_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payroll_run_id INTEGER NOT NULL,
                    employee_id INTEGER NOT NULL,
                    employee_name TEXT NOT NULL,
                    FOREIGN KEY(payroll_run_id) REFERENCES payroll_runs(id)
                );
                CREATE TRIGGER prevent_finalized_payroll_record_insert
                BEFORE INSERT ON payroll_records
                WHEN EXISTS (
                    SELECT 1 FROM payroll_runs
                    WHERE id = NEW.payroll_run_id AND status = 'Finalized'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'Finalized payroll records are immutable');
                END;
                CREATE TRIGGER prevent_finalized_payroll_record_update
                BEFORE UPDATE ON payroll_records
                WHEN EXISTS (
                    SELECT 1 FROM payroll_runs
                    WHERE id = OLD.payroll_run_id AND status = 'Finalized'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'Finalized payroll records are immutable');
                END;
                CREATE TRIGGER prevent_finalized_payroll_record_delete
                BEFORE DELETE ON payroll_records
                WHEN EXISTS (
                    SELECT 1 FROM payroll_runs
                    WHERE id = OLD.payroll_run_id AND status = 'Finalized'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'Finalized payroll records are immutable');
                END;
                INSERT INTO payroll_runs (
                    id, payroll_month, period_start, period_end, employee_count,
                    total_basic_salary, total_deduction, total_net_salary,
                    total_allowances, total_overtime, organization_id, branch_id, status
                ) VALUES (
                    7, '2026-10', '2026-10-01', '2026-10-31', 1,
                    5000, 100, 4900, 250, 50, 'ORG-A', 'BR-A1', 'Processing'
                );
                INSERT INTO payroll_records (payroll_run_id, employee_id, employee_name)
                VALUES (7, 10, 'Existing Employee');
                UPDATE payroll_runs SET status = 'Finalized' WHERE id = 7;
                CREATE TABLE payroll_runs_scoped (id INTEGER PRIMARY KEY);
            """)

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_migration_preserves_rows_and_scopes_month_uniqueness(self):
        self.assertTrue(db.init_db())
        with sqlite3.connect(self.db_path) as conn:
            original = conn.execute(
                "SELECT id, payroll_month, organization_id, branch_id FROM payroll_runs WHERE id = 7"
            ).fetchone()
            self.assertEqual(original, (7, "2026-10", "ORG-A", "BR-A1"))
            saved_record = conn.execute(
                "SELECT payroll_run_id, employee_id, employee_name FROM payroll_records"
            ).fetchone()
            self.assertEqual(saved_record, (7, 10, "Existing Employee"))
            trigger_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
            self.assertTrue({
                "prevent_finalized_payroll_record_insert",
                "prevent_finalized_payroll_record_update",
                "prevent_finalized_payroll_record_delete",
            }.issubset(trigger_names))
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

            for organization_id, branch_id in (
                ("ORG-B", "BR-B1"),
                ("ORG-A", "BR-A2"),
            ):
                conn.execute(
                    """INSERT INTO payroll_runs (
                           payroll_month, period_start, period_end,
                           organization_id, branch_id
                       ) VALUES ('2026-10', '2026-10-01', '2026-10-31', ?, ?)""",
                    (organization_id, branch_id),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO payroll_runs (
                           payroll_month, period_start, period_end,
                           organization_id, branch_id
                       ) VALUES ('2026-10', '2026-10-01', '2026-10-31', 'ORG-A', 'BR-A1')"""
                )


class UserNameMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="quanteye-user-migration-")
        self.db_path = os.path.join(self.temp_dir.name, "legacy-users.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_repairs_persisted_foreign_keys_to_missing_legacy_users_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    organization_id TEXT,
                    branch_id TEXT
                );
                CREATE TABLE attendance (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES _users_legacy_unique_old(id)
                );
                INSERT INTO users (id, name, organization_id, branch_id)
                VALUES (3, 'Existing Employee', 'ORG-A', 'BR-A1');
                INSERT INTO attendance (id, user_id) VALUES (9, 3);
            """)

            db._repair_legacy_user_foreign_key_references(conn.cursor())
            conn.execute("PRAGMA foreign_keys = ON")

            self.assertEqual(
                conn.execute("PRAGMA foreign_key_list(attendance)").fetchone()[2],
                "users",
            )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                conn.execute("SELECT id, user_id FROM attendance").fetchone(),
                (9, 3),
            )

    def test_new_users_rebuild_preserves_child_foreign_keys(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    organization_id TEXT,
                    branch_id TEXT
                );
                CREATE TABLE attendance (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                INSERT INTO users (id, name, organization_id, branch_id)
                VALUES (3, 'Existing Employee', 'ORG-A', 'BR-A1');
                INSERT INTO attendance (id, user_id) VALUES (9, 3);
            """)

            conn.execute("PRAGMA legacy_alter_table = ON")
            db._remove_legacy_global_user_name_uniqueness(conn.cursor())
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")

            self.assertEqual(
                conn.execute("PRAGMA foreign_key_list(attendance)").fetchone()[2],
                "users",
            )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            conn.execute(
                "INSERT INTO users (name, organization_id, branch_id) VALUES (?, ?, ?)",
                ("Existing Employee", "ORG-B", "BR-B1"),
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM users WHERE name = 'Existing Employee'").fetchone()[0],
                2,
            )


class LeaveTypesTenantMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="quanteye-leave-types-migration-")
        self.db_path = os.path.join(self.temp_dir.name, "legacy-leave.db")
        self.original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        try:
            self.temp_dir.cleanup()
        except OSError:
            pass

    def test_init_rebuilds_global_unique_leave_types_with_foreign_keys(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE leave_types (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    code TEXT NOT NULL,
                    yearly_allowance REAL DEFAULT 12.0,
                    is_paid INTEGER DEFAULT 1,
                    description TEXT,
                    organization_id TEXT DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT DEFAULT 'DEFAULT_BRANCH'
                );
                CREATE TABLE employee_leave_balances (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    leave_type_id INTEGER NOT NULL,
                    FOREIGN KEY(leave_type_id) REFERENCES leave_types(id)
                );
                INSERT INTO leave_types (id, name, code, yearly_allowance, is_paid, description)
                VALUES (4, 'Casual Leave', 'casual', 10, 1, 'Short leave');
                INSERT INTO employee_leave_balances (id, user_id, leave_type_id) VALUES (1, 9, 4);
            """)
        self.assertTrue(db.init_db())
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, name, organization_id FROM leave_types WHERE id = 4"
            ).fetchone()
            self.assertEqual(row, (4, "Casual Leave", "DEFAULT_ORG"))
            leftover = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='leave_types_legacy_tenant'"
            ).fetchone()
            self.assertIsNone(leftover)
            self.assertEqual(
                conn.execute("SELECT leave_type_id FROM employee_leave_balances").fetchone()[0],
                4,
            )

    def test_recovers_orphaned_leave_types_legacy_tenant_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE leave_types_legacy_tenant (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    code TEXT NOT NULL,
                    yearly_allowance REAL DEFAULT 12.0,
                    is_paid INTEGER DEFAULT 1,
                    description TEXT
                );
                INSERT INTO leave_types_legacy_tenant (id, name, code)
                VALUES (8, 'Sick Leave', 'sick');
            """)
        self.assertTrue(db.init_db())
        with sqlite3.connect(self.db_path) as conn:
            names = {
                row[0]
                for row in conn.execute("SELECT name FROM leave_types").fetchall()
            }
            self.assertIn("Sick Leave", names)
            leftover = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='leave_types_legacy_tenant'"
            ).fetchone()
            self.assertIsNone(leftover)

    def test_recovers_orphaned_leave_types_rebuild_old_and_copies_to_org(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE _leave_types_rebuild_old (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    code TEXT NOT NULL,
                    yearly_allowance REAL DEFAULT 12.0,
                    is_paid INTEGER DEFAULT 1,
                    description TEXT
                );
                INSERT INTO _leave_types_rebuild_old (id, name, code)
                VALUES (4, 'Casual Leave', 'casual');
                CREATE TABLE employee_leave_balances (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    leave_type_id INTEGER NOT NULL,
                    FOREIGN KEY(leave_type_id) REFERENCES _leave_types_rebuild_old(id)
                );
                INSERT INTO employee_leave_balances (id, user_id, leave_type_id) VALUES (1, 9, 4);
            """)
        self.assertTrue(db.init_db())
        with sqlite3.connect(self.db_path) as conn:
            leftover = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='_leave_types_rebuild_old'"
            ).fetchone()
            self.assertIsNone(leftover)
            names = {
                row[0]
                for row in conn.execute("SELECT name FROM leave_types").fetchall()
            }
            self.assertIn("Casual Leave", names)
            fk_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='employee_leave_balances'"
            ).fetchone()[0]
            self.assertNotIn("_leave_types_rebuild_old", fk_sql)

    def test_copy_survives_stale_fk_to_dropped_rebuild_table(self):
        from payroll_policy_schema import migrate_payroll_policy_tenant_scope

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript("""
                CREATE TABLE leave_types (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    code TEXT NOT NULL,
                    yearly_allowance REAL DEFAULT 12.0,
                    is_paid INTEGER DEFAULT 1,
                    description TEXT,
                    organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                    UNIQUE(organization_id, name),
                    UNIQUE(organization_id, code)
                );
                INSERT INTO leave_types (id, name, code, organization_id, branch_id)
                VALUES (4, 'Casual Leave', 'casual', 'DEFAULT_ORG', 'DEFAULT_BRANCH');
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    organization_id TEXT,
                    branch_id TEXT
                );
                INSERT INTO users (id, name, organization_id, branch_id)
                VALUES (1, 'Org A Staff', 'ORG-A', 'BR-A');
                CREATE TABLE employee_leave_balances (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    leave_type_id INTEGER NOT NULL,
                    FOREIGN KEY(leave_type_id) REFERENCES _leave_types_rebuild_old(id)
                );
                INSERT INTO employee_leave_balances (id, user_id, leave_type_id) VALUES (1, 1, 4);
            """)
            migrate_payroll_policy_tenant_scope(conn.cursor())
            conn.commit()
            names = {
                (row[0], row[1])
                for row in conn.execute("SELECT name, organization_id FROM leave_types").fetchall()
            }
            self.assertIn(("Casual Leave", "DEFAULT_ORG"), names)
            self.assertIn(("Casual Leave", "ORG-A"), names)
            fk_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='employee_leave_balances'"
            ).fetchone()[0]
            self.assertNotIn("_leave_types_rebuild_old", fk_sql)


if __name__ == "__main__":
    unittest.main()
