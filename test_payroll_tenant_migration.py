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


if __name__ == "__main__":
    unittest.main()
