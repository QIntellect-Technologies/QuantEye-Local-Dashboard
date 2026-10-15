import sqlite3
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple, Any, Union, Sequence
from logger_config import get_logger
from config import DB_PATH, ATTENDANCE_LOG_RETENTION_DAYS
from weekday_utils import WorkingDaysError, default_working_days, parse_working_days
from payroll_policy_schema import (
    DEFAULT_CURRENCY,
    DEFAULT_DECIMAL_PLACES,
    DEFAULT_OT_DIVISOR,
    DEFAULT_PAYROLL_SETTINGS,
    DEFAULT_PRORATION_BASIS,
    VALID_PRORATION_BASES,
    VALID_OFF_DAY_ATTENDANCE_MODES,
    normalize_attendance_pay_mode as _normalize_attendance_pay_mode,
    VALID_OT_PAY_MODES,
    ensure_payroll_settings_row,
    migrate_payroll_policy_tenant_scope,
    money as _payroll_money,
    off_day_attendance_extra as _off_day_attendance_extra,
    overtime_entry_pay as _overtime_entry_pay,
    proration_factor as _payroll_proration_factor,
    seed_leave_types_if_empty,
    tenant_ids as _tenant_ids,
)
from payroll_extensions import (
    DEFAULT_FEATURE_FLAGS,
    FEATURE_FLAGS,
    VALID_ADJ_MODES,
    VALID_ADJ_TYPES,
    VALID_CALC_MODES,
    VALID_COMPONENT_CATEGORIES,
    VALID_COMPONENT_KINDS,
    apply_adjustments as _apply_payroll_adjustments,
    ensure_payroll_extension_tables,
    flag_on as _payroll_flag_on,
    named_component_totals as _named_component_totals,
    payroll_excel_bytes,
    payslip_pdf_bytes,
    prorate_packages as _prorate_packages,
    write_audit as _write_payroll_audit,
)
from werkzeug.security import check_password_hash, generate_password_hash

logger = get_logger(__name__)


SALARY_MIN = 0.0
SALARY_MAX = 100_000_000.0


def parse_salary_amount(value: Any, default: Optional[float] = 0.0) -> float:
    """Reject negative or absurd salary amounts before they are stored."""
    if value is None or (isinstance(value, str) and str(value).strip() == ''):
        if default is None:
            raise ValueError('Salary is required')
        value = default
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError('Salary must be a valid number') from None
    if amount < SALARY_MIN:
        raise ValueError('Salary cannot be negative')
    if amount > SALARY_MAX:
        raise ValueError('Salary cannot exceed {:,.0f}'.format(SALARY_MAX))
    if amount != amount:  # NaN
        raise ValueError('Salary must be a valid number')
    return amount


def _user_tenant(cursor, user_id: int) -> Tuple[str, str]:
    cursor.execute(
        "SELECT COALESCE(organization_id, 'DEFAULT_ORG'), COALESCE(branch_id, 'DEFAULT_BRANCH') FROM users WHERE id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return 'DEFAULT_ORG', 'DEFAULT_BRANCH'
    return row[0], row[1]


def _cloud_mirror(action: str, *args, **kwargs) -> None:
    """Best-effort replica to Railway Postgres. Never breaks local SQLite/AI."""
    try:
        import postgres_mirror as cloud_pg
        getattr(cloud_pg, action)(*args, **kwargs)
    except Exception as error:
        logger.warning("Cloud Postgres mirror skipped (%s): %s", action, error)

def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _repair_legacy_user_foreign_key_references(cursor: sqlite3.Cursor) -> None:
    """Repair child tables stranded by the legacy users-table rename migration."""
    legacy_table = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_users_legacy_unique_old'"
    ).fetchone()
    if legacy_table:
        return

    dependent_tables = cursor.execute(
        """SELECT name, sql FROM sqlite_master
           WHERE type = 'table' AND sql LIKE '%_users_legacy_unique_old%'"""
    ).fetchall()
    for table_name, create_sql in dependent_tables:
        if not create_sql:
            continue

        repaired_sql = re.sub(
            r'(?i)(REFERENCES\s+)(?:"_users_legacy_unique_old"|`_users_legacy_unique_old`|\[_users_legacy_unique_old\]|_users_legacy_unique_old)',
            r'\1users',
            create_sql,
        )
        if repaired_sql == create_sql:
            continue

        temporary_name = f"_quanteye_repair_{uuid.uuid4().hex}"
        create_prefix = re.compile(
            r'(?is)^(\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)'
            r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|\S+)'
        )
        repaired_sql, replacements = create_prefix.subn(
            lambda match: f"{match.group(1)}{_quote_sqlite_identifier(temporary_name)}",
            repaired_sql,
            count=1,
        )
        if replacements != 1:
            raise RuntimeError(f"Could not safely repair foreign keys for table {table_name}.")

        saved_objects = cursor.execute(
            """SELECT type, sql FROM sqlite_master
               WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL
               ORDER BY type, name""",
            (table_name,),
        ).fetchall()
        columns = [
            row[1]
            for row in cursor.execute(
                f"PRAGMA table_xinfo({_quote_sqlite_identifier(table_name)})"
            ).fetchall()
            if row[6] == 0
        ]
        if not columns:
            raise RuntimeError(f"Could not determine stored columns for table {table_name}.")

        quoted_columns = ", ".join(_quote_sqlite_identifier(column) for column in columns)
        cursor.execute(repaired_sql)
        cursor.execute(
            f"INSERT INTO {_quote_sqlite_identifier(temporary_name)} ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM {_quote_sqlite_identifier(table_name)}"
        )
        cursor.execute(f"DROP TABLE {_quote_sqlite_identifier(table_name)}")
        cursor.execute(
            f"ALTER TABLE {_quote_sqlite_identifier(temporary_name)} "
            f"RENAME TO {_quote_sqlite_identifier(table_name)}"
        )
        for _, object_sql in saved_objects:
            cursor.execute(object_sql)


def _remove_legacy_global_user_name_uniqueness(cursor: sqlite3.Cursor) -> None:
    """Rebuild users without its legacy global name uniqueness constraint."""
    indexes = cursor.execute("PRAGMA index_list(users)").fetchall()
    has_global_name_unique = any(
        index[2] == 1 and ("name" in index[1].lower() or "autoindex" in index[1].lower())
        for index in indexes
    )
    if not has_global_name_unique:
        return

    cursor.execute("ALTER TABLE users RENAME TO _users_legacy_unique_old")
    cursor.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            department TEXT,
            enrollment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            active INTEGER DEFAULT 1,
            notes TEXT,
            photo_path TEXT,
            password TEXT,
            password_hash TEXT,
            role TEXT DEFAULT 'staff',
            cnic TEXT,
            position TEXT,
            salary REAL DEFAULT 0,
            join_date TEXT,
            shift_id INTEGER DEFAULT 1,
            checkin_cameras TEXT DEFAULT 'all',
            checkout_cameras TEXT DEFAULT 'all',
            exit_date TEXT,
            organization_id TEXT DEFAULT 'DEFAULT_ORG',
            branch_id TEXT DEFAULT 'DEFAULT_BRANCH',
            face_trained INTEGER DEFAULT 0
        )
    """)
    old_columns = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(_users_legacy_unique_old)").fetchall()
    }
    current_columns = [
        row[1]
        for row in cursor.execute("PRAGMA table_info(users)").fetchall()
        if row[1] != "id" and row[1] in old_columns
    ]
    if not current_columns:
        raise RuntimeError("Could not determine common columns while migrating users.")

    column_sql = ", ".join(_quote_sqlite_identifier(column) for column in current_columns)
    cursor.execute(
        f"INSERT INTO users (id, {column_sql}) "
        f"SELECT id, {column_sql} FROM _users_legacy_unique_old"
    )
    cursor.execute("DROP TABLE _users_legacy_unique_old")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_org ON users(organization_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_name_org ON users(name, organization_id)")


def _migrate_payroll_run_tenant_uniqueness(cursor: sqlite3.Cursor) -> None:
    """Replace the legacy global month constraint without changing payroll rows."""
    staged_table = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'payroll_runs_scoped'"
    ).fetchone()
    if staged_table:
        source_count = cursor.execute("SELECT COUNT(*) FROM payroll_runs").fetchone()[0]
        staged_count = cursor.execute("SELECT COUNT(*) FROM payroll_runs_scoped").fetchone()[0]
        if source_count == 0 and staged_count > 0:
            cursor.execute("DROP TABLE payroll_runs")
            cursor.execute("ALTER TABLE payroll_runs_scoped RENAME TO payroll_runs")
        else:
            cursor.execute("DROP TABLE payroll_runs_scoped")

    month_only_unique = False
    for index in cursor.execute("PRAGMA index_list(payroll_runs)").fetchall():
        if not index[2]:
            continue
        columns = [
            row[2]
            for row in cursor.execute(f"PRAGMA index_info('{index[1]}')").fetchall()
        ]
        if columns == ["payroll_month"]:
            month_only_unique = True
            break
    if not month_only_unique:
        cursor.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS ux_payroll_runs_tenant_month
               ON payroll_runs(organization_id, branch_id, payroll_month)"""
        )
        return

    cursor.execute("DROP TRIGGER IF EXISTS prevent_finalized_payroll_run_update")
    cursor.execute("DROP TRIGGER IF EXISTS prevent_finalized_payroll_run_delete")
    cursor.execute("DROP TRIGGER IF EXISTS prevent_finalized_payroll_record_insert")
    cursor.execute("DROP TRIGGER IF EXISTS prevent_finalized_payroll_record_update")
    cursor.execute("DROP TRIGGER IF EXISTS prevent_finalized_payroll_record_delete")
    cursor.execute("""
        CREATE TABLE payroll_runs_scoped (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payroll_month TEXT NOT NULL,
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
            branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
            UNIQUE (organization_id, branch_id, payroll_month)
        )
    """)
    cursor.execute("""
        INSERT INTO payroll_runs_scoped (
            id, payroll_month, period_start, period_end, employee_count,
            total_basic_salary, total_deduction, total_net_salary, status,
            created_at, finalized_at, total_allowances, total_overtime,
            organization_id, branch_id
        )
        SELECT id, payroll_month, period_start, period_end, employee_count,
               total_basic_salary, total_deduction, total_net_salary, status,
               created_at, finalized_at, total_allowances, total_overtime,
               COALESCE(NULLIF(organization_id, ''), 'DEFAULT_ORG'),
               COALESCE(NULLIF(branch_id, ''), 'DEFAULT_BRANCH')
        FROM payroll_runs
    """)
    cursor.execute("DROP TABLE payroll_runs")
    cursor.execute("ALTER TABLE payroll_runs_scoped RENAME TO payroll_runs")

def _get_local_instance_id() -> str:
    """Retrieve persistent local instance ID from config_integration.json or fallback."""
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_integration.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return str(data.get("local_instance_id") or "LOC-INST-001")
        except Exception:
            pass
    return "LOC-INST-001"

def enqueue_sync_outbox(
    cursor: sqlite3.Cursor,
    organization_id: str,
    branch_id: str,
    event_type: str,
    payload: Dict[str, Any],
    event_id: Optional[str] = None
) -> str:
    """
    Transactionally enqueue an event into sync_outbox_queue.
    Must be called within an active database transaction cursor.
    """
    ev_id = event_id or f"EVT-{uuid.uuid4().hex.upper()}"
    inst_id = _get_local_instance_id()
    cursor.execute('''
        INSERT OR IGNORE INTO sync_outbox_queue (
            event_id, organization_id, branch_id, local_instance_id,
            event_type, payload_json, status, attempts, next_attempt_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, CURRENT_TIMESTAMP)
    ''', (ev_id, organization_id, branch_id, inst_id, event_type, json.dumps(payload)))
    return ev_id


def init_db():
    """Initialize SQLite database with proper schema, columns, and indexes."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = OFF")
            cursor.execute("PRAGMA legacy_alter_table = ON")
            _repair_legacy_user_foreign_key_references(cursor)

            # Users table (scoped per organization)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT,
                    phone TEXT,
                    department TEXT,
                    enrollment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    active INTEGER DEFAULT 1,
                    notes TEXT
                )
            ''')

            # Automatic Migration: Add optional columns if they don't exist
            for col, col_type in [
                ("photo_path", "TEXT"),
                ("password", "TEXT"),
                ("password_hash", "TEXT"),
                ("role", "TEXT DEFAULT 'staff'"),
                ("cnic", "TEXT"),
                ("position", "TEXT"),
                ("salary", "REAL DEFAULT 0"),
                ("join_date", "TEXT"),
                ("shift_id", "INTEGER DEFAULT 1"),
                ("checkin_cameras", "TEXT DEFAULT 'all'"),
                ("checkout_cameras", "TEXT DEFAULT 'all'"),
                ("exit_date", "TEXT"),
                ("organization_id", "TEXT DEFAULT 'DEFAULT_ORG'"),
                ("branch_id", "TEXT DEFAULT 'DEFAULT_BRANCH'"),
                ("face_trained", "INTEGER DEFAULT 0")
            ]:
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass

            # Move legacy credentials to one-way hashes and invalidate known defaults.
            credential_rows = cursor.execute(
                "SELECT id, password, password_hash FROM users"
            ).fetchall()
            for user_id, legacy_password, password_hash in credential_rows:
                if password_hash:
                    if legacy_password is not None:
                        cursor.execute("UPDATE users SET password = NULL WHERE id = ?", (user_id,))
                    continue
                if not legacy_password:
                    continue
                legacy_value = str(legacy_password)
                if len(legacy_value) < 14:
                    cursor.execute(
                        "UPDATE users SET password = NULL, password_hash = NULL WHERE id = ?",
                        (user_id,),
                    )
                elif legacy_value.startswith(("pbkdf2:", "scrypt:")):
                    cursor.execute(
                        "UPDATE users SET password = NULL, password_hash = ? WHERE id = ?",
                        (legacy_value, user_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE users SET password = NULL, password_hash = ? WHERE id = ?",
                        (generate_password_hash(legacy_value), user_id),
                    )
            conn.commit()

            # Repair volumes affected by the old parent-table rename migration,
            # then safely remove the legacy global UNIQUE constraint.
            _repair_legacy_user_foreign_key_references(cursor)
            _remove_legacy_global_user_name_uniqueness(cursor)
            conn.commit()

            # Create the base table before applying migrations that alter it.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS leave_types (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    code TEXT NOT NULL,
                    yearly_allowance REAL DEFAULT 12.0,
                    is_paid INTEGER DEFAULT 1,
                    description TEXT,
                    active INTEGER DEFAULT 1,
                    allowance_period TEXT DEFAULT 'yearly',
                    allowance_months INTEGER NOT NULL DEFAULT 12,
                    half_leave_enabled INTEGER NOT NULL DEFAULT 0,
                    half_leave_mode TEXT NOT NULL DEFAULT 'separate',
                    half_leave_allowance_count INTEGER NOT NULL DEFAULT 0,
                    half_leave_allowance_months INTEGER NOT NULL DEFAULT 12,
                    half_leaves_per_quota_day INTEGER NOT NULL DEFAULT 2,
                    half_leave_quota_type_id INTEGER,
                    organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(organization_id, name),
                    UNIQUE(organization_id, code)
                )
            ''')

            try:
                cursor.execute("SELECT allowance_period FROM leave_types LIMIT 1")
            except sqlite3.OperationalError:
                cursor.execute("ALTER TABLE leave_types ADD COLUMN allowance_period TEXT DEFAULT 'yearly'")
                conn.commit()

            leave_type_columns = {row[1] for row in cursor.execute("PRAGMA table_info(leave_types)").fetchall()}
            if 'allowance_months' not in leave_type_columns:
                cursor.execute("ALTER TABLE leave_types ADD COLUMN allowance_months INTEGER NOT NULL DEFAULT 12")
                cursor.execute('''
                    UPDATE leave_types
                    SET allowance_months = CASE allowance_period
                        WHEN 'monthly' THEN 1
                        WHEN 'quarterly' THEN 3
                        WHEN 'half_yearly' THEN 6
                        ELSE 12
                    END
                ''')

            for column, column_type in [
                ('half_leave_enabled', 'INTEGER NOT NULL DEFAULT 0'),
                ('half_leave_mode', "TEXT NOT NULL DEFAULT 'separate'"),
                ('half_leave_allowance_count', 'INTEGER NOT NULL DEFAULT 0'),
                ('half_leave_allowance_months', 'INTEGER NOT NULL DEFAULT 12'),
                ('half_leaves_per_quota_day', 'INTEGER NOT NULL DEFAULT 2'),
                ('half_leave_quota_type_id', 'INTEGER'),
            ]:
                if column not in leave_type_columns:
                    cursor.execute(f"ALTER TABLE leave_types ADD COLUMN {column} {column_type}")
            conn.commit()

            # Face embeddings are required by enrollment and indexed below.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    source_video TEXT,
                    quality_score REAL DEFAULT 1.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            ''')
            for column, column_type in [
                ("organization_id", "TEXT"),
                ("branch_id", "TEXT"),
            ]:
                try:
                    cursor.execute(f"ALTER TABLE embeddings ADD COLUMN {column} {column_type}")
                except sqlite3.OperationalError as error:
                    if "duplicate column name" not in str(error).lower():
                        raise

            cursor.execute("""
                UPDATE embeddings
                SET organization_id = COALESCE(
                    (SELECT organization_id FROM users WHERE users.id = embeddings.user_id),
                    'DEFAULT_ORG'
                )
                WHERE organization_id IS NULL OR organization_id = ''
            """)
            cursor.execute("""
                UPDATE embeddings
                SET branch_id = COALESCE(
                    (SELECT branch_id FROM users WHERE users.id = embeddings.user_id),
                    'DEFAULT_BRANCH'
                )
                WHERE branch_id IS NULL OR branch_id = ''
            """)

            # Attendance logs migration
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    detected_name TEXT,
                    confidence REAL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    source TEXT,
                    frame_quality TEXT,
                    location TEXT,
                    device_id TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            ''')
            for col, col_type in [
                ("checkout_camera_id", "TEXT"),
                ("checkout_location", "TEXT"),
                ("notes", "TEXT DEFAULT ''"),
                ("marked_by", "TEXT DEFAULT 'System'"),
                ("shift_id", "INTEGER DEFAULT NULL")
            ]:
                try:
                    cursor.execute(f"ALTER TABLE attendance ADD COLUMN {col} {col_type}")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass

            # Manual absent overrides for today
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS manual_absent (
                    user_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(user_id, date),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            ''')

            # Persistent room-presence snapshots, throttled by the app to one per user every five minutes.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS locator_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    department TEXT,
                    camera_id TEXT NOT NULL,
                    camera_name TEXT NOT NULL,
                    room_name TEXT NOT NULL,
                    confidence REAL DEFAULT 0,
                    first_seen TEXT,
                    last_seen TEXT,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            ''')

            # Leave requests table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS leave_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    user_name TEXT,
                    leave_type TEXT NOT NULL,
                    leave_duration TEXT NOT NULL DEFAULT 'full',
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    reason TEXT,
                    status TEXT DEFAULT 'pending',
                    approved_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            ''')
            leave_request_columns = {row[1] for row in cursor.execute("PRAGMA table_info(leave_requests)").fetchall()}
            for column, column_type in [
                ('leave_duration', "TEXT NOT NULL DEFAULT 'full'"),
                ('half_leave_quota_type_id', 'INTEGER'),
                ('half_leave_quota_charge', 'INTEGER NOT NULL DEFAULT 0'),
                ('paid_days', 'INTEGER'),
                ('unpaid_days', 'INTEGER'),
                ('quota_days', 'INTEGER'),
                ('paid_through_date', 'TEXT'),
                ('quota_through_date', 'TEXT'),
            ]:
                if column not in leave_request_columns:
                    cursor.execute(f"ALTER TABLE leave_requests ADD COLUMN {column} {column_type}")

            # Company holidays table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS company_holidays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    holiday_date DATE NOT NULL,
                    end_date DATE,
                    description TEXT,
                    is_recurring INTEGER DEFAULT 0,
                    overtime_premium_percent REAL,
                    organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(organization_id, branch_id, holiday_date)
                )
            ''')
            company_holiday_columns = {row[1] for row in cursor.execute("PRAGMA table_info(company_holidays)").fetchall()}
            if 'end_date' not in company_holiday_columns:
                cursor.execute("ALTER TABLE company_holidays ADD COLUMN end_date DATE")
                cursor.execute("UPDATE company_holidays SET end_date = holiday_date")
            if 'overtime_premium_percent' not in company_holiday_columns:
                cursor.execute("ALTER TABLE company_holidays ADD COLUMN overtime_premium_percent REAL")
            conn.commit()

            # Employee leave balances table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS employee_leave_balances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    leave_type_id INTEGER NOT NULL,
                    year INTEGER NOT NULL,
                    allocated_days REAL NOT NULL DEFAULT 0.0,
                    used_days REAL NOT NULL DEFAULT 0.0,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(leave_type_id) REFERENCES leave_types(id) ON DELETE CASCADE,
                    UNIQUE(user_id, leave_type_id, year)
                )
            ''')

            # Overtime table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS overtime (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    user_name TEXT,
                    ot_date TEXT NOT NULL,
                    hours REAL NOT NULL,
                    reason TEXT,
                    status TEXT DEFAULT 'pending',
                    approved_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            ''')

            # Salary configs table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS salary_configs (
                    user_id INTEGER PRIMARY KEY,
                    basic_salary REAL DEFAULT 0,
                    allowances REAL DEFAULT 0,
                    deductions REAL DEFAULT 0,
                    ot_rate REAL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            ''')

            # Finalized payroll is stored as a monthly snapshot of employee and attendance data.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payroll_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payroll_month TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    employee_count INTEGER NOT NULL DEFAULT 0,
                    total_basic_salary REAL NOT NULL DEFAULT 0,
                    total_deduction REAL NOT NULL DEFAULT 0,
                    total_net_salary REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'Finalized',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finalized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                    UNIQUE (organization_id, branch_id, payroll_month)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payroll_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payroll_run_id INTEGER NOT NULL,
                    employee_id INTEGER NOT NULL,
                    employee_name TEXT NOT NULL,
                    department TEXT DEFAULT '',
                    designation TEXT DEFAULT '',
                    basic_salary REAL NOT NULL DEFAULT 0,
                    scheduled_workdays INTEGER NOT NULL DEFAULT 0,
                    present_days INTEGER NOT NULL DEFAULT 0,
                    absent_days INTEGER NOT NULL DEFAULT 0,
                    late_days INTEGER NOT NULL DEFAULT 0,
                    approved_leave_days INTEGER NOT NULL DEFAULT 0,
                    paid_leave_days INTEGER NOT NULL DEFAULT 0,
                    unpaid_leave_days INTEGER NOT NULL DEFAULT 0,
                    total_leave_days INTEGER NOT NULL DEFAULT 0,
                    payable_days REAL NOT NULL DEFAULT 0,
                    net_salary REAL NOT NULL DEFAULT 0,
                    applied_rule_id INTEGER,
                    matched_rule TEXT DEFAULT '',
                    shift_details TEXT NOT NULL DEFAULT '[]',
                    calendar_details TEXT NOT NULL DEFAULT '[]',
                    deduction_type TEXT NOT NULL DEFAULT 'percentage',
                    deduction_percent REAL,
                    fixed_amount REAL NOT NULL DEFAULT 0,
                    deduction_amount REAL,
                    status TEXT NOT NULL DEFAULT 'Finalized',
                    FOREIGN KEY(payroll_run_id) REFERENCES payroll_runs(id) ON DELETE CASCADE,
                    UNIQUE(payroll_run_id, employee_id)
                )
            ''')
            payroll_record_columns = {row[1] for row in cursor.execute("PRAGMA table_info(payroll_records)")}
            for column, column_type in [
                ("applied_rule_id", "INTEGER"),
                ("matched_rule", "TEXT DEFAULT ''"),
                ("shift_details", "TEXT NOT NULL DEFAULT '[]'"),
                ("calendar_details", "TEXT NOT NULL DEFAULT '[]'"),
                ("deduction_type", "TEXT NOT NULL DEFAULT 'percentage'"),
                ("deduction_percent", "REAL"),
                ("fixed_amount", "REAL NOT NULL DEFAULT 0"),
                ("deduction_amount", "REAL"),
                ("allowances", "REAL NOT NULL DEFAULT 0.0"),
                ("custom_deductions", "REAL NOT NULL DEFAULT 0.0"),
                ("overtime_hours", "REAL NOT NULL DEFAULT 0.0"),
                ("overtime_rate", "REAL NOT NULL DEFAULT 0.0"),
                ("overtime_amount", "REAL NOT NULL DEFAULT 0.0"),
                ("overtime_breakdown", "TEXT NOT NULL DEFAULT '[]'"),
                ("gross_salary", "REAL NOT NULL DEFAULT 0.0"),
                ("paid_leave_days", "INTEGER NOT NULL DEFAULT 0"),
                ("unpaid_leave_days", "INTEGER NOT NULL DEFAULT 0"),
                ("unpaid_leave_deduction", "REAL NOT NULL DEFAULT 0.0"),
                ("total_leave_days", "INTEGER NOT NULL DEFAULT 0"),
                ("bonus_amount", "REAL NOT NULL DEFAULT 0.0"),
                ("off_day_extra_pay", "REAL NOT NULL DEFAULT 0.0"),
                ("pay_lines", "TEXT NOT NULL DEFAULT '[]'"),
                ("arrears_amount", "REAL NOT NULL DEFAULT 0.0"),
                ("adjustment_amount", "REAL NOT NULL DEFAULT 0.0"),
            ]:
                if column not in payroll_record_columns:
                    cursor.execute(f"ALTER TABLE payroll_records ADD COLUMN {column} {column_type}")
            payroll_run_columns = {row[1] for row in cursor.execute("PRAGMA table_info(payroll_runs)")}
            if 'total_deduction' not in payroll_run_columns:
                cursor.execute("ALTER TABLE payroll_runs ADD COLUMN total_deduction REAL NOT NULL DEFAULT 0")
            if 'total_allowances' not in payroll_run_columns:
                cursor.execute("ALTER TABLE payroll_runs ADD COLUMN total_allowances REAL NOT NULL DEFAULT 0")
            if 'total_overtime' not in payroll_run_columns:
                cursor.execute("ALTER TABLE payroll_runs ADD COLUMN total_overtime REAL NOT NULL DEFAULT 0")
            if 'organization_id' not in payroll_run_columns:
                cursor.execute("ALTER TABLE payroll_runs ADD COLUMN organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG'")
            if 'branch_id' not in payroll_run_columns:
                cursor.execute("ALTER TABLE payroll_runs ADD COLUMN branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH'")
            if 'finalized_by' not in payroll_run_columns:
                cursor.execute("ALTER TABLE payroll_runs ADD COLUMN finalized_by TEXT")

            conn.commit()
            cursor.execute("PRAGMA foreign_keys = OFF")
            _migrate_payroll_run_tenant_uniqueness(cursor)
            conn.commit()
            cursor.execute("PRAGMA foreign_keys = ON")

            cursor.execute("DROP TRIGGER IF EXISTS prevent_finalized_payroll_run_update")
            cursor.executescript('''
                CREATE TRIGGER IF NOT EXISTS prevent_finalized_payroll_run_update
                BEFORE UPDATE ON payroll_runs
                WHEN OLD.status = 'Finalized' AND NEW.status != 'Reopened'
                BEGIN
                    SELECT RAISE(ABORT, 'Finalized payroll runs are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS prevent_finalized_payroll_run_delete
                BEFORE DELETE ON payroll_runs
                WHEN OLD.status = 'Finalized'
                BEGIN
                    SELECT RAISE(ABORT, 'Finalized payroll runs are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS prevent_finalized_payroll_record_insert
                BEFORE INSERT ON payroll_records
                WHEN EXISTS (SELECT 1 FROM payroll_runs WHERE id = NEW.payroll_run_id AND status = 'Finalized')
                BEGIN
                    SELECT RAISE(ABORT, 'Finalized payroll records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS prevent_finalized_payroll_record_update
                BEFORE UPDATE ON payroll_records
                WHEN EXISTS (SELECT 1 FROM payroll_runs WHERE id = OLD.payroll_run_id AND status = 'Finalized')
                BEGIN
                    SELECT RAISE(ABORT, 'Finalized payroll records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS prevent_finalized_payroll_record_delete
                BEFORE DELETE ON payroll_records
                WHEN EXISTS (SELECT 1 FROM payroll_runs WHERE id = OLD.payroll_run_id AND status = 'Finalized')
                BEGIN
                    SELECT RAISE(ABORT, 'Finalized payroll records are immutable');
                END;
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payroll_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                    rule_basis TEXT NOT NULL DEFAULT 'absence',
                    min_absent_days INTEGER NOT NULL,
                    max_absent_days INTEGER,
                    deduction_type TEXT NOT NULL DEFAULT 'percentage',
                    deduction_percent REAL NOT NULL DEFAULT 0,
                    fixed_amount REAL NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CHECK(min_absent_days >= 1),
                    CHECK(max_absent_days IS NULL OR max_absent_days >= min_absent_days),
                    CHECK(deduction_percent >= 0 AND deduction_percent <= 100),
                    CHECK(fixed_amount >= 0),
                    CHECK(deduction_type IN ('percentage', 'fixed'))
                )
            ''')
            payroll_rule_columns = {row[1] for row in cursor.execute("PRAGMA table_info(payroll_rules)")}
            for column, column_type in [
                ("rule_basis", "TEXT NOT NULL DEFAULT 'absence'"),
                ("deduction_type", "TEXT NOT NULL DEFAULT 'percentage'"),
                ("fixed_amount", "REAL NOT NULL DEFAULT 0"),
            ]:
                if column not in payroll_rule_columns:
                    cursor.execute(f"ALTER TABLE payroll_rules ADD COLUMN {column} {column_type}")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payroll_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                    deduct_unpaid_leave INTEGER NOT NULL DEFAULT 0 CHECK(deduct_unpaid_leave IN (0, 1)),
                    half_leave_quota_mode TEXT NOT NULL DEFAULT 'category',
                    half_leaves_per_quota_day INTEGER NOT NULL DEFAULT 2,
                    holiday_ot_premium_percent REAL NOT NULL DEFAULT 0,
                    non_working_day_ot_premium_percent REAL NOT NULL DEFAULT 0,
                    holiday_ot_premium_active INTEGER NOT NULL DEFAULT 0,
                    non_working_day_ot_premium_active INTEGER NOT NULL DEFAULT 0,
                    overtime_max_hours REAL NOT NULL DEFAULT 0,
                    overtime_max_period TEXT NOT NULL DEFAULT 'day',
                    ot_hour_divisor REAL NOT NULL DEFAULT 208,
                    proration_basis TEXT NOT NULL DEFAULT 'calendar_days',
                    currency_code TEXT NOT NULL DEFAULT 'PKR',
                    decimal_places INTEGER NOT NULL DEFAULT 2,
                    holiday_ot_pay_mode TEXT NOT NULL DEFAULT 'percent',
                    non_working_day_ot_pay_mode TEXT NOT NULL DEFAULT 'percent',
                    holiday_ot_multiplier REAL NOT NULL DEFAULT 1.5,
                    non_working_day_ot_multiplier REAL NOT NULL DEFAULT 1.5,
                    holiday_ot_fixed_amount REAL NOT NULL DEFAULT 0,
                    non_working_day_ot_fixed_amount REAL NOT NULL DEFAULT 0,
                    off_day_attendance_pay_active INTEGER NOT NULL DEFAULT 0,
                    off_day_attendance_pay_mode TEXT NOT NULL DEFAULT 'multiplier',
                    off_day_attendance_pay_multiplier REAL NOT NULL DEFAULT 1.5,
                    off_day_attendance_pay_fixed_amount REAL NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(organization_id, branch_id)
                )
            ''')
            payroll_setting_columns = {row[1] for row in cursor.execute("PRAGMA table_info(payroll_settings)")}
            migrate_holiday_rule_active = "holiday_ot_premium_active" not in payroll_setting_columns
            migrate_non_working_rule_active = "non_working_day_ot_premium_active" not in payroll_setting_columns
            for column, column_type in [
                ("half_leave_quota_mode", "TEXT NOT NULL DEFAULT 'separate'"),
                ("half_leaves_per_quota_day", "INTEGER NOT NULL DEFAULT 2"),
                ("holiday_ot_premium_percent", "REAL NOT NULL DEFAULT 0"),
                ("non_working_day_ot_premium_percent", "REAL NOT NULL DEFAULT 0"),
                ("holiday_ot_premium_active", "INTEGER NOT NULL DEFAULT 0"),
                ("non_working_day_ot_premium_active", "INTEGER NOT NULL DEFAULT 0"),
                ("overtime_max_hours", "REAL NOT NULL DEFAULT 0"),
                ("overtime_max_period", "TEXT NOT NULL DEFAULT 'day'"),
                ("holiday_ot_pay_mode", "TEXT NOT NULL DEFAULT 'percent'"),
                ("non_working_day_ot_pay_mode", "TEXT NOT NULL DEFAULT 'percent'"),
                ("holiday_ot_multiplier", "REAL NOT NULL DEFAULT 1.5"),
                ("non_working_day_ot_multiplier", "REAL NOT NULL DEFAULT 1.5"),
                ("holiday_ot_fixed_amount", "REAL NOT NULL DEFAULT 0"),
                ("non_working_day_ot_fixed_amount", "REAL NOT NULL DEFAULT 0"),
                ("off_day_attendance_pay_active", "INTEGER NOT NULL DEFAULT 0"),
                ("off_day_attendance_pay_mode", "TEXT NOT NULL DEFAULT 'multiplier'"),
                ("off_day_attendance_pay_multiplier", "REAL NOT NULL DEFAULT 1.5"),
                ("off_day_attendance_pay_fixed_amount", "REAL NOT NULL DEFAULT 0"),
                ("enable_named_components", "INTEGER NOT NULL DEFAULT 0"),
                ("enable_tax", "INTEGER NOT NULL DEFAULT 0"),
                ("enable_loans", "INTEGER NOT NULL DEFAULT 0"),
                ("enable_advances", "INTEGER NOT NULL DEFAULT 0"),
                ("enable_provident", "INTEGER NOT NULL DEFAULT 0"),
                ("enable_insurance", "INTEGER NOT NULL DEFAULT 0"),
                ("enable_bonuses", "INTEGER NOT NULL DEFAULT 1"),
                ("enable_individual_adjustments", "INTEGER NOT NULL DEFAULT 0"),
                ("enable_salary_revisions", "INTEGER NOT NULL DEFAULT 0"),
                ("attendance_pay_mode", "TEXT NOT NULL DEFAULT 'payable_days'"),
            ]:
                if column not in payroll_setting_columns:
                    cursor.execute(f"ALTER TABLE payroll_settings ADD COLUMN {column} {column_type}")
            if migrate_holiday_rule_active:
                cursor.execute("UPDATE payroll_settings SET holiday_ot_premium_active = 1 WHERE holiday_ot_premium_percent > 0")
            if migrate_non_working_rule_active:
                cursor.execute("UPDATE payroll_settings SET non_working_day_ot_premium_active = 1 WHERE non_working_day_ot_premium_percent > 0")
            cursor.execute("UPDATE payroll_settings SET half_leave_quota_mode = 'category' WHERE half_leave_quota_mode = 'separate'")
            cursor.execute("UPDATE payroll_settings SET half_leave_quota_mode = 'monthly_convert' WHERE half_leave_quota_mode = 'convert'")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payroll_scoped_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                    policy_type TEXT NOT NULL CHECK(policy_type IN ('half_leave_quota', 'overtime_premium', 'overtime_limit')),
                    scope_type TEXT NOT NULL CHECK(scope_type IN ('department', 'employee')),
                    scope_id INTEGER NOT NULL,
                    day_type TEXT NOT NULL DEFAULT '',
                    half_leave_quota_mode TEXT,
                    half_leaves_per_quota_day INTEGER,
                    overtime_premium_percent REAL,
                    overtime_max_hours REAL,
                    overtime_max_period TEXT,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(policy_type, scope_type, scope_id, day_type, organization_id, branch_id)
                )
            ''')
            
            payroll_rules_columns = {row[1] for row in cursor.execute("PRAGMA table_info(payroll_scoped_rules)")}
            if 'overtime_max_hours' not in payroll_rules_columns:
                cursor.execute("ALTER TABLE payroll_scoped_rules RENAME TO payroll_scoped_rules_legacy")
                cursor.execute('''
                    CREATE TABLE payroll_scoped_rules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                        branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                        policy_type TEXT NOT NULL CHECK(policy_type IN ('half_leave_quota', 'overtime_premium', 'overtime_limit')),
                        scope_type TEXT NOT NULL CHECK(scope_type IN ('department', 'employee')),
                        scope_id INTEGER NOT NULL,
                        day_type TEXT NOT NULL DEFAULT '',
                        half_leave_quota_mode TEXT,
                        half_leaves_per_quota_day INTEGER,
                        overtime_premium_percent REAL,
                        overtime_max_hours REAL,
                        overtime_max_period TEXT,
                        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(policy_type, scope_type, scope_id, day_type, organization_id, branch_id)
                    )
                ''')
                cursor.execute('''
                    INSERT INTO payroll_scoped_rules (
                        id, policy_type, scope_type, scope_id, day_type,
                        half_leave_quota_mode, half_leaves_per_quota_day,
                        overtime_premium_percent, active, created_at, updated_at,
                        organization_id, branch_id
                    )
                    SELECT id, policy_type, scope_type, scope_id, day_type,
                           half_leave_quota_mode, half_leaves_per_quota_day,
                           overtime_premium_percent, active, created_at, updated_at,
                           'DEFAULT_ORG', 'DEFAULT_BRANCH'
                    FROM payroll_scoped_rules_legacy
                ''')
                cursor.execute("DROP TABLE payroll_scoped_rules_legacy")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS special_date_overrides (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    calendar_date TEXT NOT NULL,
                    override_type TEXT NOT NULL CHECK(override_type IN ('working_day', 'off_day', 'holiday')),
                    note TEXT NOT NULL DEFAULT '',
                    scope_type TEXT NOT NULL DEFAULT 'all' CHECK(scope_type IN ('all', 'shift', 'employee')),
                    scope_id INTEGER NOT NULL DEFAULT 0,
                    organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(calendar_date, scope_type, scope_id, organization_id, branch_id),
                    CHECK((scope_type = 'all' AND scope_id = 0) OR (scope_type != 'all' AND scope_id > 0))
                )
            ''')
            special_date_columns = {row[1] for row in cursor.execute("PRAGMA table_info(special_date_overrides)")}
            if 'scope_type' not in special_date_columns:
                cursor.execute("ALTER TABLE special_date_overrides RENAME TO special_date_overrides_legacy")
                cursor.execute('''
                    CREATE TABLE special_date_overrides (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        calendar_date TEXT NOT NULL,
                        override_type TEXT NOT NULL CHECK(override_type IN ('working_day', 'off_day', 'holiday')),
                        note TEXT NOT NULL DEFAULT '',
                        scope_type TEXT NOT NULL DEFAULT 'all' CHECK(scope_type IN ('all', 'shift', 'employee')),
                        scope_id INTEGER NOT NULL DEFAULT 0,
                        organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                        branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(calendar_date, scope_type, scope_id, organization_id, branch_id),
                        CHECK((scope_type = 'all' AND scope_id = 0) OR (scope_type != 'all' AND scope_id > 0))
                    )
                ''')
                cursor.execute('''
                    INSERT INTO special_date_overrides (
                        id, calendar_date, override_type, note, scope_type, scope_id,
                        organization_id, branch_id, created_at, updated_at
                    )
                    SELECT id, calendar_date, override_type, note, 'all', 0,
                           'DEFAULT_ORG', 'DEFAULT_BRANCH', created_at, updated_at
                    FROM special_date_overrides_legacy
                ''')
                cursor.execute("DROP TABLE special_date_overrides_legacy")

            ensure_payroll_extension_tables(cursor)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payroll_bonuses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
                    branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'excel',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(organization_id, branch_id, year, month, user_id)
                )
            ''')

            # Branch / Organization Settings table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS branch_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_name TEXT DEFAULT 'Main Branch',
                    city TEXT DEFAULT '',
                    company_name TEXT DEFAULT 'QIntellect AI',
                    admin_name TEXT DEFAULT 'Admin',
                    address TEXT DEFAULT '',
                    phone TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            try:
                cursor.execute("ALTER TABLE branch_settings ADD COLUMN admin_name TEXT DEFAULT 'Admin'")
                conn.commit()
            except sqlite3.OperationalError:
                pass

            cursor.execute("SELECT COUNT(*) FROM branch_settings")
            if cursor.fetchone()[0] == 0:
                # Neutral placeholder row - the Admin configures real values from System Settings.
                cursor.execute('''
                    INSERT INTO branch_settings (id, branch_name, city, company_name, admin_name, address, phone, email)
                    VALUES (1, 'Main Branch', '', 'QIntellect AI', 'Admin', '', '', '')
                ''')

            # Departments table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS departments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    code TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute("SELECT COUNT(*) FROM departments")
            if cursor.fetchone()[0] == 0:
                # One-time migration: register departments already used by existing staff so
                # their records stay intact. New departments are created by the Admin only.
                try:
                    cursor.execute(
                        "SELECT DISTINCT TRIM(department) FROM users "
                        "WHERE department IS NOT NULL AND TRIM(department) != ''"
                    )
                    existing_depts = [(r[0], '', 'Migrated from existing staff records') for r in cursor.fetchall()]
                    if existing_depts:
                        cursor.executemany(
                            "INSERT OR IGNORE INTO departments (name, code, description) VALUES (?, ?, ?)",
                            existing_depts
                        )
                except sqlite3.Error as e:
                    logger.warning(f"Department migration skipped: {e}")

            # Designations table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS designations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    department_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(department_id) REFERENCES departments(id) ON DELETE CASCADE
                )
            ''')
            cursor.execute("SELECT COUNT(*) FROM designations")
            if cursor.fetchone()[0] == 0:
                # One-time migration: register (department, position) pairs already used by staff.
                try:
                    cursor.execute('''
                        SELECT DISTINCT d.id, TRIM(u.position)
                        FROM users u
                        JOIN departments d ON d.name = TRIM(u.department)
                        WHERE u.position IS NOT NULL AND TRIM(u.position) != ''
                    ''')
                    existing_desigs = [(r[0], r[1], 'Migrated from existing staff records') for r in cursor.fetchall()]
                    if existing_desigs:
                        cursor.executemany(
                            "INSERT INTO designations (department_id, title, description) VALUES (?, ?, ?)",
                            existing_desigs
                        )
                except sqlite3.Error as e:
                    logger.warning(f"Designation migration skipped: {e}")

            # Cameras table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cameras (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    room_name TEXT NOT NULL,
                    camera_type TEXT NOT NULL,
                    rtsp_url TEXT NOT NULL,
                    active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute("SELECT COUNT(*) FROM cameras")
            if cursor.fetchone()[0] == 0:
                # No RTSP URLs are seeded. Only the machine's default local webcam (index 0) is
                # registered so a fresh install has one working feed; the Admin adds all
                # DVR / NVR / IP cameras from System Settings > Camera Management.
                cursor.execute(
                    "INSERT INTO cameras (camera_id, name, room_name, camera_type, rtsp_url) VALUES (?, ?, ?, ?, ?)",
                    ('webcam', 'Local Webcam', 'Main Entrance', 'webcam', '0')
                )

            # Shifts table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    grace_period_minutes INTEGER DEFAULT 15,
                    checkout_grace_period_minutes INTEGER DEFAULT 0,
                    working_days TEXT NOT NULL DEFAULT '[0,1,2,3,4,5]',
                    is_default INTEGER DEFAULT 0,
                    active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            try:
                cursor.execute("ALTER TABLE shifts ADD COLUMN is_default INTEGER DEFAULT 0")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE shifts ADD COLUMN working_days TEXT NOT NULL DEFAULT '[0,1,2,3,4,5]'")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE shifts ADD COLUMN checkout_grace_period_minutes INTEGER DEFAULT 0")
                conn.commit()
            except sqlite3.OperationalError:
                pass

            cursor.execute("SELECT COUNT(*) FROM shifts")
            if cursor.fetchone()[0] == 0:
                cursor.execute(
                    "INSERT INTO shifts (id, name, start_time, end_time, grace_period_minutes, is_default) VALUES (?, ?, ?, ?, ?, ?)",
                    (1, 'Morning Shift', '09:00', '17:00', 15, 1)
                )
            else:
                cursor.execute("SELECT COUNT(*) FROM shifts WHERE is_default = 1")
                if cursor.fetchone()[0] == 0:
                    cursor.execute("UPDATE shifts SET is_default = 1 WHERE id = (SELECT id FROM shifts ORDER BY id ASC LIMIT 1)")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS employee_shift_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    shift_id INTEGER NOT NULL,
                    effective_from TEXT NOT NULL,
                    shift_name TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    grace_period_minutes INTEGER NOT NULL DEFAULT 15,
                    checkout_grace_period_minutes INTEGER NOT NULL DEFAULT 0,
                    working_days TEXT NOT NULL,
                    history_source TEXT NOT NULL DEFAULT 'recorded',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, effective_from),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            ''')
            try:
                cursor.execute("ALTER TABLE employee_shift_history ADD COLUMN checkout_grace_period_minutes INTEGER DEFAULT 0")
                conn.commit()
            except sqlite3.OperationalError:
                pass
            cursor.execute('''
                INSERT OR IGNORE INTO employee_shift_history (
                    user_id, shift_id, effective_from, shift_name, start_time, end_time,
                    grace_period_minutes, checkout_grace_period_minutes, working_days, history_source
                )
                SELECT u.id, COALESCE(u.shift_id, 1),
                      '2020-01-01',
                       COALESCE(s.name, 'Morning Shift'), COALESCE(s.start_time, '09:00'),
                       COALESCE(s.end_time, '17:00'), COALESCE(s.grace_period_minutes, 15),
                       COALESCE(s.checkout_grace_period_minutes, 0),
                       COALESCE(s.working_days, '[0,1,2,3,4,5]'), 'legacy_backfill'
                FROM users u
                LEFT JOIN shifts s ON COALESCE(u.shift_id, 1) = s.id
                WHERE NOT EXISTS (
                    SELECT 1 FROM employee_shift_history h WHERE h.user_id = u.id
                )
            ''')
            cursor.execute('''
                DELETE FROM employee_shift_history
                WHERE history_source = 'legacy_backfill'
                AND effective_from != '2020-01-01'
            ''')

            # =========================================================================
            # MULTI-TENANT ENTERPRISE ISOLATION: Add organization_id & branch_id
            # =========================================================================
            for table_name in [
                'users', 'attendance', 'embeddings', 'shifts', 'departments',
                'designations', 'leave_requests', 'leave_types', 'overtime',
                'salary_configs', 'payroll_runs', 'payroll_records', 'company_holidays',
                'employee_leave_balances', 'employee_shift_history', 'manual_absent', 'cameras'
            ]:
                try:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN organization_id TEXT DEFAULT 'DEFAULT_ORG'")
                except sqlite3.OperationalError:
                    pass
                try:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN branch_id TEXT DEFAULT 'DEFAULT_BRANCH'")
                except sqlite3.OperationalError:
                    pass

            # Create composite tenant indexes for ultra-fast, 100% isolated queries
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_org ON users(organization_id, active)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_attendance_org ON attendance(organization_id, timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_embeddings_org ON embeddings(organization_id, user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_departments_org ON departments(organization_id, active)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_shifts_org ON shifts(organization_id, active)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_payroll_records_org ON payroll_records(organization_id)')

            cursor.execute("PRAGMA foreign_keys = OFF")
            migrate_payroll_policy_tenant_scope(cursor)
            ensure_payroll_settings_row(cursor, 'DEFAULT_ORG', 'DEFAULT_BRANCH')
            seed_leave_types_if_empty(cursor, 'DEFAULT_ORG', 'DEFAULT_BRANCH')
            cursor.execute("PRAGMA foreign_keys = ON")
            conn.commit()

            # =========================================================================
            # PHASE 2.3 — LOCAL OUTBOX QUEUE (sync_outbox_queue)
            # =========================================================================
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sync_outbox_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    organization_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    local_instance_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_attempt_at TIMESTAMP,
                    last_error TEXT,
                    synced_at TIMESTAMP
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_outbox_status ON sync_outbox_queue(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_outbox_next_attempt ON sync_outbox_queue(next_attempt_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_outbox_event_id ON sync_outbox_queue(event_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_outbox_org_branch ON sync_outbox_queue(organization_id, branch_id)')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS attendance_ingestion_receipts (
                    event_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    response_json TEXT,
                    response_status INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            conn.commit()
            
        logger.info(f"✓ Database initialized at {DB_PATH}")
        try:
            import postgres_mirror as cloud_pg
            cloud_pg.bootstrap_cloud_postgres()
        except Exception as error:
            logger.warning("Cloud Postgres bootstrap skipped: %s", error)
        return True
    except Exception as e:
        logger.exception("Database initialization failed: %s", e)
        return False


def begin_attendance_ingestion(event_id: str, payload_hash: str) -> Dict[str, Any]:
    """Atomically claim an attendance event ID or return its prior receipt."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload_hash, response_json, response_status "
            "FROM attendance_ingestion_receipts WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO attendance_ingestion_receipts (event_id, payload_hash) VALUES (?, ?)",
                (event_id, payload_hash),
            )
            return {"state": "new"}
        if row["payload_hash"] != payload_hash:
            return {"state": "conflict"}
        if row["response_json"] is None:
            return {"state": "processing"}
        status = int(row["response_status"] or 200)
        if status >= 400:
            conn.execute(
                "DELETE FROM attendance_ingestion_receipts WHERE event_id = ?",
                (event_id,),
            )
            conn.execute(
                "INSERT INTO attendance_ingestion_receipts (event_id, payload_hash) VALUES (?, ?)",
                (event_id, payload_hash),
            )
            return {"state": "new"}
        return {
            "state": "duplicate",
            "response": json.loads(row["response_json"]),
            "status": status,
        }


def complete_attendance_ingestion(
    event_id: str,
    response: Dict[str, Any],
    status_code: int,
) -> None:
    """Persist the result returned for an accepted event ID."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.execute(
            "UPDATE attendance_ingestion_receipts "
            "SET response_json = ?, response_status = ? WHERE event_id = ?",
            (json.dumps(response, sort_keys=True), status_code, event_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Attendance ingestion receipt disappeared before completion.")


def abandon_attendance_ingestion(event_id: str) -> None:
    """Release a receipt if processing failed before a response could be stored."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute(
            "DELETE FROM attendance_ingestion_receipts WHERE event_id = ? AND response_json IS NULL",
            (event_id,),
        )


def add_user(
    name: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    department: Optional[str] = None,
    password: Optional[str] = None,
    role: str = 'staff',
    notes: str = '',
    cnic: str = '',
    position: str = '',
    salary: float = 0,
    join_date: str = '',
    shift_id: int = 1,
    checkin_cameras: str = 'all',
    checkout_cameras: str = 'all',
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None
) -> Optional[int]:
    """Add a new user with validation, HR profile attributes, shift assignment, and camera assignments."""
    if not name or len(name.strip()) == 0:
        logger.warning("Attempted to add user with empty name")
        return None

    clean_name = name.strip()
    try:
        salary = parse_salary_amount(salary, default=0.0)
    except ValueError as salary_err:
        logger.warning("Rejected staff create for '%s': %s", clean_name, salary_err)
        return None
    password = str(password).strip() if password else None
    if password and len(password) < 14:
        logger.warning("Rejected weak password when creating user '%s'", clean_name)
        return None
    password_hash = generate_password_hash(password) if password else None
    org_target = organization_id or 'DEFAULT_ORG'
    branch_target = branch_id or 'DEFAULT_BRANCH'

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            
            # 1. Multi-Tenant Check: Check if user with same name already exists in THIS organization
            cursor.execute(
                "SELECT id, active FROM users WHERE LOWER(TRIM(name)) = LOWER(?) AND (organization_id = ? OR organization_id IS NULL OR organization_id = '')",
                (clean_name, org_target)
            )
            existing = cursor.fetchone()
            if existing:
                existing_id, is_active = existing
                # If previously marked inactive or re-enrolling, re-activate with updated details
                cursor.execute("""
                    UPDATE users 
                    SET active = 1, email = ?, phone = ?, department = ?, password = NULL, password_hash = ?, role = ?,
                        notes = ?, cnic = ?, position = ?, salary = ?, join_date = ?, shift_id = ?,
                        checkin_cameras = ?, checkout_cameras = ?, organization_id = ?, branch_id = ?
                    WHERE id = ?
                """, (email, phone, department, password_hash, role, notes, cnic, position, salary, join_date, shift_id or 1, checkin_cameras or 'all', checkout_cameras or 'all', org_target, branch_target, existing_id))
                effective_from = join_date if join_date else None
                if effective_from:
                    try: datetime.strptime(effective_from, "%Y-%m-%d")
                    except ValueError: effective_from = None
                _record_employee_shift_history(cursor, existing_id, shift_id or 1, effective_from)
                conn.commit()
                logger.info(f"✓ Re-activated/Updated user '{clean_name}' for Org '{org_target}' (ID: {existing_id})")
                _cloud_mirror("mirror_user_id", existing_id)
                return existing_id

            # 2. Insert new user safely scoped to this organization
            cursor.execute(
                '''INSERT INTO users
                   (name, email, phone, department, password, password_hash, role, notes, cnic, position, salary, join_date, shift_id, checkin_cameras, checkout_cameras, organization_id, branch_id)
                   VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (clean_name, email, phone, department, password_hash, role, notes, cnic, position, salary, join_date, shift_id or 1, checkin_cameras or 'all', checkout_cameras or 'all', org_target, branch_target)
            )
            user_id = cursor.lastrowid
            if user_id is None:
                raise RuntimeError("Failed to create employee record")
            effective_from = join_date if join_date else None
            if effective_from:
                try: datetime.strptime(effective_from, "%Y-%m-%d")
                except ValueError: effective_from = None
            _record_employee_shift_history(cursor, user_id, shift_id or 1, effective_from)
            conn.commit()

        logger.info(f"✓ User '{clean_name}' created for Org '{org_target}' (ID: {user_id}, Shift ID: {shift_id})")
        _cloud_mirror("mirror_user_id", user_id)
        return user_id
    except sqlite3.IntegrityError as ie:
        logger.warning(f"Integrity check for user '{clean_name}' under Org '{org_target}': {ie}")
        # As fail-safe, query existing ID if duplicate constraint triggered
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE LOWER(TRIM(name)) = LOWER(?)", (clean_name,))
                row = cursor.fetchone()
                if row:
                    return row[0]
        except Exception:
            pass
        return None
    except Exception as e:
        logger.error(f"Failed to add user: {e}")
        return None


def purge_organization_data(organization_id: str) -> bool:
    """
    Completely purge all staff, embeddings, attendance logs, and camera configs
    when an organization is deleted or wiped from the platform.
    """
    if not organization_id or organization_id in ('DEFAULT_ORG', ''):
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # 1. Delete embeddings
            cursor.execute("""
                DELETE FROM embeddings 
                WHERE organization_id = ? 
                   OR user_id IN (SELECT id FROM users WHERE organization_id = ?)
            """, (organization_id, organization_id))
            # 2. Delete attendance records
            cursor.execute("""
                DELETE FROM attendance 
                WHERE user_id IN (SELECT id FROM users WHERE organization_id = ?)
            """, (organization_id,))
            # 3. Delete shifts
            cursor.execute("DELETE FROM shifts WHERE organization_id = ?", (organization_id,))
            # 4. Delete cameras
            cursor.execute("DELETE FROM cameras WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM payroll_records WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM payroll_runs WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM payroll_rules WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM payroll_scoped_rules WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM payroll_bonuses WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM payroll_employee_components WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM payroll_component_types WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM payroll_adjustments WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM salary_revisions WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM payroll_audit_log WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM payroll_settings WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM special_date_overrides WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM company_holidays WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM leave_types WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM leave_requests WHERE organization_id = ?", (organization_id,))
            cursor.execute("DELETE FROM overtime WHERE organization_id = ?", (organization_id,))
            cursor.execute(
                "DELETE FROM employee_leave_balances WHERE user_id IN (SELECT id FROM users WHERE organization_id = ?)",
                (organization_id,),
            )
            cursor.execute(
                "DELETE FROM salary_configs WHERE user_id IN (SELECT id FROM users WHERE organization_id = ?)",
                (organization_id,),
            )
            # 5. Delete users
            cursor.execute("DELETE FROM users WHERE organization_id = ?", (organization_id,))
            conn.commit()
        logger.info(f"✓ Purged all data for deleted organization '{organization_id}'")
        return True
    except Exception as e:
        logger.error(f"Failed to purge organization '{organization_id}': {e}")
        return False


def get_user_by_name(name: str) -> Optional[Dict]:
    """Retrieve user by name."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, name, email, COALESCE(shift_id, 1), COALESCE(checkin_cameras, "all"), COALESCE(checkout_cameras, "all") FROM users WHERE name = ?', (name,))
            row = cursor.fetchone()
        if row:
            return {'id': row[0], 'name': row[1], 'email': row[2], 'shift_id': row[3], 'checkin_cameras': row[4], 'checkout_cameras': row[5]}
    except Exception as e:
        logger.error(f"Failed to fetch user by name: {e}")
    return None


def get_all_users(role: Optional[str] = None, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict]:
    """Get all active users with today's attendance status, shift details, and camera assignments via optimized join, strictly scoped by organization and branch."""
    if organization_id and organization_id not in ('all', 'DEFAULT_ORG') and branch_id and branch_id not in ('all', 'DEFAULT_BRANCH'):
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cached = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE organization_id = ? AND branch_id = ?",
                    (organization_id, branch_id),
                ).fetchone()[0]
            if cached == 0:
                _cloud_mirror("hydrate_branch_employees", organization_id, branch_id)
        except Exception as error:
            logger.warning("Could not hydrate staff cache from Postgres: %s", error)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            query = '''
                SELECT u.id, u.name, u.email, u.phone, u.department, u.enrollment_date, 
                       u.created_at, u.active, u.notes, u.photo_path,
                       CASE 
                           WHEN ma.user_id IS NOT NULL THEN 'Absent'
                           WHEN a.user_id IS NOT NULL THEN 'Present'
                           ELSE 'Absent'
                       END as status_today,
                       COALESCE(u.position, '') as position,
                       COALESCE(u.cnic, '') as cnic,
                       COALESCE(u.salary, 0) as salary,
                       COALESCE(u.join_date, '') as join_date,
                       COALESCE(u.shift_id, 1) as shift_id,
                       COALESCE(s.name, 'Morning Shift') as shift_name,
                       COALESCE(s.start_time, '09:00') as shift_start,
                       COALESCE(s.end_time, '17:00') as shift_end,
                       COALESCE(s.grace_period_minutes, 15) as shift_grace,
                       COALESCE(u.checkin_cameras, 'all') as checkin_cameras,
                       COALESCE(u.checkout_cameras, 'all') as checkout_cameras,
                       COALESCE(u.organization_id, 'DEFAULT_ORG') as organization_id,
                       COALESCE(u.branch_id, 'DEFAULT_BRANCH') as branch_id,
                       COALESCE((SELECT COUNT(*) FROM embeddings emb WHERE emb.user_id = u.id), 0) as embedding_count
                FROM users u
                LEFT JOIN shifts s ON COALESCE(u.shift_id, 1) = s.id
                LEFT JOIN (
                    SELECT DISTINCT user_id 
                    FROM attendance 
                    WHERE date(timestamp) = date('now', 'localtime')
                ) a ON u.id = a.user_id
                LEFT JOIN (
                    SELECT DISTINCT user_id
                    FROM manual_absent
                    WHERE date = date('now', 'localtime')
                ) ma ON u.id = ma.user_id
                WHERE u.active = 1
            '''
            params = []
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    query += " AND (u.organization_id = 'DEFAULT_ORG' OR u.organization_id IS NULL)"
                else:
                    query += " AND u.organization_id = ?"
                    params.append(organization_id)
            if branch_id and branch_id != 'all':
                if branch_id == 'DEFAULT_BRANCH':
                    query += " AND (u.branch_id = 'DEFAULT_BRANCH' OR u.branch_id IS NULL)"
                else:
                    query += " AND u.branch_id = ?"
                    params.append(branch_id)
            if role:
                query += " AND u.role = ?"
                params.append(role)
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
        return [{
            'id': r[0], 'name': r[1], 'email': r[2], 'phone': r[3],
            'department': r[4], 'enrollment_date': r[5], 'created_at': r[6],
            'active': r[7], 'notes': r[8], 'photo_path': r[9], 'status_today': r[10],
            'position': r[11], 'cnic': r[12], 'salary': r[13], 'join_date': r[14],
            'shift_id': r[15], 'shift_name': r[16], 'shift_start': r[17],
            'shift_end': r[18], 'shift_grace': r[19],
            'checkin_cameras': r[20], 'checkout_cameras': r[21],
            'organization_id': r[22], 'branch_id': r[23],
            'embedding_count': r[24] if len(r) > 24 else 0,
            'has_embedding': (r[24] > 0) if len(r) > 24 else False
        } for r in rows]
    except Exception as e:
        logger.error(f"Failed to get all users: {e}")
        return []


def update_user(user_id: int, name: str, email: str | None = None, phone: str | None = None, department: str | None = None, notes: str | None = None,
                position: str | None = None, cnic: str | None = None, salary: float | None = None, join_date: str | None = None,
                shift_id: int | None = None, checkin_cameras: str | None = None, checkout_cameras: str | None = None,
                shift_effective_from: str | None = None, exit_date: str | None = None,
                confirm_shift_reclassify: bool = False) -> bool:
    """Update user information in database. HR fields and camera assignments left as None are preserved."""
    if not name or len(name.strip()) == 0:
        logger.warning("Attempted to update user with empty name")
        return False
    if salary is not None:
        try:
            salary = parse_salary_amount(salary, default=None)
        except ValueError as salary_err:
            logger.warning("Rejected staff update for ID %s: %s", user_id, salary_err)
            return False

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COALESCE(shift_id, 1) FROM users WHERE id = ?", (user_id,))
            current_shift_row = cursor.fetchone()
            if not current_shift_row:
                return False
            previous_shift_id = current_shift_row[0]
            effective_date = _normalize_shift_effective_date(shift_effective_from)
            persisted_shift_id = shift_id if effective_date <= datetime.now().date().isoformat() else None
            if shift_id is not None and (shift_id != previous_shift_id or shift_effective_from is not None):
                affected_month = effective_date[:7]
                org_row = cursor.execute(
                    "SELECT COALESCE(organization_id,'DEFAULT_ORG'), COALESCE(branch_id,'DEFAULT_BRANCH') FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
                org_id = org_row[0] if org_row else 'DEFAULT_ORG'
                branch_id = org_row[1] if org_row else 'DEFAULT_BRANCH'
                if payroll_month_is_finalized(affected_month, org_id, branch_id):
                    raise ValueError(PAYROLL_LOCK_MESSAGE)
                past_logs = cursor.execute(
                    "SELECT COUNT(*) FROM attendance_logs WHERE user_id = ? AND date(timestamp) >= ?",
                    (user_id, effective_date),
                ).fetchone()[0]
                if past_logs and not confirm_shift_reclassify:
                    raise ValueError(
                        "Changing this shift effective date reclassifies processed attendance. "
                        "Confirm to continue."
                    )
            cursor.execute('''
                UPDATE users 
                SET name = ?, email = ?, phone = ?, department = ?, notes = ?,
                    position = COALESCE(?, position),
                    cnic = COALESCE(?, cnic),
                    salary = COALESCE(?, salary),
                    join_date = COALESCE(?, join_date),
                    exit_date = COALESCE(?, exit_date),
                    shift_id = COALESCE(?, shift_id),
                    checkin_cameras = COALESCE(?, checkin_cameras),
                    checkout_cameras = COALESCE(?, checkout_cameras)
                WHERE id = ?
            ''', (name.strip(), email, phone, department, notes, position, cnic, salary, join_date, exit_date, persisted_shift_id, checkin_cameras, checkout_cameras, user_id))
            updated = cursor.rowcount > 0
            if updated and shift_id is not None and (shift_id != previous_shift_id or shift_effective_from is not None):
                _record_employee_shift_history(cursor, user_id, shift_id, effective_date)
            conn.commit()
            
        if updated:
            logger.info(f"✓ User ID {user_id} updated: {name} (Shift: {shift_id}, In: {checkin_cameras}, Out: {checkout_cameras})")
            _cloud_mirror("mirror_user_id", user_id)
        return updated
    except Exception as e:
        logger.error(f"Failed to update user ID {user_id}: {e}")
        return False


def deactivate_user(user_id: int, exit_date: Optional[str] = None, organization_id: Optional[str] = None) -> bool:
    """Deactivate a staff member. exit_date is required (defaults to today at the API)."""
    if not str(exit_date or '').strip():
        raise ValueError('exit_date is required to deactivate an employee')
    try:
        datetime.strptime(str(exit_date)[:10], '%Y-%m-%d')
    except (TypeError, ValueError) as exc:
        raise ValueError('exit_date must use YYYY-MM-DD') from exc
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET active = 0, exit_date = ? WHERE id = ?",
            (str(exit_date)[:10], user_id),
        )
        updated = cursor.rowcount > 0
        conn.commit()
        return updated


def reactivate_user(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
        updated = cursor.rowcount > 0
        conn.commit()
        return updated


def bulk_assign_cameras(user_ids: List[int] | str, checkin_cameras: str = 'all', checkout_cameras: str = 'all') -> Tuple[bool, int]:
    """
    Bulk assign Check-In and Check-Out cameras to multiple or all employees.
    Pass user_ids='all' or a list of user IDs.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            checkin_val = (checkin_cameras or 'all').strip()
            checkout_val = (checkout_cameras or 'all').strip()

            if user_ids == 'all' or not user_ids or (isinstance(user_ids, list) and len(user_ids) == 0):
                cursor.execute(
                    "UPDATE users SET checkin_cameras = ?, checkout_cameras = ? WHERE active = 1",
                    (checkin_val, checkout_val)
                )
            else:
                placeholders = ','.join('?' for _ in user_ids)
                cursor.execute(
                    f"UPDATE users SET checkin_cameras = ?, checkout_cameras = ? WHERE id IN ({placeholders}) AND active = 1",
                    (checkin_val, checkout_val, *user_ids)
                )
            count = cursor.rowcount
            conn.commit()
            logger.info(f"✓ Bulk camera assignment applied to {count} staff members (In: '{checkin_val}', Out: '{checkout_val}')")
            return True, count
    except Exception as e:
        logger.error(f"Failed to bulk assign cameras: {e}")
        return False, 0


def check_user_camera_allowed(user_id: int, camera_id: str, action: str = 'checkin') -> Tuple[bool, str]:
    """
    Validate whether the detected camera is authorized to mark Check-In or Check-Out for this employee.
    Strict enforcement: If camera is not in the employee's assigned camera list, attendance is blocked.
    """
    if not camera_id:
        return False, "Invalid camera identifier"

    action = action.lower().strip()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(checkin_cameras, 'all'), COALESCE(checkout_cameras, 'all'), name FROM users WHERE id = ?",
                (user_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False, f"User ID {user_id} not found"

            checkin_cams_raw, checkout_cams_raw, user_name = row[0], row[1], row[2]

            # Fetch camera info from cameras table to match by camera_id, room_name, or name
            cam_str = str(camera_id).strip()
            cursor.execute("SELECT camera_id, name, room_name, camera_type, rtsp_url FROM cameras WHERE camera_id = ? OR id = ?", (cam_str, cam_str))
            cam_row = cursor.fetchone()
            
            cam_id_token = cam_str.lower()
            room_token = cam_row[2].lower() if cam_row else ''
            name_token = cam_row[1].lower() if cam_row else ''
            rtsp_token = str(cam_row[4]).lower() if cam_row else ''

        assigned_str = checkin_cams_raw if action == 'checkin' else checkout_cams_raw
        if not assigned_str or assigned_str.strip() == '' or assigned_str.strip().lower() == 'all':
            return True, f"All cameras authorized for {action}"

        allowed_tokens = [t.strip().lower() for t in assigned_str.split(',') if t.strip()]
        if 'all' in allowed_tokens or '*' in allowed_tokens:
            return True, f"All cameras authorized for {action}"

        # Match against camera_id, room_name, camera name, or RTSP index
        for token in allowed_tokens:
            if token == cam_id_token:
                return True, f"Camera '{camera_id}' matches assigned camera ID for {action}"
            if room_token and (token == room_token or token in room_token or room_token in token):
                return True, f"Camera room '{cam_row[2]}' matches assigned room for {action}"
            if name_token and (token == name_token or token in name_token):
                return True, f"Camera name '{cam_row[1]}' matches assigned camera for {action}"
            if rtsp_token and token == rtsp_token:
                return True, f"Camera source matches assigned camera for {action}"

        room_disp = f" ({cam_row[2]})" if cam_row else ""
        action_disp = "Check-In" if action == 'checkin' else "Check-Out"
        logger.info(f"[CAMERA REJECTED] Staff '{user_name}' (ID {user_id}) appeared on '{camera_id}'{room_disp} but camera is NOT authorized for {action_disp} (Assigned: '{assigned_str}')")
        return False, f"Camera '{camera_id}'{room_disp} is not authorized for {action_disp} (Allowed: {assigned_str})"

    except Exception as e:
        logger.error(f"Error checking user camera permissions: {e}")
        return False, f"Camera validation error: {e}"


def delete_user(user_id: int) -> bool:
    """Delete a user, associated embeddings and logs cascade automatically."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("SELECT organization_id FROM users WHERE id = ?", (user_id,))
            org_row = cursor.fetchone()
            org_id = org_row[0] if org_row else None
            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            
        if deleted:
            logger.info(f"✓ User ID {user_id} deleted successfully with related data.")
            _cloud_mirror("delete_employee", user_id, org_id)
        return deleted
    except Exception as e:
        logger.error(f"Failed to delete user ID {user_id}: {e}")
        return False


def store_embedding(user_id: int, embedding: List[float], source_video: str | None = None, organization_id: Optional[str] = None, branch_id: Optional[str] = None):
    """Store face embedding for a user with tenant organization_id and branch_id."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if not organization_id or not branch_id:
                cursor.execute('SELECT organization_id, branch_id FROM users WHERE id = ?', (user_id,))
                urow = cursor.fetchone()
                if urow:
                    organization_id = organization_id or urow[0] or 'DEFAULT_ORG'
                    branch_id = branch_id or urow[1] or 'DEFAULT_BRANCH'
                else:
                    organization_id = organization_id or 'DEFAULT_ORG'
                    branch_id = branch_id or 'DEFAULT_BRANCH'
            embedding_blob = json.dumps(embedding).encode()
            cursor.execute(
                'INSERT INTO embeddings (user_id, embedding, source_video, organization_id, branch_id) VALUES (?, ?, ?, ?, ?)',
                (user_id, embedding_blob, source_video, organization_id, branch_id)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to store embedding for user {user_id}: {e}")


def get_embeddings_for_user(user_id: int, organization_id: Optional[str] = None) -> List[Dict]:
    """Retrieve all embeddings for a user, verifying tenant ownership if organization_id is provided."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT id FROM users WHERE id = ? AND (organization_id = 'DEFAULT_ORG' OR organization_id IS NULL)", (user_id,))
                else:
                    cursor.execute("SELECT id FROM users WHERE id = ? AND organization_id = ?", (user_id, organization_id))
                if not cursor.fetchone():
                    return []
            cursor.execute(
                'SELECT id, embedding, source_video, created_at, organization_id, branch_id FROM embeddings WHERE user_id = ?',
                (user_id,)
            )
            rows = cursor.fetchall()
            
        return [{
            'id': row[0],
            'embedding': json.loads(row[1].decode()),
            'source_video': row[2],
            'created_at': row[3],
            'organization_id': row[4],
            'branch_id': row[5]
        } for row in rows]
    except Exception as e:
        logger.error(f"Failed to get embeddings for user {user_id}: {e}")
        return []


# ============================================
# SHIFT MANAGEMENT & VALIDATION FUNCTIONS
# ============================================

def parse_time_to_minutes(time_str: str) -> int:
    """Convert any time string (24h '20:11', 12h '08:11 PM', '8:11 pm', '08:11:11') to minutes from midnight."""
    if not time_str:
        return 0
    try:
        t_str = str(time_str).strip().upper()
        is_pm = 'PM' in t_str
        is_am = 'AM' in t_str
        clean_str = t_str.replace('PM', '').replace('AM', '').strip()
        parts = clean_str.split(':')
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        if is_pm and hours < 12:
            hours += 12
        elif is_am and hours == 12:
            hours = 0
        return (hours % 24) * 60 + (minutes % 60)
    except Exception:
        return 0


def is_time_in_grace_window(start_time_str: str, grace_minutes: int, check_dt: Optional[datetime] = None, end_time_str: Optional[str] = None) -> bool:
    """
    Check if check_dt (defaults to local now) falls within allowed shift/grace window.
    If grace_minutes > 0: allowed within [start_time, start_time + grace_minutes].
    If grace_minutes <= 0 (disabled): allowed during full shift [start_time, end_time].
    """
    if check_dt is None:
        check_dt = datetime.now()
    
    try:
        start_minutes = parse_time_to_minutes(start_time_str)
        current_minutes = check_dt.hour * 60 + check_dt.minute

        if grace_minutes <= 0 and end_time_str:
            end_minutes = parse_time_to_minutes(end_time_str)
            if start_minutes <= end_minutes:
                return start_minutes <= current_minutes <= end_minutes
            else:
                return current_minutes >= start_minutes or current_minutes <= end_minutes

        grace_end_minutes = start_minutes + max(0, int(grace_minutes))
        
        if grace_end_minutes < 1440:
            return start_minutes <= current_minutes <= grace_end_minutes
        else:
            end_minutes_next_day = grace_end_minutes % 1440
            return current_minutes >= start_minutes or current_minutes <= end_minutes_next_day
    except Exception as e:
        logger.error(f"Error checking grace window for {start_time_str} + {grace_minutes}m: {e}")
        return True


def check_user_shift_allowed(user_id: int, dt: Optional[datetime] = None) -> Tuple[bool, str, Optional[Dict]]:
    """
    Check if attendance can be marked for user_id at datetime dt (default now).
    Returns (is_allowed: bool, reason: str, shift_info: dict).
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT u.id, u.name, COALESCE(u.shift_id, 1),
                       s.id, s.name, s.start_time, s.end_time, s.grace_period_minutes
                FROM users u
                LEFT JOIN shifts s ON COALESCE(u.shift_id, 1) = s.id
                WHERE u.id = ?
            ''', (user_id,))
            row = cursor.fetchone()

        if not row:
            return False, "User not found", None

        shift_id = row[3] or 1
        shift_name = row[4] or "Morning Shift"
        start_time = row[5] or "09:00"
        end_time = row[6] or "17:00"
        grace_minutes = row[7] if row[7] is not None else 15

        shift_info = {
            'id': shift_id,
            'name': shift_name,
            'start_time': start_time,
            'end_time': end_time,
            'grace_period_minutes': grace_minutes
        }

        allowed = is_time_in_grace_window(start_time, grace_minutes, dt, end_time)
        if allowed:
            return True, f"Allowed: Within '{shift_name}' window", shift_info
        else:
            if grace_minutes > 0:
                try:
                    start_mins = parse_time_to_minutes(start_time)
                    em_total = start_mins + grace_minutes
                    end_grace_str = f"{(em_total // 60) % 24:02d}:{em_total % 60:02d}"
                except Exception:
                    end_grace_str = f"{start_time}+{grace_minutes}m"
                reason_str = f"Outside '{shift_name}' grace window ({start_time} - {end_grace_str})"
            else:
                reason_str = f"Outside '{shift_name}' shift hours ({start_time} - {end_time})"

            return False, f"Attendance rejected: {reason_str}", shift_info

    except Exception as e:
        logger.error(f"Error checking user shift for user {user_id}: {e}")
        return True, f"Shift check error: {e}", None


def _normalize_shift_working_days(working_days: Any = None) -> List[int]:
    if working_days is None:
        return default_working_days()
    try:
        return parse_working_days(working_days)
    except WorkingDaysError as exc:
        raise ValueError(str(exc)) from exc


def _normalize_shift_effective_date(effective_from: Optional[str] = None) -> str:
    effective_date = effective_from or datetime.now().date().isoformat()
    try:
        return datetime.strptime(effective_date, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError) as e:
        raise ValueError("Shift effective date must use YYYY-MM-DD format") from e


def _record_employee_shift_history(
    cursor: sqlite3.Cursor,
    user_id: int,
    shift_id: int,
    effective_from: Optional[str] = None,
    history_source: str = 'recorded',
) -> None:
    effective_date = _normalize_shift_effective_date(effective_from)
    cursor.execute(
        "SELECT name, start_time, end_time, grace_period_minutes, checkout_grace_period_minutes, working_days FROM shifts WHERE id = ?",
        (shift_id,),
    )
    shift = cursor.fetchone()
    if not shift:
        raise ValueError("Shift not found")
    cursor.execute("SELECT organization_id, branch_id FROM users WHERE id = ?", (user_id,))
    u_row = cursor.fetchone()
    u_org = (u_row[0] if u_row else None) or 'DEFAULT_ORG'
    u_branch = (u_row[1] if u_row else None) or 'DEFAULT_BRANCH'
    cursor.execute('''
        INSERT INTO employee_shift_history (
            user_id, shift_id, effective_from, shift_name, start_time, end_time,
            grace_period_minutes, checkout_grace_period_minutes, working_days, history_source,
            organization_id, branch_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, effective_from) DO UPDATE SET
            shift_id = excluded.shift_id,
            shift_name = excluded.shift_name,
            start_time = excluded.start_time,
            end_time = excluded.end_time,
            grace_period_minutes = excluded.grace_period_minutes,
            checkout_grace_period_minutes = excluded.checkout_grace_period_minutes,
            working_days = excluded.working_days,
            history_source = excluded.history_source,
            organization_id = excluded.organization_id,
            branch_id = excluded.branch_id
    ''', (user_id, shift_id, effective_date, shift[0], shift[1], shift[2], shift[3] or 15, shift[4] or 0,
          shift[5] or '[0,1,2,3,4,5]', history_source, u_org, u_branch))


def _resolve_calendar_override(
    calendar_overrides: Dict[Tuple[str, str, int], str],
    calendar_date: str,
    employee_id: int,
    shift_id: int,
) -> Optional[str]:
    return (
        calendar_overrides.get((calendar_date, 'employee', employee_id))
        or calendar_overrides.get((calendar_date, 'shift', shift_id))
        or calendar_overrides.get((calendar_date, 'all', 0))
    )


def _get_company_holiday_for_date(
    cursor: sqlite3.Cursor,
    overtime_date: str,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find a scoped company holiday covering a date, including recurring holidays."""
    date_key = overtime_date[5:]
    query = "SELECT * FROM company_holidays WHERE 1 = 1"
    params: List[Any] = []
    if organization_id is not None:
        query += " AND COALESCE(organization_id, 'DEFAULT_ORG') = ?"
        params.append(organization_id)
    if branch_id is not None:
        query += " AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?"
        params.append(branch_id)
    query += " ORDER BY holiday_date, id"
    cursor.execute(query, params)
    for row in cursor.fetchall():
        holiday = dict(row)
        holiday_start = str(holiday.get('holiday_date') or '')
        holiday_end = str(holiday.get('end_date') or holiday_start)
        if holiday_start <= overtime_date <= holiday_end:
            return holiday
        if holiday.get('is_recurring'):
            start_key, end_key = holiday_start[5:], holiday_end[5:]
            recurring_match = (
                start_key <= date_key <= end_key
                if start_key <= end_key
                else date_key >= start_key or date_key <= end_key
            )
            if recurring_match:
                return holiday
    return None


def _get_overtime_premium_percent(
    cursor: sqlite3.Cursor,
    overtime_date: str,
    day_type: str,
    settings: Optional[Any],
    holiday: Optional[Dict[str, Any]] = None,
) -> float:
    """Return a holiday override or the configured day-type premium fallback."""
    if day_type == 'holiday':
        if holiday and holiday.get('overtime_premium_percent') is not None:
            return float(holiday['overtime_premium_percent'])
        if settings and settings['holiday_ot_premium_active']:
            return float(settings['holiday_ot_premium_percent'] or 0.0)
    elif day_type == 'non_working_day' and settings and settings['non_working_day_ot_premium_active']:
        return float(settings['non_working_day_ot_premium_percent'] or 0.0)
    return 0.0


def _get_overtime_day_type(cursor: sqlite3.Cursor, user_id: int, overtime_date: str) -> str:
    """Classify overtime by its effective shift schedule and calendar exceptions."""
    try:
        cursor.execute(
            "SELECT COALESCE(organization_id, 'DEFAULT_ORG'), COALESCE(branch_id, 'DEFAULT_BRANCH') "
            "FROM users WHERE id = ?",
            (user_id,),
        )
        owner = cursor.fetchone()
        if not owner:
            return 'regular'
        organization_id, branch_id = owner[0], owner[1]
        work_date = datetime.strptime(overtime_date, "%Y-%m-%d").date()
        cursor.execute('''
            SELECT shift_id, working_days FROM employee_shift_history
            WHERE user_id = ? AND effective_from <= ?
            ORDER BY effective_from DESC, id DESC LIMIT 1
        ''', (user_id, overtime_date))
        history = cursor.fetchone()
        if history:
            shift_id, working_days = history['shift_id'], history['working_days']
        else:
            cursor.execute('''
                SELECT COALESCE(u.shift_id, 1) AS shift_id,
                       COALESCE(s.working_days, '[0,1,2,3,4,5]') AS working_days
                FROM users u LEFT JOIN shifts s ON s.id = COALESCE(u.shift_id, 1)
                WHERE u.id = ?
            ''', (user_id,))
            current = cursor.fetchone()
            if not current:
                return 'regular'
            shift_id, working_days = current['shift_id'], current['working_days']

        cursor.execute('''
            SELECT calendar_date, override_type, scope_type, scope_id
            FROM special_date_overrides
            WHERE calendar_date = ?
              AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
              AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
        ''', (overtime_date, organization_id, branch_id))
        overrides = {
            (row['calendar_date'], row['scope_type'], row['scope_id']): row['override_type']
            for row in cursor.fetchall()
        }
        override_type = _resolve_calendar_override(overrides, overtime_date, user_id, int(shift_id or 1))
        if override_type == 'holiday':
            return 'holiday'
        if override_type == 'working_day':
            return 'regular'
        if override_type == 'off_day':
            return 'non_working_day'

        if _get_company_holiday_for_date(cursor, overtime_date, organization_id, branch_id):
            return 'holiday'

        working_days_set = set(_normalize_shift_working_days(working_days))
        return 'regular' if work_date.weekday() in working_days_set else 'non_working_day'
    except Exception as e:
        logger.error(f"Failed to classify overtime date {overtime_date} for user {user_id}: {e}")
        return 'regular'


def _is_scheduled_workday(work_date, working_days: Any, override_type: Optional[str] = None) -> bool:
    if override_type == 'working_day':
        return True
    if override_type in ('off_day', 'holiday'):
        return False
    weekdays = working_days if isinstance(working_days, set) else set(_normalize_shift_working_days(working_days))
    return work_date.weekday() in weekdays


def get_all_shifts(
    active_only: bool = True,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict]:
    """Retrieve shifts with employee counts scoped to the requested organization and branch."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            where_clauses = []
            params = []
            join_conditions = [
                "COALESCE(u.shift_id, 1) = s.id",
                "u.active = 1",
            ]
            join_params = []
            if active_only:
                where_clauses.append("s.active = 1")
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    where_clauses.append("(s.organization_id = 'DEFAULT_ORG' OR s.organization_id IS NULL OR s.id = 1)")
                    join_conditions.append("COALESCE(NULLIF(u.organization_id, ''), 'DEFAULT_ORG') = 'DEFAULT_ORG'")
                else:
                    where_clauses.append("(s.organization_id = ? OR s.id = 1)")
                    params.append(organization_id)
                    join_conditions.append("u.organization_id = ?")
                    join_params.append(organization_id)
            else:
                join_conditions.extend([
                    "COALESCE(NULLIF(u.organization_id, ''), 'DEFAULT_ORG') = COALESCE(NULLIF(s.organization_id, ''), 'DEFAULT_ORG')",
                    "COALESCE(NULLIF(u.branch_id, ''), 'DEFAULT_BRANCH') = COALESCE(NULLIF(s.branch_id, ''), 'DEFAULT_BRANCH')",
                ])
            if branch_id and branch_id != 'all':
                where_clauses.append("(s.branch_id = ? OR s.id = 1)")
                params.append(branch_id)
                join_conditions.append("u.branch_id = ?")
                join_params.append(branch_id)
            where_clause = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            join_clause = " AND ".join(join_conditions)
            cursor.execute(f'''
                SELECT s.id, s.name, s.start_time, s.end_time, s.grace_period_minutes, 
                       COALESCE(s.is_default, CASE WHEN s.id = 1 THEN 1 ELSE 0 END) as is_default,
                       s.active, s.created_at,
                       COUNT(u.id) as user_count, s.working_days, s.checkout_grace_period_minutes,
                       COALESCE(s.organization_id, 'DEFAULT_ORG') as organization_id
                FROM shifts s
                LEFT JOIN users u ON {join_clause}
                {where_clause}
                GROUP BY s.id
                ORDER BY s.id ASC
            ''', join_params + params)
            rows = cursor.fetchall()

        return [{
            'id': r[0],
            'name': r[1],
            'start_time': r[2],
            'end_time': r[3],
            'grace_period_minutes': r[4],
            'is_default': bool(r[5]),
            'active': r[6],
            'created_at': r[7],
            'user_count': r[8],
            'working_days': _normalize_shift_working_days(r[9]),
            'checkout_grace_period_minutes': r[10] or 0,
            'organization_id': r[11]
        } for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch shifts: {e}")
        return []


def set_default_shift(shift_id: int) -> bool:
    """Set a specific shift as the organization-wide default shift."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE shifts SET is_default = 0")
            cursor.execute("UPDATE shifts SET is_default = 1 WHERE id = ?", (shift_id,))
            conn.commit()
            logger.info(f"✓ Shift ID #{shift_id} set as system default shift")
            return True
    except Exception as e:
        logger.error(f"Failed to set default shift {shift_id}: {e}")
        return False


def get_default_shift() -> Optional[Dict]:
    """Retrieve current system default shift."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, start_time, end_time, grace_period_minutes, is_default, active FROM shifts WHERE is_default = 1 AND active = 1 LIMIT 1")
            row = cursor.fetchone()
            if not row:
                cursor.execute("SELECT id, name, start_time, end_time, grace_period_minutes, is_default, active FROM shifts WHERE active = 1 ORDER BY id ASC LIMIT 1")
                row = cursor.fetchone()
            if row:
                return {
                    'id': row[0],
                    'name': row[1],
                    'start_time': row[2],
                    'end_time': row[3],
                    'grace_period_minutes': row[4],
                    'is_default': bool(row[5]),
                    'active': row[6]
                }
    except Exception as e:
        logger.error(f"Failed to get default shift: {e}")
    return None


def get_shift_by_id(shift_id: int, organization_id: Optional[str] = None) -> Optional[Dict]:
    """Retrieve a single shift by ID, optionally verifying tenant isolation."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, name, start_time, end_time, grace_period_minutes, active, organization_id FROM shifts WHERE id = ?', (shift_id,))
            row = cursor.fetchone()
            if row:
                shift_dict = {
                    'id': row[0],
                    'name': row[1],
                    'start_time': row[2],
                    'end_time': row[3],
                    'grace_period_minutes': row[4],
                    'active': row[5],
                    'organization_id': row[6] or 'DEFAULT_ORG'
                }
                if organization_id and organization_id != 'all' and shift_dict['id'] != 1:
                    if organization_id == 'DEFAULT_ORG':
                        if shift_dict['organization_id'] not in ('DEFAULT_ORG', None):
                            return None
                    elif shift_dict['organization_id'] != organization_id:
                        return None
                return shift_dict
    except Exception as e:
        logger.error(f"Failed to fetch shift ID {shift_id}: {e}")
    return None


def add_shift(name: str, start_time: str, end_time: str, grace_period_minutes: int = 15, checkout_grace_period_minutes: int = 0, working_days: Any = None, organization_id: Optional[str] = 'DEFAULT_ORG', branch_id: Optional[str] = 'DEFAULT_BRANCH') -> Optional[int]:
    """Add a new custom shift with tenant organization_id and branch_id."""
    if not name or not name.strip():
        logger.warning("Attempted to add shift with empty name")
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO shifts (name, start_time, end_time, grace_period_minutes, checkout_grace_period_minutes, working_days, organization_id, branch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (name.strip(), start_time, end_time, max(0, int(grace_period_minutes)), max(0, int(checkout_grace_period_minutes)),
                 json.dumps(_normalize_shift_working_days(working_days)), organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH')
            )
            conn.commit()
            shift_id = cursor.lastrowid
            logger.info(f"✓ Shift '{name}' created for Org '{organization_id or 'DEFAULT_ORG'}' (ID: {shift_id})")
            _cloud_mirror("mirror_shift", {
                "id": shift_id,
                "name": name.strip(),
                "start_time": start_time,
                "end_time": end_time,
                "grace_period_minutes": grace_period_minutes,
                "organization_id": organization_id or "DEFAULT_ORG",
                "branch_id": branch_id or "DEFAULT_BRANCH",
            })
            return shift_id
    except ValueError:
        raise
    except sqlite3.IntegrityError:
        logger.warning(f"Shift '{name}' already exists")
        return None
    except Exception as e:
        logger.error(f"Failed to add shift: {e}")
        return None


def update_shift(shift_id: int, name: str, start_time: str, end_time: str, grace_period_minutes: int = 15, checkout_grace_period_minutes: int = 0, working_days: Any = None, organization_id: Optional[str] = None) -> bool:
    """Update custom shift configuration, ensuring tenant authorization."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id and organization_id != 'all' and shift_id != 1:
                cursor.execute("SELECT organization_id FROM shifts WHERE id = ?", (shift_id,))
                s_row = cursor.fetchone()
                if not s_row or (s_row[0] and s_row[0] not in (organization_id, 'DEFAULT_ORG')):
                    return False
            if working_days is None:
                cursor.execute(
                    'UPDATE shifts SET name = ?, start_time = ?, end_time = ?, grace_period_minutes = ?, checkout_grace_period_minutes = ? WHERE id = ?',
                    (name.strip(), start_time, end_time, max(0, int(grace_period_minutes)), max(0, int(checkout_grace_period_minutes)), shift_id)
                )
            else:
                cursor.execute(
                    'UPDATE shifts SET name = ?, start_time = ?, end_time = ?, grace_period_minutes = ?, checkout_grace_period_minutes = ?, working_days = ? WHERE id = ?',
                    (name.strip(), start_time, end_time, max(0, int(grace_period_minutes)), max(0, int(checkout_grace_period_minutes)),
                     json.dumps(_normalize_shift_working_days(working_days)), shift_id)
                )
            updated = cursor.rowcount > 0
            if updated:
                cursor.execute("SELECT id FROM users WHERE COALESCE(shift_id, 1) = ?", (shift_id,))
                for (user_id,) in cursor.fetchall():
                    _record_employee_shift_history(cursor, user_id, shift_id)
            conn.commit()
            if updated:
                logger.info(f"✓ Shift ID {shift_id} updated: {name}")
            return updated
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to update shift ID {shift_id}: {e}")
        return False


def delete_shift(shift_id: int, organization_id: Optional[str] = None) -> bool:
    """Delete shift (reassigns users of this shift back to shift ID 1), ensuring tenant authorization."""
    if shift_id == 1:
        logger.warning("Cannot delete default Morning Shift")
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id and organization_id != 'all':
                cursor.execute("SELECT organization_id FROM shifts WHERE id = ?", (shift_id,))
                s_row = cursor.fetchone()
                if not s_row or (s_row[0] and s_row[0] not in (organization_id, 'DEFAULT_ORG')):
                    return False
            cursor.execute("SELECT id FROM users WHERE shift_id = ?", (shift_id,))
            reassigned_users = [row[0] for row in cursor.fetchall()]
            cursor.execute('UPDATE users SET shift_id = 1 WHERE shift_id = ?', (shift_id,))
            for user_id in reassigned_users:
                _record_employee_shift_history(cursor, user_id, 1)
            cursor.execute('DELETE FROM shifts WHERE id = ?', (shift_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            if deleted:
                logger.info(f"✓ Shift ID {shift_id} deleted successfully.")
            return deleted
    except Exception as e:
        logger.error(f"Failed to delete shift ID {shift_id}: {e}")
        return False


def assign_user_shift(user_id: int, shift_id: int, effective_from: Optional[str] = None) -> bool:
    """Assign a shift to an individual user immediately in the database."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            effective_date = _normalize_shift_effective_date(effective_from)
            cursor.execute("SELECT COALESCE(shift_id, 1) FROM users WHERE id = ? AND active = 1", (user_id,))
            current_shift = cursor.fetchone()
            if not current_shift:
                return False
            is_current = effective_date <= datetime.now().date().isoformat()
            if is_current:
                cursor.execute("UPDATE users SET shift_id = ? WHERE id = ? AND active = 1", (shift_id, user_id))
            updated = True
            if effective_date > datetime.now().date().isoformat() or current_shift[0] != shift_id or effective_from is not None:
                _record_employee_shift_history(cursor, user_id, shift_id, effective_date)
            conn.commit()
            if updated:
                logger.info(f"✓ Shift ID {shift_id} assigned to User ID {user_id}")
            return updated
    except Exception as e:
        logger.error(f"Failed to assign shift {shift_id} to user {user_id}: {e}")
        return False


def bulk_assign_shifts(user_ids: Union[str, List[int]], shift_id: int, effective_from: Optional[str] = None) -> Tuple[bool, int]:
    """Bulk assign shift to multiple or all employees."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            effective_date = _normalize_shift_effective_date(effective_from)
            if user_ids == 'all' or not user_ids or (isinstance(user_ids, list) and len(user_ids) == 0):
                cursor.execute("SELECT id, COALESCE(shift_id, 1) FROM users WHERE active = 1")
                selected_users = cursor.fetchall()
                if effective_date <= datetime.now().date().isoformat():
                    cursor.execute("UPDATE users SET shift_id = ? WHERE active = 1", (shift_id,))
            else:
                placeholders = ','.join('?' for _ in user_ids)
                cursor.execute(
                    f"SELECT id, COALESCE(shift_id, 1) FROM users WHERE id IN ({placeholders}) AND active = 1",
                    (*user_ids,),
                )
                selected_users = cursor.fetchall()
                if effective_date <= datetime.now().date().isoformat():
                    cursor.execute(f"UPDATE users SET shift_id = ? WHERE id IN ({placeholders}) AND active = 1", (shift_id, *user_ids))
            count = len(selected_users)
            for user_id, current_shift_id in selected_users:
                if effective_date > datetime.now().date().isoformat() or current_shift_id != shift_id or effective_from is not None:
                    _record_employee_shift_history(cursor, user_id, shift_id, effective_date)
            conn.commit()
            logger.info(f"✓ Bulk shift ID {shift_id} assigned to {count} staff members")
            return True, count
    except Exception as e:
        logger.error(f"Failed to bulk assign shift: {e}")
        return False, 0


def _record_automatic_checkout_overtime(
    cursor: sqlite3.Cursor,
    user_id: int,
    user_name: str,
    checkout_time: datetime,
) -> float:
    actual_date = checkout_time.strftime('%Y-%m-%d')
    actual_time = checkout_time.strftime('%H:%M')
    cursor.execute('''
        SELECT start_time, end_time, checkout_grace_period_minutes
        FROM employee_shift_history
        WHERE user_id = ? AND effective_from <= ?
        ORDER BY effective_from DESC, id DESC LIMIT 1
    ''', (user_id, actual_date))
    shift = cursor.fetchone()
    if not shift:
        cursor.execute('''
            SELECT s.start_time, s.end_time, s.checkout_grace_period_minutes
            FROM users u LEFT JOIN shifts s ON s.id = COALESCE(u.shift_id, 1)
            WHERE u.id = ?
        ''', (user_id,))
        shift = cursor.fetchone()
    if not shift:
        return 0.0

    shift_start, shift_end, grace_minutes = shift[0], shift[1], shift[2] or 0
    overtime_hours = calculate_eligible_overtime(shift_start, shift_end, actual_time, grace_minutes)
    if overtime_hours <= 0:
        return 0.0

    reason = f"Automatic camera checkout at {actual_time}"
    cursor.execute(
        "SELECT id, status, reason FROM overtime WHERE user_id = ? AND ot_date = ? ORDER BY id LIMIT 1",
        (user_id, actual_date),
    )
    existing = cursor.fetchone()
    limit_status = _get_overtime_limit_status_for_cursor(
        cursor, user_id, actual_date, overtime_hours,
        exclude_id=existing[0] if existing else None,
    )
    if limit_status.get('exceeded'):
        reason += limit_status.get('review_note', '')
    if existing:
        if str(existing[1] or 'pending').lower() == 'pending' and str(existing[2] or '').startswith('Automatic camera checkout'):
            cursor.execute(
                "UPDATE overtime SET hours = ?, reason = ? WHERE id = ?",
                (overtime_hours, reason, existing[0]),
            )
        return overtime_hours

    cursor.execute("SELECT organization_id, branch_id FROM users WHERE id = ?", (user_id,))
    u_ot_row = cursor.fetchone()
    u_ot_org = (u_ot_row[0] if u_ot_row else None) or 'DEFAULT_ORG'
    u_ot_branch = (u_ot_row[1] if u_ot_row else None) or 'DEFAULT_BRANCH'
    cursor.execute(
        "INSERT INTO overtime (user_id, user_name, ot_date, hours, reason, status, organization_id, branch_id) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        (user_id, user_name, actual_date, overtime_hours, reason, u_ot_org, u_ot_branch),
    )
    return overtime_hours


def log_attendance(user_id: int, detected_name: str, confidence: float, source: str = 'camera', location: str | None = None, device_id: str | None = None, bypass_shift_check: bool = False, checkout_camera_id: str | None = None, checkout_location: str | None = None, attendance_time: Optional[datetime] = None) -> Tuple[bool, str]:
    """Log attendance detection with room location, device ID, and shift grace period verification."""
    if attendance_time is None:
        attendance_time = datetime.now()
    if not bypass_shift_check:
        allowed, reason, shift_info = check_user_shift_allowed(user_id, dt=attendance_time)
        if not allowed:
            logger.info(f"Skipped attendance log for '{detected_name}' (User ID {user_id}): {reason}")
            return False, reason

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM manual_absent WHERE user_id = ? AND date = date(?)',
                (user_id, attendance_time.strftime('%Y-%m-%d'))
            )
            cursor.execute("SELECT organization_id, branch_id FROM users WHERE id = ?", (user_id,))
            u_row = cursor.fetchone()
            u_org = (u_row[0] if u_row else None) or 'DEFAULT_ORG'
            u_branch = (u_row[1] if u_row else None) or 'DEFAULT_BRANCH'
            cursor.execute(
                '''INSERT INTO attendance (
                       user_id, detected_name, confidence, source, location, device_id,
                       checkout_camera_id, checkout_location, timestamp, organization_id, branch_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, detected_name, confidence, source, location, device_id,
                 checkout_camera_id, checkout_location, attendance_time.strftime('%Y-%m-%d %H:%M:%S'),
                 u_org, u_branch)
            )
            att_id = cursor.lastrowid
            if checkout_camera_id:
                overtime_hours = _record_automatic_checkout_overtime(
                    cursor, user_id, detected_name, attendance_time
                )

            # PHASE 2.3: Transactional Outbox Enqueue
            ev_id = f"ATT-{u_org}-{u_branch}-{att_id}-{int(attendance_time.timestamp())}"
            payload = {
                "local_attendance_id": att_id,
                "user_id": user_id,
                "detected_name": detected_name,
                "confidence": float(confidence),
                "source": source,
                "location": location or "",
                "device_id": device_id or "",
                "checkout_camera_id": checkout_camera_id or "",
                "checkout_location": checkout_location or "",
                "timestamp": attendance_time.strftime('%Y-%m-%d %H:%M:%S'),
                "organization_id": u_org,
                "branch_id": u_branch
            }
            enqueue_sync_outbox(
                cursor=cursor,
                organization_id=u_org,
                branch_id=u_branch,
                event_type="CHECK_IN" if not checkout_camera_id else "CHECK_OUT",
                payload=payload,
                event_id=ev_id
            )

            conn.commit()
            logger.info(f"✓ Attendance logged for '{detected_name}' (User ID {user_id}) [Outbox: {ev_id}]")
            if checkout_camera_id and overtime_hours > 0:
                logger.info(f"✓ Pending automatic overtime created for '{detected_name}': {overtime_hours:.2f} hour(s)")
            return True, "Attendance marked successfully"
    except Exception as e:
        logger.error(f"Failed to log attendance for user {user_id}: {e}")
        return False, f"Database error: {e}"


def get_latest_user_location(user_id: int) -> Optional[Dict]:
    """Get the most recent location/room detection log for a user from database history."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT location, device_id, timestamp, confidence, source
                FROM attendance
                WHERE user_id = ? AND location IS NOT NULL AND location != ''
                ORDER BY timestamp DESC LIMIT 1
            ''', (user_id,))
            row = cursor.fetchone()
            if row:
                return {
                    'location': row[0],
                    'device_id': row[1],
                    'timestamp': row[2],
                    'confidence': row[3],
                    'source': row[4]
                }
    except Exception as e:
        logger.error(f"Failed to fetch latest location for user {user_id}: {e}")
    return None


def record_locator_snapshot(user_id: int, name: str, department: str, camera_id: str,
                            camera_name: str, room_name: str, confidence: float,
                            first_seen: float, last_seen: float, interval_seconds: int = 300) -> bool:
    """Persist at most one room snapshot per user during each five-minute interval."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT recorded_at FROM locator_history
                WHERE user_id = ?
                ORDER BY recorded_at DESC LIMIT 1
            ''', (user_id,))
            row = cursor.fetchone()
            if row:
                cursor.execute("SELECT (julianday('now', 'localtime') - julianday(?)) * 86400", (row[0],))
                elapsed = cursor.fetchone()[0] or 0
                if elapsed < interval_seconds:
                    return False

            cursor.execute('''
                INSERT INTO locator_history
                    (user_id, name, department, camera_id, camera_name, room_name,
                     confidence, first_seen, last_seen, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch', 'localtime'),
                        datetime(?, 'unixepoch', 'localtime'), datetime('now', 'localtime'))
            ''', (user_id, name, department or '', camera_id, camera_name, room_name,
                  float(confidence or 0), first_seen, last_seen))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to record locator history for user {user_id}: {e}")
        return False


def get_locator_history(user_id: Optional[int] = None, room_name: Optional[str] = None, limit: int = 500) -> List[Dict]:
    """Return persistent room snapshots, newest first."""
    try:
        clauses = []
        params = []
        if user_id is not None:
            clauses.append('user_id = ?')
            params.append(user_id)
        if room_name:
            clauses.append('room_name = ?')
            params.append(room_name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT id, user_id, name, department, camera_id, camera_name,
                       room_name, confidence, first_seen, last_seen, recorded_at
                FROM locator_history
                {where}
                ORDER BY recorded_at DESC LIMIT ?
            ''', (*params, max(1, min(limit, 2000))))
            rows = cursor.fetchall()
        return [{
            'id': r[0], 'user_id': r[1], 'name': r[2], 'department': r[3],
            'camera_id': r[4], 'camera_name': r[5], 'room_name': r[6],
            'confidence': r[7], 'first_seen': r[8], 'last_seen': r[9],
            'recorded_at': r[10]
        } for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch locator history: {e}")
        return []


def get_attendance_logs(limit: int = 100, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict]:
    """Retrieve recent attendance logs with location and device, strictly scoped by organization and branch."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            query = '''
                SELECT a.id, u.name, a.detected_name, a.confidence, a.timestamp, a.source, a.location, a.device_id
                FROM attendance a
                JOIN users u ON a.user_id = u.id
                WHERE 1=1
            '''
            params = []
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    query += " AND (a.organization_id = 'DEFAULT_ORG' OR a.organization_id IS NULL OR u.organization_id = 'DEFAULT_ORG' OR u.organization_id IS NULL)"
                else:
                    query += " AND (a.organization_id = ? OR u.organization_id = ?)"
                    params.extend([organization_id, organization_id])
            if branch_id and branch_id != 'all':
                if branch_id == 'DEFAULT_BRANCH':
                    query += " AND (a.branch_id = 'DEFAULT_BRANCH' OR a.branch_id IS NULL OR u.branch_id = 'DEFAULT_BRANCH' OR u.branch_id IS NULL)"
                else:
                    query += " AND (a.branch_id = ? OR u.branch_id = ?)"
                    params.extend([branch_id, branch_id])
            query += " ORDER BY a.timestamp DESC LIMIT ?"
            params.append(limit)
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
        def serialize_timestamp(value):
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone().isoformat(timespec='seconds')
            except (TypeError, ValueError):
                return str(value)

        return [{
            'id': r[0], 'name': r[1], 'detected_name': r[2],
            'confidence': r[3], 'timestamp': serialize_timestamp(r[4]), 'source': r[5],
            'location': r[6], 'device_id': r[7]
        } for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch logs: {e}")
        return []


def get_attendance_by_user(
    user_id: int,
    days: int = 7,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> List[Dict]:
    """Get attendance logs for a user in the last N days, or between start_date and end_date (YYYY-MM-DD), verifying tenant ownership."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT id FROM users WHERE id = ? AND (organization_id = 'DEFAULT_ORG' OR organization_id IS NULL)", (user_id,))
                else:
                    cursor.execute("SELECT id FROM users WHERE id = ? AND organization_id = ?", (user_id, organization_id))
                if not cursor.fetchone():
                    return []
            if start_date and end_date:
                cursor.execute('''
                    SELECT id, timestamp, confidence, source FROM attendance
                    WHERE user_id = ?
                      AND date(timestamp) >= ?
                      AND date(timestamp) <= ?
                    ORDER BY timestamp DESC
                ''', (user_id, start_date, end_date))
            else:
                cursor.execute('''
                    SELECT id, timestamp, confidence, source FROM attendance
                    WHERE user_id = ? AND date(timestamp) >= date('now', 'localtime', '-' || ? || ' days')
                    ORDER BY timestamp DESC
                ''', (user_id, days))
            rows = cursor.fetchall()

        return [{
            'id': r[0], 'timestamp': r[1], 'confidence': r[2], 'source': r[3]
        } for r in rows]
    except Exception as e:
        logger.error(f"Failed to get attendance logs: {e}")
        return []


def cleanup_old_logs(days: int = ATTENDANCE_LOG_RETENTION_DAYS):
    """Delete attendance logs older than specified days."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM attendance
                WHERE date(timestamp) < date('now', 'localtime', '-' || ? || ' days')
            ''', (days,))
            deleted = cursor.rowcount
            conn.commit()
            
        logger.info(f"✓ Cleaned up {deleted} old attendance records")
        return deleted
    except Exception as e:
        logger.error(f"Failed to cleanup logs: {e}")
        return 0


def get_attendance_statistics(organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> Dict:
    """Get comprehensive attendance statistics scoped strictly to organization and branch."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            
            att_where = "WHERE 1=1"
            user_where = "WHERE 1=1"
            ma_where = "WHERE 1=1"
            params_att = []
            params_user = []
            params_ma = []

            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    att_where += " AND (organization_id = 'DEFAULT_ORG' OR organization_id IS NULL)"
                    user_where += " AND (organization_id = 'DEFAULT_ORG' OR organization_id IS NULL)"
                    ma_where += " AND (organization_id = 'DEFAULT_ORG' OR organization_id IS NULL)"
                else:
                    att_where += " AND organization_id = ?"
                    user_where += " AND organization_id = ?"
                    ma_where += " AND organization_id = ?"
                    params_att.append(organization_id)
                    params_user.append(organization_id)
                    params_ma.append(organization_id)

            cursor.execute(f'SELECT COUNT(*) FROM attendance {att_where}', params_att)
            total_records = cursor.fetchone()[0]
            
            cursor.execute(f'''
                SELECT COUNT(*) FROM attendance
                {att_where} AND date(timestamp) = date('now', 'localtime')
            ''', params_att)
            today_count = cursor.fetchone()[0]

            cursor.execute(f'''
                SELECT COUNT(DISTINCT user_id) FROM attendance
                {att_where} AND date(timestamp) = date('now', 'localtime')
                AND user_id NOT IN (
                    SELECT user_id FROM manual_absent {ma_where} AND date = date('now', 'localtime')
                )
            ''', [*params_att, *params_ma])
            unique_today = cursor.fetchone()[0]
            
            cursor.execute(f'SELECT AVG(confidence) FROM attendance {att_where}', params_att)
            avg_confidence = cursor.fetchone()[0] or 0
            
            cursor.execute(f'''
                SELECT u.name, a.timestamp, a.confidence FROM attendance a
                JOIN users u ON a.user_id = u.id
                {att_where.replace('organization_id', 'a.organization_id')}
                ORDER BY a.timestamp DESC LIMIT 10
            ''', params_att)
            recent = cursor.fetchall()
            
        return {
            'total_records': total_records,
            'today_count': today_count,
            'unique_users_today': unique_today,
            'avg_confidence': float(avg_confidence),
            'recent_entries': [
                {'name': r[0], 'timestamp': r[1], 'confidence': r[2]}
                for r in recent
            ]
        }
    except Exception as e:
        logger.error(f"Failed to get statistics: {e}")
        return {}


def get_attendance_today(organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict]:
    """Get all attendance logs for today with room location and device info, scoped by tenant."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            query = '''
                SELECT a.id, u.name as user_name, a.detected_name, a.confidence, a.timestamp, a.source, a.user_id, u.department, a.location, a.device_id
                FROM attendance a
                JOIN users u ON a.user_id = u.id
                WHERE date(a.timestamp) = date('now', 'localtime')
            '''
            params = []
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    query += " AND (a.organization_id = 'DEFAULT_ORG' OR a.organization_id IS NULL OR u.organization_id = 'DEFAULT_ORG' OR u.organization_id IS NULL)"
                else:
                    query += " AND (a.organization_id = ? OR u.organization_id = ?)"
                    params.extend([organization_id, organization_id])
            query += " ORDER BY a.timestamp DESC"
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
        return [{
            'id': r[0], 'user_name': r[1], 'detected_name': r[2],
            'confidence': r[3], 'timestamp': r[4], 'source': r[5], 'user_id': r[6], 'department': r[7],
            'location': r[8], 'device_id': r[9]
        } for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch today's logs: {e}")
        return []


def get_all_departments(organization_id: Optional[str] = None) -> List[str]:
    """Get list of distinct departments from registered active users."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT DISTINCT department FROM users WHERE active = 1 AND department IS NOT NULL AND department != '' AND (organization_id = ? OR organization_id IS NULL OR organization_id = '') ORDER BY department ASC", (organization_id,))
                else:
                    cursor.execute("SELECT DISTINCT department FROM users WHERE active = 1 AND department IS NOT NULL AND department != '' AND organization_id = ? ORDER BY department ASC", (organization_id,))
            else:
                cursor.execute("SELECT DISTINCT department FROM users WHERE active = 1 AND department IS NOT NULL AND department != '' ORDER BY department ASC")
            rows = cursor.fetchall()
            return [r[0] for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch departments: {e}")
        return []


def get_detailed_attendance_report(
    target_date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    department: Optional[str] = None,
    shift_id: Optional[Any] = None,
    status_filter: Optional[str] = None,
    search_query: Optional[str] = None,
    checkin_camera: Optional[str] = None,
    checkout_camera: Optional[str] = None,
    camera_id: Optional[str] = None,
    late_time_threshold: str = "09:15:00",
    user_id: Optional[int] = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate comprehensive department-wise, shift-wise, staff-wise, and camera-wise attendance reports.
    For multi-day periods (week, month, range): Returns ONE ROW PER EMPLOYEE with summary metrics
    and a complete daily timeline attached for the 'Complete View' drilldown.
    For single-day queries (today, yesterday, specific date): Returns standard daily attendance rows
    with actual Check-In and Check-Out cameras recorded.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            attendance_source_name = 'Branch'
            try:
                cursor.execute("SELECT branch_name, company_name FROM branch_settings WHERE id = 1")
                cfg_row = cursor.fetchone()
                if cfg_row:
                    attendance_source_name = (
                        (cfg_row['branch_name'] or cfg_row['company_name'] or '').strip() or 'Branch'
                    )
            except Exception:
                pass

            # Determine query date parameters
            if target_date == 'today' or (not target_date and not start_date):
                cursor.execute("SELECT date('now', 'localtime')")
                target_date = cursor.fetchone()[0]
                start_date = target_date
                end_date = target_date
                is_multi_day = False
            elif target_date == 'yesterday':
                cursor.execute("SELECT date('now', 'localtime', '-1 day')")
                target_date = cursor.fetchone()[0]
                start_date = target_date
                end_date = target_date
                is_multi_day = False
            elif target_date == 'this_week':
                cursor.execute("SELECT date('now', 'localtime', 'weekday 0', '-6 days'), date('now', 'localtime')")
                start_date, end_date = cursor.fetchone()
                is_multi_day = (start_date != end_date)
            elif target_date == 'this_month':
                cursor.execute("SELECT date('now', 'localtime', 'start of month'), date('now', 'localtime')")
                start_date, end_date = cursor.fetchone()
                is_multi_day = (start_date != end_date)
            elif target_date == 'last7':
                cursor.execute("SELECT date('now', 'localtime', '-6 days'), date('now', 'localtime')")
                start_date, end_date = cursor.fetchone()
                is_multi_day = True
            elif target_date == 'last30':
                cursor.execute("SELECT date('now', 'localtime', '-29 days'), date('now', 'localtime')")
                start_date, end_date = cursor.fetchone()
                is_multi_day = True
            elif target_date and not start_date:
                start_date = target_date
                end_date = target_date
                is_multi_day = False
            else:
                end_date = end_date or start_date
                start_date = start_date or end_date
                is_multi_day = (start_date != end_date)

            # Generate chronological list of dates in the range
            dt_start = datetime.strptime(start_date, "%Y-%m-%d")
            dt_end = datetime.strptime(end_date, "%Y-%m-%d")
            total_days = (dt_end - dt_start).days + 1
            date_list = [(dt_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(total_days)]
            report_org = organization_id or 'DEFAULT_ORG'
            report_branch = branch_id or 'DEFAULT_BRANCH'
            cursor.execute(
                """SELECT calendar_date, override_type, scope_type, scope_id
                   FROM special_date_overrides
                   WHERE calendar_date >= ? AND calendar_date <= ?
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND (? IS NULL OR COALESCE(branch_id, 'DEFAULT_BRANCH') = ?)""",
                (start_date, end_date, report_org, branch_id, report_branch),
            )
            calendar_overrides = {
                (row['calendar_date'], row['scope_type'], row['scope_id']): row['override_type']
                for row in cursor.fetchall()
            }

            # Automatically include company public holidays for all staff
            cursor.execute(
                """SELECT holiday_date, end_date, is_recurring FROM company_holidays
                   WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND (? IS NULL OR COALESCE(branch_id, 'DEFAULT_BRANCH') = ?)""",
                (report_org, branch_id, report_branch),
            )
            for h_row in cursor.fetchall():
                h_date = h_row['holiday_date']
                e_date = h_row['end_date'] or h_date
                is_rec = h_row['is_recurring']
                
                # Expand holiday date range
                h_start = datetime.strptime(h_date, "%Y-%m-%d")
                h_end = datetime.strptime(e_date, "%Y-%m-%d")
                curr_h = h_start
                while curr_h <= h_end:
                    curr_h_str = curr_h.strftime("%Y-%m-%d")
                    if start_date <= curr_h_str <= end_date:
                        if (curr_h_str, 'all', 0) not in calendar_overrides:
                            calendar_overrides[(curr_h_str, 'all', 0)] = 'holiday'
                    elif is_rec:
                        try:
                            h_md = curr_h_str[5:]  # "MM-DD"
                            for y in range(dt_start.year, dt_end.year + 1):
                                occ = f"{y:04d}-{h_md}"
                                if start_date <= occ <= end_date and (occ, 'all', 0) not in calendar_overrides:
                                    calendar_overrides[(occ, 'all', 0)] = 'holiday'
                        except Exception:
                            pass
                    curr_h += timedelta(days=1)



            # Fetch users matching department and shift filters, including those who exited during/after the report period
            user_query = '''
                SELECT u.id, u.name, u.email, u.phone, u.department, u.photo_path, u.cnic, u.position, u.salary, 
                       u.join_date, u.exit_date, u.enrollment_date, u.created_at, u.active,
                       COALESCE(u.shift_id, 1) as shift_id,
                       COALESCE(s.name, 'General Morning Shift') as shift_name,
                       COALESCE(s.start_time, '08:00') as start_time,
                       COALESCE(s.end_time, '20:00') as end_time,
                       COALESCE(s.grace_period_minutes, 15) as grace_period_minutes,
                       COALESCE(s.working_days, '[0,1,2,3,4,5]') as working_days,
                       COALESCE(u.checkin_cameras, 'all') as checkin_cameras,
                       COALESCE(u.checkout_cameras, 'all') as checkout_cameras
                FROM users u
                LEFT JOIN shifts s ON COALESCE(u.shift_id, 1) = s.id
                WHERE (u.active = 1 OR (u.exit_date IS NOT NULL AND u.exit_date >= ?))
            '''
            user_params = [start_date]
            if department and department.strip() and department.lower() != 'all':
                user_query += " AND LOWER(TRIM(u.department)) = LOWER(TRIM(?))"
                user_params.append(department.strip())
            if shift_id and str(shift_id).strip() and str(shift_id).lower() != 'all':
                user_query += " AND (u.shift_id = ? OR LOWER(TRIM(s.name)) = LOWER(TRIM(?)))"
                user_params.extend([str(shift_id).strip(), str(shift_id).strip()])
            if user_id is not None:
                user_query += " AND u.id = ?"
                user_params.append(int(user_id))
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    user_query += " AND (u.organization_id = 'DEFAULT_ORG' OR u.organization_id IS NULL)"
                else:
                    user_query += " AND u.organization_id = ?"
                    user_params.append(organization_id)
            if branch_id and branch_id != 'all':
                if branch_id == 'DEFAULT_BRANCH':
                    user_query += " AND (u.branch_id = 'DEFAULT_BRANCH' OR u.branch_id IS NULL)"
                else:
                    user_query += " AND u.branch_id = ?"
                    user_params.append(branch_id)

            user_query += " ORDER BY u.department ASC, u.name ASC"
            cursor.execute(user_query, user_params)
            users = [dict(r) for r in cursor.fetchall()]

            if not users:
                return {
                    'is_multi_day': is_multi_day,
                    'date_label': start_date if not is_multi_day else f"{start_date} to {end_date}",
                    'start_date': start_date,
                    'end_date': end_date,
                    'total_days': total_days,
                    'summary': {
                        'total_enrolled': 0,
                        'total_matching': 0,
                        'attended_count': 0,
                        'zero_attendance_count': 0,
                        'total_present_days': 0,
                        'total_late_days': 0,
                        'total_absent_days': 0,
                        'attendance_rate': 0.0
                    },
                    'records': [],
                    'departments': []
                }

            user_ids = [u['id'] for u in users]
            placeholders = ','.join('?' for _ in user_ids)
            cursor.execute(f'''
                SELECT user_id, shift_id, effective_from, shift_name, start_time, end_time,
                       grace_period_minutes, working_days, checkout_grace_period_minutes
                FROM employee_shift_history
                WHERE user_id IN ({placeholders}) AND effective_from <= ?
                ORDER BY user_id, effective_from, id
            ''', (*user_ids, end_date))
            shift_history_by_user = {}
            for history_row in cursor.fetchall():
                shift_history_by_user.setdefault(history_row['user_id'], []).append(dict(history_row))

            cursor.execute("SELECT id, name, start_time, end_time, grace_period_minutes, checkout_grace_period_minutes, working_days FROM shifts")
            all_shifts_lookup = {r['id']: dict(r) for r in cursor.fetchall()}

            # One range query, aggregated in Python (avoids six correlated subqueries per day).
            cursor.execute(
                f'''
                SELECT user_id, timestamp, location, device_id, checkout_location, checkout_camera_id,
                       source, notes, marked_by, shift_id
                FROM attendance
                WHERE user_id IN ({placeholders})
                  AND date(timestamp) >= ?
                  AND date(timestamp) <= ?
                ORDER BY user_id, timestamp
                ''',
                (*user_ids, start_date, end_date),
            )
            att_map = {}
            for raw in cursor.fetchall():
                ts = str(raw['timestamp'] or '')
                log_date = ts.split('T', 1)[0].split(' ', 1)[0][:10]
                key = (raw['user_id'], log_date)
                rec = att_map.get(key)
                if rec is None:
                    att_map[key] = {
                        'user_id': raw['user_id'],
                        'log_date': log_date,
                        'first_seen': raw['timestamp'],
                        'last_seen': raw['timestamp'],
                        'log_count': 1,
                        'checkin_location': raw['location'],
                        'checkin_camera': raw['device_id'],
                        'checkout_location': raw['checkout_location'] or raw['location'],
                        'checkout_camera': raw['checkout_camera_id'] or raw['device_id'],
                        'source': raw['source'],
                        'notes': raw['notes'],
                        'marked_by': raw['marked_by'],
                        'record_shift_id': raw['shift_id'],
                    }
                else:
                    rec['last_seen'] = raw['timestamp']
                    rec['log_count'] += 1
                    rec['checkout_location'] = raw['checkout_location'] or raw['location'] or rec.get('checkout_location')
                    rec['checkout_camera'] = raw['checkout_camera_id'] or raw['device_id'] or rec.get('checkout_camera')
                    if rec.get('record_shift_id') is None and raw['shift_id'] is not None:
                        rec['record_shift_id'] = raw['shift_id']

            # Retrieve manual absences
            cursor.execute(
                f"SELECT user_id, date FROM manual_absent WHERE user_id IN ({placeholders}) AND date >= ? AND date <= ?",
                (*user_ids, start_date, end_date)
            )
            absent_set = set((r[0], r[1]) for r in cursor.fetchall())

            # Retrieve approved leaves with leave_type & is_paid lookup
            cursor.execute(f'''
                  SELECT lr.user_id, lr.start_date, lr.end_date, lr.leave_type, lr.leave_duration,
                      lr.paid_days, lr.unpaid_days, lr.paid_through_date,
                       COALESCE(lt.is_paid, CASE WHEN lr.leave_type = 'unpaid' THEN 0 ELSE 1 END) as is_paid
                FROM leave_requests lr
                LEFT JOIN leave_types lt ON lt.code = lr.leave_type OR lt.name = lr.leave_type
                WHERE lr.user_id IN ({placeholders}) AND lr.status = 'approved' AND lr.end_date >= ? AND lr.start_date <= ?
            ''', (*user_ids, start_date, end_date))
            leave_rows = cursor.fetchall()
            leave_map = {}
            for lr in leave_rows:
                l_uid, l_start, l_end, l_type = lr['user_id'], lr['start_date'], lr['end_date'], lr['leave_type']
                is_half_leave = lr['leave_duration'] == 'half'
                explicit_allocation = not is_half_leave and lr['paid_days'] is not None and lr['unpaid_days'] is not None
                paid_days = int(lr['paid_days'] or 0) if explicit_allocation else None
                unpaid_days = int(lr['unpaid_days'] or 0) if explicit_allocation else None
                paid_through_date = lr['paid_through_date']
                leave_is_paid = bool(lr['is_paid'])
                try:
                    cur_l = datetime.strptime(l_start, "%Y-%m-%d")
                    end_l = datetime.strptime(l_end, "%Y-%m-%d")
                    while cur_l <= end_l:
                        current_date = cur_l.strftime("%Y-%m-%d")
                        is_paid_day = leave_is_paid
                        if explicit_allocation:
                            is_paid_day = unpaid_days == 0 or (
                                paid_days > 0 and paid_through_date is not None and current_date <= paid_through_date
                            )
                        leave_map[(l_uid, current_date)] = {
                            'leave_type': l_type,
                            'is_paid': is_paid_day,
                            'is_half_leave': is_half_leave,
                        }
                        cur_l += timedelta(days=1)
                except Exception:
                    pass

            dept_stats = {}
            for u in users:
                d = u['department'] or 'General / Unassigned'
                if d not in dept_stats:
                    dept_stats[d] = {'total_staff': 0, 'attended_staff': 0, 'present_days': 0, 'absent_days': 0, 'late_days': 0}
                dept_stats[d]['total_staff'] += 1

            records = []
            total_present_days_all = 0
            total_late_days_all = 0
            total_absent_days_all = 0
            attended_users_count = 0

            for u in users:
                uid = u['id']
                dept = u['department'] or 'General / Unassigned'
                shift_history = shift_history_by_user.get(uid, [])
                history_index = 0
                effective_shift = None

                emp_daily_records = []
                emp_present_days = 0
                emp_late_days = 0
                emp_absent_days = 0
                emp_working_days = 0
                emp_leave_days = 0

                for cur_date in date_list:
                    while history_index < len(shift_history) and shift_history[history_index]['effective_from'] <= cur_date:
                        effective_shift = shift_history[history_index]
                        history_index += 1
                    att = att_map.get((uid, cur_date))
                    schedule_known = True
                    if att and att.get('record_shift_id') and att['record_shift_id'] in all_shifts_lookup:
                        rec_shift = all_shifts_lookup[att['record_shift_id']]
                        shift_id_for_day = rec_shift['id']
                        day_shift_name = rec_shift['name']
                        shift_start = rec_shift['start_time']
                        shift_end = rec_shift['end_time']
                        grace_mins = int(rec_shift.get('grace_period_minutes') or 15)
                        shift_working_days = set(_normalize_shift_working_days(rec_shift.get('working_days')))
                        schedule_known = True
                    else:
                        shift_id_for_day = effective_shift['shift_id'] if effective_shift is not None else u['shift_id']
                        day_shift_name = effective_shift['shift_name'] if effective_shift is not None else u['shift_name']
                        shift_start = effective_shift['start_time'] if effective_shift is not None else u.get('start_time') or '08:00'
                        shift_end = effective_shift['end_time'] if effective_shift is not None else u.get('end_time') or '20:00'
                        grace_mins = int(effective_shift['grace_period_minutes'] if effective_shift is not None else u.get('grace_period_minutes') or 15)
                        shift_working_days = set(_normalize_shift_working_days(
                            effective_shift['working_days'] if effective_shift is not None else u.get('working_days')
                        ))
                    start_mins = parse_time_to_minutes(shift_start)
                    grace_deadline_mins = start_mins + grace_mins
                    dt_obj = datetime.strptime(cur_date, "%Y-%m-%d")
                    day_name = dt_obj.strftime("%a")
                    override_type = _resolve_calendar_override(
                        calendar_overrides, cur_date, uid, shift_id_for_day
                    )
                    is_scheduled_workday = _is_scheduled_workday(
                        dt_obj, shift_working_days, override_type
                    )

                    # TICKET 18/19: Joiner/Exit Pro-Rata
                    employed_from = (u.get('join_date') or str(u.get('enrollment_date') or '').split(' ')[0] or str(u.get('created_at') or '').split(' ')[0]).strip()
                    exit_date_str = (u.get('exit_date') or '').strip()
                    is_employed_on_date = True
                    if employed_from and cur_date < employed_from:
                        is_employed_on_date = False
                    if exit_date_str and cur_date > exit_date_str:
                        is_employed_on_date = False

                    if not is_employed_on_date:
                        is_scheduled_workday = False
                        status_if_no_attendance = 'not_employed'
                    else:
                        status_if_no_attendance = 'absent'

                    is_manual_absent = (uid, cur_date) in absent_set
                    leave_info = leave_map.get((uid, cur_date))
                    half_leave_active = False
                    if leave_info and leave_info['is_half_leave'] and att:
                        now = datetime.now()
                        try:
                            shift_end_mins = parse_time_to_minutes(shift_end)
                            if shift_end_mins <= start_mins:
                                shift_end_mins += 24 * 60
                            midpoint_mins = start_mins + (shift_end_mins - start_mins) // 2
                            first_seen_text = str(att.get('first_seen') or '')
                            first_seen_time = first_seen_text.split(' ')[-1] if ' ' in first_seen_text else first_seen_text.split('T')[-1]
                            first_seen_mins = parse_time_to_minutes(first_seen_time)
                            if first_seen_mins <= midpoint_mins % (24 * 60):
                                if dt_obj.date() < now.date():
                                    half_leave_active = True
                                elif dt_obj.date() == now.date():
                                    now_mins = now.hour * 60 + now.minute
                                    if midpoint_mins >= 24 * 60 and now_mins < start_mins:
                                        now_mins += 24 * 60
                                    half_leave_active = now_mins >= midpoint_mins
                        except (TypeError, ValueError):
                            half_leave_active = False

                    # Priority order: Actual Face Attendance > Approved Leave > Manual Absent override only when no attendance exists > Unexcused Absence
                    if att:
                        first_seen = att['first_seen']
                        raw_last_seen = att['last_seen']
                        last_seen = None
                        if first_seen and raw_last_seen and str(first_seen) != str(raw_last_seen):
                            try:
                                fmt = "%Y-%m-%d %H:%M:%S"
                                t1 = datetime.strptime(str(first_seen).split('.')[0], fmt)
                                t2 = datetime.strptime(str(raw_last_seen).split('.')[0], fmt)
                                if (t2 - t1).total_seconds() >= 60:
                                    last_seen = raw_last_seen
                            except Exception:
                                if str(first_seen) != str(raw_last_seen):
                                    last_seen = raw_last_seen

                        is_manual_punch = str(att.get('source') or '').lower() == 'manual'
                        if is_manual_punch:
                            checkin_cam = 'Manual'
                            checkin_loc = '—'
                            checkout_cam = 'Manual' if last_seen else '-'
                            checkout_loc = '—'
                            loc = '—'
                            dev = 'Manual'
                        else:
                            checkin_cam = att.get('checkin_camera') or att.get('device_id') or ''
                            checkin_loc = att.get('checkin_location') or att.get('location') or ''
                            checkout_cam = att.get('checkout_camera') or (checkin_cam if last_seen else '-')
                            checkout_loc = att.get('checkout_location') or (checkin_loc if last_seen else '-')
                            loc = checkin_loc
                            dev = checkin_cam

                        # Determine late vs present vs checked_out
                        try:
                            time_part = str(first_seen).split(' ')[-1] if ' ' in str(first_seen) else str(first_seen).split('T')[-1]
                            checkin_mins = parse_time_to_minutes(time_part)
                            shift_end_mins = parse_time_to_minutes(shift_end)

                            # Determine if last_seen is a genuine Check-Out at or near shift end time
                            is_genuine_checkout = False
                            if last_seen:
                                last_time_part = str(last_seen).split(' ')[-1] if ' ' in str(last_seen) else str(last_seen).split('T')[-1]
                                last_seen_mins = parse_time_to_minutes(last_time_part)
                                if shift_end_mins >= start_mins:
                                    is_genuine_checkout = (last_seen_mins >= shift_end_mins - 1)
                                else:
                                    is_genuine_checkout = (last_seen_mins >= shift_end_mins - 1)

                            if checkin_mins > grace_deadline_mins:
                                status = 'late'
                                emp_late_days += 1
                            elif is_genuine_checkout:
                                status = 'checked_out'
                            else:
                                status = 'present'
                        except Exception:
                            status = 'present'

                        emp_present_days += 1
                        if is_scheduled_workday:
                            emp_working_days += 1
                    elif is_scheduled_workday and leave_info:
                        if leave_info['is_paid']:
                            status = 'on_leave'
                            emp_leave_days += 1
                        else:
                            status = 'on_leave_unpaid'
                            emp_absent_days += 1
                        first_seen = None
                        last_seen = None
                        loc = '-'
                        dev = '-'
                        checkin_cam = '-'
                        checkout_cam = '-'
                        emp_working_days += 1
                    elif is_manual_absent and is_scheduled_workday:
                        status = 'absent'
                        first_seen = None
                        last_seen = None
                        loc = '-'
                        dev = '-'
                        checkin_cam = '-'
                        checkout_cam = '-'
                        emp_absent_days += 1
                        emp_working_days += 1
                    else:
                        # No attendance record
                        if not is_scheduled_workday:
                            if status_if_no_attendance == 'not_employed':
                                status = 'not_employed'
                            else:
                                status = 'schedule_unknown' if not schedule_known else ('holiday' if override_type == 'holiday' else (
                                    'off_day' if override_type == 'off_day' else 'weekend'
                                ))
                            first_seen = None
                            last_seen = None
                            loc = '-'
                            dev = '-'
                            checkin_cam = '-'
                            checkout_cam = '-'
                        else:
                            status = 'absent'
                            first_seen = None
                            last_seen = None
                            loc = '-'
                            dev = '-'
                            checkin_cam = '-'
                            checkout_cam = '-'
                            emp_absent_days += 1
                            emp_working_days += 1

                    day_source = att.get('source', 'manual' if is_manual_absent else 'auto') if att is not None else ('manual' if is_manual_absent else 'auto')
                    day_marked_by = (att.get('marked_by', 'Admin') if att and att.get('source') == 'manual' else ('Admin' if is_manual_absent else 'System')) if att is not None or is_manual_absent else 'System'
                    if str(day_source).lower() == 'manual':
                        day_source_label = 'Manual'
                    elif att is not None:
                        day_source_label = attendance_source_name
                    else:
                        day_source_label = '-'
                    emp_daily_records.append({
                        'date': cur_date,
                        'date_formatted': dt_obj.strftime("%d %b %Y"),
                        'day': day_name,
                        'status': status,
                        'scheduled_workday': is_scheduled_workday,
                        'shift_history_missing': not schedule_known,
                        'calendar_override': override_type,
                        'day_classification': (
                            'special_working_day' if override_type == 'working_day' else
                            'special_off_day' if override_type == 'off_day' else
                            'holiday' if override_type == 'holiday' else
                            'scheduled_working_day' if is_scheduled_workday else
                            'non_working_day' if schedule_known else 'schedule_unknown'
                        ),
                        'status_display': f"{status.upper().replace('_', ' ')} + HALF LEAVE" if half_leave_active else status.upper().replace('_', ' '),
                        'half_leave_count': int(half_leave_active),
                        'half_leave_type': leave_info['leave_type'] if half_leave_active and leave_info else None,
                        'first_seen': first_seen,
                        'last_seen': last_seen,
                        'location': loc,
                        'device_id': dev,
                        'checkin_camera': checkin_cam,
                        'checkout_camera': checkout_cam,
                        'checkin_location': loc if att else '-',
                        'checkout_location': checkout_loc if (att and last_seen) else '-',
                        'source': day_source,
                        'source_label': day_source_label,
                        'marked_by': day_marked_by,
                        'notes': att.get('notes', '') if att else '',
                        'shift_id': shift_id_for_day if schedule_known else None,
                        'shift_name': day_shift_name,
                        'shift_effective_from': effective_shift['effective_from'] if effective_shift is not None else None,
                        'shift_working_days': sorted(shift_working_days) if schedule_known else []
                    })

                if emp_present_days > 0:
                    attended_users_count += 1
                    dept_stats[dept]['attended_staff'] += 1

                dept_stats[dept]['present_days'] += emp_present_days
                dept_stats[dept]['absent_days'] += emp_absent_days
                dept_stats[dept]['late_days'] += emp_late_days

                total_present_days_all += emp_present_days
                total_late_days_all += emp_late_days
                total_absent_days_all += emp_absent_days

                effective_working_days = max(emp_working_days, 1) if emp_working_days > 0 else total_days
                emp_rate = round((emp_present_days / effective_working_days * 100), 1) if effective_working_days > 0 else 0.0

                # Filter matching logic
                if search_query and search_query.strip():
                    sq = search_query.strip().lower()
                    if not (sq in u['name'].lower() or sq in dept.lower() or sq in (u['position'] or '').lower() or sq in str(uid)):
                        continue

                if status_filter and status_filter.lower() != 'all':
                    sf = status_filter.lower()
                    if sf == 'present' and emp_present_days == 0:
                        continue
                    elif sf == 'absent' and emp_present_days > 0:
                        continue
                    elif sf == 'late' and emp_late_days == 0:
                        continue
                    elif sf == 'checked_out' and not any(d.get('status') == 'checked_out' for d in emp_daily_records):
                        continue

                # Camera filtering
                if checkin_camera and checkin_camera.strip() and checkin_camera.lower() != 'all':
                    cc = checkin_camera.strip().lower()
                    if not any(cc in str(d.get('checkin_camera', '')).lower() or cc in str(d.get('checkin_location', '')).lower() for d in emp_daily_records if d.get('status') in ('present', 'checked_out', 'late')):
                        continue

                if checkout_camera and checkout_camera.strip() and checkout_camera.lower() != 'all':
                    coc = checkout_camera.strip().lower()
                    if not any(coc in str(d.get('checkout_camera', '')).lower() or coc in str(d.get('checkout_location', '')).lower() for d in emp_daily_records if d.get('status') == 'checked_out'):
                        continue

                if camera_id and camera_id.strip() and camera_id.lower() != 'all':
                    cid = camera_id.strip().lower()
                    if not any((cid in str(d.get('checkin_camera', '')).lower() or cid in str(d.get('checkout_camera', '')).lower() or cid in str(d.get('checkin_location', '')).lower() or cid in str(d.get('checkout_location', '')).lower()) for d in emp_daily_records if d.get('status') in ('present', 'checked_out', 'late')):
                        continue

                if is_multi_day:
                    # ONE ROW PER EMPLOYEE
                    unpaid_leave_days = sum(1 for day in emp_daily_records if day.get('status') == 'on_leave_unpaid')
                    total_leave_days = emp_leave_days + unpaid_leave_days
                    records.append({
                        'user_id': uid,
                        'name': u['name'],
                        'email': u['email'] or '',
                        'phone': u['phone'] or '',
                        'department': dept,
                        'position': u['position'] or 'Staff Member',
                        'cnic': u['cnic'] or '',
                        'salary': float(u['salary'] or 0),
                        'join_date': u['join_date'] or '',
                        'shift_name': u['shift_name'],
                        'shift_start': u['start_time'],
                        'shift_end': u['end_time'],
                        'photo_path': u['photo_path'],
                        'checkin_cameras': u['checkin_cameras'],
                        'checkout_cameras': u['checkout_cameras'],
                        'total_days': total_days,
                        'working_days': emp_working_days,
                        'present_days': emp_present_days,
                        'absent_days': emp_absent_days,
                        'late_days': emp_late_days,
                        'leave_days': total_leave_days,
                        'paid_leave_days': emp_leave_days,
                        'unpaid_leave_days': unpaid_leave_days,
                        'total_leave_days': total_leave_days,
                        'payable_days': min(emp_working_days, emp_present_days + emp_late_days + emp_leave_days),
                        'attendance_rate': emp_rate,
                        'present_display': f"{emp_present_days} / {emp_working_days}",
                        'absent_display': f"{emp_absent_days} / {emp_working_days}",
                        'daily_records': emp_daily_records
                    })
                else:
                    # Single day view: 1 row for that day
                    day_entry = emp_daily_records[0] if emp_daily_records else {}
                    records.append({
                        'user_id': uid,
                        'name': u['name'],
                        'email': u['email'] or '',
                        'phone': u['phone'] or '',
                        'department': dept,
                        'position': u['position'] or 'Staff Member',
                        'cnic': u['cnic'] or '',
                        'salary': float(u['salary'] or 0),
                        'join_date': u['join_date'] or '',
                        'shift_name': day_entry.get('shift_name') or u['shift_name'],
                        'shift_start': u['start_time'],
                        'shift_end': u['end_time'],
                        'photo_path': u['photo_path'],
                        'checkin_cameras': u['checkin_cameras'],
                        'checkout_cameras': u['checkout_cameras'],
                        'status': day_entry.get('status', 'absent'),
                        'shift_id': day_entry.get('shift_id') or u['shift_id'],
                        'first_seen': day_entry.get('first_seen'),
                        'last_seen': day_entry.get('last_seen'),
                        'location': day_entry.get('location', 'Main Gate / Entrance'),
                        'device_id': day_entry.get('device_id') or '',
                        'checkin_camera': day_entry.get('checkin_camera', '-'),
                        'source_label': day_entry.get('source_label') or '',
                        'checkout_camera': day_entry.get('checkout_camera', '-'),
                        'checkin_location': day_entry.get('checkin_location', '-'),
                        'checkout_location': day_entry.get('checkout_location', '-'),
                        'source': day_entry.get('source', 'auto'),
                        'marked_by': day_entry.get('marked_by', 'System'),
                        'notes': day_entry.get('notes', ''),
                        'paid_leave_days': int(day_entry.get('status') == 'on_leave'),
                        'unpaid_leave_days': int(day_entry.get('status') == 'on_leave_unpaid'),
                        'total_leave_days': int(day_entry.get('status') in ('on_leave', 'on_leave_unpaid')),
                        'leave_days': int(day_entry.get('status') in ('on_leave', 'on_leave_unpaid')),
                        'date': start_date,
                        'daily_records': emp_daily_records
                    })

            # Calculate overall attendance percentage
            total_possible_workdays = len(users) * total_days
            overall_rate = round((total_present_days_all / total_possible_workdays * 100), 1) if total_possible_workdays > 0 else 0.0

            dept_summary = []
            for dname, ddata in dept_stats.items():
                tot_st = ddata['total_staff']
                pres_d = ddata['present_days']
                max_d = tot_st * total_days
                d_rate = round((pres_d / max_d * 100), 1) if max_d > 0 else 0.0
                dept_summary.append({
                    'department': dname,
                    'total_staff': tot_st,
                    'total': tot_st,
                    'attended_staff': ddata['attended_staff'],
                    'present_days': pres_d,
                    'present': pres_d,
                    'absent_days': ddata['absent_days'],
                    'absent': ddata['absent_days'],
                    'late_days': ddata['late_days'],
                    'late': ddata['late_days'],
                    'attendance_rate': d_rate
                })

            employee_summary = {}
            for record in records:
                user_key = str(record.get('user_id'))
                employee_summary[user_key] = {
                    'user_id': record.get('user_id'),
                    'name': record.get('name'),
                    'department': record.get('department'),
                    'shift_name': record.get('shift_name'),
                    'present_days': record.get('present_days', 0),
                    'late_days': record.get('late_days', 0),
                    'absent_days': record.get('absent_days', 0),
                    'leave_days': record.get('leave_days', 0),
                    'paid_leave_days': record.get('paid_leave_days', 0),
                    'unpaid_leave_days': record.get('unpaid_leave_days', 0),
                    'total_leave_days': record.get('total_leave_days', record.get('leave_days', 0)),
                    'payable_days': record.get('payable_days', 0),
                    'attendance_rate': record.get('attendance_rate', 0.0),
                    'status': record.get('status', 'absent'),
                }

            date_label = start_date if not is_multi_day else f"{start_date} to {end_date}"

            for record in records:
                source = record.get('source') or 'auto'
                if record.get('source_label'):
                    continue
                record['source_label'] = 'Manual' if str(source).lower() == 'manual' else attendance_source_name

            response = {
                'is_multi_day': is_multi_day,
                'date_label': date_label,
                'start_date': start_date,
                'end_date': end_date,
                'total_days': total_days,
                'summary': {
                    'total_enrolled': len(users),
                    'total_registered': len(users),
                    'total_matching': len(records),
                    'attended_count': attended_users_count,
                    'zero_attendance_count': len(users) - attended_users_count,
                    'total_present_days': total_present_days_all,
                    'present': total_present_days_all,
                    'total_late_days': total_late_days_all,
                    'late': total_late_days_all,
                    'total_absent_days': total_absent_days_all,
                    'absent': total_absent_days_all,
                    'attendance_rate': overall_rate,
                    'employees': employee_summary,
                },
                'records': records,
                'rows': records,
                'departments': dept_summary
            }
            return response
    except Exception as e:
        import traceback
        traceback.print_exc()
        logger.error(f"Error generating attendance report: {e}")
        return {
            'is_multi_day': False,
            'summary': {
                'total_enrolled': 0,
                'total_matching': 0,
                'attended_count': 0,
                'zero_attendance_count': 0,
                'total_present_days': 0,
                'total_late_days': 0,
                'total_absent_days': 0,
                'attendance_rate': 0.0
            },
            'records': [],
            'departments': []
        }

def is_user_present_today(user_id: int, source: str | None = None) -> bool:
    """Check if a user is already marked present today, optionally scoped to a specific source."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()

            cursor.execute('''
                SELECT COUNT(*) FROM manual_absent
                WHERE user_id = ? AND date = date('now', 'localtime')
            ''', (user_id,))
            if cursor.fetchone()[0] > 0:
                return False

            if source:
                cursor.execute('''
                    SELECT COUNT(*) FROM attendance 
                    WHERE user_id = ? AND source = ? AND date(timestamp) = date('now', 'localtime')
                ''', (user_id, source))
                count = cursor.fetchone()[0]
                return count > 0

            cursor.execute('''
                SELECT COUNT(*) FROM attendance 
                WHERE user_id = ? AND date(timestamp) = date('now', 'localtime')
            ''', (user_id,))
            count = cursor.fetchone()[0]
        return count > 0
    except Exception as e:
        logger.error(f"Failed to check today's attendance for user {user_id}: {e}")
        return False


PAYROLL_LOCK_MESSAGE = "Month is finalized. Reopen payroll to change this."


def payroll_month_is_finalized(
    payroll_month: str,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """SELECT 1 FROM payroll_runs
               WHERE payroll_month = ? AND status = 'Finalized'
                 AND COALESCE(organization_id,'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id,'DEFAULT_BRANCH') = ?""",
            (payroll_month, org, branch),
        ).fetchone()
        return bool(row)


def raise_if_payroll_month_locked(
    work_date: Optional[str],
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> None:
    if not work_date:
        return
    month = str(work_date)[:7]
    if payroll_month_is_finalized(month, organization_id, branch_id):
        try:
            with sqlite3.connect(DB_PATH) as conn:
                _write_payroll_audit(
                    conn.cursor(), 'period_lock_blocked', organization_id, branch_id, month, 'system',
                    {'date': work_date, 'message': PAYROLL_LOCK_MESSAGE},
                )
                conn.commit()
        except Exception:
            logger.exception('Failed to audit period lock')
        raise ValueError(PAYROLL_LOCK_MESSAGE)


def mark_user_absent_today(user_id: int, target_date: Optional[str] = None, organization_id: Optional[str] = None) -> bool:
    """Manually mark a user absent today or on a specific date, enforcing tenant isolation."""
    try:
        if target_date == 'today':
            target_date = None
        lock_day = target_date or datetime.now().date().isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT organization_id, branch_id FROM users WHERE id = ?", (user_id,))
            u_row = cursor.fetchone()
            if not u_row:
                return False
            user_org = u_row[0] or 'DEFAULT_ORG'
            user_branch = u_row[1] or 'DEFAULT_BRANCH'
            raise_if_payroll_month_locked(lock_day, organization_id or user_org, user_branch)
            user_branch = u_row[1] or 'DEFAULT_BRANCH'
            if organization_id and organization_id != 'all':
                if organization_id != 'DEFAULT_ORG' and user_org != organization_id:
                    return False
            if target_date:
                cursor.execute('''
                    DELETE FROM attendance 
                    WHERE user_id = ? AND date(timestamp) = ?
                ''', (user_id, target_date))
                deleted_count = cursor.rowcount

                cursor.execute('''
                    INSERT OR REPLACE INTO manual_absent (user_id, date, organization_id, branch_id)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, target_date, user_org, user_branch))
            else:
                cursor.execute('''
                    DELETE FROM attendance 
                    WHERE user_id = ? AND date(timestamp) = date('now', 'localtime')
                ''', (user_id,))
                deleted_count = cursor.rowcount

                cursor.execute('''
                    INSERT OR REPLACE INTO manual_absent (user_id, date, organization_id, branch_id)
                    VALUES (?, date('now', 'localtime'), ?, ?)
                ''', (user_id, user_org, user_branch))
            conn.commit()
        logger.info(f"✓ Manually marked user ID {user_id} absent for {target_date or 'today'} (removed {deleted_count} logs, recorded manual_absent)")
        return True
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to mark user ID {user_id} absent: {e}")
        return False


def get_manual_absent_user_ids_today(organization_id: str, branch_id: str) -> List[int]:
    """Return today's manual-absence overrides for one organization and branch."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT user_id
            FROM manual_absent
            WHERE organization_id = ?
              AND branch_id = ?
              AND date = date('now', 'localtime')
            ORDER BY user_id
            """,
            (organization_id, branch_id),
        ).fetchall()
    return [int(row[0]) for row in rows]


def calculate_eligible_overtime(shift_start: str, shift_end: str, actual_checkout: str, checkout_grace_period_minutes: int = 0) -> float:
    """
    Calculate eligible overtime duration in hours:
    OT duration = max(0, actual checkout - (applicable shift end + checkout grace period))
    Handles standard daytime shifts as well as overnight shifts.
    """
    try:
        def to_mins(t_str: str) -> int:
            t = str(t_str).strip()
            if ' ' in t:
                t = t.split(' ')[-1]
            if 'T' in t:
                t = t.split('T')[-1]
            parts = t.split(':')
            return int(parts[0]) * 60 + int(parts[1])

        start_m = to_mins(shift_start)
        end_m = to_mins(shift_end)
        co_m = to_mins(actual_checkout)

        is_overnight = end_m < start_m
        if is_overnight:
            end_m += 24 * 60
            if co_m < start_m:
                co_m += 24 * 60
        elif co_m < start_m and (co_m + 24 * 60 - end_m) <= 16 * 60:
            co_m += 24 * 60

        grace_threshold = end_m + max(0, int(checkout_grace_period_minutes or 0))
        if co_m > grace_threshold:
            ot_mins = co_m - grace_threshold
            return round(ot_mins / 60.0, 2)
        return 0.0
    except Exception as e:
        logger.error(f"Error calculating eligible overtime: {e}")
        return 0.0


def validate_check_times(check_in_time: str, check_out_time: Optional[str], is_overnight: bool = False):
    """
    Validate check_in and check_out times.
    Raises ValueError with explanatory message on invalid time or checkout <= checkin.
    """
    if not check_out_time or not str(check_out_time).strip():
        return True
    co_str = str(check_out_time).strip()
    if co_str in ('0', '00', '0000'):
        raise ValueError('Invalid check-out time.')
    
    def parse_m(s: str) -> int:
        s = s.strip()
        if ' ' in s: s = s.split(' ')[-1]
        if 'T' in s: s = s.split('T')[-1]
        parts = s.split(':')
        if len(parts) < 2:
            raise ValueError(f'Malformed time: {s}')
        h, m = int(parts[0]), int(parts[1])
        if h < 0 or h > 23 or m < 0 or m > 59:
            raise ValueError(f'Time out of range: {s}')
        return h * 60 + m

    ci = parse_m(check_in_time)
    co = parse_m(co_str)

    if is_overnight:
        if co < ci:
            co += 24 * 60
        elif co <= ci:
            raise ValueError('Check-out time must be strictly after check-in time.')
    elif co <= ci:
        if co < ci and (co + 24 * 60 - ci) <= 16 * 60:
            co += 24 * 60
        else:
            raise ValueError('Check-out time must be strictly after check-in time.')
    return True


def mark_user_present_today(
    user_id: int, 
    target_date: Optional[str] = None, 
    check_in_time: Optional[str] = None, 
    check_out_time: Optional[str] = None, 
    shift_id: Optional[int] = None,
    notes: str = "", 
    marked_by: str = "Admin",
    organization_id: Optional[str] = None
) -> bool:
    """
    Manually mark a user present today or on a target date, recording check-in and optional check-out,
    enforcing tenant isolation and propagating organization_id & branch_id.
    """
    try:
        if check_in_time is not None and str(check_in_time).strip() == '':
            raise ValueError('Check-in time is required')
        if check_out_time is not None and str(check_out_time).strip() == '':
            raise ValueError('Check-out time is required')

        if target_date == 'today' or not target_date:
            target_date = datetime.now().strftime("%Y-%m-%d")
        actual_date = target_date
        
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            
            # Verify user exists and check tenant
            cursor.execute('SELECT name, shift_id, organization_id, branch_id FROM users WHERE id = ?', (user_id,))
            user_row = cursor.fetchone()
            if not user_row:
                return False
            name, user_default_shift_id = user_row[0], user_row[1]
            user_org = user_row[2] or 'DEFAULT_ORG'
            user_branch = user_row[3] or 'DEFAULT_BRANCH'
            raise_if_payroll_month_locked(actual_date, organization_id or user_org, user_branch)
            if organization_id and organization_id != 'all':
                if organization_id != 'DEFAULT_ORG' and user_org != organization_id:
                    return False
            
            # Clear manual absent
            cursor.execute('DELETE FROM manual_absent WHERE user_id = ? AND date = ?', (user_id, actual_date))
            
            # Resolve applicable shift
            selected_shift = None
            if shift_id:
                cursor.execute('SELECT id, name, start_time, end_time, grace_period_minutes, checkout_grace_period_minutes FROM shifts WHERE id = ?', (shift_id,))
                selected_shift = cursor.fetchone()
                if not selected_shift:
                    raise ValueError(f"Shift ID {shift_id} not found")
            else:
                # Find historical shift effective on actual_date
                cursor.execute('''
                    SELECT shift_id FROM employee_shift_history 
                    WHERE user_id = ? AND effective_from <= ? 
                    ORDER BY effective_from DESC, id DESC LIMIT 1
                ''', (user_id, actual_date))
                hist_row = cursor.fetchone()
                eff_shift_id = hist_row[0] if hist_row else (user_default_shift_id or 1)
                cursor.execute('SELECT id, name, start_time, end_time, grace_period_minutes, checkout_grace_period_minutes FROM shifts WHERE id = ?', (eff_shift_id,))
                selected_shift = cursor.fetchone()
                if not selected_shift:
                    cursor.execute('SELECT id, name, start_time, end_time, grace_period_minutes, checkout_grace_period_minutes FROM shifts WHERE id = 1')
                    selected_shift = cursor.fetchone()

            shift_db_id = selected_shift[0] if selected_shift else 1
            shift_start = selected_shift[2] if selected_shift else '09:00'
            shift_end = selected_shift[3] if selected_shift else '17:00'
            checkout_grace = int(selected_shift[5] or 0) if selected_shift else 0
            
            # Check overnight shift
            def _to_mins(t):
                p = str(t).strip().split(':')
                return int(p[0]) * 60 + int(p[1])
            is_overnight = _to_mins(shift_end) < _to_mins(shift_start)
            
            # Determine check-in time
            cursor.execute('SELECT MIN(timestamp), MAX(timestamp) FROM attendance WHERE user_id = ? AND date(timestamp) = ?', (user_id, actual_date))
            ex_row = cursor.fetchone()
            existing_first = ex_row[0] if ex_row else None
            
            if check_in_time and str(check_in_time).strip():
                ci_clean = str(check_in_time).strip()
                if len(ci_clean) > 5 and ':' in ci_clean:
                    ci_clean = ci_clean[:5]
            elif existing_first:
                ci_clean = str(existing_first).split(' ')[-1][:5]
            else:
                ci_clean = shift_start[:5]

            # Determine check-out time & validate
            co_clean = None
            if check_out_time and str(check_out_time).strip():
                co_raw = str(check_out_time).strip()
                if len(co_raw) > 5 and ':' in co_raw:
                    co_raw = co_raw[:5]
                validate_check_times(ci_clean, co_raw, is_overnight=is_overnight)
                co_clean = co_raw

            # Delete old attendance logs for this user on this day so we have clean manual records
            cursor.execute('DELETE FROM attendance WHERE user_id = ? AND date(timestamp) = ?', (user_id, actual_date))

            # Insert check-in log
            timestamp_val_in = f"{actual_date} {ci_clean}:00"
            cursor.execute(
                "INSERT INTO attendance (user_id, detected_name, confidence, source, timestamp, notes, marked_by, shift_id, organization_id, branch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, name, 1.0, 'manual', timestamp_val_in, notes, marked_by, shift_db_id, user_org, user_branch)
            )

            # Insert check-out log if provided
            if co_clean:
                timestamp_val_out = f"{actual_date} {co_clean}:00"
                cursor.execute(
                    "INSERT INTO attendance (user_id, detected_name, confidence, source, timestamp, notes, marked_by, shift_id, organization_id, branch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, name, 1.0, 'manual', timestamp_val_out, notes, marked_by, shift_db_id, user_org, user_branch)
                )

                # Calculate OT
                ot_hours = calculate_eligible_overtime(shift_start, shift_end, co_clean, checkout_grace)
                cursor.execute("SELECT id, status FROM overtime WHERE user_id = ? AND ot_date = ?", (user_id, actual_date))
                ot_existing = cursor.fetchone()

                if ot_hours > 0:
                    if ot_existing:
                        ot_id, cur_status = ot_existing[0], (ot_existing[1] or 'pending').lower()
                        if cur_status == 'pending':
                            cursor.execute("UPDATE overtime SET hours = ?, reason = ? WHERE id = ?", 
                                           (ot_hours, f"Manual attendance checkout {co_clean}", ot_id))
                    else:
                        cursor.execute("INSERT INTO overtime (user_id, user_name, ot_date, hours, reason, status, organization_id, branch_id) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                                       (user_id, name, actual_date, ot_hours, f"Manual attendance checkout {co_clean}", user_org, user_branch))
                else:
                    if ot_existing and (ot_existing[1] or 'pending').lower() == 'pending':
                        cursor.execute("DELETE FROM overtime WHERE id = ?", (ot_existing[0],))
            else:
                # If no checkout provided, remove any pending OT for this date
                cursor.execute("SELECT id, status FROM overtime WHERE user_id = ? AND ot_date = ?", (user_id, actual_date))
                ot_existing = cursor.fetchone()
                if ot_existing and (ot_existing[1] or 'pending').lower() == 'pending':
                    cursor.execute("DELETE FROM overtime WHERE id = ?", (ot_existing[0],))

            # PHASE 2.3: Enqueue manual attendance into sync_outbox_queue
            ev_in = f"ATT-{user_org}-{user_branch}-MAN-{user_id}-{actual_date}-IN"
            enqueue_sync_outbox(
                cursor=cursor,
                organization_id=user_org,
                branch_id=user_branch,
                event_type="CHECK_IN",
                payload={
                    "user_id": user_id,
                    "detected_name": name,
                    "confidence": 1.0,
                    "source": "manual",
                    "timestamp": timestamp_val_in,
                    "notes": notes,
                    "marked_by": marked_by,
                    "shift_id": shift_db_id,
                    "organization_id": user_org,
                    "branch_id": user_branch
                },
                event_id=ev_in
            )
            if co_clean:
                ev_out = f"ATT-{user_org}-{user_branch}-MAN-{user_id}-{actual_date}-OUT"
                enqueue_sync_outbox(
                    cursor=cursor,
                    organization_id=user_org,
                    branch_id=user_branch,
                    event_type="CHECK_OUT",
                    payload={
                        "user_id": user_id,
                        "detected_name": name,
                        "confidence": 1.0,
                        "source": "manual",
                        "timestamp": timestamp_val_out,
                        "notes": notes,
                        "marked_by": marked_by,
                        "shift_id": shift_db_id,
                        "organization_id": user_org,
                        "branch_id": user_branch
                    },
                    event_id=ev_out
                )

            conn.commit()

        logger.info(f"✓ Manually marked user ID {user_id} present for {actual_date} (shift={shift_db_id}, in={ci_clean}, out={co_clean})")
        return True
    except Exception as e:
        logger.error(f"Failed to manually mark user ID {user_id} present: {e}")
        raise e


def is_user_manually_absent_today(user_id: int) -> bool:
    """Check if a user has been manually marked absent today."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM manual_absent 
                WHERE user_id = ? AND date = date('now', 'localtime')
            ''', (user_id,))
            count = cursor.fetchone()[0]
        return count > 0
    except Exception as e:
        logger.error(f"Failed to check manual absent status for user {user_id}: {e}")
        return False


def clear_manual_absent_today(user_id: int) -> bool:
    """Clear today's manual-absence override when the person is seen by a camera."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM manual_absent WHERE user_id = ? AND date = date("now", "localtime")',
                (user_id,)
            )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to clear manual absent status for user {user_id}: {e}")
        return False


def save_user_photo(user_id: int, photo_path: str, organization_id: Optional[str] = None) -> bool:
    """Save the path of professional profile photo for a user, verifying tenant ownership."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id and organization_id != 'all':
                cursor.execute("SELECT organization_id FROM users WHERE id = ?", (user_id,))
                urow = cursor.fetchone()
                if not urow or (urow[0] and urow[0] not in (organization_id, 'DEFAULT_ORG')):
                    return False
            cursor.execute("UPDATE users SET photo_path = ? WHERE id = ?", (photo_path, user_id))
            updated = cursor.rowcount > 0
            conn.commit()
        return updated
    except Exception as e:
        logger.error(f"Failed to save photo path for user {user_id}: {e}")
        return False


def get_user_photo(user_id: int, organization_id: Optional[str] = None) -> Optional[str]:
    """Get professional profile photo path for a user, verifying tenant ownership."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id and organization_id != 'all':
                cursor.execute("SELECT organization_id FROM users WHERE id = ?", (user_id,))
                urow = cursor.fetchone()
                if not urow or (urow[0] and urow[0] not in (organization_id, 'DEFAULT_ORG')):
                    return None
            cursor.execute("SELECT photo_path FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logger.error(f"Failed to get photo path for user {user_id}: {e}")
        return None


# ============================================
# HR / AUTH / LEAVE / OVERTIME / SALARY HELPERS
# ============================================

def get_user_by_id(user_id: int, organization_id: Optional[str] = None) -> Optional[Dict]:
    """Retrieve full user profile by user_id, optionally verifying organization_id."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT * FROM users WHERE id = ? AND (organization_id = 'DEFAULT_ORG' OR organization_id IS NULL)", (user_id,))
                else:
                    cursor.execute("SELECT * FROM users WHERE id = ? AND organization_id = ?", (user_id, organization_id))
            else:
                cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Failed to get user by id {user_id}: {e}")
        return None


def authenticate_user(email: str, password: str) -> Optional[Dict]:
    """Authenticate an active local user against their stored password hash."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM users WHERE (email = ? OR name = ?) AND active = 1",
                (email.strip(), email.strip())
            )
            row = cursor.fetchone()
            if row:
                user_dict = dict(row)
                password_hash = user_dict.get('password_hash')
                if password_hash and check_password_hash(password_hash, password):
                    return user_dict
        return None
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        return None


def change_password(user_id: int, new_password: str) -> bool:
    """Store a new password hash and clear any legacy plaintext value."""
    if len(new_password) < 14:
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET password = NULL, password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Change password error: {e}")
        return False


def split_leave_request_dates(user_id: int, start_date: str, end_date: str, available_days: int) -> Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
    """Splits a leave date range into two based on available scheduled working days."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('SELECT working_days FROM shifts WHERE id = (SELECT shift_id FROM users WHERE id = ?)', (user_id,))
            row = cursor.fetchone()
            working_days = set(_normalize_shift_working_days(row['working_days'])) if row and row['working_days'] else {0,1,2,3,4,5,6}
            
            org_id, branch_id = _user_tenant(cursor, user_id)
            cursor.execute(
                """SELECT holiday_date FROM company_holidays
                   WHERE holiday_date >= ? AND holiday_date <= ?
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                (start_date, end_date, org_id, branch_id),
            )
            holidays = {r['holiday_date'] for r in cursor.fetchall()}
            
            cursor.execute(
                """SELECT calendar_date, override_type FROM special_date_overrides
                   WHERE calendar_date >= ? AND calendar_date <= ?
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                     AND (scope_type = 'all' OR (scope_type = 'employee' AND scope_id = ?))""",
                (start_date, end_date, org_id, branch_id, user_id),
            )
            overrides = {r['calendar_date']: r['override_type'] for r in cursor.fetchall()}
            
            d1 = datetime.strptime(start_date, '%Y-%m-%d')
            d2 = datetime.strptime(end_date, '%Y-%m-%d')

            if available_days <= 0:
                return None, (start_date, end_date)
            
            counted = 0
            split_date = None
            
            for i in range((d2 - d1).days + 1):
                curr = d1 + timedelta(days=i)
                curr_str = curr.strftime('%Y-%m-%d')
                
                override = overrides.get(curr_str)
                if override:
                    is_work = (override == 'working_day')
                elif curr_str in holidays:
                    is_work = False
                else:
                    is_work = curr.weekday() in working_days
                    
                if is_work:
                    counted += 1
                    
                if counted == available_days and split_date is None:
                    split_date = curr
                    
            if split_date is None or split_date == d2:
                return (start_date, end_date), None
                
            range1 = (start_date, split_date.strftime('%Y-%m-%d'))
            next_day = split_date + timedelta(days=1)
            range2 = (next_day.strftime('%Y-%m-%d'), end_date)
            return range1, range2
    except Exception as e:
        logger.error(f"Error splitting leave dates: {e}")
        return (start_date, end_date), None

def _find_active_leave_overlap(cursor, user_id: int, start_date: str, end_date: str, exclude_id: Optional[int] = None):
    query = """
        SELECT lr.id, COALESCE(lt.name, lr.leave_type) AS type_name,
               lr.start_date, lr.end_date, LOWER(COALESCE(lr.status, 'pending')) AS status
        FROM leave_requests lr
        LEFT JOIN leave_types lt ON lt.code = lr.leave_type OR lt.name = lr.leave_type
        WHERE lr.user_id = ?
          AND LOWER(COALESCE(lr.status, 'pending')) IN ('pending', 'approved', 'partially_approved')
          AND lr.start_date <= ? AND lr.end_date >= ?
    """
    params = [user_id, end_date, start_date]
    if exclude_id is not None:
        query += " AND lr.id != ?"
        params.append(exclude_id)
    query += " ORDER BY lr.start_date, lr.id LIMIT 1"
    cursor.execute(query, params)
    return cursor.fetchone()


def add_leave_request(
    user_id: int,
    user_name: str,
    leave_type: str,
    start_date: str,
    end_date: str,
    reason: str = '',
    paid_days: Optional[int] = None,
    unpaid_days: Optional[int] = None,
    quota_days: Optional[int] = None,
    paid_through_date: Optional[str] = None,
    quota_through_date: Optional[str] = None,
    leave_duration: str = 'full',
    half_leave_quota_type_id: Optional[int] = None,
    half_leave_quota_charge: bool = False,
) -> Optional[int]:
    """Insert a new leave request with strict date ordering validation."""
    try:
        s_str = (start_date or '').strip()
        e_str = (end_date or '').strip()
        d1 = datetime.strptime(s_str, '%Y-%m-%d')
        d2 = datetime.strptime(e_str, '%Y-%m-%d')
        if d2 < d1:
            logger.error(f"Cannot add leave request: end_date ({e_str}) < start_date ({s_str})")
            return None

        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('BEGIN IMMEDIATE')
            conflict = _find_active_leave_overlap(cursor, user_id, s_str, e_str)
            if conflict:
                logger.warning(
                    f"Cannot add leave request for user {user_id}: dates overlap active request #{conflict['id']}"
                )
                return None
            cursor.execute("SELECT organization_id, branch_id FROM users WHERE id = ?", (user_id,))
            u_row = cursor.fetchone()
            u_org = (u_row[0] if u_row else None) or 'DEFAULT_ORG'
            u_branch = (u_row[1] if u_row else None) or 'DEFAULT_BRANCH'
            cursor.execute(
                     '''INSERT INTO leave_requests (
                                user_id, user_name, leave_type, leave_duration, start_date, end_date, reason,
                                paid_days, unpaid_days, quota_days, paid_through_date, quota_through_date,
                                half_leave_quota_type_id, half_leave_quota_charge, organization_id, branch_id
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                         (user_id, user_name, leave_type, leave_duration, s_str, e_str, reason,
                      paid_days, unpaid_days, quota_days, paid_through_date, quota_through_date,
                      half_leave_quota_type_id, int(bool(half_leave_quota_charge)), u_org, u_branch)
            )
            conn.commit()
            leave_id = cursor.lastrowid
            _cloud_mirror("mirror_leave", {
                "id": leave_id,
                "user_id": user_id,
                "leave_type": leave_type,
                "start_date": s_str,
                "end_date": e_str,
                "reason": reason,
                "status": "pending",
                "organization_id": u_org,
                "branch_id": u_branch,
            })
            return leave_id
    except Exception as e:
        logger.error(f"Add leave request error: {e}")
        return None


def add_leave_requests(user_id: int, user_name: str, requests: List[Tuple[str, str, str, str]]) -> Optional[List[int]]:
    """Insert related paid/unpaid leave segments atomically."""
    try:
        for _, start_date, end_date, _ in requests:
            start = datetime.strptime(start_date, '%Y-%m-%d')
            end = datetime.strptime(end_date, '%Y-%m-%d')
            if end < start:
                return None
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('BEGIN IMMEDIATE')
            cursor.execute("SELECT organization_id, branch_id FROM users WHERE id = ?", (user_id,))
            u_row = cursor.fetchone()
            u_org = (u_row[0] if u_row else None) or 'DEFAULT_ORG'
            u_branch = (u_row[1] if u_row else None) or 'DEFAULT_BRANCH'
            ids = []
            for leave_type, start_date, end_date, reason in requests:
                cursor.execute(
                    '''INSERT INTO leave_requests (user_id, user_name, leave_type, start_date, end_date, reason, organization_id, branch_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (user_id, user_name, leave_type, start_date, end_date, reason, u_org, u_branch)
                )
                ids.append(cursor.lastrowid)
            conn.commit()
            return ids
    except Exception as e:
        logger.error(f"Add leave request segments error: {e}")
        return None


def get_leave_requests(user_id: Optional[int] = None, status: Optional[str] = None, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict]:
    """Get list of leave requests optionally filtered by user_id, status, organization_id, and branch_id."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = "SELECT * FROM leave_requests WHERE 1=1"
            params = []
            if user_id:
                query += " AND user_id = ?"
                params.append(user_id)
            if status:
                query += " AND status = ?"
                params.append(status)
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    query += " AND (organization_id = 'DEFAULT_ORG' OR organization_id IS NULL)"
                else:
                    query += " AND organization_id = ?"
                    params.append(organization_id)
            if branch_id and branch_id != 'all':
                if branch_id == 'DEFAULT_BRANCH':
                    query += " AND (branch_id = 'DEFAULT_BRANCH' OR branch_id IS NULL)"
                else:
                    query += " AND branch_id = ?"
                    params.append(branch_id)
            query += " ORDER BY created_at DESC"
            cursor.execute(query, params)
            requests = [dict(r) for r in cursor.fetchall()]
            for leave_request in requests:
                requested_days = 1 if leave_request.get('leave_duration') == 'half' else _count_scheduled_leave_days(
                    cursor, leave_request['user_id'], leave_request['start_date'], leave_request['end_date']
                )
                leave_request['requested_days'] = requested_days
                if leave_request.get('paid_days') is None or leave_request.get('unpaid_days') is None:
                    leave_type = str(leave_request.get('leave_type') or '').lower()
                    if leave_type == 'unpaid':
                        leave_request['paid_days'] = 0
                        leave_request['unpaid_days'] = requested_days
                        leave_request['quota_days'] = 0
                    elif leave_type == 'excess_paid':
                        leave_request['paid_days'] = requested_days
                        leave_request['unpaid_days'] = 0
                        leave_request['quota_days'] = 0
                    else:
                        leave_request['paid_days'] = requested_days
                        leave_request['unpaid_days'] = 0
                        leave_request['quota_days'] = requested_days
            return requests
    except Exception as e:
        logger.error(f"Get leave requests error: {e}")
        return []


def get_leave_by_id(leave_id: int, organization_id: Optional[str] = None) -> Optional[Dict]:
    """Retrieve single leave request by id with tenant scoping."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM leave_requests WHERE id = ?", (leave_id,))
            row = cursor.fetchone()
            if row:
                d = dict(row)
                if organization_id and organization_id != 'all':
                    if organization_id == 'DEFAULT_ORG':
                        if d.get('organization_id') not in ('DEFAULT_ORG', None):
                            return None
                    elif d.get('organization_id') != organization_id:
                        return None
                return d
    except Exception as e:
        logger.error(f"Get leave by id error: {e}")
    return None


def update_leave_status(leave_id: int, status: str = 'approved', approved_by: str = 'Admin', organization_id: Optional[str] = None) -> bool:
    """Update status unless it would create overlapping active leave, verifying tenant authorization."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('BEGIN IMMEDIATE')
            cursor.execute(
                "SELECT user_id, start_date, end_date, organization_id FROM leave_requests WHERE id = ?",
                (leave_id,)
            )
            leave = cursor.fetchone()
            if not leave:
                return False
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    if leave['organization_id'] not in ('DEFAULT_ORG', None):
                        return False
                elif leave['organization_id'] != organization_id:
                    return False
            raise_if_payroll_month_locked(
                str(leave['start_date'])[:10],
                leave['organization_id'],
            )
            if str(status or '').strip().lower() in ('pending', 'approved', 'partially_approved'):
                conflict = _find_active_leave_overlap(
                    cursor, leave['user_id'], leave['start_date'], leave['end_date'], exclude_id=leave_id
                )
                if conflict:
                    logger.warning(
                        f"Cannot set leave request #{leave_id} to {status}: dates overlap active request #{conflict['id']}"
                    )
                    return False
            cursor.execute(
                "UPDATE leave_requests SET status = ?, approved_by = ? WHERE id = ?",
                (status, approved_by, leave_id)
            )
            conn.commit()
            return cursor.rowcount > 0
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Update leave status error: {e}")
        return False


def delete_leave_request(leave_id: int, organization_id: Optional[str] = None) -> bool:
    """Delete a leave request, ensuring tenant authorization."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id and organization_id != 'all':
                cursor.execute("SELECT organization_id FROM leave_requests WHERE id = ?", (leave_id,))
                lr = cursor.fetchone()
                if not lr:
                    return False
                if organization_id == 'DEFAULT_ORG':
                    if lr[0] not in ('DEFAULT_ORG', None):
                        return False
                elif lr[0] != organization_id:
                    return False
            cursor.execute("DELETE FROM leave_requests WHERE id = ?", (leave_id,))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Delete leave request error: {e}")
        return False


# ============================================
# LEAVE TYPES & HOLIDAYS MANAGEMENT HELPERS
# ============================================

def get_leave_types(
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict]:
    """Get leave types owned by the authenticated organization."""
    org, branch = _tenant_ids(organization_id, branch_id)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            seed_leave_types_if_empty(cursor, org, branch)
            conn.commit()
            cursor.execute(
                """SELECT * FROM leave_types
                   WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                   ORDER BY id ASC""",
                (org,),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get leave types error: {e}")
        return []


def add_leave_type(
    name: str, code: str, yearly_allowance: float = 12.0, is_paid: int = 1,
    description: str = '', allowance_period: str = 'yearly',
    allowance_months: Optional[int] = None, half_leave_enabled: bool = False,
    half_leave_mode: str = 'separate', half_leave_allowance_count: int = 0,
    half_leave_allowance_months: int = 12, half_leaves_per_quota_day: int = 2,
    half_leave_quota_type_id: Optional[int] = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Optional[int]:
    """Add a new leave type policy for one organization."""
    try:
        default_months = {'monthly': 1, 'half_yearly': 6, 'yearly': 12}.get(allowance_period, 12)
        duration_months = int(allowance_months if allowance_months is not None else default_months)
        half_mode = str(half_leave_mode or 'separate').strip().lower()
        half_count = int(half_leave_allowance_count or 0)
        half_months = int(half_leave_allowance_months or 12)
        half_conversion = int(half_leaves_per_quota_day or 2)
        if not 1 <= duration_months <= 12 or half_mode not in ('separate', 'convert') or half_count < 0 or not 1 <= half_months <= 12 or half_conversion < 1:
            return None
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO leave_types (
                       name, code, yearly_allowance, is_paid, description, allowance_period, allowance_months,
                       half_leave_enabled, half_leave_mode, half_leave_allowance_count,
                       half_leave_allowance_months, half_leaves_per_quota_day, half_leave_quota_type_id,
                       organization_id, branch_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, code, yearly_allowance, is_paid, description, allowance_period, duration_months,
                 int(bool(half_leave_enabled)), half_mode, half_count, half_months, half_conversion,
                 half_leave_quota_type_id, *_tenant_ids(organization_id, branch_id))
            )
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"Add leave type error: {e}")
        return None


def update_leave_type(
    type_id: int, name: str, yearly_allowance: float, is_paid: int,
    description: str, active: int = 1, allowance_period: str = 'yearly',
    allowance_months: Optional[int] = None, half_leave_enabled: bool = False,
    half_leave_mode: str = 'separate', half_leave_allowance_count: int = 0,
    half_leave_allowance_months: int = 12, half_leaves_per_quota_day: int = 2,
    half_leave_quota_type_id: Optional[int] = None,
    organization_id: Optional[str] = None,
) -> bool:
    """Update an existing leave type owned by the organization."""
    try:
        default_months = {'monthly': 1, 'half_yearly': 6, 'yearly': 12}.get(allowance_period, 12)
        duration_months = int(allowance_months if allowance_months is not None else default_months)
        half_mode = str(half_leave_mode or 'separate').strip().lower()
        half_count = int(half_leave_allowance_count or 0)
        half_months = int(half_leave_allowance_months or 12)
        half_conversion = int(half_leaves_per_quota_day or 2)
        if not 1 <= duration_months <= 12 or half_mode not in ('separate', 'convert') or half_count < 0 or not 1 <= half_months <= 12 or half_conversion < 1:
            return False
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE leave_types SET name = ?, yearly_allowance = ?, is_paid = ?, description = ?, active = ?,
                   allowance_period = ?, allowance_months = ?, half_leave_enabled = ?, half_leave_mode = ?,
                   half_leave_allowance_count = ?, half_leave_allowance_months = ?,
                   half_leaves_per_quota_day = ?, half_leave_quota_type_id = ?
                   WHERE id = ? AND COALESCE(organization_id, 'DEFAULT_ORG') = ?""",
                (name, yearly_allowance, is_paid, description, active, allowance_period, duration_months,
                 int(bool(half_leave_enabled)), half_mode, half_count, half_months, half_conversion,
                 half_leave_quota_type_id, type_id, _tenant_ids(organization_id)[0])
            )
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Update leave type error: {e}")
        return False


def delete_leave_type(type_id: int, organization_id: Optional[str] = None) -> bool:
    """Delete a leave type policy owned by the organization."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM leave_types WHERE id = ? AND COALESCE(organization_id, 'DEFAULT_ORG') = ?",
                (type_id, _tenant_ids(organization_id)[0]),
            )
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Delete leave type error: {e}")
        return False


def get_company_holidays(
    year: Optional[int] = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict]:
    """Get holidays scoped to the requested organization and branch."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = "SELECT * FROM company_holidays WHERE 1=1"
            params = []
            if year:
                query += " AND strftime('%Y', holiday_date) = ?"
                params.append(str(year))
            if organization_id and organization_id != 'all':
                query += " AND COALESCE(organization_id, 'DEFAULT_ORG') = ?"
                params.append(organization_id)
            if branch_id and branch_id != 'all':
                query += " AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?"
                params.append(branch_id)
            query += " ORDER BY holiday_date ASC"
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get company holidays error: {e}")
        return []


def get_company_holiday_in_range(
    start_date: str,
    end_date: str,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Optional[Dict]:
    """Return the first company holiday that overlaps a leave date range."""
    try:
        range_start = datetime.strptime(start_date, '%Y-%m-%d').date()
        range_end = datetime.strptime(end_date, '%Y-%m-%d').date()
        if range_end < range_start:
            return None

        holidays = get_company_holidays(organization_id=organization_id, branch_id=branch_id)
        current_date = range_start
        while current_date <= range_end:
            current_iso = current_date.isoformat()
            current_month_day = current_iso[5:]
            for holiday in holidays:
                holiday_start = str(holiday.get('holiday_date') or '')
                holiday_end = str(holiday.get('end_date') or holiday_start)
                if holiday.get('is_recurring'):
                    start_month_day = holiday_start[5:]
                    end_month_day = holiday_end[5:]
                    matches = (
                        start_month_day <= current_month_day <= end_month_day
                        if start_month_day <= end_month_day
                        else current_month_day >= start_month_day or current_month_day <= end_month_day
                    )
                else:
                    matches = holiday_start <= current_iso <= holiday_end
                if matches:
                    return {**holiday, 'matched_date': current_iso}
            current_date += timedelta(days=1)
        return None
    except (TypeError, ValueError) as e:
        logger.error(f"Invalid leave date range for holiday check: {e}")
        return None


def add_company_holiday(name: str, holiday_date: str, end_date: str = None, description: str = '', is_recurring: int = 0,
                        organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> Optional[int]:
    """Add a new company holiday."""
    try:
        if not end_date:
            end_date = holiday_date
        organization_id = organization_id or 'DEFAULT_ORG'
        branch_id = branch_id or 'DEFAULT_BRANCH'
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO company_holidays (name, holiday_date, end_date, description, is_recurring, organization_id, branch_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, holiday_date, end_date, description, is_recurring, organization_id, branch_id)
            )
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"Add company holiday error: {e}")
        return None


def delete_company_holiday(
    holiday_id: int,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    """Delete a company holiday only within its owning tenant and branch."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            query = "DELETE FROM company_holidays WHERE id = ?"
            params: List[Any] = [holiday_id]
            if organization_id is not None:
                query += " AND COALESCE(organization_id, 'DEFAULT_ORG') = ?"
                params.append(organization_id)
            if branch_id is not None:
                query += " AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?"
                params.append(branch_id)
            cursor.execute(query, params)
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Delete company holiday error: {e}")
        return False


def _leave_allowance_cycle_window(reference_date: datetime, allowance_period: str, allowance_months: Optional[int] = None) -> Tuple[str, str]:
    """Return calendar-aligned allowance dates for a 1-12 month policy cycle."""
    legacy_months = {'monthly': 1, 'half_yearly': 6, 'yearly': 12}
    try:
        duration_months = int(allowance_months or legacy_months.get(allowance_period, 12))
    except (TypeError, ValueError):
        duration_months = legacy_months.get(allowance_period, 12)
    duration_months = max(1, min(12, duration_months))
    start_month = ((reference_date.month - 1) // duration_months) * duration_months + 1
    end_month = min(12, start_month + duration_months - 1)
    start = datetime(reference_date.year, start_month, 1)
    next_month = datetime(reference_date.year + 1, 1, 1) if end_month == 12 else datetime(reference_date.year, end_month + 1, 1)
    end = next_month - timedelta(days=1)
    return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')


def get_leave_balances(year: int = 2026, user_id: Optional[int] = None, organization_id: Optional[str] = None) -> List[Dict]:
    """Get employee leave balances for a year, auto-initializing rows if needed, scoped by tenant."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Active users
            user_query = "SELECT id, name, department FROM users WHERE active = 1"
            params = []
            if user_id:
                user_query += " AND id = ?"
                params.append(user_id)
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    user_query += " AND (organization_id = 'DEFAULT_ORG' OR organization_id IS NULL)"
                else:
                    user_query += " AND organization_id = ?"
                    params.append(organization_id)
            cursor.execute(user_query, params)
            users = [dict(u) for u in cursor.fetchall()]

            # Leave types for this organization only
            org = organization_id or 'DEFAULT_ORG'
            seed_leave_types_if_empty(cursor, org)
            conn.commit()
            cursor.execute(
                "SELECT * FROM leave_types WHERE active = 1 AND COALESCE(organization_id, 'DEFAULT_ORG') = ?",
                (org,),
            )
            ltypes = [dict(lt) for lt in cursor.fetchall()]

            result = []
            for u in users:
                u_balances = {'user_id': u['id'], 'user_name': u['name'], 'department': u['department'], 'year': year, 'balances': {}}
                for lt in ltypes:
                    allowance_period = lt.get('allowance_period', 'yearly')
                    cursor.execute(
                        "SELECT allocated_days, used_days FROM employee_leave_balances WHERE user_id = ? AND leave_type_id = ? AND year = ?",
                        (u['id'], lt['id'], year)
                    )
                    row = cursor.fetchone()
                    if row:
                        alloc = row['allocated_days']
                    else:
                        alloc = lt['yearly_allowance']
                        if allowance_period == 'yearly':
                            try:
                                cursor.execute(
                                    "INSERT OR IGNORE INTO employee_leave_balances (user_id, leave_type_id, year, allocated_days, used_days) VALUES (?, ?, ?, ?, ?)",
                                    (u['id'], lt['id'], year, alloc, 0.0)
                                )
                                conn.commit()
                            except Exception:
                                pass
                    
                    reference_month = datetime.now().month if year == datetime.now().year else 12
                    cycle_start, cycle_end = _leave_allowance_cycle_window(
                        datetime(year, reference_month, 1),
                        allowance_period,
                        lt.get('allowance_months')
                    )
                    cursor.execute("""
                        SELECT start_date, end_date, quota_days, quota_through_date, leave_duration,
                               half_leave_quota_type_id, half_leave_quota_charge FROM leave_requests
                        WHERE user_id = ? AND status = 'approved' AND (leave_type = ? OR leave_type = ?)
                          AND end_date >= ? AND start_date <= ?
                    """, (u['id'], lt['code'], lt['name'], cycle_start, cycle_end))
                    actual_used = 0.0
                    for request_row in cursor.fetchall():
                        if request_row['leave_duration'] == 'half':
                            if request_row['half_leave_quota_charge'] and request_row['half_leave_quota_type_id'] == lt['id']:
                                actual_used += 1
                            continue
                        clipped_start = max(request_row['start_date'], cycle_start)
                        clipped_end = min(request_row['end_date'], cycle_end)
                        if request_row['quota_days'] is None:
                            actual_used += _count_scheduled_leave_days(cursor, u['id'], clipped_start, clipped_end)
                        else:
                            quota_end = min(clipped_end, request_row['quota_through_date'] or request_row['end_date'])
                            if quota_end >= clipped_start:
                                actual_used += _count_scheduled_leave_days(cursor, u['id'], clipped_start, quota_end)
                    cursor.execute("""
                        SELECT COUNT(*) FROM leave_requests
                        WHERE user_id = ? AND status = 'approved' AND leave_duration = 'half'
                          AND half_leave_quota_charge = 1 AND half_leave_quota_type_id = ?
                          AND (leave_type != ? AND leave_type != ?)
                          AND end_date >= ? AND start_date <= ?
                    """, (u['id'], lt['id'], lt['code'], lt['name'], cycle_start, cycle_end))
                    external_half_quota_charges = int(cursor.fetchone()[0] or 0)
                    actual_used += external_half_quota_charges
                    used_val = float(actual_used)

                    half_cycle_start, half_cycle_end = _leave_allowance_cycle_window(
                        datetime(year, reference_month, 1),
                        'custom',
                        lt.get('half_leave_allowance_months', 12),
                    )
                    cursor.execute("""
                        SELECT COUNT(*) FROM leave_requests
                        WHERE user_id = ? AND status = 'approved' AND leave_type = ?
                          AND leave_duration = 'half' AND end_date >= ? AND start_date <= ?
                    """, (u['id'], lt['code'], half_cycle_start, half_cycle_end))
                    half_leave_count = int(cursor.fetchone()[0] or 0)
                    half_allowance = int(lt.get('half_leave_allowance_count') or 0)
                    half_leave_remaining = max(0, half_allowance - half_leave_count) if lt.get('half_leave_mode') == 'separate' else None

                    u_balances['balances'][lt['code']] = {
                        'type_name': lt['name'],
                        'allocated': alloc,
                        'used': used_val,
                        'remaining': max(0.0, alloc - used_val),
                        'half_leave_count': half_leave_count,
                        'half_leave_remaining': half_leave_remaining,
                        'half_leave_enabled': bool(lt.get('half_leave_enabled')),
                        'half_leave_mode': lt.get('half_leave_mode', 'separate'),
                        'half_leave_allowance_count': half_allowance,
                        'half_leave_allowance_months': int(lt.get('half_leave_allowance_months') or 12),
                        'is_paid': lt['is_paid'],
                        'allowance_period': lt.get('allowance_period', 'yearly'),
                        'allowance_months': lt.get('allowance_months', 12)
                    }
                result.append(u_balances)
            return result
    except Exception as e:
        logger.error(f"Get leave balances error: {e}")
        return []


def _count_scheduled_leave_days(cursor, user_id: int, start_date: str, end_date: str) -> int:
    """Calculate the number of scheduled working days in a leave period."""
    if not user_id:
        try:
            d1 = datetime.strptime(start_date, '%Y-%m-%d')
            d2 = datetime.strptime(end_date, '%Y-%m-%d')
            return max(0, (d2 - d1).days + 1)
        except Exception:
            return 0

    cursor.execute('''SELECT working_days FROM shifts WHERE id = (
        SELECT shift_id FROM users WHERE id = ?
    )''', (user_id,))
    row = cursor.fetchone()
    working_days = set(_normalize_shift_working_days(row['working_days'])) if row and row['working_days'] else {0,1,2,3,4,5,6}

    org_id, branch_id = _user_tenant(cursor, user_id)
    cursor.execute(
        """SELECT holiday_date FROM company_holidays
           WHERE holiday_date >= ? AND holiday_date <= ?
             AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
             AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
        (start_date, end_date, org_id, branch_id),
    )
    holidays = {r['holiday_date'] for r in cursor.fetchall()}

    cursor.execute(
        """SELECT calendar_date, override_type FROM special_date_overrides
           WHERE calendar_date >= ? AND calendar_date <= ?
             AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
             AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
             AND (scope_type = 'all' OR (scope_type = 'employee' AND scope_id = ?))""",
        (start_date, end_date, org_id, branch_id, user_id),
    )
    overrides = {r['calendar_date']: r['override_type'] for r in cursor.fetchall()}

    try:
        d1 = datetime.strptime(start_date, '%Y-%m-%d')
        d2 = datetime.strptime(end_date, '%Y-%m-%d')
    except Exception:
        return 0

    days = 0
    for i in range((d2 - d1).days + 1):
        curr_date = d1 + timedelta(days=i)
        curr_str = curr_date.strftime('%Y-%m-%d')
        override = overrides.get(curr_str)
        if override:
            if override == 'working_day': days += 1
        elif curr_str in holidays:
            pass
        elif curr_date.weekday() in working_days:
            days += 1
    return days

def check_leave_quota(
    user_id: Optional[int] = None,
    leave_type_code: str = 'casual',
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    exclude_id: Optional[int] = None,
    leave_duration: str = 'full',
) -> Dict[str, Any]:
    """
    Check if a requested leave duration is within the employee's allowed paid quota.
    Unpaid Leave (LWP) is exempt (unlimited days allowed).
    When quota is exceeded, returns exact user-friendly warning message.
    Always returns valid numeric remaining_days and requested_days.
    """
    try:
        duration = str(leave_duration or 'full').strip().lower()
        if duration not in ('full', 'half'):
            raise ValueError('Leave duration must be full or half.')
        requested_days = 0
        reference_date = datetime.now()

        if start_date and end_date:
            s_str = start_date.strip()
            e_str = end_date.strip()
            if s_str and e_str:
                d1 = datetime.strptime(s_str, '%Y-%m-%d')
                d2 = datetime.strptime(e_str, '%Y-%m-%d')
                if d2 < d1:
                    return {
                        'allowed': False,
                        'message': 'End date cannot be earlier than start date.',
                        'requested_days': 0,
                        'remaining_days': 0.0,
                        'allocated_days': 0.0,
                        'used_days': 0.0,
                        'leave_type_name': leave_type_code
                    }
                if duration == 'half' and d1.date() != d2.date():
                    return {
                        'allowed': False,
                        'message': 'Half-day leave must be requested for a single date.',
                        'requested_days': 1,
                        'quota_days_requested': 0,
                        'remaining_days': 0.0,
                        'allocated_days': 0.0,
                        'used_days': 0.0,
                        'leave_type_name': leave_type_code,
                    }
                reference_date = d1

        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Calculate requested days based on scheduled workdays only
            if user_id and start_date and end_date:
                requested_days = _count_scheduled_leave_days(cursor, user_id, start_date.strip(), end_date.strip())
            elif start_date and end_date:
                requested_days = max(0, (d2 - d1).days + 1)

            if user_id and start_date and end_date:
                conflict = _find_active_leave_overlap(
                    cursor, user_id, start_date.strip(), end_date.strip(), exclude_id=exclude_id
                )
                if conflict:
                    status_label = str(conflict['status']).replace('_', ' ').title()
                    return {
                        'allowed': False,
                        'reason_code': 'overlapping_leave',
                        'message': f"Looks like this employee already has a leave request from {conflict['start_date']} to {conflict['end_date']} ({status_label}). No need to submit another for those dates. Please choose different dates or review the existing request.",
                        'requested_days': 1 if duration == 'half' else requested_days,
                        'quota_days_requested': 0,
                        'remaining_days': 9999.0,
                        'allocated_days': 9999.0,
                        'used_days': 0.0,
                        'leave_type_name': leave_type_code,
                    }

            # Find leave type owned by the employee's organization
            org_id, _branch_id = _user_tenant(cursor, user_id) if user_id else ('DEFAULT_ORG', 'DEFAULT_BRANCH')
            cursor.execute(
                """SELECT * FROM leave_types
                   WHERE (code = ? OR name = ?)
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                   LIMIT 1""",
                (leave_type_code, leave_type_code, org_id)
            )
            lt = cursor.fetchone()
            if not lt:
                return {
                    'allowed': True,
                    'requested_days': 1 if duration == 'half' else requested_days,
                    'quota_days_requested': 0 if duration == 'half' else requested_days,
                    'remaining_days': 10.0,
                    'allocated_days': 10.0,
                    'used_days': 0.0,
                    'leave_type_name': leave_type_code,
                    'is_paid': 1
                }

            lt_dict = dict(lt)
            leave_name = lt_dict['name']
            is_paid = int(lt_dict.get('is_paid', 1))

            if duration == 'half':
                if not bool(lt_dict.get('half_leave_enabled')):
                    return {
                        'allowed': False,
                        'message': f'Half-day leave is not enabled for {leave_name}.',
                        'requested_days': 1,
                        'quota_days_requested': 0,
                        'remaining_days': 0,
                        'leave_type_name': leave_name,
                        'half_leave_enabled': False,
                    }
                org_id, branch_id = _user_tenant(cursor, user_id)
                ensure_payroll_settings_row(cursor, org_id, branch_id)
                org_policy = cursor.execute(
                    """SELECT half_leave_quota_mode, half_leaves_per_quota_day FROM payroll_settings
                       WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                         AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                    (org_id, branch_id),
                ).fetchone()
                scoped_policy = _get_scoped_payroll_rule(cursor, 'half_leave_quota', user_id)
                policy_mode = str(
                    scoped_policy['half_leave_quota_mode'] if scoped_policy
                    else org_policy['half_leave_quota_mode'] if org_policy
                    else 'category'
                ).lower()
                monthly_threshold = max(1, int(
                    scoped_policy['half_leaves_per_quota_day'] if scoped_policy
                    else org_policy['half_leaves_per_quota_day'] if org_policy
                    else 2
                ))
                if policy_mode in ('monthly_cap', 'monthly_convert'):
                    month_start = reference_date.date().replace(day=1)
                    if month_start.month == 12:
                        next_month = month_start.replace(year=month_start.year + 1, month=1)
                    else:
                        next_month = month_start.replace(month=month_start.month + 1)
                    month_start_str = month_start.isoformat()
                    month_end_str = (next_month - timedelta(days=1)).isoformat()
                    type_filter = "" if policy_mode == 'monthly_cap' else "AND (leave_type = ? OR leave_type = ?)"
                    params = [user_id]
                    if policy_mode == 'monthly_convert':
                        params.extend([lt_dict['code'], leave_name])
                    cursor.execute(f"""
                        SELECT COUNT(*) FROM leave_requests
                        WHERE user_id = ? {type_filter}
                          AND leave_duration = 'half' AND LOWER(status) IN ('pending', 'approved')
                          AND end_date >= ? AND start_date <= ?
                    """, (*params, month_start_str, month_end_str))
                    existing_half_count = int(cursor.fetchone()[0] or 0)
                    if policy_mode == 'monthly_cap':
                        if existing_half_count >= monthly_threshold:
                            return {
                                'allowed': False,
                                'reason_code': 'half_leave_monthly_cap',
                                'message': f'Monthly half-leave limit reached ({monthly_threshold} occurrence(s) per calendar month).',
                                'requested_days': 1,
                                'quota_days_requested': 0,
                                'remaining_days': 0,
                                'leave_type_name': leave_name,
                                'half_leave_enabled': True,
                                'half_leave_mode': policy_mode,
                                'half_leave_count': existing_half_count,
                            }
                        return {
                            'allowed': True,
                            'message': 'OK',
                            'requested_days': 1,
                            'quota_days_requested': 0,
                            'remaining_days': max(0, monthly_threshold - existing_half_count),
                            'leave_type_name': leave_name,
                            'is_paid': is_paid,
                            'half_leave_enabled': True,
                            'half_leave_mode': policy_mode,
                            'half_leave_count': existing_half_count + 1,
                            'half_leaves_per_month': monthly_threshold,
                        }
                    half_mode = 'convert'
                    half_months = 1
                    conversion_ratio = monthly_threshold
                else:
                    half_months = max(1, min(12, int(lt_dict.get('half_leave_allowance_months') or 12)))
                    half_cycle_start, half_cycle_end = _leave_allowance_cycle_window(
                        reference_date, 'custom', half_months
                    )
                    cursor.execute("""
                        SELECT COUNT(*) FROM leave_requests
                        WHERE user_id = ? AND (leave_type = ? OR leave_type = ?)
                          AND leave_duration = 'half' AND LOWER(status) IN ('pending', 'approved')
                          AND end_date >= ? AND start_date <= ?
                    """, (user_id, lt_dict['code'], leave_name, half_cycle_start, half_cycle_end))
                    existing_half_count = int(cursor.fetchone()[0] or 0)
                    half_mode = str(lt_dict.get('half_leave_mode') or 'separate').lower()
                    conversion_ratio = max(1, int(lt_dict.get('half_leaves_per_quota_day') or 2))
                half_allowance = int(lt_dict.get('half_leave_allowance_count') or 0)
                if half_mode == 'separate':
                    if existing_half_count >= half_allowance:
                        return {
                            'allowed': False,
                            'message': f'{leave_name} half-leave allowance reached ({half_allowance} occurrence(s) per {half_months}-month cycle).',
                            'requested_days': 1,
                            'quota_days_requested': 0,
                            'remaining_days': max(0, half_allowance - existing_half_count),
                            'leave_type_name': leave_name,
                            'half_leave_enabled': True,
                            'half_leave_mode': half_mode,
                            'half_leave_count': existing_half_count,
                            'half_leave_allowance_count': half_allowance,
                            'half_leave_allowance_months': half_months,
                        }
                    return {
                        'allowed': True,
                        'message': 'OK',
                        'requested_days': 1,
                        'quota_days_requested': 0,
                        'remaining_days': max(0, half_allowance - existing_half_count),
                        'leave_type_name': leave_name,
                        'is_paid': is_paid,
                        'half_leave_enabled': True,
                        'half_leave_mode': half_mode,
                        'half_leave_count': existing_half_count + 1,
                        'half_leave_allowance_count': half_allowance,
                        'half_leave_allowance_months': half_months,
                    }

                target_id = lt_dict.get('half_leave_quota_type_id')
                if not target_id:
                    return {
                        'allowed': False,
                        'message': f'Choose a quota category for converted {leave_name} half-leaves.',
                        'requested_days': 1,
                        'quota_days_requested': 0,
                        'remaining_days': 0,
                        'leave_type_name': leave_name,
                        'half_leave_enabled': True,
                        'half_leave_mode': 'convert',
                    }
                cursor.execute(
                    """SELECT code, name FROM leave_types
                       WHERE id = ? AND active = 1
                         AND COALESCE(organization_id, 'DEFAULT_ORG') = ?""",
                    (target_id, org_id),
                )
                target_type = cursor.fetchone()
                if not target_type:
                    return {
                        'allowed': False,
                        'message': 'The quota category selected for half-leave conversion is no longer active.',
                        'requested_days': 1,
                        'quota_days_requested': 0,
                        'remaining_days': 0,
                        'leave_type_name': leave_name,
                    }
                charge_next = (existing_half_count + 1) // conversion_ratio > existing_half_count // conversion_ratio
                target_quota = {'allowed': True, 'remaining_days': 0, 'leave_type_name': target_type['name']}
                if charge_next:
                    target_quota = check_leave_quota(
                        user_id=user_id,
                        leave_type_code=target_type['code'],
                        start_date=start_date,
                        end_date=end_date,
                        leave_duration='full',
                    )
                    if not target_quota.get('allowed'):
                        return {
                            'allowed': False,
                            'message': f'Half-leave conversion would use one {target_type["name"]} quota day. {target_quota.get("message", "No quota day is available.")}',
                            'requested_days': 1,
                            'quota_days_requested': 1,
                            'remaining_days': target_quota.get('remaining_days', 0),
                            'leave_type_name': leave_name,
                            'half_leave_enabled': True,
                            'half_leave_mode': 'convert',
                            'half_leave_count': existing_half_count + 1,
                            'half_leaves_per_quota_day': conversion_ratio,
                            'half_leave_quota_type_id': target_id,
                            'half_leave_quota_type_name': target_type['name'],
                        }
                return {
                    'allowed': True,
                    'message': 'OK',
                    'requested_days': 1,
                    'quota_days_requested': int(charge_next),
                    'remaining_days': target_quota.get('remaining_days', 0),
                    'leave_type_name': leave_name,
                    'is_paid': is_paid,
                    'half_leave_enabled': True,
                    'half_leave_mode': 'convert',
                    'half_leave_count': existing_half_count + 1,
                    'half_leaves_per_quota_day': conversion_ratio,
                    'half_leave_quota_type_id': target_id,
                    'half_leave_quota_type_name': target_type['name'],
                }

            # Unpaid Leave (LWP) and Excess Paid Leave have no upper limit
            if is_paid == 0 or lt_dict.get('code') in ('unpaid', 'excess_paid'):
                return {
                    'allowed': True,
                    'is_paid': is_paid,
                    'requested_days': 1 if duration == 'half' else requested_days,
                    'quota_days_requested': 0 if duration == 'half' else requested_days,
                    'remaining_days': 9999.0,
                    'allocated_days': 9999.0,
                    'used_days': 0.0,
                    'leave_type_name': leave_name
                }

            # Check allocated quota
            allocated = float(lt_dict.get('yearly_allowance', 10.0))
            if user_id:
                cursor.execute(
                    "SELECT allocated_days FROM employee_leave_balances WHERE user_id = ? AND leave_type_id = ? AND year = ?",
                    (user_id, lt_dict['id'], reference_date.year)
                )
                bal_row = cursor.fetchone()
                if bal_row:
                    allocated = float(bal_row['allocated_days'])

            # Sum already approved days in this year/month
            already_used = 0.0
            if user_id:
                allowance_period = lt_dict.get('allowance_period', 'yearly')
                cycle_start, cycle_end = _leave_allowance_cycle_window(
                    reference_date,
                    allowance_period,
                    lt_dict.get('allowance_months')
                )
                cursor.execute("""
                                             SELECT id, start_date, end_date, quota_days, quota_through_date, leave_duration,
                                                 half_leave_quota_type_id, half_leave_quota_charge FROM leave_requests
                    WHERE user_id = ? AND status = 'approved' AND (leave_type = ? OR leave_type = ?)
                      AND end_date >= ? AND start_date <= ?
                """, (user_id, lt_dict['code'], lt_dict['name'], cycle_start, cycle_end))
                approved_requests = cursor.fetchall()

                for req in approved_requests:
                    if exclude_id and req['id'] == exclude_id:
                        continue
                    if req['leave_duration'] == 'half':
                        if req['half_leave_quota_charge'] and req['half_leave_quota_type_id'] == lt_dict['id']:
                            already_used += 1
                        continue
                    try:
                        clipped_start = max(req['start_date'], cycle_start)
                        clipped_end = min(req['end_date'], cycle_end)
                        if req['quota_days'] is None:
                            already_used += _count_scheduled_leave_days(cursor, user_id, clipped_start, clipped_end)
                        else:
                            quota_end = min(clipped_end, req['quota_through_date'] or req['end_date'])
                            if quota_end >= clipped_start:
                                already_used += _count_scheduled_leave_days(cursor, user_id, clipped_start, quota_end)
                    except Exception:
                        already_used += 1.0
                cursor.execute("""
                    SELECT COUNT(*) FROM leave_requests
                    WHERE user_id = ? AND status = 'approved' AND leave_duration = 'half'
                      AND half_leave_quota_charge = 1 AND half_leave_quota_type_id = ?
                      AND leave_type != ? AND leave_type != ?
                      AND end_date >= ? AND start_date <= ?
                """, (user_id, lt_dict['id'], lt_dict['code'], lt_dict['name'], cycle_start, cycle_end))
                already_used += int(cursor.fetchone()[0] or 0)
            quota_days_requested = requested_days

            remaining = max(0.0, allocated - already_used)

            if quota_days_requested > remaining:
                rem_str = int(remaining) if remaining.is_integer() else remaining
                req_str = int(quota_days_requested) if float(quota_days_requested).is_integer() else quota_days_requested
                msg = (f"This half-leave would use {req_str} {leave_name} quota day(s), but only {rem_str} remain."
                       if duration == 'half' else
                       f"{leave_name} only has {rem_str} allowed paid days for this employee. You cannot request {req_str} days. Please adjust the dates or file the excess days under Unpaid Leave (LWP).")
                return {
                    'allowed': False,
                    'reason_code': 'quota_exceeded',
                    'message': msg,
                    'requested_days': requested_days,
                    'quota_days_requested': quota_days_requested,
                    'remaining_days': remaining,
                    'allocated_days': allocated,
                    'used_days': already_used,
                    'leave_type_name': leave_name,
                    'is_paid': 1
                }

            return {
                'allowed': True,
                'message': 'OK',
                'requested_days': requested_days,
                'quota_days_requested': quota_days_requested,
                'remaining_days': remaining,
                'allocated_days': allocated,
                'used_days': already_used,
                'leave_type_name': leave_name,
                'is_paid': 1
            }
    except Exception as e:
        logger.error(f"check_leave_quota error: {e}")
        return {
            'allowed': False,
            'message': f"Error validating leave quota: {str(e)}",
            'requested_days': 0,
            'remaining_days': 10.0,
            'allocated_days': 10.0,
            'used_days': 0.0,
            'leave_type_name': leave_type_code,
            'is_paid': 1
        }



def add_overtime(user_id: int, user_name: str = '', ot_date: str = '', hours: float = 0.0, reason: str = '',
                 organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> Optional[int]:
    """Record an overtime log."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name, organization_id, branch_id FROM users WHERE id = ?", (user_id,))
            user_row = cursor.fetchone()
            if not user_row:
                return None
            user_organization = user_row[1] or 'DEFAULT_ORG'
            user_branch = user_row[2] or 'DEFAULT_BRANCH'
            if organization_id and organization_id != user_organization:
                logger.warning("Rejected overtime for user %s outside organization %s", user_id, organization_id)
                return None
            if branch_id and branch_id != user_branch:
                logger.warning("Rejected overtime for user %s outside branch %s", user_id, branch_id)
                return None
            if not user_name:
                user_name = user_row[0]
            organization_id = user_organization
            branch_id = user_branch
            organization_id = organization_id or 'DEFAULT_ORG'
            branch_id = branch_id or 'DEFAULT_BRANCH'
            cursor.execute(
                '''INSERT INTO overtime (user_id, user_name, ot_date, hours, reason, organization_id, branch_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (user_id, user_name, ot_date, hours, reason, organization_id, branch_id)
            )
            conn.commit()
            overtime_id = cursor.lastrowid
            _cloud_mirror("mirror_overtime", {
                "id": overtime_id,
                "user_id": user_id,
                "hours": hours,
                "ot_date": ot_date,
                "reason": reason,
                "organization_id": organization_id,
                "branch_id": branch_id,
            })
            return overtime_id
    except Exception as e:
        logger.error(f"Add overtime error: {e}")
        return None


def get_overtime_rate_preview(
    user_id: int,
    overtime_date: str,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    try:
        datetime.strptime(overtime_date, '%Y-%m-%d')
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT u.salary, sc.basic_salary, sc.ot_rate,
                       COALESCE(u.organization_id, 'DEFAULT_ORG') AS organization_id,
                       COALESCE(u.branch_id, 'DEFAULT_BRANCH') AS branch_id
                FROM users u LEFT JOIN salary_configs sc ON sc.user_id = u.id
                WHERE u.id = ?
                  AND (? IS NULL OR COALESCE(u.organization_id, 'DEFAULT_ORG') = ?)
                  AND (? IS NULL OR COALESCE(u.branch_id, 'DEFAULT_BRANCH') = ?)
            ''', (user_id, organization_id, organization_id, branch_id, branch_id))
            employee = cursor.fetchone()
            if not employee:
                return None
            configured_rate = float(employee['ot_rate'] or 0.0)
            salary = float(employee['basic_salary'] or employee['salary'] or 0.0)
            emp_org, emp_branch = str(employee['organization_id']), str(employee['branch_id'])
            ensure_payroll_settings_row(cursor, emp_org, emp_branch)
            settings = cursor.execute('''
                SELECT holiday_ot_premium_percent, non_working_day_ot_premium_percent,
                       holiday_ot_premium_active, non_working_day_ot_premium_active,
                       COALESCE(ot_hour_divisor, 208) AS ot_hour_divisor
                FROM payroll_settings
                WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                  AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
            ''', (emp_org, emp_branch)).fetchone()
            divisor = float(settings['ot_hour_divisor'] if settings else DEFAULT_OT_DIVISOR) or DEFAULT_OT_DIVISOR
            base_rate = round(configured_rate if configured_rate > 0 else (salary / divisor if salary > 0 else 0.0), 2)
            day_type = _get_overtime_day_type(cursor, user_id, overtime_date)
            holiday = _get_company_holiday_for_date(
                cursor, overtime_date, employee['organization_id'], employee['branch_id']
            ) if day_type == 'holiday' else None
            premium_percent = _get_overtime_premium_percent(cursor, overtime_date, day_type, settings, holiday)
            return {
                'user_id': user_id,
                'ot_date': overtime_date,
                'day_type': day_type,
                'holiday_id': holiday.get('id') if holiday else None,
                'holiday_name': holiday.get('name') if holiday else None,
                'base_rate': base_rate,
                'premium_percent': premium_percent,
                'effective_rate': round(base_rate * (1 + premium_percent / 100), 2),
                'premium_active': premium_percent > 0,
            }
    except (TypeError, ValueError):
        return None
    except Exception as e:
        logger.error(f"Failed to preview overtime rate for user {user_id} on {overtime_date}: {e}")
        return None


def get_overtime(user_id: Optional[int] = None, status: Optional[str] = None,
                 department: Optional[str] = None, ot_date: Optional[str] = None,
                 start_date: Optional[str] = None, end_date: Optional[str] = None,
                 organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict]:
    """Get overtime records optionally filtered by user_id, status, department, and date range."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = """
                SELECT o.*,
                       COALESCE(u.department, '') as department,
                       COALESCE(u.position, '') as designation,
                       COALESCE(sc.ot_rate, CASE WHEN COALESCE(u.salary, sc.basic_salary, 0) > 0 THEN ROUND(COALESCE(u.salary, sc.basic_salary) / COALESCE(ps.ot_hour_divisor, 208.0), 2) ELSE 0.0 END) as ot_rate
                FROM overtime o
                LEFT JOIN users u ON o.user_id = u.id
                LEFT JOIN salary_configs sc ON o.user_id = sc.user_id
                LEFT JOIN payroll_settings ps
                  ON COALESCE(ps.organization_id, 'DEFAULT_ORG') = COALESCE(u.organization_id, o.organization_id, 'DEFAULT_ORG')
                 AND COALESCE(ps.branch_id, 'DEFAULT_BRANCH') = COALESCE(u.branch_id, o.branch_id, 'DEFAULT_BRANCH')
                WHERE 1=1
            """
            params = []
            if user_id:
                query += " AND o.user_id = ?"
                params.append(user_id)
            if status and status.strip() and status.strip().lower() != 'all':
                query += " AND LOWER(o.status) = LOWER(?)"
                params.append(status.strip())
            if department and department.strip() and department.strip().lower() != 'all':
                query += " AND LOWER(TRIM(u.department)) = LOWER(TRIM(?))"
                params.append(department.strip())
            if ot_date:
                query += " AND o.ot_date = ?"
                params.append(ot_date)
            if start_date:
                query += " AND o.ot_date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND o.ot_date <= ?"
                params.append(end_date)
            if organization_id and organization_id != 'all':
                query += """ AND COALESCE(o.organization_id, u.organization_id, 'DEFAULT_ORG') = ?
                             AND COALESCE(u.organization_id, 'DEFAULT_ORG') = ?"""
                params.extend([organization_id, organization_id])
            if branch_id and branch_id != 'all':
                query += """ AND COALESCE(o.branch_id, u.branch_id, 'DEFAULT_BRANCH') = ?
                             AND COALESCE(u.branch_id, 'DEFAULT_BRANCH') = ?"""
                params.extend([branch_id, branch_id])
            query += " ORDER BY o.ot_date DESC, o.created_at DESC"
            cursor.execute(query, params)
            overtime_rows = cursor.fetchall()
            records = []
            settings_cache: Dict[Tuple[str, str], Any] = {}
            for r in overtime_rows:
                d = dict(r)
                hours = float(d.get('hours') or 0.0)
                base_rate = float(d.get('ot_rate') or 0.0)
                emp_org = str(d.get('organization_id') or 'DEFAULT_ORG')
                emp_branch = str(d.get('branch_id') or 'DEFAULT_BRANCH')
                cache_key = (emp_org, emp_branch)
                if cache_key not in settings_cache:
                    ensure_payroll_settings_row(cursor, emp_org, emp_branch)
                    settings_cache[cache_key] = cursor.execute(
                        """SELECT holiday_ot_premium_percent, non_working_day_ot_premium_percent,
                                  holiday_ot_premium_active, non_working_day_ot_premium_active
                           FROM payroll_settings
                           WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                             AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                        cache_key,
                    ).fetchone()
                settings_row = settings_cache[cache_key]
                day_type = _get_overtime_day_type(cursor, int(d['user_id']), str(d['ot_date']))
                holiday = _get_company_holiday_for_date(
                    cursor,
                    str(d['ot_date']),
                    emp_org,
                    emp_branch,
                ) if day_type == 'holiday' else None
                premium = _get_overtime_premium_percent(cursor, str(d['ot_date']), day_type, settings_row, holiday)
                effective_rate = round(base_rate * (1 + premium / 100), 2)
                d['base_ot_rate'] = base_rate
                d['ot_day_type'] = day_type
                d['holiday_id'] = holiday.get('id') if holiday else None
                d['holiday_name'] = holiday.get('name') if holiday else None
                d['ot_premium_percent'] = premium
                d['ot_rate'] = effective_rate
                d['ot_amount'] = round(hours * effective_rate, 2)
                records.append(d)
            return records
    except Exception as e:
        logger.error(f"Get overtime error: {e}")
        return []


def get_overtime_by_id(
    ot_id: int,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Optional[Dict]:
    """Retrieve an overtime record by ID, optionally constrained to its owning scope."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = """SELECT o.* FROM overtime o
                       JOIN users u ON u.id = o.user_id
                       WHERE o.id = ?"""
            params: List[Any] = [ot_id]
            if organization_id is not None:
                query += """ AND COALESCE(o.organization_id, u.organization_id, 'DEFAULT_ORG') = ?
                             AND COALESCE(u.organization_id, 'DEFAULT_ORG') = ?"""
                params.extend([organization_id, organization_id])
            if branch_id is not None:
                query += """ AND COALESCE(o.branch_id, u.branch_id, 'DEFAULT_BRANCH') = ?
                             AND COALESCE(u.branch_id, 'DEFAULT_BRANCH') = ?"""
                params.extend([branch_id, branch_id])
            cursor.execute(query, params)
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error fetching overtime record {ot_id}: {e}")
        return None


def get_user_attendance_status(user_id: int, target_date: str) -> Optional[str]:
    """Get an employee's resolved attendance status for one date."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM manual_absent WHERE user_id = ? AND date = ? LIMIT 1",
                (user_id, target_date)
            )
            if cursor.fetchone():
                return 'absent'

        report = get_detailed_attendance_report(target_date=target_date, user_id=user_id)
        record = next(
            (row for row in report.get('records', []) if int(row.get('user_id', 0)) == int(user_id)),
            None
        )
        return record.get('status') if record else None
    except Exception as e:
        logger.error(f"Error resolving attendance status for user {user_id} on {target_date}: {e}")
        return None


def update_overtime_status(
    ot_id: int,
    status: str = 'approved',
    approved_by: str = 'Admin',
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    """Finalize a pending overtime record without allowing later status changes."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT ot_date, COALESCE(organization_id,'DEFAULT_ORG'), COALESCE(branch_id,'DEFAULT_BRANCH') FROM overtime WHERE id = ?",
                (ot_id,),
            ).fetchone()
            if row:
                raise_if_payroll_month_locked(str(row[0])[:10], row[1], row[2])
            query = """UPDATE overtime SET status = ?, approved_by = ?
                       WHERE id = ? AND LOWER(COALESCE(status, 'pending')) = 'pending'"""
            params: List[Any] = [status, approved_by, ot_id]
            if organization_id is not None:
                query += """ AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                             AND EXISTS (SELECT 1 FROM users u WHERE u.id = overtime.user_id
                                         AND COALESCE(u.organization_id, 'DEFAULT_ORG') = ?)"""
                params.extend([organization_id, organization_id])
            if branch_id is not None:
                query += """ AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                             AND EXISTS (SELECT 1 FROM users u WHERE u.id = overtime.user_id
                                         AND COALESCE(u.branch_id, 'DEFAULT_BRANCH') = ?)"""
                params.extend([branch_id, branch_id])
            cursor.execute(query, params)
            conn.commit()
            return cursor.rowcount > 0
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Update overtime status error: {e}")
        return False


def delete_overtime(
    ot_id: int,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    """Delete an overtime record by ID only within its owning scope."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            query = "DELETE FROM overtime WHERE id = ?"
            params: List[Any] = [ot_id]
            if organization_id is not None:
                query += """ AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                             AND EXISTS (SELECT 1 FROM users u WHERE u.id = overtime.user_id
                                         AND COALESCE(u.organization_id, 'DEFAULT_ORG') = ?)"""
                params.extend([organization_id, organization_id])
            if branch_id is not None:
                query += """ AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                             AND EXISTS (SELECT 1 FROM users u WHERE u.id = overtime.user_id
                                         AND COALESCE(u.branch_id, 'DEFAULT_BRANCH') = ?)"""
                params.extend([branch_id, branch_id])
            cursor.execute(query, params)
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Delete overtime error: {e}")
        return False


def get_all_salary_configs(active_only: bool = True, department: Optional[str] = None, search: Optional[str] = None, organization_id: Optional[str] = None) -> List[Dict]:
    """Get salary configuration entries for all employees (joined with user details)."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            conditions = []
            params = []
            if organization_id:
                conditions.append("u.organization_id = ?")
                params.append(organization_id)
            if active_only:
                conditions.append("u.active = 1")
            if department and department.strip() and department.strip().lower() != 'all':
                conditions.append("u.department = ?")
                params.append(department.strip())
            if search and search.strip():
                s = f"%{search.strip()}%"
                conditions.append("(u.name LIKE ? OR u.email LIKE ? OR u.department LIKE ? OR u.position LIKE ? OR CAST(u.id AS TEXT) LIKE ?)")
                params.extend([s, s, s, s, s])

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            query = f"""
                SELECT 
                    u.id as user_id, 
                    u.name as user_name, 
                    u.email,
                    COALESCE(u.department, '') as department, 
                    COALESCE(u.position, 'Staff Member') as designation,
                    u.active,
                    COALESCE(s.basic_salary, u.salary, 0.0) as basic_salary,
                    COALESCE(s.allowances, 0.0) as allowances,
                    COALESCE(s.deductions, 0.0) as deductions,
                    COALESCE(s.ot_rate, 0.0) as ot_rate,
                    ROUND(COALESCE(s.basic_salary, u.salary, 0.0) + COALESCE(s.allowances, 0.0) - COALESCE(s.deductions, 0.0), 2) as net_estimated,
                    CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END as is_configured,
                    s.updated_at
                FROM users u
                LEFT JOIN salary_configs s ON u.id = s.user_id
                {where_clause}
                ORDER BY u.department COLLATE NOCASE, u.name COLLATE NOCASE
            """
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get all salary configs error: {e}")
        return []


def get_salary_config(user_id: int) -> Optional[Dict]:
    """Get salary configuration for a single user with user profile details."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    u.id as user_id, 
                    u.name as user_name, 
                    u.email,
                    COALESCE(u.department, '') as department, 
                    COALESCE(u.position, 'Staff Member') as designation,
                    u.active,
                    COALESCE(s.basic_salary, u.salary, 0.0) as basic_salary,
                    COALESCE(s.allowances, 0.0) as allowances,
                    COALESCE(s.deductions, 0.0) as deductions,
                    COALESCE(s.ot_rate, 0.0) as ot_rate,
                    ROUND(COALESCE(s.basic_salary, u.salary, 0.0) + COALESCE(s.allowances, 0.0) - COALESCE(s.deductions, 0.0), 2) as net_estimated,
                    CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END as is_configured,
                    s.updated_at
                FROM users u
                LEFT JOIN salary_configs s ON u.id = s.user_id
                WHERE u.id = ?
            """, (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Get salary config error for user {user_id}: {e}")
        return None


def set_salary_config(user_id: int, basic_salary: float = 0.0, allowances: float = 0.0, deductions: float = 0.0, ot_rate: float = 0.0, effective_from: Optional[str] = None) -> bool:
    """Insert or update salary configuration for a user and synchronize users.salary."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, COALESCE(organization_id,'DEFAULT_ORG'), COALESCE(branch_id,'DEFAULT_BRANCH') FROM users WHERE id = ?",
                (user_id,),
            )
            owner = cursor.fetchone()
        if not owner:
            logger.error(f"Cannot set salary config: user {user_id} not found")
            return False
        raise_if_payroll_month_locked(str(effective_from or date.today().isoformat())[:10], owner[1], owner[2])
        settings = get_payroll_settings(organization_id=owner[1], branch_id=owner[2])
        b_sal = max(0.0, float(basic_salary or 0.0))
        allow = max(0.0, float(allowances or 0.0))
        deduct = max(0.0, float(deductions or 0.0))
        ot = max(0.0, float(ot_rate or 0.0))
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT INTO salary_configs (user_id, basic_salary, allowances, deductions, ot_rate, updated_at)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(user_id) DO UPDATE SET
                   basic_salary=excluded.basic_salary,
                   allowances=excluded.allowances,
                   deductions=excluded.deductions,
                   ot_rate=excluded.ot_rate,
                   updated_at=CURRENT_TIMESTAMP''',
                (user_id, b_sal, allow, deduct, ot)
            )
            cursor.execute("UPDATE users SET salary = ? WHERE id = ?", (b_sal, user_id))
            if _payroll_flag_on(settings, 'enable_salary_revisions'):
                start = str(effective_from or date.today().isoformat())[:10]
                cursor.execute(
                    """INSERT INTO salary_revisions (
                           organization_id, branch_id, user_id, effective_from,
                           basic_salary, allowances, deductions, ot_rate
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (owner[1], owner[2], user_id, start, b_sal, allow, deduct, ot),
                )
                _write_payroll_audit(cursor, 'salary_revised', owner[1], owner[2], start[:7], 'Admin', {
                    'user_id': user_id, 'basic_salary': b_sal, 'effective_from': start,
                })
            conn.commit()
            return True
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Set salary config error for user {user_id}: {e}")
        return False


def reset_salary_config(user_id: int) -> bool:
    """Reset salary configuration to defaults for a user."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM salary_configs WHERE user_id = ?", (user_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Reset salary config error for user {user_id}: {e}")
        return False


def get_salary_summary(organization_id: Optional[str] = None) -> Dict[str, Any]:
    """Return summary KPI metrics across active employee salary structures."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            where_sql = "WHERE u.active = 1"
            params = []
            if organization_id:
                where_sql += " AND u.organization_id = ?"
                params.append(organization_id)
            cursor.execute(f"""
                SELECT 
                    COUNT(u.id) as total_employees,
                    SUM(CASE WHEN s.user_id IS NOT NULL THEN 1 ELSE 0 END) as configured_count,
                    SUM(COALESCE(s.basic_salary, u.salary, 0.0)) as total_basic,
                    SUM(COALESCE(s.allowances, 0.0)) as total_allowances,
                    SUM(COALESCE(s.deductions, 0.0)) as total_deductions,
                    SUM(COALESCE(s.basic_salary, u.salary, 0.0) + COALESCE(s.allowances, 0.0) - COALESCE(s.deductions, 0.0)) as total_net_payroll,
                    AVG(COALESCE(s.basic_salary, u.salary, 0.0)) as avg_basic
                FROM users u
                LEFT JOIN salary_configs s ON u.id = s.user_id
                {where_sql}
            """, params)
            row = cursor.fetchone()
            if row:
                return {
                    'total_employees': row['total_employees'] or 0,
                    'configured_count': row['configured_count'] or 0,
                    'unconfigured_count': (row['total_employees'] or 0) - (row['configured_count'] or 0),
                    'total_basic': round(row['total_basic'] or 0.0, 2),
                    'total_allowances': round(row['total_allowances'] or 0.0, 2),
                    'total_deductions': round(row['total_deductions'] or 0.0, 2),
                    'total_net_payroll': round(row['total_net_payroll'] or 0.0, 2),
                    'avg_basic': round(row['avg_basic'] or 0.0, 2)
                }
            return {
                'total_employees': 0, 'configured_count': 0, 'unconfigured_count': 0,
                'total_basic': 0.0, 'total_allowances': 0.0, 'total_deductions': 0.0,
                'total_net_payroll': 0.0, 'avg_basic': 0.0
            }
    except Exception as e:
        logger.error(f"Get salary summary error: {e}")
        return {
            'total_employees': 0, 'configured_count': 0, 'unconfigured_count': 0,
            'total_basic': 0.0, 'total_allowances': 0.0, 'total_deductions': 0.0,
            'total_net_payroll': 0.0, 'avg_basic': 0.0
        }


def auto_calculate_ot_rates(
    rate_divisor: float = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> int:
    """Auto-calculate hourly overtime rate for active employees of one organization."""
    org, branch = _tenant_ids(organization_id, branch_id)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            ensure_payroll_settings_row(cursor, org, branch)
            if rate_divisor is None:
                row = cursor.execute(
                    """SELECT COALESCE(ot_hour_divisor, 208) FROM payroll_settings
                       WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                         AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                    (org, branch),
                ).fetchone()
                rate_divisor = float(row[0] if row else DEFAULT_OT_DIVISOR) or DEFAULT_OT_DIVISOR
            cursor.execute("""
                SELECT u.id, COALESCE(s.basic_salary, u.salary, 0.0) as basic_salary,
                       COALESCE(s.allowances, 0.0) as allowances,
                       COALESCE(s.deductions, 0.0) as deductions
                FROM users u
                LEFT JOIN salary_configs s ON u.id = s.user_id
                WHERE u.active = 1
                  AND COALESCE(u.organization_id, 'DEFAULT_ORG') = ?
                  AND COALESCE(u.branch_id, 'DEFAULT_BRANCH') = ?
            """, (org, branch))
            employees = cursor.fetchall()
            updated_count = 0
            for emp in employees:
                b_sal = float(emp['basic_salary'] or 0.0)
                ot_rate = round(b_sal / rate_divisor, 2) if (b_sal > 0 and rate_divisor > 0) else 0.0
                cursor.execute("""
                    INSERT INTO salary_configs (user_id, basic_salary, allowances, deductions, ot_rate, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                    ot_rate = excluded.ot_rate,
                    updated_at = CURRENT_TIMESTAMP
                """, (emp['id'], b_sal, float(emp['allowances'] or 0.0), float(emp['deductions'] or 0.0), ot_rate))
                updated_count += 1
            conn.commit()
            return updated_count
    except Exception as e:
        logger.error(f"Auto calculate OT rates error: {e}")
        return 0


def get_payroll_rules(
    active_only: bool = False,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return absence-based deduction rules for one organization/branch."""
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        where = """WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?"""
        params: List[Any] = [org, branch]
        if active_only:
            where += " AND active = 1"
        cursor.execute(
            f"SELECT * FROM payroll_rules {where} ORDER BY min_absent_days, max_absent_days, id",
            params,
        )
        return [dict(row) for row in cursor.fetchall()]


def get_payroll_settings(
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return payroll options owned by one organization/branch."""
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        ensure_payroll_settings_row(cursor, org, branch)
        conn.commit()
        row = cursor.execute(
            """SELECT * FROM payroll_settings
               WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
            (org, branch),
        ).fetchone()
        defaults = {
            'organization_id': org,
            'branch_id': branch,
            **DEFAULT_PAYROLL_SETTINGS,
        }
        settings = dict(row) if row else defaults
        try:
            absence_count = cursor.execute(
                """SELECT COUNT(*) FROM payroll_rules
                   WHERE active = 1
                     AND COALESCE(rule_basis, 'absence') = 'absence'
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                (org, branch),
            ).fetchone()[0]
        except sqlite3.Error:
            absence_count = 0
        settings['absence_rules_configured'] = bool(absence_count)
        settings['notices'] = []
        mode = _normalize_attendance_pay_mode(settings.get('attendance_pay_mode'))
        if mode == 'full_salary' and not absence_count:
            settings['notices'].append('No absence rules configured')
        if mode == 'full_salary':
            settings['legacy_full_salary'] = True
            settings['notices'].append(
                "This organization still uses full monthly salary. New organizations default to days present."
            )
        else:
            settings['legacy_full_salary'] = False
        return settings


def get_payroll_scope_options(
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return departments and employees for scoped payroll rules in the current tenant."""
    org, branch = _tenant_ids(organization_id, branch_id)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            departments = [dict(row) for row in conn.execute(
                """SELECT id, name FROM departments
                   WHERE active = 1
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                   ORDER BY name COLLATE NOCASE""",
                (org, branch),
            ).fetchall()]
            employees = [dict(row) for row in conn.execute(
                """SELECT id, name, COALESCE(department, '') AS department FROM users
                   WHERE active = 1
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                   ORDER BY name COLLATE NOCASE""",
                (org, branch),
            ).fetchall()]
            return {'departments': departments, 'employees': employees}
    except Exception as e:
        logger.error(f"Failed to load payroll rule scope options: {e}")
        return {'departments': [], 'employees': []}


def get_payroll_scoped_rules(
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return scoped payroll rules with readable department/employee names."""
    org, branch = _tenant_ids(organization_id, branch_id)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT r.*,
                       d.name AS department_name,
                       u.name AS employee_name
                FROM payroll_scoped_rules r
                LEFT JOIN departments d ON r.scope_type = 'department' AND d.id = r.scope_id
                LEFT JOIN users u ON r.scope_type = 'employee' AND u.id = r.scope_id
                WHERE r.active = 1
                  AND COALESCE(r.organization_id, 'DEFAULT_ORG') = ?
                  AND COALESCE(r.branch_id, 'DEFAULT_BRANCH') = ?
                ORDER BY r.policy_type, r.scope_type, department_name COLLATE NOCASE, employee_name COLLATE NOCASE, r.day_type
            ''', (org, branch)).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Failed to load scoped payroll rules: {e}")
        return []


def save_scoped_half_leave_rule(
    scope_type: str,
    scope_id: int,
    quota_mode: str,
    monthly_count: int,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    scope = str(scope_type or '').strip().lower()
    mode = str(quota_mode or '').strip().lower()
    if scope not in ('department', 'employee') or mode not in ('category', 'monthly_cap', 'monthly_convert'):
        return False
    try:
        target_id = int(scope_id)
        count = int(monthly_count)
    except (TypeError, ValueError, OverflowError):
        return False
    if target_id <= 0 or count < 1:
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            org, branch = _tenant_ids(organization_id, branch_id)
            conn.execute('''
                INSERT INTO payroll_scoped_rules (
                    policy_type, scope_type, scope_id, day_type, half_leave_quota_mode,
                    half_leaves_per_quota_day, active, updated_at, organization_id, branch_id
                ) VALUES ('half_leave_quota', ?, ?, '', ?, ?, 1, CURRENT_TIMESTAMP, ?, ?)
                ON CONFLICT(policy_type, scope_type, scope_id, day_type, organization_id, branch_id) DO UPDATE SET
                    half_leave_quota_mode = excluded.half_leave_quota_mode,
                    half_leaves_per_quota_day = excluded.half_leaves_per_quota_day,
                    active = 1,
                    updated_at = CURRENT_TIMESTAMP
            ''', (scope, target_id, mode, count, org, branch))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to save scoped half-leave rule: {e}")
        return False

def save_scoped_overtime_limit_rule(
    scope_type: str,
    scope_id: int,
    max_hours: float,
    period: str,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    scope = str(scope_type or '').strip().lower()
    p = str(period or '').strip().lower()
    if scope not in ('department', 'employee') or p not in ('day', 'week', 'month', 'per_entry'):
        return False
    try:
        target_id = int(scope_id)
        max_h = float(max_hours)
    except (TypeError, ValueError, OverflowError):
        return False
    if target_id <= 0 or max_h <= 0:
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            org, branch = _tenant_ids(organization_id, branch_id)
            conn.execute('''
                INSERT INTO payroll_scoped_rules (
                    policy_type, scope_type, scope_id, day_type, overtime_max_hours,
                    overtime_max_period, active, updated_at, organization_id, branch_id
                ) VALUES ('overtime_limit', ?, ?, '', ?, ?, 1, CURRENT_TIMESTAMP, ?, ?)
                ON CONFLICT(policy_type, scope_type, scope_id, day_type, organization_id, branch_id) DO UPDATE SET
                    overtime_max_hours = excluded.overtime_max_hours,
                    overtime_max_period = excluded.overtime_max_period,
                    active = 1,
                    updated_at = CURRENT_TIMESTAMP
            ''', (scope, target_id, max_h, p, org, branch))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Failed to save scoped overtime limit rule: {e}")
        return False


def save_scoped_overtime_premium_rule(
    scope_type: str,
    scope_id: int,
    day_type: str,
    premium_percent: float,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    scope = str(scope_type or '').strip().lower()
    kind = str(day_type or '').strip().lower()
    if scope not in ('department', 'employee') or kind not in ('holiday', 'non_working_day'):
        return False
    try:
        target_id = int(scope_id)
        premium = float(premium_percent)
    except (TypeError, ValueError, OverflowError):
        return False
    if target_id <= 0 or not 0 <= premium <= 1000:
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            org, branch = _tenant_ids(organization_id, branch_id)
            conn.execute('''
                INSERT INTO payroll_scoped_rules (
                    policy_type, scope_type, scope_id, day_type,
                    overtime_premium_percent, active, updated_at, organization_id, branch_id
                ) VALUES ('overtime_premium', ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, ?, ?)
                ON CONFLICT(policy_type, scope_type, scope_id, day_type, organization_id, branch_id) DO UPDATE SET
                    overtime_premium_percent = excluded.overtime_premium_percent,
                    active = 1,
                    updated_at = CURRENT_TIMESTAMP
            ''', (scope, target_id, kind, premium, org, branch))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to save scoped overtime premium rule: {e}")
        return False


def delete_payroll_scoped_rule(
    rule_id: int,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    try:
        org, branch = _tenant_ids(organization_id, branch_id)
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                """DELETE FROM payroll_scoped_rules
                   WHERE id = ?
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                (int(rule_id), org, branch),
            )
            conn.commit()
            return cursor.rowcount > 0
    except (TypeError, ValueError, OverflowError):
        return False
    except Exception as e:
        logger.error(f"Failed to delete scoped payroll rule: {e}")
        return False


def _get_scoped_payroll_rule(
    cursor: sqlite3.Cursor,
    policy_type: str,
    user_id: Optional[int],
    day_type: str = '',
) -> Optional[Dict[str, Any]]:
    if not user_id:
        return None
    cursor.execute("SELECT department FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    department_name = ''
    if user:
        dept_val = user['department'] if hasattr(user, 'keys') else user[0]
        department_name = str(dept_val or '').strip()
    department_id = None
    if department_name:
        org_id, branch_id = _user_tenant(cursor, user_id)
        cursor.execute(
            """SELECT id FROM departments
               WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
                 AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
               LIMIT 1""",
            (department_name, org_id),
        )
        department = cursor.fetchone()
        if department:
            department_id = int(department['id'] if hasattr(department, 'keys') else department[0])
    conditions = ["(scope_type = 'employee' AND scope_id = ?)"]
    rule_day_type = day_type if policy_type == 'overtime_premium' else ''
    org_id, branch_id = _user_tenant(cursor, user_id)
    params: List[Any] = [policy_type, rule_day_type, org_id, branch_id, int(user_id)]
    if department_id is not None:
        conditions.append("(scope_type = 'department' AND scope_id = ?)")
        params.append(department_id)
    query = f'''
        SELECT * FROM payroll_scoped_rules
        WHERE policy_type = ? AND day_type = ? AND active = 1
          AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
          AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
          AND ({' OR '.join(conditions)})
        ORDER BY CASE scope_type WHEN 'employee' THEN 0 ELSE 1 END
        LIMIT 1
    '''
    cursor.execute(query, params)
    row = cursor.fetchone()
    if not row:
        return None
    if hasattr(row, 'keys'):
        return dict(row)
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))


def update_overtime_limit_settings(
    max_hours: float,
    period: str,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    """Save the organization-wide overtime review limit."""
    try:
        hours = float(max_hours)
    except (TypeError, ValueError, OverflowError):
        return False
    scope = str(period or '').strip().lower()
    if not 0 <= hours <= 744 or scope not in ('per_entry', 'day', 'week', 'month'):
        return False
    org, branch = _tenant_ids(organization_id, branch_id)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            ensure_payroll_settings_row(cursor, org, branch)
            cursor.execute(
                """UPDATE payroll_settings
                   SET overtime_max_hours = ?, overtime_max_period = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                (hours, scope, org, branch),
            )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update overtime limit settings: {e}")
        return False


def _get_overtime_limit_status_for_cursor(
    cursor: sqlite3.Cursor,
    user_id: int,
    ot_date: str,
    requested_hours: float,
    exclude_id: Optional[int] = None,
) -> Dict[str, Any]:
    rule = _get_scoped_payroll_rule(cursor, 'overtime_limit', user_id)
    if rule and rule.get('overtime_max_hours') is not None and rule.get('overtime_max_period') is not None:
        maximum = float(rule['overtime_max_hours'])
        period = str(rule['overtime_max_period']).lower()
    else:
        org_id, branch_id = _user_tenant(cursor, user_id)
        ensure_payroll_settings_row(cursor, org_id, branch_id)
        settings = cursor.execute(
            """SELECT overtime_max_hours, overtime_max_period FROM payroll_settings
               WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
            (org_id, branch_id),
        ).fetchone()
        maximum = float(settings[0] or 0) if settings else 0.0
        period = str(settings[1] or 'day').lower() if settings else 'day'
    if maximum <= 0:
        return {'enabled': False, 'exceeded': False, 'maximum_hours': 0.0, 'period': period, 'current_hours': 0.0}

    if period == 'per_entry':
        current_hours = 0.0
    else:
        overtime_day = datetime.strptime(ot_date, '%Y-%m-%d').date()
        if period == 'week':
            period_start = overtime_day - timedelta(days=overtime_day.weekday())
            period_end = period_start + timedelta(days=6)
        elif period == 'month':
            period_start = overtime_day.replace(day=1)
            if overtime_day.month == 12:
                period_end = overtime_day.replace(year=overtime_day.year + 1, month=1, day=1) - timedelta(days=1)
            else:
                period_end = overtime_day.replace(month=overtime_day.month + 1, day=1) - timedelta(days=1)
        else:
            period_start = overtime_day
            period_end = overtime_day
        query = '''
            SELECT COALESCE(SUM(hours), 0)
            FROM overtime
            WHERE user_id = ? AND ot_date >= ? AND ot_date <= ?
              AND LOWER(COALESCE(status, 'pending')) != 'rejected'
        '''
        params: List[Any] = [user_id, period_start.isoformat(), period_end.isoformat()]
        if exclude_id is not None:
            query += ' AND id != ?'
            params.append(exclude_id)
        current_hours = float(cursor.execute(query, params).fetchone()[0] or 0)

    total_hours = current_hours + float(requested_hours)
    period_label = {
        'per_entry': 'per OT entry',
        'day': 'per day',
        'week': 'per week',
        'month': 'per calendar month',
    }.get(period, f'per {period}')
    review_note = (
        f" [OT limit exceeded: {total_hours:.2f}/{maximum:g} hours {period_label}; admin review required]"
        if total_hours > maximum + 1e-9 else ''
    )
    return {
        'enabled': True,
        'exceeded': total_hours > maximum + 1e-9,
        'maximum_hours': maximum,
        'period': period,
        'period_label': period_label,
        'current_hours': current_hours,
        'requested_total_hours': total_hours,
        'review_note': review_note,
    }


def get_overtime_limit_status(user_id: int, ot_date: str, requested_hours: float) -> Dict[str, Any]:
    """Check whether adding OT exceeds the configured period limit for an employee."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        return _get_overtime_limit_status_for_cursor(cursor, user_id, ot_date, requested_hours)


def _normalize_ot_pay_mode(value: Any, fallback: str = 'percent') -> Optional[str]:
    mode = str(value if value is not None else fallback or 'percent').strip().lower()
    return mode if mode in VALID_OT_PAY_MODES else None


def _normalize_off_day_pay_mode(value: Any, fallback: str = 'multiplier') -> Optional[str]:
    mode = str(value if value is not None else fallback or 'multiplier').strip().lower()
    aliases = {'fixed': 'fixed_day', 'fixed_amount': 'fixed_day', 'factor': 'multiplier'}
    mode = aliases.get(mode, mode)
    return mode if mode in VALID_OFF_DAY_ATTENDANCE_MODES else None


def update_payroll_settings(
    deduct_unpaid_leave: bool,
    half_leave_quota_mode: str = 'category',
    half_leaves_per_quota_day: int = 2,
    holiday_ot_premium_percent: float = 0.0,
    non_working_day_ot_premium_percent: float = 0.0,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    ot_hour_divisor: Optional[float] = None,
    proration_basis: Optional[str] = None,
    attendance_pay_mode: Optional[str] = None,
    currency_code: Optional[str] = None,
    decimal_places: Optional[int] = None,
    holiday_ot_pay_mode: Optional[str] = None,
    non_working_day_ot_pay_mode: Optional[str] = None,
    holiday_ot_multiplier: Optional[float] = None,
    non_working_day_ot_multiplier: Optional[float] = None,
    holiday_ot_fixed_amount: Optional[float] = None,
    non_working_day_ot_fixed_amount: Optional[float] = None,
    off_day_attendance_pay_active: Optional[bool] = None,
    off_day_attendance_pay_mode: Optional[str] = None,
    off_day_attendance_pay_multiplier: Optional[float] = None,
    off_day_attendance_pay_fixed_amount: Optional[float] = None,
    feature_flags: Optional[Dict[str, Any]] = None,
) -> bool:
    """Save organization-owned payroll and half-leave quota policies."""
    mode = str(half_leave_quota_mode or 'category').strip().lower()
    mode = {'separate': 'category', 'convert': 'monthly_convert'}.get(mode, mode)
    if mode not in ('category', 'monthly_cap', 'monthly_convert'):
        raise ValueError('Choose a valid half-leave quota mode.')
    try:
        conversion_count = int(half_leaves_per_quota_day)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Half-leave conversion count must be a positive integer.')
    if conversion_count < 1:
        raise ValueError('Half-leave conversion count must be a positive integer.')
    try:
        holiday_premium = float(holiday_ot_premium_percent)
        non_working_premium = float(non_working_day_ot_premium_percent)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Overtime premiums must be numbers between 0 and 1000.')
    if not (0 <= holiday_premium <= 1000 and 0 <= non_working_premium <= 1000):
        raise ValueError('Overtime premiums must be between 0 and 1000 percent.')
    org, branch = _tenant_ids(organization_id, branch_id)
    current = get_payroll_settings(organization_id=org, branch_id=branch)
    try:
        divisor = float(ot_hour_divisor if ot_hour_divisor is not None else current.get('ot_hour_divisor', DEFAULT_OT_DIVISOR))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('OT hour divisor must be a positive number.')
    if divisor <= 0 or divisor != divisor:
        raise ValueError('OT hour divisor must be a positive number.')
    basis = str(proration_basis if proration_basis is not None else current.get('proration_basis', DEFAULT_PRORATION_BASIS)).strip().lower()
    if basis not in VALID_PRORATION_BASES:
        raise ValueError('Choose calendar days, scheduled working days, or a 30-day month.')
    pay_mode = _normalize_attendance_pay_mode(
        attendance_pay_mode if attendance_pay_mode is not None else current.get('attendance_pay_mode')
    )
    currency = str(currency_code if currency_code is not None else current.get('currency_code', DEFAULT_CURRENCY)).strip().upper() or DEFAULT_CURRENCY
    if len(currency) > 8:
        raise ValueError('Currency code must be 8 characters or fewer.')
    try:
        places = int(decimal_places if decimal_places is not None else current.get('decimal_places', DEFAULT_DECIMAL_PLACES))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Decimal places must be a whole number from 0 to 6.')
    if not 0 <= places <= 6:
        raise ValueError('Decimal places must be a whole number from 0 to 6.')
    h_mode = _normalize_ot_pay_mode(
        holiday_ot_pay_mode if holiday_ot_pay_mode is not None else current.get('holiday_ot_pay_mode'),
        'percent',
    )
    nw_mode = _normalize_ot_pay_mode(
        non_working_day_ot_pay_mode if non_working_day_ot_pay_mode is not None else current.get('non_working_day_ot_pay_mode'),
        'percent',
    )
    off_mode = _normalize_off_day_pay_mode(
        off_day_attendance_pay_mode if off_day_attendance_pay_mode is not None else current.get('off_day_attendance_pay_mode'),
        'multiplier',
    )
    if not h_mode or not nw_mode:
        raise ValueError('Choose percent, multiplier, or a fixed hourly amount for overtime extra pay.')
    if not off_mode:
        raise ValueError('Rest-day extra must be a daily-rate multiplier or a fixed amount per rest day.')
    try:
        h_mult = float(holiday_ot_multiplier if holiday_ot_multiplier is not None else current.get('holiday_ot_multiplier') or 1.5)
        nw_mult = float(non_working_day_ot_multiplier if non_working_day_ot_multiplier is not None else current.get('non_working_day_ot_multiplier') or 1.5)
        h_fixed = float(holiday_ot_fixed_amount if holiday_ot_fixed_amount is not None else current.get('holiday_ot_fixed_amount') or 0)
        nw_fixed = float(non_working_day_ot_fixed_amount if non_working_day_ot_fixed_amount is not None else current.get('non_working_day_ot_fixed_amount') or 0)
        off_mult = float(off_day_attendance_pay_multiplier if off_day_attendance_pay_multiplier is not None else current.get('off_day_attendance_pay_multiplier') or 1.5)
        off_fixed = float(off_day_attendance_pay_fixed_amount if off_day_attendance_pay_fixed_amount is not None else current.get('off_day_attendance_pay_fixed_amount') or 0)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Extra-pay amounts must be valid numbers.')
    if any(value < 0 or value != value for value in (h_mult, nw_mult, h_fixed, nw_fixed, off_mult, off_fixed)):
        raise ValueError('Extra-pay amounts cannot be negative.')
    if off_day_attendance_pay_active is None:
        off_active = int(bool(current.get('off_day_attendance_pay_active')))
    else:
        off_active = int(bool(off_day_attendance_pay_active))

    def _ot_active(pay_mode: str, percent: float, multiplier: float, fixed: float) -> int:
        if pay_mode == 'multiplier':
            return 1 if multiplier > 0 else 0
        if pay_mode == 'fixed_hour':
            return 1 if fixed > 0 else 0
        return 1 if percent > 0 else 0

    holiday_active = _ot_active(h_mode, holiday_premium, h_mult, h_fixed)
    non_working_active = _ot_active(nw_mode, non_working_premium, nw_mult, nw_fixed)
    incoming_flags = feature_flags or {}
    flag_values = []
    for key in FEATURE_FLAGS:
        if key in incoming_flags and incoming_flags[key] is not None:
            flag_values.append(int(bool(incoming_flags[key])))
        else:
            flag_values.append(int(bool(current.get(key, DEFAULT_FEATURE_FLAGS.get(key)))))
    assignments = [
        "deduct_unpaid_leave = ?",
        "half_leave_quota_mode = ?",
        "half_leaves_per_quota_day = ?",
        "holiday_ot_premium_percent = ?",
        "non_working_day_ot_premium_percent = ?",
        "holiday_ot_premium_active = ?",
        "non_working_day_ot_premium_active = ?",
        "ot_hour_divisor = ?",
        "proration_basis = ?",
        "attendance_pay_mode = ?",
        "currency_code = ?",
        "decimal_places = ?",
        "holiday_ot_pay_mode = ?",
        "non_working_day_ot_pay_mode = ?",
        "holiday_ot_multiplier = ?",
        "non_working_day_ot_multiplier = ?",
        "holiday_ot_fixed_amount = ?",
        "non_working_day_ot_fixed_amount = ?",
        "off_day_attendance_pay_active = ?",
        "off_day_attendance_pay_mode = ?",
        "off_day_attendance_pay_multiplier = ?",
        "off_day_attendance_pay_fixed_amount = ?",
    ]
    assignments.extend(f"{key} = ?" for key in FEATURE_FLAGS)
    params = (
        int(bool(deduct_unpaid_leave)),
        mode,
        conversion_count,
        holiday_premium,
        non_working_premium,
        holiday_active,
        non_working_active,
        divisor,
        basis,
        pay_mode,
        currency,
        places,
        h_mode,
        nw_mode,
        h_mult,
        nw_mult,
        h_fixed,
        nw_fixed,
        off_active,
        off_mode,
        off_mult,
        off_fixed,
        *flag_values,
    )
    insert_columns = [
        "organization_id",
        "branch_id",
        "deduct_unpaid_leave",
        "half_leave_quota_mode",
        "half_leaves_per_quota_day",
        "holiday_ot_premium_percent",
        "non_working_day_ot_premium_percent",
        "holiday_ot_premium_active",
        "non_working_day_ot_premium_active",
        "ot_hour_divisor",
        "proration_basis",
        "attendance_pay_mode",
        "currency_code",
        "decimal_places",
        "holiday_ot_pay_mode",
        "non_working_day_ot_pay_mode",
        "holiday_ot_multiplier",
        "non_working_day_ot_multiplier",
        "holiday_ot_fixed_amount",
        "non_working_day_ot_fixed_amount",
        "off_day_attendance_pay_active",
        "off_day_attendance_pay_mode",
        "off_day_attendance_pay_multiplier",
        "off_day_attendance_pay_fixed_amount",
        *FEATURE_FLAGS,
    ]
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            migrate_payroll_policy_tenant_scope(cur)
            row = cur.execute(
                """
                SELECT id FROM payroll_settings
                 WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                   AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                 ORDER BY id
                 LIMIT 1
                """,
                (org, branch),
            ).fetchone()
            if row:
                cur.execute(
                    f"UPDATE payroll_settings SET {', '.join(assignments)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (*params, row[0]),
                )
            else:
                placeholders = ", ".join("?" for _ in insert_columns)
                cur.execute(
                    f"INSERT INTO payroll_settings ({', '.join(insert_columns)}) VALUES ({placeholders})",
                    (org, branch, *params),
                )
            conn.commit()
            previous_mode = _normalize_attendance_pay_mode(current.get('attendance_pay_mode'))
            if previous_mode != pay_mode:
                _write_payroll_audit(cur, 'attendance_pay_mode_changed', org, branch, date.today().isoformat()[:7], 'Admin', {
                    'from': previous_mode, 'to': pay_mode,
                })
                conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update payroll settings: {e}")
        raise ValueError(f"Could not write payroll settings: {e}") from e


def list_payroll_bonuses(
    year: int,
    month: int,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT b.id, b.user_id, b.amount, b.note, b.source, b.year, b.month,
                   u.name AS employee_name, u.email
            FROM payroll_bonuses b
            LEFT JOIN users u ON u.id = b.user_id
            WHERE b.year = ? AND b.month = ?
              AND COALESCE(b.organization_id, 'DEFAULT_ORG') = ?
              AND COALESCE(b.branch_id, 'DEFAULT_BRANCH') = ?
            ORDER BY u.name COLLATE NOCASE
            """,
            (int(year), int(month), org, branch),
        ).fetchall()
        return [dict(row) for row in rows]


def get_payroll_bonus_amounts(
    year: int,
    month: int,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Dict[int, float]:
    return {
        int(row['user_id']): float(row.get('amount') or 0)
        for row in list_payroll_bonuses(year, month, organization_id, branch_id)
    }


DEFAULT_BONUS_LIMIT = 1_000_000.0


def replace_payroll_bonuses(
    year: int,
    month: int,
    rows: Sequence[Dict[str, Any]],
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    source: str = 'excel',
    commit: bool = False,
    bonus_limit: Optional[float] = None,
) -> Dict[str, Any]:
    """Validate bonus rows. Duplicate employees are rejected (never silently summed). Commit only when commit=True."""
    org, branch = _tenant_ids(organization_id, branch_id)
    if commit:
        raise_if_payroll_month_locked(f'{int(year):04d}-{int(month):02d}-01', org, branch)
    limit = float(bonus_limit if bonus_limit is not None else DEFAULT_BONUS_LIMIT)
    errors: List[Dict[str, Any]] = []
    preview: List[Dict[str, Any]] = []
    seen: Dict[int, int] = {}
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute(
            """
            SELECT id, name, email FROM users
            WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
              AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
            """,
            (org, branch),
        ).fetchall()
        by_id = {int(row['id']): dict(row) for row in users}
        by_email = {str(row['email'] or '').strip().lower(): dict(row) for row in users if str(row['email'] or '').strip()}
        by_name: Dict[str, List[Dict[str, Any]]] = {}
        for row in users:
            by_name.setdefault(str(row['name'] or '').strip().lower(), []).append(dict(row))

        for index, item in enumerate(rows, start=1):
            raw_amount = item.get('amount')
            note = str(item.get('note') or '').strip()
            try:
                amount = float(raw_amount)
            except (TypeError, ValueError, OverflowError):
                errors.append({'row': index, 'field': 'amount', 'error': 'Amount must be numeric'})
                continue
            if amount < 0:
                errors.append({'row': index, 'field': 'amount', 'error': 'Amount cannot be negative'})
                continue
            if amount == 0:
                errors.append({'row': index, 'field': 'amount', 'error': 'Amount cannot be zero'})
                continue
            if amount > limit:
                errors.append({'row': index, 'field': 'amount', 'error': f'Amount exceeds the organization bonus limit of {limit:g}'})
                continue
            user = None
            raw_id = item.get('user_id') or item.get('employee_id')
            if raw_id not in (None, ''):
                try:
                    user = by_id.get(int(raw_id))
                except (TypeError, ValueError, OverflowError):
                    user = None
            if user is None and item.get('email'):
                user = by_email.get(str(item.get('email')).strip().lower())
            if user is None and item.get('name'):
                candidates = by_name.get(str(item.get('name')).strip().lower()) or []
                if len(candidates) == 1:
                    user = candidates[0]
            if user is None:
                errors.append({'row': index, 'field': 'employee_id', 'error': f'Unknown employee ({item.get("name") or item.get("email") or raw_id})'})
                continue
            uid = int(user['id'])
            if uid in seen:
                errors.append({'row': index, 'field': 'employee_id', 'error': f'Duplicate employee #{uid}; duplicates are rejected, not summed'})
                continue
            seen[uid] = index
            preview.append({'user_id': uid, 'employee_name': user.get('name'), 'amount': amount, 'note': note})

        if commit and not errors and preview:
            cursor = conn.cursor()
            cursor.execute(
                """DELETE FROM payroll_bonuses
                   WHERE year = ? AND month = ?
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                (int(year), int(month), org, branch),
            )
            for item in preview:
                cursor.execute(
                    """
                    INSERT INTO payroll_bonuses (
                        organization_id, branch_id, year, month, user_id, amount, note, source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(organization_id, branch_id, year, month, user_id) DO UPDATE SET
                        amount = excluded.amount,
                        note = excluded.note,
                        source = excluded.source,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (org, branch, int(year), int(month), item['user_id'], item['amount'], item['note'], source),
                )
            _write_payroll_audit(cursor, 'bonuses_imported', org, branch, f'{int(year):04d}-{int(month):02d}', 'Admin', {
                'imported': len(preview), 'source': source,
            })
            conn.commit()
    return {
        'imported': len(preview) if commit and not errors else 0,
        'preview': preview,
        'errors': errors,
        'unmatched': [e['error'] for e in errors],
        'committed': bool(commit and not errors and preview),
        'duplicate_policy': 'reject',
        'bonuses': list_payroll_bonuses(year, month, org, branch) if commit else preview,
    }


def save_payroll_bonus_row(
    year: int,
    month: int,
    user_id: int,
    amount: float,
    note: str = '',
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Dict[str, Any]:
    return replace_payroll_bonuses(
        year, month, [{'user_id': user_id, 'amount': amount, 'note': note}],
        organization_id=organization_id, branch_id=branch_id, source='grid', commit=True,
    )


def list_payroll_component_types(organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict[str, Any]]:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM payroll_component_types
               WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
               ORDER BY kind, name COLLATE NOCASE""",
            (org, branch),
        ).fetchall()
        return [dict(row) for row in rows]


def save_payroll_component_type(
    name: str,
    code: str,
    kind: str,
    category: str = 'allowance',
    calc_mode: str = 'fixed',
    default_amount: float = 0.0,
    active: bool = True,
    component_id: Optional[int] = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Optional[int]:
    kind_n = str(kind or '').strip().lower()
    cat = str(category or 'allowance').strip().lower()
    mode = str(calc_mode or 'fixed').strip().lower()
    if not str(name or '').strip() or not str(code or '').strip():
        return None
    if kind_n not in VALID_COMPONENT_KINDS or cat not in VALID_COMPONENT_CATEGORIES or mode not in VALID_CALC_MODES:
        return None
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        if component_id:
            conn.execute(
                """UPDATE payroll_component_types
                   SET name=?, code=?, kind=?, category=?, calc_mode=?, default_amount=?, active=?
                   WHERE id=? AND COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?""",
                (name.strip(), code.strip().lower(), kind_n, cat, mode, float(default_amount or 0), int(bool(active)), int(component_id), org, branch),
            )
            conn.commit()
            return int(component_id)
        cursor = conn.execute(
            """INSERT INTO payroll_component_types (
                   organization_id, branch_id, name, code, kind, category, calc_mode, default_amount, active
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (org, branch, name.strip(), code.strip().lower(), kind_n, cat, mode, float(default_amount or 0), int(bool(active))),
        )
        conn.commit()
        return cursor.lastrowid


def delete_payroll_component_type(component_id: int, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> bool:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """DELETE FROM payroll_component_types WHERE id=?
               AND COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?""",
            (int(component_id), org, branch),
        )
        conn.execute("DELETE FROM payroll_employee_components WHERE component_type_id=?", (int(component_id),))
        conn.commit()
        return cursor.rowcount > 0


def list_payroll_component_duplicates(organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict[str, Any]]:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        ensure_payroll_extension_tables(cursor)
        conn.commit()
        rows = cursor.execute(
            """SELECT * FROM payroll_duplicate_component_report
               WHERE COALESCE(organization_id,'DEFAULT_ORG')=?
                 AND COALESCE(branch_id,'DEFAULT_BRANCH')=?
               ORDER BY id""",
            (org, branch),
        ).fetchall()
        return [dict(row) for row in rows]


def list_employee_components(user_id: int, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict[str, Any]]:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT c.*, t.name, t.code, t.kind, t.category, t.calc_mode
               FROM payroll_employee_components c
               JOIN payroll_component_types t ON t.id = c.component_type_id
               WHERE c.user_id=?
                 AND COALESCE(c.organization_id,'DEFAULT_ORG')=?
                 AND COALESCE(c.branch_id,'DEFAULT_BRANCH')=?
               ORDER BY t.kind, t.name""",
            (int(user_id), org, branch),
        ).fetchall()
        return [dict(row) for row in rows]


def save_employee_component(
    user_id: Optional[int] = None,
    component_type_id: int = 0,
    amount: float = 0.0,
    percent: float = 0.0,
    effective_from: Optional[str] = None,
    effective_to: Optional[str] = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    scope_type: str = 'employee',
    scope_id: Optional[str] = None,
    excluded: bool = False,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    rounding: str = 'cent',
    calc_mode: Optional[str] = None,
    confirm_affected_months: bool = False,
    join_date: Optional[str] = None,
) -> Dict[str, Any]:
    from payroll_extensions import VALID_SCOPES, _dates_overlap, _refresh_duplicate_component_report
    org, branch = _tenant_ids(organization_id, branch_id)
    scope = str(scope_type or 'employee').strip().lower()
    if scope not in VALID_SCOPES:
        raise ValueError('scope_type must be one of: org, branch, shift, department, designation, employee')
    try:
        type_id = int(component_type_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Pay line is required') from exc
    if type_id <= 0:
        raise ValueError('Pay line is required')
    uid = 0
    if scope == 'employee':
        try:
            uid = int(user_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Employee is required for employee-scope assignments') from exc
        if uid <= 0:
            raise ValueError('Employee is required for employee-scope assignments')
    else:
        if not str(scope_id or '').strip() and scope not in ('org',):
            raise ValueError(f'{scope} is required')
        uid = int(user_id or 0)
    start = str(effective_from or '').strip()[:10]
    if not start:
        start = str(join_date or '').strip()[:10]
    if not start:
        raise ValueError('effective_from is required; it does not default to today. Use the employee start date or pick a date.')
    try:
        datetime.strptime(start, '%Y-%m-%d')
    except ValueError as exc:
        raise ValueError('effective_from must use YYYY-MM-DD') from exc
    end = str(effective_to).strip()[:10] if effective_to else None
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        ensure_payroll_extension_tables(cursor)
        existing = [dict(r) for r in cursor.execute(
            """SELECT * FROM payroll_employee_components
               WHERE COALESCE(organization_id,'DEFAULT_ORG')=?
                 AND COALESCE(branch_id,'DEFAULT_BRANCH')=?
                 AND component_type_id=?
                 AND COALESCE(scope_type,'employee')=?
                 AND COALESCE(user_id,0)=?
                 AND COALESCE(scope_id,'')=?
                 AND excluded=0
                 AND active=1""",
            (org, branch, type_id, scope, uid, str(scope_id or '')),
        ).fetchall()]
        for row in existing:
            if _dates_overlap(start, end, row.get('effective_from'), row.get('effective_to')):
                raise ValueError(
                    f'This pay line is already assigned to the same {scope} for overlapping dates '
                    f'({row.get("effective_from")} to {row.get("effective_to") or "open"}).'
                )
        affected = [
            r['payroll_month'] for r in cursor.execute(
                """SELECT DISTINCT payroll_month FROM payroll_runs
                   WHERE COALESCE(organization_id,'DEFAULT_ORG')=?
                     AND COALESCE(branch_id,'DEFAULT_BRANCH')=?
                     AND payroll_month >= ?
                   ORDER BY payroll_month""",
                (org, branch, start[:7]),
            ).fetchall()
        ]
        if affected and not confirm_affected_months:
            return {
                'ok': False,
                'needs_confirmation': True,
                'field': 'effective_from',
                'error': 'This pay line starts after months already calculated. Confirm to save.',
                'affected_months': affected,
            }
        cursor.execute(
            """INSERT INTO payroll_employee_components (
                   organization_id, branch_id, user_id, component_type_id, amount, percent,
                   effective_from, effective_to, scope_type, scope_id, excluded,
                   min_amount, max_amount, rounding, calc_mode
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                org, branch, uid, type_id, float(amount or 0), float(percent or 0),
                start, end, scope, str(scope_id or ''), int(bool(excluded)),
                min_amount, max_amount, rounding or 'cent', calc_mode,
            ),
        )
        _refresh_duplicate_component_report(cursor)
        _write_payroll_audit(cursor, 'pay_line_assigned', org, branch, start[:7], 'Admin', {
            'user_id': uid, 'component_type_id': type_id, 'scope_type': scope, 'scope_id': scope_id,
            'effective_from': start, 'excluded': bool(excluded),
        })
        conn.commit()
        return {'ok': True, 'id': cursor.lastrowid}


def delete_employee_component(assignment_id: int, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> bool:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """DELETE FROM payroll_employee_components WHERE id=?
               AND COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?""",
            (int(assignment_id), org, branch),
        )
        conn.commit()
        return cursor.rowcount > 0


def list_payroll_adjustments(payroll_month: str, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict[str, Any]]:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT a.*, u.name AS employee_name
               FROM payroll_adjustments a
               LEFT JOIN users u ON u.id = a.user_id
               WHERE a.payroll_month=?
                 AND COALESCE(a.organization_id,'DEFAULT_ORG')=?
                 AND COALESCE(a.branch_id,'DEFAULT_BRANCH')=?
               ORDER BY a.id""",
            (payroll_month, org, branch),
        ).fetchall()
        return [dict(row) for row in rows]


def save_payroll_adjustment(
    user_id: int,
    payroll_month: str,
    adj_type: str,
    pay_mode: str = 'fixed',
    amount: float = 0.0,
    multiplier: float = 0.0,
    work_date: Optional[str] = None,
    note: str = '',
    source_month: Optional[str] = None,
    created_by: Optional[str] = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Optional[int]:
    kind = str(adj_type or '').strip().lower()
    mode = str(pay_mode or 'fixed').strip().lower()
    if kind not in VALID_ADJ_TYPES or mode not in VALID_ADJ_MODES:
        return None
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """INSERT INTO payroll_adjustments (
                   organization_id, branch_id, user_id, payroll_month, work_date, adj_type, pay_mode,
                   amount, multiplier, note, source_month, created_by
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (org, branch, int(user_id), payroll_month, work_date, kind, mode, float(amount or 0),
             float(multiplier or 0), note or '', source_month, created_by),
        )
        _write_payroll_audit(conn.cursor(), 'adjustment_added', org, branch, payroll_month, created_by, {
            'user_id': int(user_id), 'adj_type': kind, 'amount': amount, 'multiplier': multiplier, 'work_date': work_date,
        })
        conn.commit()
        return cursor.lastrowid


def delete_payroll_adjustment(adjustment_id: int, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> bool:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """DELETE FROM payroll_adjustments WHERE id=?
               AND COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?""",
            (int(adjustment_id), org, branch),
        )
        conn.commit()
        return cursor.rowcount > 0


def list_payroll_audit(payroll_month: Optional[str] = None, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict[str, Any]]:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if payroll_month:
            rows = conn.execute(
                """SELECT * FROM payroll_audit_log
                   WHERE COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?
                     AND payroll_month=?
                   ORDER BY id DESC LIMIT 200""",
                (org, branch, payroll_month),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM payroll_audit_log
                   WHERE COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?
                   ORDER BY id DESC LIMIT 200""",
                (org, branch),
            ).fetchall()
        return [dict(row) for row in rows]


def _load_payroll_extension_maps(cursor, organization_id: str, branch_id: Optional[str], payroll_month: str) -> Dict[str, Any]:
    org, branch = _tenant_ids(organization_id, branch_id)
    types = {int(row['id']): dict(row) for row in cursor.execute(
        """SELECT * FROM payroll_component_types
           WHERE COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?""",
        (org, branch),
    ).fetchall()}
    assignments: List[Dict[str, Any]] = [
        dict(row) for row in cursor.execute(
            """SELECT * FROM payroll_employee_components
               WHERE COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?""",
            (org, branch),
        ).fetchall()
    ]
    revisions: Dict[int, List[Dict[str, Any]]] = {}
    for row in cursor.execute(
        """SELECT * FROM salary_revisions
           WHERE COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?
           ORDER BY effective_from""",
        (org, branch),
    ).fetchall():
        revisions.setdefault(int(row['user_id']), []).append(dict(row))
    adjustments: Dict[int, List[Dict[str, Any]]] = {}
    for row in cursor.execute(
        """SELECT * FROM payroll_adjustments
           WHERE payroll_month=? AND COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?""",
        (payroll_month, org, branch),
    ).fetchall():
        adjustments.setdefault(int(row['user_id']), []).append(dict(row))
    return {'types': types, 'assignments': assignments, 'revisions': revisions, 'adjustments': adjustments}


def get_overtime_premium_rules(
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    settings = get_payroll_settings(organization_id=organization_id, branch_id=branch_id)
    return [
        {
            'day_type': 'holiday',
            'label': 'Public holiday',
            'premium_percent': float(settings.get('holiday_ot_premium_percent') or 0),
            'active': bool(settings.get('holiday_ot_premium_active')),
        },
        {
            'day_type': 'non_working_day',
            'label': 'Scheduled day off',
            'premium_percent': float(settings.get('non_working_day_ot_premium_percent') or 0),
            'active': bool(settings.get('non_working_day_ot_premium_active')),
        },
    ]


def get_overtime_holiday_premium_rules(
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return company holidays with an explicitly configured OT premium."""
    org, branch = _tenant_ids(organization_id, branch_id)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT id, name, holiday_date, end_date, is_recurring, overtime_premium_percent
                FROM company_holidays
                WHERE overtime_premium_percent IS NOT NULL
                  AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                  AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                ORDER BY holiday_date, name
            ''', (org, branch)).fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Failed to load holiday overtime premium rules: {e}")
        return []


def set_company_holiday_overtime_premium(
    holiday_id: int,
    premium_percent: Optional[float],
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    """Set a holiday-specific OT premium, or clear it to use the global fallback."""
    if premium_percent is not None:
        try:
            premium = float(premium_percent)
        except (TypeError, ValueError, OverflowError):
            return False
        if not 0 <= premium <= 1000:
            return False
    else:
        premium = None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            org, branch = _tenant_ids(organization_id, branch_id)
            cursor = conn.execute(
                """UPDATE company_holidays SET overtime_premium_percent = ?
                   WHERE id = ?
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                (premium, int(holiday_id), org, branch),
            )
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Failed to update holiday overtime premium: {e}")
        return False


def save_overtime_premium_rule(
    day_type: str,
    premium_percent: float,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    fields = {
        'holiday': ('holiday_ot_premium_percent', 'holiday_ot_premium_active'),
        'non_working_day': ('non_working_day_ot_premium_percent', 'non_working_day_ot_premium_active'),
    }
    if day_type not in fields:
        return False
    try:
        value = float(premium_percent)
    except (TypeError, ValueError, OverflowError):
        return False
    if not 0 <= value <= 1000:
        return False
    percent_column, active_column = fields[day_type]
    try:
        with sqlite3.connect(DB_PATH) as conn:
            org, branch = _tenant_ids(organization_id, branch_id)
            ensure_payroll_settings_row(conn.cursor(), org, branch)
            conn.execute(
                f"""UPDATE payroll_settings SET {percent_column} = ?, {active_column} = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                      AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                (value, org, branch),
            )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to save overtime premium rule {day_type}: {e}")
        return False


def set_overtime_premium_rule_active(
    day_type: str,
    active: bool,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    active_columns = {
        'holiday': 'holiday_ot_premium_active',
        'non_working_day': 'non_working_day_ot_premium_active',
    }
    active_column = active_columns.get(day_type)
    if not active_column:
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            org, branch = _tenant_ids(organization_id, branch_id)
            ensure_payroll_settings_row(conn.cursor(), org, branch)
            conn.execute(
                f"""UPDATE payroll_settings SET {active_column} = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                      AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                (int(bool(active)), org, branch),
            )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update overtime premium rule {day_type}: {e}")
        return False


def remove_overtime_premium_rule(
    day_type: str,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    fields = {
        'holiday': ('holiday_ot_premium_percent', 'holiday_ot_premium_active'),
        'non_working_day': ('non_working_day_ot_premium_percent', 'non_working_day_ot_premium_active'),
    }
    columns = fields.get(day_type)
    if not columns:
        return False
    percent_column, active_column = columns
    try:
        with sqlite3.connect(DB_PATH) as conn:
            org, branch = _tenant_ids(organization_id, branch_id)
            ensure_payroll_settings_row(conn.cursor(), org, branch)
            conn.execute(
                f"""UPDATE payroll_settings SET {percent_column} = 0, {active_column} = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                      AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                (org, branch),
            )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to remove overtime premium rule {day_type}: {e}")
        return False


def get_special_date_overrides(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return configured payroll calendar overrides, optionally bounded by date."""
    org, branch = _tenant_ids(organization_id, branch_id)
    clauses = [
        "COALESCE(organization_id, 'DEFAULT_ORG') = ?",
        "COALESCE(branch_id, 'DEFAULT_BRANCH') = ?",
    ]
    params: List[Any] = [org, branch]
    if start_date:
        clauses.append("calendar_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("calendar_date <= ?")
        params.append(end_date)
    where = f"WHERE {' AND '.join(clauses)}"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM special_date_overrides {where} ORDER BY calendar_date, id",
            params,
        )
        return [dict(row) for row in cursor.fetchall()]


def save_special_date_override(
    calendar_date: Any,
    override_type: Any,
    note: Any = '',
    override_id: Optional[int] = None,
    scope_type: Any = 'all',
    scope_id: Any = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update a unique payroll calendar override date."""
    date_text = str(calendar_date or '').strip()
    try:
        normalized_date = datetime.strptime(date_text, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError) as e:
        raise ValueError("Enter a date using YYYY-MM-DD format") from e
    if normalized_date != date_text:
        raise ValueError("Enter a date using YYYY-MM-DD format")
    mode = str(override_type or '').strip().lower()
    if mode not in ('working_day', 'off_day', 'holiday'):
        raise ValueError("Override type must be Working Day, Off Day, or Holiday")
    target_scope = str(scope_type or 'all').strip().lower()
    if target_scope not in ('all', 'shift', 'employee'):
        raise ValueError("Calendar scope must be all, shift, or employee")
    if target_scope == 'all':
        target_scope_id = 0
    else:
        try:
            target_scope_id = int(scope_id)
        except (TypeError, ValueError, OverflowError) as e:
            raise ValueError("Choose a valid shift or employee for this override") from e
        if target_scope_id < 1:
            raise ValueError("Choose a valid shift or employee for this override")
    note_text = str(note or '').strip()
    try:
        target_id = int(override_id) if override_id is not None else None
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError("Invalid special date identifier") from e

    try:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cursor = conn.cursor()
            if target_scope != 'all':
                table = 'shifts' if target_scope == 'shift' else 'users'
                cursor.execute(f"SELECT 1 FROM {table} WHERE id = ?", (target_scope_id,))
                if not cursor.fetchone():
                    raise ValueError(f"{target_scope.title()} not found")
            org, branch = _tenant_ids(organization_id, branch_id)
            if target_id is None:
                cursor.execute(
                    """INSERT INTO special_date_overrides (
                           calendar_date, override_type, note, scope_type, scope_id, organization_id, branch_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (normalized_date, mode, note_text, target_scope, target_scope_id, org, branch),
                )
                target_id = cursor.lastrowid
            else:
                cursor.execute(
                    """SELECT 1 FROM special_date_overrides
                       WHERE id = ?
                         AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                         AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                    (target_id, org, branch),
                )
                if not cursor.fetchone():
                    raise ValueError("Special date not found")
                cursor.execute(
                    """UPDATE special_date_overrides
                       SET calendar_date = ?, override_type = ?, note = ?, scope_type = ?, scope_id = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND COALESCE(organization_id, 'DEFAULT_ORG') = ? AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                    (normalized_date, mode, note_text, target_scope, target_scope_id, target_id, org, branch),
                )
            conn.commit()
    except sqlite3.IntegrityError as e:
        raise ValueError("A special date is already configured for this date") from e
    return next(
        item for item in get_special_date_overrides(organization_id=organization_id, branch_id=branch_id)
        if item['id'] == target_id
    )


def delete_special_date_override(
    override_id: int,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    """Remove a payroll calendar override."""
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """DELETE FROM special_date_overrides
               WHERE id = ?
                 AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
            (override_id, org, branch),
        )
        conn.commit()
        return cursor.rowcount > 0


def _validate_payroll_rule_range(
    cursor: sqlite3.Cursor,
    min_absent_days: int,
    max_absent_days: Optional[int],
    deduction_type: str,
    deduction_percent: float,
    fixed_amount: float,
    active: bool,
    exclude_rule_id: Optional[int] = None,
    rule_basis: str = 'absence',
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> None:
    if rule_basis not in ('absence', 'half_leave'):
        raise ValueError("Rule basis must be absence or half leave")
    if min_absent_days < 1:
        raise ValueError("Minimum count must be at least 1")
    if max_absent_days is not None and max_absent_days < min_absent_days:
        raise ValueError("Maximum absent days must be greater than or equal to minimum absent days")
    if deduction_type not in ("percentage", "fixed"):
        raise ValueError("Deduction type must be percentage or fixed amount")
    if deduction_type == "percentage" and not 0 <= deduction_percent <= 100:
        raise ValueError("Deduction percentage must be between 0 and 100")
    if deduction_type == "fixed" and fixed_amount < 0:
        raise ValueError("Fixed deduction amount cannot be negative")
    org, branch = _tenant_ids(organization_id, branch_id)
    query = """SELECT id, min_absent_days, max_absent_days FROM payroll_rules
               WHERE rule_basis = ?
                 AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?"""
    params: Tuple[Any, ...] = (rule_basis, org, branch)
    if exclude_rule_id is not None:
        query += " AND id != ?"
        params = (rule_basis, org, branch, exclude_rule_id)
    cursor.execute(query, params)
    for existing_id, existing_min, existing_max in cursor.fetchall():
        overlaps = (
            (max_absent_days is None or existing_min <= max_absent_days)
            and (existing_max is None or min_absent_days <= existing_max)
        )
        if overlaps:
            raise ValueError(f"This range overlaps active payroll rule #{existing_id}")


def save_payroll_rule(
    min_absent_days: Any,
    max_absent_days: Any,
    deduction_percent: Any = None,
    active: Any = True,
    rule_id: Optional[int] = None,
    deduction_type: Any = "percentage",
    fixed_amount: Any = 0,
    rule_basis: Any = 'absence',
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update a rule, rejecting ambiguous active ranges."""
    try:
        min_days = int(min_absent_days)
        max_days = None if max_absent_days in (None, "") else int(max_absent_days)
        mode = str(deduction_type or "percentage").strip().lower()
        percent = float(deduction_percent or 0)
        amount = float(fixed_amount or 0)
        is_active = bool(int(active)) if isinstance(active, (str, int, bool)) else bool(active)
        target_id = int(rule_id) if rule_id is not None else None
        basis = str(rule_basis or 'absence').strip().lower()
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError("Enter valid whole-number ranges and a numeric deduction value") from e

    try:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            org, branch = _tenant_ids(organization_id, branch_id)
            if target_id is not None:
                cursor.execute(
                    """SELECT id FROM payroll_rules
                       WHERE id = ?
                         AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                         AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                    (target_id, org, branch),
                )
                if not cursor.fetchone():
                    raise ValueError("Payroll rule not found")
            _validate_payroll_rule_range(
                cursor, min_days, max_days, mode, percent, amount, is_active,
                target_id, basis, org, branch,
            )

            if target_id is None:
                cursor.execute('''
                    INSERT INTO payroll_rules (
                        rule_basis, min_absent_days, max_absent_days, deduction_type,
                        deduction_percent, fixed_amount, active, organization_id, branch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (basis, min_days, max_days, mode, percent, amount, int(is_active), org, branch))
                target_id = cursor.lastrowid
            else:
                cursor.execute('''
                    UPDATE payroll_rules
                    SET rule_basis = ?, min_absent_days = ?, max_absent_days = ?, deduction_type = ?,
                        deduction_percent = ?, fixed_amount = ?, active = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                      AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                      AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                ''', (basis, min_days, max_days, mode, percent, amount, int(is_active), target_id, org, branch))
            conn.commit()
        return next(
            rule for rule in get_payroll_rules(organization_id=organization_id, branch_id=branch_id)
            if rule['id'] == target_id
        )
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to save payroll rule: {e}")
        raise


def set_payroll_rule_active(
    rule_id: int,
    active: bool,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Activate/deactivate a rule; activation must not overlap another active range."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        org, branch = _tenant_ids(organization_id, branch_id)
        cursor.execute(
            """SELECT * FROM payroll_rules
               WHERE id = ?
                 AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
            (rule_id, org, branch),
        )
        row = cursor.fetchone()
        if not row:
            return None
        if active:
            _validate_payroll_rule_range(
                cursor, row['min_absent_days'], row['max_absent_days'],
                row['deduction_type'], row['deduction_percent'], row['fixed_amount'], True,
                rule_id, row['rule_basis'], org, branch,
            )
        cursor.execute(
            """UPDATE payroll_rules SET active = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?
                 AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
            (int(bool(active)), rule_id, org, branch)
        )
        conn.commit()
    return next(
        rule for rule in get_payroll_rules(organization_id=organization_id, branch_id=branch_id)
        if rule['id'] == rule_id
    )


def _sanitize_orphan_payroll_records() -> int:
    """Remove stale payroll rows that reference deleted payroll runs so database integrity stays valid."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM payroll_records WHERE payroll_run_id IS NULL OR payroll_run_id NOT IN (SELECT id FROM payroll_runs)")
            deleted = cursor.rowcount
            conn.commit()
            return deleted
    except Exception as exc:
        logger.error(f"Failed to sanitize orphan payroll records: {exc}")
        return 0


def calculate_monthly_payroll(payroll_month: str | int | None = None, month: Optional[int] = None, organization_id: str = 'DEFAULT_ORG', branch_id: Optional[str] = None) -> Dict[str, Any] | list:
    """Build an attendance-based monthly payroll preview without saving it.

    Supports the modern single-string contract (YYYY-MM) and a legacy compatibility
    pattern that accepts year and month separately, returning the flattened employee
    records list for those callers.
    """
    _sanitize_orphan_payroll_records()
    if month is not None:
        if payroll_month is None:
            raise ValueError("Payroll year is required when month is provided")
        payroll_month = f"{int(payroll_month):04d}-{int(month):02d}"
    elif isinstance(payroll_month, (tuple, list)) and len(payroll_month) == 2:
        year, month = payroll_month
        payroll_month = f"{int(year):04d}-{int(month):02d}"

    try:
        month_start = datetime.strptime(str(payroll_month), "%Y-%m").date().replace(day=1)
    except (TypeError, ValueError) as e:
        raise ValueError("Payroll month must use YYYY-MM format") from e
    if month_start.strftime("%Y-%m") != str(payroll_month):
        raise ValueError("Payroll month must use YYYY-MM format")
    finalized_run = get_monthly_payroll(
        payroll_month, organization_id=organization_id, branch_id=branch_id
    )
    if finalized_run:
        return finalized_run

    today = datetime.now().date()
    current_month = today.replace(day=1)
    if month_start > current_month:
        raise ValueError("Payroll cannot be calculated for a future month")

    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    month_end = next_month - timedelta(days=1)
    period_end = min(month_end, today)
    payroll_settings = get_payroll_settings(organization_id=organization_id, branch_id=branch_id)
    deduct_unpaid_leave = bool(payroll_settings.get('deduct_unpaid_leave', 0))
    ot_hour_divisor = float(payroll_settings.get('ot_hour_divisor') or DEFAULT_OT_DIVISOR) or DEFAULT_OT_DIVISOR
    proration_basis = str(payroll_settings.get('proration_basis') or DEFAULT_PRORATION_BASIS)
    attendance_mode = _normalize_attendance_pay_mode(payroll_settings.get('attendance_pay_mode'))
    month_complete = period_end >= month_end
    decimal_places = int(payroll_settings.get('decimal_places') or DEFAULT_DECIMAL_PLACES)
    def _m(value):
        return _payroll_money(value, decimal_places)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT u.id AS employee_id, u.name AS employee_name,
                   COALESCE(u.department, '') AS department,
                   COALESCE(u.position, '') AS designation,
                   CASE WHEN COALESCE(sc.basic_salary, 0) > 0 THEN sc.basic_salary
                        WHEN COALESCE(u.salary, 0) > 0 THEN u.salary
                        ELSE 0 END AS basic_salary,
                   COALESCE(sc.allowances, 0.0) AS allowances,
                   COALESCE(sc.deductions, 0.0) AS custom_deductions,
                   COALESCE(sc.ot_rate, 0.0) AS ot_rate,
                   COALESCE(NULLIF(TRIM(u.join_date), ''), date(u.enrollment_date), date(u.created_at)) AS employed_from,
                   u.exit_date,
                   u.active,
                   COALESCE(u.shift_id, 0) AS shift_id
            FROM users u
            LEFT JOIN salary_configs sc ON sc.user_id = u.id
            WHERE (u.active = 1 OR (u.exit_date IS NOT NULL AND u.exit_date >= ?))
              AND COALESCE(u.organization_id, 'DEFAULT_ORG') = ?
              AND (? IS NULL OR COALESCE(u.branch_id, 'DEFAULT_BRANCH') = ?)
            ORDER BY u.department COLLATE NOCASE, u.name COLLATE NOCASE
        ''', (month_start.isoformat(), organization_id, branch_id, branch_id))
        employees = [dict(row) for row in cursor.fetchall()]
        inactive_no_exit = [dict(row) for row in cursor.execute(
            """SELECT id, name FROM users
               WHERE COALESCE(active, 1) = 0
                 AND (exit_date IS NULL OR TRIM(exit_date) = '')
                 AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                 AND (? IS NULL OR COALESCE(branch_id, 'DEFAULT_BRANCH') = ?)""",
            (organization_id, branch_id, branch_id),
        ).fetchall()]

        # Query approved overtime for this payroll month
        cursor.execute('''
            SELECT o.user_id, o.ot_date, SUM(o.hours) as total_ot_hours
            FROM overtime o
            JOIN users u ON u.id = o.user_id
            WHERE LOWER(o.status) = 'approved'
              AND o.ot_date >= ? AND o.ot_date <= ?
              AND COALESCE(u.organization_id, 'DEFAULT_ORG') = ?
              AND COALESCE(o.organization_id, u.organization_id, 'DEFAULT_ORG') = ?
              AND (? IS NULL OR COALESCE(u.branch_id, 'DEFAULT_BRANCH') = ?)
              AND (? IS NULL OR COALESCE(o.branch_id, u.branch_id, 'DEFAULT_BRANCH') = ?)
            GROUP BY o.user_id, o.ot_date
        ''', (
            month_start.isoformat(), month_end.isoformat(),
            organization_id, organization_id,
            branch_id, branch_id, branch_id, branch_id,
        ))
        approved_ot_rows = [dict(row) for row in cursor.fetchall()]
        approved_ot_by_user: Dict[int, List[Dict[str, Any]]] = {}
        for overtime_row in approved_ot_rows:
            overtime_user_id = int(overtime_row['user_id'])
            overtime_date = str(overtime_row['ot_date'])
            day_type = _get_overtime_day_type(cursor, overtime_user_id, overtime_date)
            holiday = _get_company_holiday_for_date(
                cursor, overtime_date, organization_id, branch_id or 'DEFAULT_BRANCH'
            ) if day_type == 'holiday' else None
            approved_ot_by_user.setdefault(overtime_user_id, []).append({
                'ot_date': overtime_date,
                'day_type': day_type,
                'holiday': holiday,
                'holiday_id': holiday.get('id') if holiday else None,
                'holiday_name': holiday.get('name') if holiday else None,
                'hours': float(overtime_row['total_ot_hours'] or 0.0),
                'premium_percent': _get_overtime_premium_percent(cursor, overtime_date, day_type, payroll_settings, holiday),
            })

        extra_maps = _load_payroll_extension_maps(
            cursor, organization_id, branch_id, month_start.strftime('%Y-%m')
        )
        cursor.execute('''
            SELECT id, rule_basis, min_absent_days, max_absent_days, deduction_type, deduction_percent, fixed_amount
            FROM payroll_rules
            WHERE active = 1
              AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
              AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
            ORDER BY min_absent_days, max_absent_days, id
        ''', (organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH'))
        active_rules = [dict(row) for row in cursor.fetchall()]

    absence_rules_configured = any(
        (rule.get('rule_basis') or 'absence') == 'absence' for rule in active_rules
    )
    payroll_notices: List[str] = []
    if attendance_mode == 'full_salary' and not absence_rules_configured:
        payroll_notices.append('No absence rules configured')
    if attendance_mode == 'full_salary':
        payroll_notices.append(
            "This organization still uses full monthly salary. New organizations default to days present."
        )
    if inactive_no_exit:
        names = ', '.join(item['name'] for item in inactive_no_exit)
        payroll_notices.append(
            f"Deactivated employees without an exit date are excluded from payroll: {names}"
        )

    report = get_detailed_attendance_report(
        start_date=month_start.isoformat(),
        end_date=month_end.isoformat(),
        organization_id=organization_id,
        branch_id=branch_id,
    )
    report_records = {record['user_id']: record for record in report.get('records', [])}
    bonus_amounts = get_payroll_bonus_amounts(
        month_start.year, month_start.month, organization_id, branch_id or 'DEFAULT_BRANCH'
    )
    missing_reports = [employee['employee_id'] for employee in employees
                       if employee['employee_id'] not in report_records]
    if missing_reports:
        raise RuntimeError(f"Attendance report is missing active employees: {missing_reports}")

    rows = []
    for employee in employees:
        employee_id = employee['employee_id']
        try:
            employed_from = datetime.strptime(str(employee['employed_from'])[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            employed_from = month_start
            
        try:
            exited_on = datetime.strptime(str(employee.get('exit_date') or '')[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            exited_on = None

        eligible_start = max(month_start, employed_from)
        eligible_end = min(period_end, exited_on) if exited_on else period_end

        if eligible_start > month_end or (exited_on and exited_on < month_start):
            continue

        daily_records = report_records[employee_id].get('daily_records', [])
        eligible_records = [day for day in daily_records
                            if eligible_start.isoformat() <= str(day.get('date', '')) <= eligible_end.isoformat()]
        
        # Join/exit proration uses the full remaining month. Attendance KPIs and
        # payable-day pay use the live window through period_end (today for the current month).
        schedule_end = min(month_end, exited_on) if exited_on else month_end
        month_schedule_records = [day for day in daily_records
                            if eligible_start.isoformat() <= str(day.get('date', '')) <= schedule_end.isoformat()]
        period_schedule_records = [day for day in daily_records
                            if eligible_start.isoformat() <= str(day.get('date', '')) <= eligible_end.isoformat()]
        month_scheduled_workdays = sum(bool(day.get('scheduled_workday')) for day in month_schedule_records)
        scheduled_workdays = sum(bool(day.get('scheduled_workday')) for day in period_schedule_records)
        shift_history_missing = any(day.get('shift_history_missing') for day in eligible_records)
        shift_details = list({
            (day['shift_id'], day['shift_effective_from']): {
                'shift_id': day['shift_id'],
                'shift_name': day['shift_name'],
                'effective_from': day['shift_effective_from'],
                'working_days': day['shift_working_days'],
            }
            for day in month_schedule_records if day.get('shift_id') is not None
        }.values())
        calendar_details = [
            {'date': day['date'], 'override_type': day['calendar_override']}
            for day in month_schedule_records if day.get('calendar_override')
        ]
        present_days = sum(day.get('scheduled_workday', True) and day.get('status') in ('present', 'checked_out') for day in eligible_records)
        late_days = sum(day.get('scheduled_workday', True) and day.get('status') == 'late' for day in eligible_records)
        half_leave_count = sum(int(day.get('half_leave_count') or 0) for day in eligible_records)
        unpaid_leave_days = sum(1 for day in month_schedule_records if day.get('scheduled_workday', True) and day.get('status') == 'on_leave_unpaid')
        unexcused_absent_days = sum(
            bool(day.get('scheduled_workday')) and day.get('status') == 'absent' for day in eligible_records
        )
        absent_days = unexcused_absent_days + sum(
            1 for day in eligible_records if day.get('scheduled_workday', True) and day.get('status') == 'on_leave_unpaid'
        )
        
        approved_leave_days = sum(day.get('scheduled_workday', True) and day.get('status') == 'on_leave' for day in month_schedule_records)
        approved_leave_days_period = sum(day.get('scheduled_workday', True) and day.get('status') == 'on_leave' for day in eligible_records)
        
        payable_days = min(scheduled_workdays, present_days + late_days + approved_leave_days_period)
        
        full_month_workdays = sum(bool(day.get('scheduled_workday')) for day in daily_records)
        proration_factor = _payroll_proration_factor(
            proration_basis,
            eligible_start,
            schedule_end,
            month_start,
            month_end,
            month_scheduled_workdays,
            full_month_workdays,
        )
        
        fallback_pkg = {
            'basic_salary': float(employee['basic_salary'] or 0),
            'allowances': float(employee.get('allowances') or 0),
            'deductions': float(employee.get('custom_deductions') or 0),
            'ot_rate': float(employee.get('ot_rate') or 0),
        }
        user_revisions = extra_maps['revisions'].get(int(employee_id), [])
        if _payroll_flag_on(payroll_settings, 'enable_salary_revisions') and user_revisions:
            basic_salary, lump_allowances, lump_deductions, revision_ot = _prorate_packages(
                user_revisions, fallback_pkg, eligible_start, schedule_end,
                month_start, month_end, decimal_places, True,
            )
            if revision_ot > 0:
                employee['ot_rate'] = revision_ot
        else:
            basic_salary = _m(max(0.0, float(employee['basic_salary'] or 0)) * proration_factor)
            lump_allowances = round(max(0.0, float(employee.get('allowances') or 0.0)) * proration_factor, 2)
            lump_deductions = round(max(0.0, float(employee.get('custom_deductions') or 0.0)) * proration_factor, 2)

        # Apply calendar-based premiums to approved overtime hours.
        overtime_entries = approved_ot_by_user.get(employee_id, [])
        ot_hours = round(sum(entry['hours'] for entry in overtime_entries), 2)
        configured_ot_rate = float(employee.get('ot_rate') or 0.0)
        if configured_ot_rate > 0.0:
            ot_rate = _m(configured_ot_rate)
        elif basic_salary > 0.0:
            ot_rate = _m(basic_salary / ot_hour_divisor)
        else:
            ot_rate = 0.0
        overtime_breakdown = []
        overtime_amount = 0.0
        for overtime_entry in overtime_entries:
            entry_hours = float(overtime_entry['hours'] or 0.0)
            if entry_hours <= 0:
                continue
            day_type = overtime_entry['day_type']
            effective_rate, category_amount, premium_percent = _overtime_entry_pay(
                ot_rate,
                entry_hours,
                day_type,
                payroll_settings,
                holiday=overtime_entry.get('holiday'),
                decimal_places=decimal_places,
            )
            overtime_amount += category_amount
            breakdown_entry = {
                'day_type': day_type,
                'date': overtime_entry['ot_date'],
                'hours': round(entry_hours, 2),
                'premium_percent': _m(premium_percent),
                'hourly_rate': effective_rate,
                'amount': category_amount,
            }
            if overtime_entry.get('holiday_id') is not None:
                breakdown_entry['holiday_id'] = overtime_entry['holiday_id']
                breakdown_entry['holiday_name'] = overtime_entry['holiday_name']
            overtime_breakdown.append(breakdown_entry)
        overtime_amount = _m(overtime_amount)

        ot_dates = {str(entry.get('ot_date') or '') for entry in overtime_entries}
        present_statuses = {'present', 'checked_out', 'late'}
        off_day_dates: List[str] = []
        for day in eligible_records:
            if day.get('scheduled_workday'):
                continue
            if str(day.get('status') or '') not in present_statuses:
                continue
            if str(day.get('date') or '') in ot_dates:
                continue
            off_day_dates.append(str(day.get('date') or ''))
        daily_rate = _m(basic_salary / month_scheduled_workdays) if month_scheduled_workdays else 0.0
        adj_pack = {
            'extra_earn': 0.0, 'extra_deduct': 0.0, 'arrears': 0.0,
            'remaining_off_days': off_day_dates, 'lines': [],
        }
        if _payroll_flag_on(payroll_settings, 'enable_individual_adjustments'):
            adj_pack = _apply_payroll_adjustments(
                extra_maps['adjustments'].get(int(employee_id), []),
                daily_rate, off_day_dates, decimal_places,
            )
        off_days_worked = len(adj_pack.get('remaining_off_days') or [])
        off_day_extra_pay = _off_day_attendance_extra(
            daily_rate, off_days_worked, payroll_settings, decimal_places
        )
        bonus_amount = _m(bonus_amounts.get(int(employee_id), 0.0)) if _payroll_flag_on(payroll_settings, 'enable_bonuses') else 0.0

        allowances = lump_allowances
        custom_deductions = lump_deductions
        pay_lines: List[Dict[str, Any]] = []
        if _payroll_flag_on(payroll_settings, 'enable_named_components'):
            named_earn, named_deduct, named_lines = _named_component_totals(
                extra_maps['assignments'],
                extra_maps['types'],
                basic_salary,
                eligible_end,
                payroll_settings,
                decimal_places,
                employee=employee,
                organization_id=organization_id or 'DEFAULT_ORG',
                branch_id=branch_id or 'DEFAULT_BRANCH',
                working_days=float(month_scheduled_workdays or 0),
                overtime_hours=float(present_days + late_days) * 8.0,
            )
            pay_lines.extend(named_lines)
            if any(item.get('kind') == 'earning' and item.get('category') != 'bonus' for item in named_lines):
                allowances = _m(sum(item['amount'] for item in named_lines if item.get('kind') == 'earning' and item.get('category') != 'bonus'))
            if any(item.get('kind') == 'deduction' for item in named_lines):
                custom_deductions = named_deduct
            if _payroll_flag_on(payroll_settings, 'enable_bonuses'):
                bonus_amount = _m(bonus_amount + sum(item['amount'] for item in named_lines if item.get('category') == 'bonus'))
        pay_lines.extend(adj_pack.get('lines') or [])
        adjustment_amount = _m(adj_pack.get('extra_earn') or 0)
        arrears_amount = _m(adj_pack.get('arrears') or 0)
        custom_deductions = _m(custom_deductions + (adj_pack.get('extra_deduct') or 0))

        # Absence rule deduction
        matching_rules = [rule for rule in active_rules
                  if rule.get('rule_basis', 'absence') == 'absence'
                          if unexcused_absent_days >= rule['min_absent_days']
                          and (rule['max_absent_days'] is None or unexcused_absent_days <= rule['max_absent_days'])]
        if len(matching_rules) > 1:
            raise RuntimeError(f"Multiple active payroll rules match {unexcused_absent_days} absences for employee #{employee_id}")
        matched_rule = matching_rules[0] if matching_rules else None
        absence_rules_configured = any(
            (rule.get('rule_basis') or 'absence') == 'absence' for rule in active_rules
        )
        rule_missing = unexcused_absent_days > 0 and matched_rule is None and absence_rules_configured
        deduction_type = matched_rule['deduction_type'] if matched_rule else 'percentage'
        deduction_percent = float(matched_rule['deduction_percent']) if matched_rule and deduction_type == 'percentage' else None
        fixed_amount = float(matched_rule['fixed_amount']) if matched_rule and deduction_type == 'fixed' else 0.0
        absence_rule_deduction = 0.0 if unexcused_absent_days == 0 else round(
            basic_salary * deduction_percent / 100 if deduction_type == 'percentage' and deduction_percent is not None else fixed_amount,
            2,
        )
        matching_half_leave_rules = [rule for rule in active_rules
                                     if rule.get('rule_basis', 'absence') == 'half_leave'
                                     and half_leave_count >= rule['min_absent_days']
                                     and (rule['max_absent_days'] is None or half_leave_count <= rule['max_absent_days'])]
        if len(matching_half_leave_rules) > 1:
            raise RuntimeError(f"Multiple active payroll rules match {half_leave_count} half-leave occurrences for employee #{employee_id}")
        matched_half_leave_rule = matching_half_leave_rules[0] if matching_half_leave_rules else None
        half_leave_deduction = 0.0
        if matched_half_leave_rule:
            half_leave_deduction = round(
                basic_salary * float(matched_half_leave_rule['deduction_percent']) / 100
                if matched_half_leave_rule['deduction_type'] == 'percentage'
                else float(matched_half_leave_rule['fixed_amount']),
                2,
            )
        unpaid_leave_deduction = round(basic_salary * unpaid_leave_days / month_scheduled_workdays, 2) if deduct_unpaid_leave and month_scheduled_workdays else 0.0
        attendance_shortfall = 0.0
        issues_remarks = ''
        if attendance_mode == 'payable_days':
            earn_divisor = float(month_scheduled_workdays or 0)
            earn_factor = (float(payable_days) / earn_divisor) if earn_divisor else 0.0
            attendance_shortfall = _m((basic_salary + allowances) * (1.0 - earn_factor))
            absence_rule_deduction = 0.0
            half_leave_deduction = 0.0
            unpaid_leave_deduction = 0.0
            matched_rule = None
            deduction_percent = None
            rule_missing = False
            issues_remarks = 'Rule not applied (pay method: payable days)'
        attendance_deduction = round(absence_rule_deduction + unpaid_leave_deduction + half_leave_deduction + attendance_shortfall, 2)
        deduction_amount = round(attendance_deduction + custom_deductions, 2)

        # Gross = Basic + Allowances + Approved Overtime + Excel bonus + rest-day extra + adjustments/arrears
        gross_salary = _m(
            basic_salary + allowances + overtime_amount + bonus_amount + off_day_extra_pay + adjustment_amount
        )
        total_emp_deductions = deduction_amount
        net_salary = round(max(0.0, gross_salary - total_emp_deductions), 2)

        if attendance_mode == 'payable_days':
            matched_rule_label = f'Pay {int(payable_days)}/{int(month_scheduled_workdays)} working days'
        elif matched_rule:
            max_absent = matched_rule['max_absent_days']
            matched_rule_label = (
                f"{matched_rule['min_absent_days']}+ absent days" if max_absent is None
                else f"{matched_rule['min_absent_days']} absent days" if max_absent == matched_rule['min_absent_days']
                else f"{matched_rule['min_absent_days']}-{max_absent} absent days"
            )
        else:
            matched_rule_label = 'No absence deduction' if unexcused_absent_days == 0 else 'No matching rule'
        paid_leave_days = approved_leave_days
        total_leave_days = paid_leave_days + unpaid_leave_days
        package = basic_salary + allowances
        earn_div = float(month_scheduled_workdays or 0)
        earned_so_far = _m(package * (float(payable_days) / earn_div) if earn_div else 0.0)
        remaining_days = max(0, int(month_scheduled_workdays or 0) - int(scheduled_workdays or 0))
        projected_payable = min(int(month_scheduled_workdays or 0), int(payable_days or 0) + remaining_days)
        projected_month_end = _m(package * (float(projected_payable) / earn_div) if earn_div else 0.0)
        rows.append({
            'employee_id': employee_id,
            'user_id': employee_id,
            'employee_name': employee['employee_name'],
            'department': employee['department'],
            'designation': employee['designation'] or 'Staff Member',
            'basic_salary': basic_salary,
            'allowances': allowances,
            'custom_deductions': custom_deductions,
            'overtime_hours': ot_hours,
            'overtime_rate': ot_rate,
            'overtime_amount': overtime_amount,
            'overtime_breakdown': overtime_breakdown,
            'bonus_amount': bonus_amount,
            'off_day_extra_pay': off_day_extra_pay,
            'off_days_worked': off_days_worked,
            'adjustment_amount': adjustment_amount,
            'arrears_amount': arrears_amount,
            'pay_lines': pay_lines,
            'gross_salary': gross_salary,
            'scheduled_workdays': scheduled_workdays,
            'shift_details': shift_details,
            'calendar_details': calendar_details,
            'present_days': present_days,
            'half_leave_count': half_leave_count,
            'half_leave_deduction': half_leave_deduction,
            'matched_half_leave_rule': (
                f"{matched_half_leave_rule['min_absent_days']}+ half-leave occurrences"
                if matched_half_leave_rule and matched_half_leave_rule['max_absent_days'] is None
                else f"{matched_half_leave_rule['min_absent_days']}-{matched_half_leave_rule['max_absent_days']} half-leave occurrences"
                if matched_half_leave_rule else 'No half-leave deduction'
            ),
            'absent_days': absent_days,
            'late_days': late_days,
            'approved_leave_days': approved_leave_days,
            'paid_leave_days': paid_leave_days,
            'unpaid_leave_days': unpaid_leave_days,
            'unpaid_leave_deduction': unpaid_leave_deduction,
            'deduct_unpaid_leave': deduct_unpaid_leave,
            'total_leave_days': total_leave_days,
            'leave_days': total_leave_days,
            'payable_days': payable_days,
            'month_scheduled_workdays': month_scheduled_workdays,
            'attendance_pay_mode': attendance_mode,
            'attendance_shortfall': attendance_shortfall,
            'shift_history_missing': shift_history_missing,
            'applied_rule_id': matched_rule['id'] if matched_rule else None,
            'matched_rule': matched_rule_label,
            'deduction_type': deduction_type,
            'deduction_percent': deduction_percent,
            'fixed_amount': fixed_amount,
            'absence_deduction': absence_rule_deduction,
            'deduction_amount': deduction_amount,
            'rule_missing': rule_missing,
            'issues_remarks': issues_remarks,
            'earned_so_far': earned_so_far,
            'projected_month_end': projected_month_end,
            'net_salary': net_salary,
            'status': 'Missing shift history' if shift_history_missing else (
                'Missing salary' if basic_salary <= 0 else ('Missing payroll rule' if rule_missing else 'Draft')
            ),
        })

    preview = {
        'payroll_month': payroll_month,
        'period_start': month_start.isoformat(),
        'period_end': period_end.isoformat(),
        'status': 'Draft',
        'is_finalized': False,
        'month_complete': month_complete,
        'can_finalize': month_complete,
        'attendance_pay_mode': attendance_mode,
        'employee_count': len(rows),
        'total_basic_salary': round(sum(row['basic_salary'] for row in rows), 2),
        'total_allowances': round(sum(row['allowances'] for row in rows), 2),
        'total_overtime': round(sum(row['overtime_amount'] for row in rows), 2),
        'total_bonus': round(sum(row.get('bonus_amount', 0) for row in rows), 2),
        'total_off_day_extra': round(sum(row.get('off_day_extra_pay', 0) for row in rows), 2),
        'total_adjustments': round(sum(row.get('adjustment_amount', 0) for row in rows), 2),
        'total_arrears': round(sum(row.get('arrears_amount', 0) for row in rows), 2),
        'total_deduction': round(sum(row['deduction_amount'] for row in rows), 2),
        'total_net_salary': round(sum(row['net_salary'] for row in rows), 2),
        'absence_rules_configured': absence_rules_configured,
        'notices': payroll_notices,
        'earned_so_far_label': f'Earned so far (through {period_end.isoformat()})',
        'projected_month_end_label': 'Projected month-end',
        'deactivated_policy': 'Included only when exit_date is set and on or after the month start. Deactivated with no exit date are excluded and listed in notices.',
        'records': rows,
    }
    if payroll_notices:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                _write_payroll_audit(
                    conn.cursor(),
                    'full_salary_no_absence_rules',
                    organization_id,
                    branch_id,
                    payroll_month,
                    'system',
                    {
                        'notice': payroll_notices[0],
                        'attendance_pay_mode': attendance_mode,
                        'behavior': 'honored_saved_full_salary_with_no_absence_deduction',
                    },
                )
                conn.commit()
        except Exception:
            logger.exception('Failed to write full_salary notice audit')
    if rows:
        preview['payroll_run_id'] = _persist_draft_monthly_payroll(
            preview, organization_id=organization_id, branch_id=branch_id
        )
    else:
        preview['payroll_run_id'] = None
    finalized_run = get_monthly_payroll(
        payroll_month, organization_id=organization_id, branch_id=branch_id
    )
    if finalized_run:
        return finalized_run
    if month is not None and payroll_month.endswith(f"-{int(month):02d}"):
        return rows
    return preview


def _insert_payroll_records(cursor: sqlite3.Cursor, run_id: int, rows: List[Dict[str, Any]], finalized: bool, organization_id: str = 'DEFAULT_ORG', branch_id: Optional[str] = None) -> None:
    record_status = 'Finalized' if finalized else None
    cursor.executemany('''
        INSERT INTO payroll_records (
            payroll_run_id, employee_id, employee_name, department, designation,
            basic_salary, allowances, custom_deductions, overtime_hours, overtime_rate,
            overtime_amount, overtime_breakdown, bonus_amount, off_day_extra_pay,
            pay_lines, arrears_amount, adjustment_amount, gross_salary,
            scheduled_workdays, present_days, absent_days,
            late_days, approved_leave_days, paid_leave_days, unpaid_leave_days, total_leave_days,
            unpaid_leave_deduction,
            payable_days, applied_rule_id, matched_rule,
            shift_details, calendar_details, deduction_type, deduction_percent,
            fixed_amount, deduction_amount, net_salary, status, organization_id, branch_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', [(
        run_id, row['employee_id'], row['employee_name'], row['department'], row['designation'],
        row.get('basic_salary', 0.0), row.get('allowances', 0.0), row.get('custom_deductions', 0.0),
        row.get('overtime_hours', 0.0), row.get('overtime_rate', 0.0), row.get('overtime_amount', 0.0),
        json.dumps(row.get('overtime_breakdown', [])),
        row.get('bonus_amount', 0.0), row.get('off_day_extra_pay', 0.0),
        json.dumps(row.get('pay_lines') or []),
        row.get('arrears_amount', 0.0), row.get('adjustment_amount', 0.0),
        row.get('gross_salary', row.get('basic_salary', 0.0)),
        row['scheduled_workdays'], row['present_days'], row['absent_days'],
        row['late_days'], row['approved_leave_days'], row.get('paid_leave_days', row['approved_leave_days']),
        row.get('unpaid_leave_days', 0), row.get('total_leave_days', row['approved_leave_days'] + row.get('unpaid_leave_days', 0)),
        row.get('unpaid_leave_deduction', 0.0),
        row['payable_days'], row.get('applied_rule_id'),
        row.get('matched_rule', ''), json.dumps(row.get('shift_details', [])),
        json.dumps(row.get('calendar_details', [])), row.get('deduction_type', 'percentage'),
        row.get('deduction_percent'), row.get('fixed_amount', 0), row.get('deduction_amount', 0),
        row['net_salary'], record_status or row.get('status', 'Draft'),
        organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH'
    ) for row in rows])


def _persist_draft_monthly_payroll(preview: Dict[str, Any], organization_id: str = 'DEFAULT_ORG', branch_id: Optional[str] = None) -> Optional[int]:
    _sanitize_orphan_payroll_records()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """SELECT id, status, COALESCE(organization_id, 'DEFAULT_ORG') AS organization_id,
                      COALESCE(branch_id, 'DEFAULT_BRANCH') AS branch_id
               FROM payroll_runs
               WHERE payroll_month = ?
                 AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
            (preview['payroll_month'], organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH'),
        )
        existing = cursor.fetchone()
        if existing and existing[1] == 'Finalized':
            return existing[0]
        if existing:
            run_id = existing[0]
            cursor.execute('''
                UPDATE payroll_runs
                SET period_start = ?, period_end = ?, employee_count = ?, total_basic_salary = ?,
                    total_allowances = ?, total_overtime = ?, total_deduction = ?, total_net_salary = ?,
                    status = 'Draft', finalized_at = NULL, organization_id = ?, branch_id = ?
                WHERE id = ?
            ''', (
                preview['period_start'], preview['period_end'], preview['employee_count'],
                preview['total_basic_salary'], preview.get('total_allowances', 0.0), preview.get('total_overtime', 0.0),
                preview['total_deduction'], preview['total_net_salary'], organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH', run_id
            ))
            cursor.execute("DELETE FROM payroll_records WHERE payroll_run_id = ?", (run_id,))
        else:
            cursor.execute('''
                INSERT INTO payroll_runs (
                    payroll_month, period_start, period_end, employee_count,
                    total_basic_salary, total_allowances, total_overtime, total_deduction, total_net_salary,
                    status, finalized_at, organization_id, branch_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft', NULL, ?, ?)
            ''', (
                preview['payroll_month'], preview['period_start'], preview['period_end'],
                preview['employee_count'], preview['total_basic_salary'], preview.get('total_allowances', 0.0),
                preview.get('total_overtime', 0.0), preview['total_deduction'],
                preview['total_net_salary'], organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH'
            ))
            run_id = cursor.lastrowid
        if run_id is None:
            raise RuntimeError("Failed to create payroll draft")
        _insert_payroll_records(cursor, run_id, preview.get('records', []), finalized=False, organization_id=organization_id, branch_id=branch_id)
        conn.commit()
        return run_id


def get_payroll_history(
    limit: int = 24,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List persisted monthly payroll runs without recalculating them, filtered by tenant."""
    safe_limit = max(1, min(int(limit), 120))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if organization_id:
            query = '''
                SELECT id, payroll_month, period_start, period_end, employee_count,
                       total_basic_salary, total_allowances, total_overtime, total_deduction, total_net_salary, status,
                       created_at, finalized_at, organization_id, branch_id
                FROM payroll_runs
                WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
            '''
            params: List[Any] = [organization_id]
            if branch_id:
                query += " AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?"
                params.append(branch_id)
            query += " ORDER BY payroll_month DESC, id DESC LIMIT ?"
            params.append(safe_limit)
            cursor.execute(query, params)
        else:
            cursor.execute('''
                SELECT id, payroll_month, period_start, period_end, employee_count,
                       total_basic_salary, total_allowances, total_overtime, total_deduction, total_net_salary, status,
                       created_at, finalized_at, organization_id, branch_id
                FROM payroll_runs
                ORDER BY payroll_month DESC, id DESC
                LIMIT ?
            ''', (safe_limit,))
        return [dict(row) for row in cursor.fetchall()]


def get_monthly_payroll(
    payroll_month: str,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Load a finalized payroll snapshot for a month, if one exists for the specified tenant."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if organization_id:
            query = """SELECT * FROM payroll_runs
                       WHERE payroll_month = ? AND status = 'Finalized'
                         AND COALESCE(organization_id, 'DEFAULT_ORG') = ?"""
            params: List[Any] = [payroll_month, organization_id]
            if branch_id:
                query += " AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?"
                params.append(branch_id)
            cursor.execute(query, params)
        else:
            cursor.execute("SELECT * FROM payroll_runs WHERE payroll_month = ? AND status = 'Finalized'", (payroll_month,))
        run = cursor.fetchone()
        if not run:
            return None
        result = dict(run)
        records_query = "SELECT * FROM payroll_records WHERE payroll_run_id = ?"
        records_params: List[Any] = [run['id']]
        if organization_id:
            records_query += " AND COALESCE(organization_id, 'DEFAULT_ORG') = ?"
            records_params.append(organization_id)
        if branch_id:
            records_query += " AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?"
            records_params.append(branch_id)
        records_query += " ORDER BY department COLLATE NOCASE, employee_name COLLATE NOCASE"
        cursor.execute(records_query, records_params)
        result['records'] = []
        for row in cursor.fetchall():
            record = dict(row)
            try:
                record['overtime_breakdown'] = json.loads(record.get('overtime_breakdown') or '[]')
            except (TypeError, ValueError):
                record['overtime_breakdown'] = []
            try:
                record['pay_lines'] = json.loads(record.get('pay_lines') or '[]')
            except (TypeError, ValueError):
                record['pay_lines'] = []
            record['paid_leave_days'] = int(record.get('paid_leave_days') or record.get('approved_leave_days') or 0)
            record['unpaid_leave_days'] = int(record.get('unpaid_leave_days') or 0)
            record['total_leave_days'] = int(record.get('total_leave_days') or (record['paid_leave_days'] + record['unpaid_leave_days']))
            result['records'].append(record)
    result['is_finalized'] = True
    result['total_allowances'] = round(sum(float(row.get('allowances') or 0) for row in result['records']), 2)
    result['total_overtime'] = round(sum(float(row.get('overtime_amount') or 0) for row in result['records']), 2)
    result['total_bonus'] = round(sum(float(row.get('bonus_amount') or 0) for row in result['records']), 2)
    result['total_off_day_extra'] = round(sum(float(row.get('off_day_extra_pay') or 0) for row in result['records']), 2)
    result['total_deduction'] = round(sum(float(row.get('deduction_amount') or 0) for row in result['records']), 2)
    result['total_net_salary'] = round(sum(float(row.get('net_salary') or 0) for row in result['records']), 2)
    return result


def get_employee_payslip(
    payroll_month: str,
    employee_id: int,
    organization_id: str = 'DEFAULT_ORG',
    branch_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return one employee's payroll line as a payslip from the current month run."""
    payroll = get_monthly_payroll(
        payroll_month, organization_id=organization_id, branch_id=branch_id
    ) or calculate_monthly_payroll(
        payroll_month, organization_id=organization_id, branch_id=branch_id
    )
    if isinstance(payroll, list):
        return None
    settings = get_payroll_settings(organization_id=organization_id, branch_id=branch_id)
    record = next(
        (row for row in payroll.get('records', []) if int(row.get('employee_id') or row.get('user_id') or 0) == int(employee_id)),
        None,
    )
    if not record:
        return None
    return {
        'payroll_month': payroll.get('payroll_month'),
        'period_start': payroll.get('period_start'),
        'period_end': payroll.get('period_end'),
        'status': payroll.get('status'),
        'is_finalized': payroll.get('is_finalized'),
        'currency_code': settings.get('currency_code') or DEFAULT_CURRENCY,
        'finalized_at': payroll.get('finalized_at'),
        'finalized_by': payroll.get('finalized_by'),
        'employee': record,
        'gross_salary': record.get('gross_salary'),
        'deductions': {
            'absence': record.get('absence_deduction') or 0,
            'unpaid_leave': record.get('unpaid_leave_deduction') or 0,
            'half_leave': record.get('half_leave_deduction') or 0,
            'other': record.get('custom_deductions') or 0,
            'total': record.get('deduction_amount') or 0,
        },
        'net_salary': record.get('net_salary'),
    }


def export_monthly_payroll_excel(
    payroll_month: str,
    organization_id: str = 'DEFAULT_ORG',
    branch_id: Optional[str] = None,
) -> bytes:
    payroll = get_monthly_payroll(
        payroll_month, organization_id=organization_id, branch_id=branch_id
    ) or calculate_monthly_payroll(
        payroll_month, organization_id=organization_id, branch_id=branch_id
    )
    if isinstance(payroll, list):
        payroll = {'payroll_month': payroll_month, 'records': payroll, 'status': 'Draft'}
    settings = get_payroll_settings(organization_id=organization_id, branch_id=branch_id)
    return payroll_excel_bytes(payroll, settings.get('currency_code') or DEFAULT_CURRENCY)


def export_employee_payslip_pdf(
    payroll_month: str,
    employee_id: int,
    organization_id: str = 'DEFAULT_ORG',
    branch_id: Optional[str] = None,
) -> Optional[bytes]:
    payslip = get_employee_payslip(payroll_month, employee_id, organization_id, branch_id)
    if not payslip:
        return None
    return payslip_pdf_bytes(payslip)


def finalize_monthly_payroll(
    preview: Dict[str, Any],
    organization_id: str = 'DEFAULT_ORG',
    branch_id: Optional[str] = None,
    finalized_by: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist a reviewed payroll preview as an immutable monthly snapshot for tenant."""
    _sanitize_orphan_payroll_records()
    try:
        run_month = datetime.strptime(str(preview.get('payroll_month') or ''), "%Y-%m").date().replace(day=1)
        period_end_value = datetime.strptime(str(preview.get('period_end') or '')[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        run_month = None
        period_end_value = None
    if run_month is not None:
        if run_month.month == 12:
            run_month_end = run_month.replace(year=run_month.year + 1, month=1) - timedelta(days=1)
        else:
            run_month_end = run_month.replace(month=run_month.month + 1) - timedelta(days=1)
        if period_end_value is not None and period_end_value < run_month_end and not preview.get('finalize_early'):
            raise ValueError(
                f"This month is still in progress through {period_end_value.isoformat()}. "
                "Use finalize early (admin) or wait until month-end."
            )
    if any(row.get('shift_history_missing') for row in preview.get('records', [])):
        raise ValueError("Configure shift assignment history for every employee before finalizing payroll")
    if any(row['basic_salary'] <= 0 for row in preview.get('records', [])):
        raise ValueError("Set a basic salary for every active employee before finalizing payroll")
    if any(row.get('rule_missing') for row in preview.get('records', [])):
        missing_names = [row['employee_name'] for row in preview.get('records', []) if row.get('rule_missing')]
        raise ValueError("Configure active payroll rules for: " + ', '.join(missing_names))
    try:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                """SELECT id, status, COALESCE(organization_id, 'DEFAULT_ORG') AS organization_id,
                          COALESCE(branch_id, 'DEFAULT_BRANCH') AS branch_id
                   FROM payroll_runs
                   WHERE payroll_month = ?
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
                (preview['payroll_month'], organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH'),
            )
            existing = cursor.fetchone()
            if existing and existing[1] == 'Finalized':
                return None
            if existing:
                run_id = existing[0]
                cursor.execute('''
                    UPDATE payroll_runs
                    SET period_start = ?, period_end = ?, employee_count = ?, total_basic_salary = ?,
                        total_allowances = ?, total_overtime = ?, total_deduction = ?, total_net_salary = ?,
                        status = 'Draft', finalized_at = NULL, organization_id = ?, branch_id = ?
                    WHERE id = ?
                ''', (
                    preview['period_start'], preview['period_end'], preview['employee_count'],
                    preview['total_basic_salary'], preview.get('total_allowances', 0.0), preview.get('total_overtime', 0.0),
                    preview['total_deduction'], preview['total_net_salary'], organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH', run_id
                ))
                cursor.execute("DELETE FROM payroll_records WHERE payroll_run_id = ?", (run_id,))
            else:
                cursor.execute('''
                    INSERT INTO payroll_runs (
                        payroll_month, period_start, period_end, employee_count,
                        total_basic_salary, total_allowances, total_overtime, total_deduction, total_net_salary,
                        status, finalized_at, organization_id, branch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft', NULL, ?, ?)
                ''', (
                    preview['payroll_month'], preview['period_start'], preview['period_end'],
                    preview['employee_count'], preview['total_basic_salary'], preview.get('total_allowances', 0.0),
                    preview.get('total_overtime', 0.0), preview['total_deduction'],
                    preview['total_net_salary'], organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH'
                ))
                run_id = cursor.lastrowid
            if run_id is None:
                raise RuntimeError("Failed to create payroll run")
            _insert_payroll_records(cursor, run_id, preview.get('records', []), finalized=False, organization_id=organization_id, branch_id=branch_id)
            cursor.execute("UPDATE payroll_records SET status = 'Finalized' WHERE payroll_run_id = ?", (run_id,))
            cursor.execute('''
                UPDATE payroll_runs SET status = 'Finalized', finalized_at = CURRENT_TIMESTAMP, finalized_by = ?
                WHERE id = ?
            ''', (finalized_by, run_id,))
            _write_payroll_audit(cursor, 'payroll_finalized', organization_id, branch_id, preview.get('payroll_month'), finalized_by, {
                'run_id': run_id,
                'employee_count': preview.get('employee_count'),
                'total_net_salary': preview.get('total_net_salary'),
                'excluded': preview.get('excluded') or [],
                'finalize_early': bool(preview.get('finalize_early')),
            })
            conn.commit()
        return get_monthly_payroll(
            preview['payroll_month'],
            organization_id=organization_id,
            branch_id=branch_id,
        )
    except Exception as e:
        logger.error(f"Failed to finalize payroll for {preview.get('payroll_month')}: {e}")
        raise


def reopen_monthly_payroll(
    payroll_month: str,
    reason: str,
    organization_id: str = 'DEFAULT_ORG',
    branch_id: Optional[str] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    if not str(reason or '').strip():
        raise ValueError('A reopen reason is required')
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        row = cursor.execute(
            """SELECT * FROM payroll_runs
               WHERE payroll_month = ? AND status = 'Finalized'
                 AND COALESCE(organization_id,'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id,'DEFAULT_BRANCH') = ?""",
            (payroll_month, org, branch),
        ).fetchone()
        if not row:
            raise ValueError(f'No finalized payroll for {payroll_month}')
        cursor.execute(
            "UPDATE payroll_runs SET status = 'Reopened' WHERE id = ?",
            (row['id'],),
        )
        _write_payroll_audit(cursor, 'payroll_reopened', org, branch, payroll_month, actor or 'Admin', {
            'run_id': row['id'], 'reason': reason.strip(), 'old_status': 'Finalized',
        })
        conn.commit()
        return {'success': True, 'previous_run_id': row['id'], 'status': 'Reopened'}


# ============================================
# BRANCH SETTINGS & SYSTEM CONFIGURATION
# ============================================

def get_branch_settings() -> Dict:
    """Retrieve system branch, company, admin display name, and location settings."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM branch_settings WHERE id = 1")
            row = cursor.fetchone()
            if row:
                res = dict(row)
                if not res.get('admin_name'):
                    res['admin_name'] = 'Mr. Junaid'
                return res
    except Exception as e:
        logger.error(f"Failed to fetch branch settings: {e}")
    return {
        'id': 1,
        'branch_name': '',
        'city': '',
        'company_name': '',
        'admin_name': 'Mr. Junaid',
        'address': '',
        'phone': '',
        'email': ''
    }

def update_branch_settings(branch_name: str, city: str, company_name: str = '', address: str = '', phone: str = '', email: str = '', admin_name: str = 'Mr. Junaid') -> bool:
    """Update branch and organization settings."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO branch_settings (id, branch_name, city, company_name, address, phone, email, admin_name, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                branch_name=excluded.branch_name,
                city=excluded.city,
                company_name=excluded.company_name,
                address=excluded.address,
                phone=excluded.phone,
                email=excluded.email,
                admin_name=excluded.admin_name,
                updated_at=CURRENT_TIMESTAMP
            ''', (branch_name.strip(), city.strip(), company_name.strip(), address.strip(), phone.strip(), email.strip(), admin_name.strip() or 'Mr. Junaid'))
            conn.commit()
            logger.info(f"✓ Branch settings updated: {branch_name} ({city}) | Admin: {admin_name}")
            return True
    except Exception as e:
        logger.error(f"Failed to update branch settings: {e}")
        return False


# ============================================
# DEPARTMENT MANAGEMENT
# ============================================

def get_all_departments_list(active_only: bool = False, organization_id: Optional[str] = None) -> List[Dict]:
    """Retrieve all departments with registered employee counts and designation counts, scoped by organization."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            conditions = []
            params = []
            if active_only:
                conditions.append("d.active = 1")
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    conditions.append("(d.organization_id = ? OR d.organization_id IS NULL OR d.organization_id = '')")
                else:
                    conditions.append("d.organization_id = ?")
                params.append(organization_id)
            where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            
            user_org_clause = ""
            user_params = []
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    user_org_clause = "AND (u.organization_id = ? OR u.organization_id IS NULL OR u.organization_id = '')"
                else:
                    user_org_clause = "AND u.organization_id = ?"
                user_params.append(organization_id)

            cursor.execute(f'''
                SELECT d.id, d.name, d.code, d.description, d.active, d.created_at, d.organization_id, d.branch_id,
                       COUNT(DISTINCT u.id) as staff_count,
                       COUNT(DISTINCT des.id) as designation_count
                FROM departments d
                LEFT JOIN users u ON u.department = d.name AND u.active = 1 {user_org_clause}
                LEFT JOIN designations des ON des.department_id = d.id AND des.active = 1
                {where_clause}
                GROUP BY d.id
                ORDER BY d.name ASC
            ''', user_params + params)
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch departments list: {e}")
        raise

def add_department(name: str, code: str = '', description: str = '', organization_id: str = 'DEFAULT_ORG', branch_id: Optional[str] = None) -> Optional[int]:
    """Add a new department scoped to organization."""
    if not name or not name.strip():
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO departments (name, code, description, organization_id, branch_id) VALUES (?, ?, ?, ?, ?)",
                (name.strip(), code.strip(), description.strip(), organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH')
            )
            conn.commit()
            dept_id = cursor.lastrowid
            logger.info(f"✓ Department '{name}' created for org '{organization_id}' (ID: {dept_id})")
            return dept_id
    except sqlite3.IntegrityError:
        logger.warning(f"Department '{name}' already exists")
        return None
    except Exception as e:
        logger.error(f"Failed to add department: {e}")
        raise

def update_department(dept_id: int, name: str, code: str = '', description: str = '', active: int = 1, organization_id: Optional[str] = None) -> bool:
    """Update department information with tenant check and sync assigned user records."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT name FROM departments WHERE id = ? AND (organization_id = ? OR organization_id IS NULL OR organization_id = '')", (dept_id, organization_id))
                else:
                    cursor.execute("SELECT name FROM departments WHERE id = ? AND organization_id = ?", (dept_id, organization_id))
            else:
                cursor.execute("SELECT name FROM departments WHERE id = ?", (dept_id,))
            row = cursor.fetchone()
            if not row:
                logger.warning(f"Department ID {dept_id} not found or tenant unauthorized")
                return False
            old_name = row[0]
            
            cursor.execute('''
                UPDATE departments
                SET name = ?, code = ?, description = ?, active = ?
                WHERE id = ?
            ''', (name.strip(), code.strip(), description.strip(), active, dept_id))
            
            if old_name and old_name != name.strip():
                if organization_id:
                    cursor.execute("UPDATE users SET department = ? WHERE department = ? AND organization_id = ?", (name.strip(), old_name, organization_id))
                else:
                    cursor.execute("UPDATE users SET department = ? WHERE department = ?", (name.strip(), old_name))
                
            conn.commit()
            logger.info(f"✓ Department ID {dept_id} updated: {name}")
            return True
    except Exception as e:
        logger.error(f"Failed to update department ID {dept_id}: {e}")
        return False

def delete_department(dept_id: int, organization_id: Optional[str] = None) -> bool:
    """Soft delete / disable department with tenant check."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT id FROM departments WHERE id = ? AND (organization_id = ? OR organization_id IS NULL OR organization_id = '')", (dept_id, organization_id))
                else:
                    cursor.execute("SELECT id FROM departments WHERE id = ? AND organization_id = ?", (dept_id, organization_id))
                if not cursor.fetchone():
                    logger.warning(f"Department ID {dept_id} not found or tenant unauthorized")
                    return False
            cursor.execute("UPDATE departments SET active = 0 WHERE id = ?", (dept_id,))
            conn.commit()
            logger.info(f"✓ Department ID {dept_id} disabled.")
            return True
    except Exception as e:
        logger.error(f"Failed to delete department ID {dept_id}: {e}")
        return False


def get_department(dept_id: int, organization_id: Optional[str] = None) -> Optional[Dict]:
    """Retrieve single department by id with tenant check."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT id, name, code, description, active, organization_id, branch_id FROM departments WHERE id = ? AND (organization_id = ? OR organization_id IS NULL OR organization_id = '')", (dept_id, organization_id))
                else:
                    cursor.execute("SELECT id, name, code, description, active, organization_id, branch_id FROM departments WHERE id = ? AND organization_id = ?", (dept_id, organization_id))
            else:
                cursor.execute("SELECT id, name, code, description, active, organization_id, branch_id FROM departments WHERE id = ?", (dept_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Failed to fetch department ID {dept_id}: {e}")
        return None


# ============================================
# DESIGNATION MANAGEMENT
# ============================================

def get_designations(department_id: Optional[int] = None, department_name: Optional[str] = None, active_only: bool = False, organization_id: Optional[str] = None) -> List[Dict]:
    """Retrieve designations filtered by department, active status, and organization."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = '''
                SELECT des.id, des.department_id, des.title, des.description, des.active, des.created_at,
                       des.organization_id, des.branch_id,
                       d.name as department_name
                FROM designations des
                JOIN departments d ON des.department_id = d.id
                WHERE 1=1
            '''
            params = []
            if department_id:
                query += " AND des.department_id = ?"
                params.append(department_id)
            if department_name:
                query += " AND d.name = ?"
                params.append(department_name.strip())
            if active_only:
                query += " AND des.active = 1 AND d.active = 1"
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    query += " AND (des.organization_id = ? OR des.organization_id IS NULL OR des.organization_id = '')"
                else:
                    query += " AND des.organization_id = ?"
                params.append(organization_id)
            query += " ORDER BY des.title ASC"
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch designations: {e}")
        return []

def add_designation(department_id: int, title: str, description: str = '', organization_id: str = 'DEFAULT_ORG', branch_id: Optional[str] = None) -> Optional[int]:
    """Add a new designation under a specific department, scoped by tenant."""
    if not title or not title.strip() or not department_id:
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO designations (department_id, title, description, organization_id, branch_id) VALUES (?, ?, ?, ?, ?)",
                (department_id, title.strip(), description.strip(), organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH')
            )
            conn.commit()
            desig_id = cursor.lastrowid
            logger.info(f"✓ Designation '{title}' created under Department ID {department_id} (ID: {desig_id})")
            return desig_id
    except sqlite3.IntegrityError as e:
        logger.warning(f"Integrity error adding designation '{title}' under dept {department_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to add designation: {e}")
        return None

def update_designation(desig_id: int, title: str, description: str = '', active: int = 1, organization_id: Optional[str] = None) -> bool:
    """Update designation title or active status with tenant check."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT id FROM designations WHERE id = ? AND (organization_id = ? OR organization_id IS NULL OR organization_id = '')", (desig_id, organization_id))
                else:
                    cursor.execute("SELECT id FROM designations WHERE id = ? AND organization_id = ?", (desig_id, organization_id))
                if not cursor.fetchone():
                    logger.warning(f"Designation ID {desig_id} not found or tenant unauthorized")
                    return False
            cursor.execute(
                "UPDATE designations SET title = ?, description = ?, active = ? WHERE id = ?",
                (title.strip(), description.strip(), active, desig_id)
            )
            conn.commit()
            logger.info(f"✓ Designation ID {desig_id} updated: {title}")
            return True
    except Exception as e:
        logger.error(f"Failed to update designation ID {desig_id}: {e}")
        return False

def delete_designation(desig_id: int, organization_id: Optional[str] = None) -> bool:
    """Soft delete / disable designation with tenant check."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT id FROM designations WHERE id = ? AND (organization_id = ? OR organization_id IS NULL OR organization_id = '')", (desig_id, organization_id))
                else:
                    cursor.execute("SELECT id FROM designations WHERE id = ? AND organization_id = ?", (desig_id, organization_id))
                if not cursor.fetchone():
                    logger.warning(f"Designation ID {desig_id} not found or tenant unauthorized")
                    return False
            cursor.execute("UPDATE designations SET active = 0 WHERE id = ?", (desig_id,))
            conn.commit()
            logger.info(f"✓ Designation ID {desig_id} disabled.")
            return True
    except Exception as e:
        logger.error(f"Failed to delete designation ID {desig_id}: {e}")
        return False


# ============================================
# DYNAMIC CAMERA MANAGEMENT
# ============================================

def get_all_cameras(active_only: bool = False, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict]:
    """Retrieve cameras filtered by active status, organization_id, and branch_id."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            conditions = []
            params = []
            if active_only:
                conditions.append("active = 1")
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    conditions.append("(organization_id = ? OR organization_id IS NULL OR organization_id = '')")
                else:
                    conditions.append("organization_id = ?")
                params.append(organization_id)
            if branch_id:
                conditions.append("branch_id = ?")
                params.append(branch_id)
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            cursor.execute(f"SELECT * FROM cameras {where} ORDER BY id ASC", params)
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch cameras: {e}")
        return []

def get_camera_by_id(camera_id: str, organization_id: Optional[str] = None) -> Optional[Dict]:
    """Get single camera by camera_id string or integer DB id, with optional tenant check."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            conditions = ["(camera_id = ? OR id = ?)"]
            params = [str(camera_id), str(camera_id)]
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    conditions.append("(organization_id = ? OR organization_id IS NULL OR organization_id = '')")
                else:
                    conditions.append("organization_id = ?")
                params.append(organization_id)
            where = "WHERE " + " AND ".join(conditions)
            cursor.execute(f"SELECT * FROM cameras {where}", params)
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Failed to fetch camera {camera_id}: {e}")
        return None

def add_camera(camera_id: str, name: str, room_name: str, camera_type: str = 'ip_camera', rtsp_url: str = '', organization_id: str = 'DEFAULT_ORG', branch_id: Optional[str] = None) -> Optional[int]:
    """Add a new camera configuration scoped to tenant organization and branch."""
    if not name or not camera_id or not room_name:
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT INTO cameras (camera_id, name, room_name, camera_type, rtsp_url, active, organization_id, branch_id)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)''',
                (camera_id.strip(), name.strip(), room_name.strip(), camera_type.strip(), rtsp_url.strip(), organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH')
            )
            conn.commit()
            cam_db_id = cursor.lastrowid
            logger.info(f"✓ Camera '{name}' ({camera_id}) added for room '{room_name}' (ID: {cam_db_id})")
            _cloud_mirror("mirror_camera_row", cam_db_id)
            return cam_db_id
    except sqlite3.IntegrityError:
        logger.warning(f"Camera ID '{camera_id}' already exists")
        return None
    except Exception as e:
        logger.error(f"Failed to add camera: {e}")
        return None

def update_camera(cam_db_id: int, camera_id: str, name: str, room_name: str, camera_type: str = 'ip_camera', rtsp_url: str = '', active: int = 1, organization_id: Optional[str] = None) -> bool:
    """Update camera configuration with tenant check."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT id FROM cameras WHERE id = ? AND (organization_id = ? OR organization_id IS NULL OR organization_id = '')", (cam_db_id, organization_id))
                else:
                    cursor.execute("SELECT id FROM cameras WHERE id = ? AND organization_id = ?", (cam_db_id, organization_id))
                if not cursor.fetchone():
                    logger.warning(f"Camera ID {cam_db_id} not found or tenant unauthorized")
                    return False
            cursor.execute(
                '''UPDATE cameras
                   SET camera_id = ?, name = ?, room_name = ?, camera_type = ?, rtsp_url = ?, active = ?
                   WHERE id = ?''',
                (camera_id.strip(), name.strip(), room_name.strip(), camera_type.strip(), rtsp_url.strip(), active, cam_db_id)
            )
            conn.commit()
            logger.info(f"✓ Camera DB ID {cam_db_id} updated: {name}")
            _cloud_mirror("mirror_camera_row", cam_db_id)
            return True
    except Exception as e:
        logger.error(f"Failed to update camera DB ID {cam_db_id}: {e}")
        return False

def delete_camera(cam_db_id: int, organization_id: Optional[str] = None) -> bool:
    """Delete camera configuration with tenant check."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("SELECT id FROM cameras WHERE id = ? AND (organization_id = ? OR organization_id IS NULL OR organization_id = '')", (cam_db_id, organization_id))
                else:
                    cursor.execute("SELECT id FROM cameras WHERE id = ? AND organization_id = ?", (cam_db_id, organization_id))
                if not cursor.fetchone():
                    logger.warning(f"Camera ID {cam_db_id} not found or tenant unauthorized")
                    return False
            cursor.execute("SELECT camera_id, organization_id FROM cameras WHERE id = ?", (cam_db_id,))
            cam_row = cursor.fetchone()
            cursor.execute("DELETE FROM cameras WHERE id = ?", (cam_db_id,))
            conn.commit()
            logger.info(f"✓ Camera DB ID {cam_db_id} deleted.")
            if cam_row:
                _cloud_mirror("delete_camera", cam_row[0], cam_row[1])
            return True
    except Exception as e:
        logger.error(f"Failed to delete camera DB ID {cam_db_id}: {e}")
        return False


def get_camera_room_mappings(active_only: bool = True, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> Dict[str, Dict]:
    """Return {camera_id: {camera_name, room_name, camera_type, description}} built from the cameras table."""
    mappings = {}
    for cam in get_all_cameras(active_only=active_only, organization_id=organization_id, branch_id=branch_id):
        mappings[cam['camera_id']] = {
            'camera_name': cam['name'],
            'room_name': cam['room_name'],
            'camera_type': cam['camera_type'],
            'description': f"{cam['camera_type'].upper().replace('_', ' ')} - {cam['room_name']}",
        }
    return mappings


# Automatically ensure schema and migrations are up-to-date on load.
# S18 restores a pre-migration fixture first; skip auto-init when asked.
if os.environ.get("QINTELLECT_SKIP_AUTO_INIT") != "1":
    try:
        init_db()
    except Exception as _e:
        logger.warning(f"Database auto-migration on import failed: {_e}")

if __name__ == '__main__':
    init_db()