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
    PAYSLIP_LETTERHEAD_FIELDS,
    DEFAULT_PRORATION_BASIS,
    DEFAULT_OT_ROUNDING,
    LEGACY_OT_ROUNDING,
    LEGACY_PRORATION_BASIS,
    VALID_PRORATION_BASES,
    VALID_OT_ROUNDING,
    normalize_ot_rounding as _normalize_ot_rounding,
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

# Universal safe SQLite connection wrapper ensuring generous timeouts and WAL mode
_orig_sqlite3_connect = sqlite3.connect

def _safe_sqlite3_connect(*args, **kwargs):
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 60.0
    conn = _orig_sqlite3_connect(*args, **kwargs)
    try:
        conn.execute("PRAGMA busy_timeout = 60000")
        conn.execute("PRAGMA journal_mode = WAL")
    except Exception:
        pass
    return conn

sqlite3.connect = _safe_sqlite3_connect



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


def postgres_is_configured() -> bool:
    try:
        import postgres_mirror as cloud_pg
        return cloud_pg.postgres_is_configured()
    except Exception:
        return False


def _cloud_mirror(action: str, *args, **kwargs) -> Any:
    """Best-effort replica to Railway Postgres. Never breaks local SQLite/AI."""
    try:
        import postgres_mirror as cloud_pg
        fn = getattr(cloud_pg, action, None)
        if fn:
            return fn(*args, **kwargs)
    except Exception as error:
        logger.warning("Cloud Postgres mirror skipped (%s): %s", action, error)
    return None

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
        with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA busy_timeout = 60000")
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
                ("face_trained", "INTEGER DEFAULT 0"),
                ("mobile_login", "INTEGER DEFAULT 0")
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
                ('start_time', 'TEXT'),
                ('end_time', 'TEXT'),
                ('paid_status', "TEXT DEFAULT 'pending'"),
                ('unpaid_status', "TEXT DEFAULT 'pending'"),
                ('rejection_reason', 'TEXT'),
                ('restored_dates', "TEXT DEFAULT ''"),
            ]:
                if column not in leave_request_columns:
                    cursor.execute(f"ALTER TABLE leave_requests ADD COLUMN {column} {column_type}")
                    if column in ('paid_status', 'unpaid_status'):
                        cursor.execute(f"UPDATE leave_requests SET {column} = COALESCE(status, 'pending') WHERE {column} IS NULL")

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
            ot_columns = {row[1] for row in cursor.execute("PRAGMA table_info(overtime)").fetchall()}
            for column, column_type in [
                ('rejection_reason', 'TEXT'),
                ('rejected_by', 'TEXT'),
                ('rejected_at', 'TEXT'),
                ('audit_trail', 'TEXT'),
            ]:
                if column not in ot_columns:
                    cursor.execute(f"ALTER TABLE overtime ADD COLUMN {column} {column_type}")


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
                ("is_prorated", "INTEGER NOT NULL DEFAULT 0"),
                ("unprorated_basic_salary", "REAL NOT NULL DEFAULT 0.0"),
                ("proration_days_paid", "INTEGER NOT NULL DEFAULT 0"),
                ("proration_method_days", "INTEGER NOT NULL DEFAULT 0"),
                ("proration_explanation", "TEXT DEFAULT ''"),
                ("days_paid", "REAL DEFAULT NULL"),
                ("missed_day_details", "TEXT DEFAULT ''"),
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
            if 'snapshot_json' not in payroll_run_columns:
                cursor.execute("ALTER TABLE payroll_runs ADD COLUMN snapshot_json TEXT")

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
                ("ot_rounding", "TEXT NOT NULL DEFAULT 'round_rate_first'"),
                ("payslip_company_name", "TEXT NOT NULL DEFAULT 'QIntellect Technologies'"),
                ("payslip_tagline", "TEXT NOT NULL DEFAULT 'DRIVING THE FUTURE TECHNOLOGY POWERED BY QUANTUM & AI'"),
                ("payslip_phone", "TEXT NOT NULL DEFAULT '+92 302 9633999'"),
                ("payslip_email", "TEXT NOT NULL DEFAULT 'info@qintellecttechnologies.com'"),
                ("payslip_website", "TEXT NOT NULL DEFAULT 'qintellecttechnologies.com'"),
                ("payslip_address", "TEXT NOT NULL DEFAULT 'Main Branch, Rahim Yar Khan'"),
                ("payslip_ntn", "TEXT NOT NULL DEFAULT 'J756903'"),
                ("payslip_footer", "TEXT NOT NULL DEFAULT '© 2026 QIntellect Technologies. All rights reserved.'"),
                ("payslip_powered", "TEXT NOT NULL DEFAULT 'Powered by QIntellect Technologies'"),
                ("payslip_bank_title", "TEXT NOT NULL DEFAULT 'QIntellect Technologies'"),
                ("payslip_bank_name", "TEXT NOT NULL DEFAULT 'United Bank Limited (UBL)'"),
                ("payslip_bank_iban", "TEXT NOT NULL DEFAULT 'PK46UNIL0109000400912116'"),
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
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_attendance_user_ts ON attendance(user_id, timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_attendance_ts ON attendance(timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_leave_requests_user_status ON leave_requests(user_id, status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_manual_absent_user_date ON manual_absent(user_id, date)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_shift_history_user ON employee_shift_history(user_id, effective_from)')
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
        try:
            from cloud_seed import ensure_cloud_mercury_seeded
            ensure_cloud_mercury_seeded(DB_PATH)
        except Exception as error:
            logger.warning("Cloud seed skipped: %s", error)
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


def get_user_by_name(name: str, organization_id: Optional[str] = None) -> Optional[Dict]:
    """Retrieve user by name with PostgreSQL authority when configured."""
    if postgres_is_configured():
        try:
            import postgres_mirror as cloud_pg
            pg_user = cloud_pg.fetch_pg_user_by_name(name, organization_id)
            if pg_user is False:
                return None
            if isinstance(pg_user, dict):
                return {
                    'id': pg_user.get('id'),
                    'name': pg_user.get('name'),
                    'email': pg_user.get('email'),
                    'shift_id': pg_user.get('shift_id', 1),
                    'checkin_cameras': pg_user.get('checkin_cameras', 'all'),
                    'checkout_cameras': pg_user.get('checkout_cameras', 'all'),
                    'organization_id': pg_user.get('organization_id', 'DEFAULT_ORG'),
                    'branch_id': pg_user.get('branch_id', 'DEFAULT_BRANCH'),
                }
        except Exception as e:
            logger.warning("Postgres unreachable for get_user_by_name; falling back to SQLite cache: %s", e)
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


def _live_camera_tokens(cursor: sqlite3.Cursor, organization_id: str, branch_id: Optional[str] = None) -> set:
    clauses = ["COALESCE(active, 1) = 1"]
    params: List[Any] = []
    if organization_id and organization_id != 'all':
        if organization_id == 'DEFAULT_ORG':
            clauses.append("(organization_id = 'DEFAULT_ORG' OR organization_id IS NULL OR organization_id = '')")
        else:
            clauses.append("organization_id = ?")
            params.append(organization_id)
    if branch_id and branch_id not in ('all', 'DEFAULT_BRANCH'):
        clauses.append("branch_id = ?")
        params.append(branch_id)
    cursor.execute(
        f"SELECT camera_id, name, room_name FROM cameras WHERE {' AND '.join(clauses)}",
        params,
    )
    tokens = set()
    for camera_id, name, room_name in cursor.fetchall():
        for value in (camera_id, name, room_name):
            text = str(value or '').strip().lower()
            if text:
                tokens.add(text)
    return tokens


def _camera_assignment_without_deleted(raw: str, live_tokens: set) -> str:
    text = str(raw or 'all').strip()
    if not text or text.lower() in ('all', '*'):
        return 'all'
    kept = [piece.strip() for piece in text.split(',') if piece.strip() and piece.strip().lower() in live_tokens]
    return ','.join(kept) if kept else 'all'


def heal_deleted_camera_assignments(organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> int:
    """Drop staff camera IDs that no longer exist so a replaced camera does not block attendance."""
    if not organization_id or organization_id == 'all':
        return 0
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            live_tokens = _live_camera_tokens(cursor, organization_id, branch_id)
            user_sql = "SELECT id, COALESCE(checkin_cameras, 'all'), COALESCE(checkout_cameras, 'all') FROM users WHERE active = 1"
            params: List[Any] = []
            if organization_id == 'DEFAULT_ORG':
                user_sql += " AND (organization_id = 'DEFAULT_ORG' OR organization_id IS NULL OR organization_id = '')"
            else:
                user_sql += " AND organization_id = ?"
                params.append(organization_id)
            if branch_id and branch_id not in ('all',):
                if branch_id == 'DEFAULT_BRANCH':
                    user_sql += " AND (branch_id = 'DEFAULT_BRANCH' OR branch_id IS NULL OR branch_id = '')"
                else:
                    user_sql += " AND branch_id = ?"
                    params.append(branch_id)
            changed = 0
            for user_id, checkin_raw, checkout_raw in cursor.execute(user_sql, params).fetchall():
                checkin = _camera_assignment_without_deleted(checkin_raw, live_tokens)
                checkout = _camera_assignment_without_deleted(checkout_raw, live_tokens)
                if checkin == str(checkin_raw or 'all') and checkout == str(checkout_raw or 'all'):
                    continue
                cursor.execute(
                    "UPDATE users SET checkin_cameras = ?, checkout_cameras = ? WHERE id = ?",
                    (checkin, checkout, user_id),
                )
                changed += 1
            if changed:
                conn.commit()
                logger.info(
                    "Cleared %s stale camera assignment(s) for %s/%s",
                    changed, organization_id, branch_id or '*',
                )
            return changed
    except Exception as error:
        logger.error("Failed to clear stale camera assignments: %s", error)
        return 0


def get_all_users(role: Optional[str] = None, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict]:
    """Get all active users with today's attendance status, shift details, and camera assignments via optimized join, strictly scoped by organization and branch."""
    pg_ids = None
    if organization_id and organization_id not in ('all', 'DEFAULT_ORG'):
        if postgres_is_configured():
            try:
                import postgres_mirror as cloud_pg
                if branch_id and branch_id not in ('all', 'DEFAULT_BRANCH'):
                    cloud_pg.hydrate_branch_employees(organization_id, branch_id)
                pg_ids = cloud_pg.fetch_pg_branch_employee_ids(organization_id, branch_id)
            except Exception as error:
                logger.warning("Could not hydrate staff cache from Postgres: %s", error)
    if organization_id and organization_id != 'all':
        heal_deleted_camera_assignments(organization_id, branch_id)
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
                       COALESCE(u.exit_date, '') as exit_date,
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
            
        user_list = [{
            'id': r[0], 'name': r[1], 'email': r[2], 'phone': r[3],
            'department': r[4], 'enrollment_date': r[5], 'created_at': r[6],
            'active': r[7], 'notes': r[8], 'photo_path': r[9], 'status_today': r[10],
            'position': r[11], 'cnic': r[12], 'salary': r[13], 'join_date': r[14],
            'exit_date': r[15],
            'shift_id': r[16], 'shift_name': r[17], 'shift_start': r[18],
            'shift_end': r[19], 'shift_grace': r[20],
            'checkin_cameras': r[21], 'checkout_cameras': r[22],
            'organization_id': r[23], 'branch_id': r[24],
            'embedding_count': r[25] if len(r) > 25 else 0,
            'has_embedding': (r[25] > 0) if len(r) > 25 else False
        } for r in rows]
        if pg_ids is not None:
            user_list = [u for u in user_list if u['id'] in pg_ids]
        return user_list
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
                    "SELECT COUNT(*) FROM attendance WHERE user_id = ? AND date(timestamp) >= ?",
                    (user_id, effective_date),
                ).fetchone()[0]
                if past_logs and not confirm_shift_reclassify:
                    raise ValueError(
                        "Changing this shift effective date reclassifies processed attendance. "
                        "Confirm to continue."
                    )
            norm_exit = str(exit_date).strip()[:10] if (exit_date is not None and str(exit_date).strip()) else (None if exit_date is not None else '__KEEP__')
            cursor.execute('''
                UPDATE users 
                SET name = ?, email = ?, phone = ?, department = ?, notes = ?,
                    position = COALESCE(?, position),
                    cnic = COALESCE(?, cnic),
                    salary = COALESCE(?, salary),
                    join_date = COALESCE(?, join_date),
                    exit_date = CASE WHEN ? = '__KEEP__' THEN exit_date ELSE ? END,
                    shift_id = COALESCE(?, shift_id),
                    checkin_cameras = COALESCE(?, checkin_cameras),
                    checkout_cameras = COALESCE(?, checkout_cameras)
                WHERE id = ?
            ''', (name.strip(), email, phone, department, notes, position, cnic, salary, join_date, norm_exit, norm_exit, persisted_shift_id, checkin_cameras, checkout_cameras, user_id))
            updated = cursor.rowcount > 0
            cursor.execute("UPDATE overtime SET user_name = ? WHERE user_id = ?", (name.strip(), user_id))
            if updated and shift_id is not None and (shift_id != previous_shift_id or shift_effective_from is not None):
                _record_employee_shift_history(cursor, user_id, shift_id, effective_date)
            if updated and join_date:
                norm_join = str(join_date)[:10]
                earliest_history = cursor.execute(
                    "SELECT id, effective_from, shift_id FROM employee_shift_history WHERE user_id = ? ORDER BY effective_from ASC, id ASC LIMIT 1",
                    (user_id,)
                ).fetchone()
                current_active_shift = shift_id or previous_shift_id or 1
                if not earliest_history:
                    _record_employee_shift_history(cursor, user_id, current_active_shift, norm_join)
                elif earliest_history[1] > norm_join:
                    cursor.execute(
                        "UPDATE employee_shift_history SET effective_from = ? WHERE id = ?",
                        (norm_join, earliest_history[0])
                    )
            conn.commit()
            
        if updated:
            logger.info(f"✓ User ID {user_id} updated: {name} (Shift: {shift_id}, In: {checkin_cameras}, Out: {checkout_cameras})")
            _cloud_mirror("mirror_user_id", user_id)
        return updated
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to update user ID {user_id}: {e}")
        return False


def get_employee_employment_start_date(employee_or_user: Union[Dict[str, Any], sqlite3.Row, Tuple, List, None]) -> Optional[str]:
    """
    LAYER 5: Standardized business employment start date precedence rule.
    Do NOT blindly use min(created_at, enrollment_date, join_date).
    
    Precedence:
    1. Valid join_date (legitimate business hire date, YYYY-MM-DD)
    2. Validated employment/enrollment date (if join_date is missing/NULL)
    3. created_at as final fallback (when no explicit employment date was recorded)
    
    Ensures:
    - Legitimate historical join_date is respected.
    - Future join_date is respected (employee not treated as active before joining).
    - Newly created employees without join_date are not treated as employed months earlier.
    - Consistent across attendance reports, payroll, leave, overtime, and staff reports.
    """
    if not employee_or_user:
        return None

    def _parse_date_candidate(cand: Any) -> Optional[str]:
        if not cand:
            return None
        clean = str(cand).split(' ')[0].split('T')[0].strip()
        if len(clean) >= 10:
            try:
                datetime.strptime(clean[:10], '%Y-%m-%d')
                return clean[:10]
            except ValueError:
                pass
        return None

    if isinstance(employee_or_user, (dict, sqlite3.Row)):
        jd = _parse_date_candidate(employee_or_user.get('join_date'))
        if jd:
            return jd
        ed = _parse_date_candidate(employee_or_user.get('enrollment_date'))
        if ed:
            return ed
        ca = _parse_date_candidate(employee_or_user.get('created_at'))
        if ca:
            return ca
        ef = _parse_date_candidate(employee_or_user.get('employed_from'))
        if ef:
            return ef
        return None
    return None


def validate_employment_dates(cursor_or_conn, user_id: int, start_date: str, end_date: Optional[str] = None, activity_name: str = 'Activity') -> None:
    """Validate that an activity date does not fall before join_date or after exit_date (QA-20)."""
    if not user_id or not start_date:
        return
    s_date = str(start_date)[:10]
    e_date = str(end_date)[:10] if end_date else s_date

    cursor = cursor_or_conn.cursor() if hasattr(cursor_or_conn, 'cursor') else cursor_or_conn
    row = cursor.execute("SELECT join_date, exit_date, name, active FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return
    join_d = str(row[0])[:10] if row[0] else None
    exit_d = str(row[1])[:10] if row[1] else None
    user_name = row[2] or f"Employee #{user_id}"

    if join_d and s_date < join_d:
        raise ValueError(f"{activity_name} date ({s_date}) cannot precede employee joining date ({join_d}) for {user_name}.")
    if exit_d:
        if s_date > exit_d:
            raise ValueError(f"{activity_name} date ({s_date}) cannot be after employee exit date ({exit_d}) for {user_name}.")
        if e_date and e_date > exit_d:
            raise ValueError(f"{activity_name} date range ({s_date} to {e_date}) extends beyond employee exit date ({exit_d}) for {user_name}.")


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
        if organization_id and organization_id != 'all':
            cursor.execute("SELECT organization_id FROM users WHERE id = ?", (user_id,))
            urow = cursor.fetchone()
            if not urow or urow[0] != organization_id:
                logger.warning(f"Tenant mismatch on deactivate_user {user_id}: belongs to {urow[0] if urow else None}, not {organization_id}")
                return False
        cursor.execute(
            "UPDATE users SET active = 0, exit_date = ? WHERE id = ?",
            (str(exit_date)[:10], user_id),
        )
        updated = cursor.rowcount > 0
        conn.commit()
        if updated:
            _cloud_mirror("mirror_user_id", user_id)
        return updated


def reactivate_user(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
        updated = cursor.rowcount > 0
        conn.commit()
        if updated:
            _cloud_mirror("mirror_user_id", user_id)
        return updated


def set_user_active(user_id: int, active: bool, organization_id: Optional[str] = None) -> bool:
    """Set employee active/inactive status across local and cloud databases."""
    if not active:
        today = datetime.now().date().isoformat()
        return deactivate_user(user_id, exit_date=today, organization_id=organization_id)
    return reactivate_user(user_id)


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
            if room_token and token == room_token:
                return True, f"Camera room '{cam_row[2]}' matches assigned room for {action}"
            if name_token and token == name_token:
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


def delete_user(user_id: int, organization_id: Optional[str] = None) -> bool:
    """Delete a user. If the employee has attendance or payroll history, soft-delete via deactivation to preserve audit and payroll integrity (QA-14)."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("SELECT organization_id, join_date, exit_date FROM users WHERE id = ?", (user_id,))
            org_row = cursor.fetchone()
            if not org_row:
                return False
            org_id = org_row[0] if org_row else None
            if organization_id and org_id != organization_id:
                logger.warning(
                    f"Blocked delete_user {user_id}: belongs to {org_id}, not requested tenant {organization_id}"
                )
                return False

            # Check if user has historical records
            has_history = cursor.execute("""
                SELECT 1 FROM attendance WHERE user_id = ?
                UNION ALL
                SELECT 1 FROM overtime WHERE user_id = ?
                UNION ALL
                SELECT 1 FROM leave_requests WHERE user_id = ?
                UNION ALL
                SELECT 1 FROM payroll_bonuses WHERE user_id = ?
                LIMIT 1
            """, (user_id, user_id, user_id, user_id)).fetchone()

            if has_history:
                exit_today = org_row[2] or datetime.now().date().isoformat()
                cursor.execute(
                    "UPDATE users SET active = 0, exit_date = ? WHERE id = ?",
                    (exit_today, user_id)
                )
                conn.commit()
                logger.info(f"✓ User ID {user_id} has employment history; soft-deleted with exit_date {exit_today}.")
                _cloud_mirror("mirror_user_id", user_id)
                return True

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
    user_id: Optional[int] = None,
) -> float:
    """Person/department extra first, then a named holiday, then company default."""
    if day_type in ('holiday', 'non_working_day') and user_id:
        scoped = _get_scoped_payroll_rule(cursor, 'overtime_premium', int(user_id), day_type)
        if scoped and scoped.get('overtime_premium_percent') is not None:
            return float(scoped['overtime_premium_percent'] or 0.0)
    if day_type == 'holiday':
        if holiday and holiday.get('overtime_premium_percent') is not None:
            return float(holiday['overtime_premium_percent'])
        if settings:
            s_map = dict(settings) if hasattr(settings, 'keys') else (settings if isinstance(settings, dict) else {})
            mode = str(s_map.get('holiday_ot_pay_mode') or 'percent').strip().lower()
            if mode == 'multiplier':
                mult = float(s_map.get('holiday_ot_multiplier') or 1.0)
                if mult > 1.0:
                    return (mult - 1.0) * 100.0
            if s_map.get('holiday_ot_premium_active'):
                return float(s_map.get('holiday_ot_premium_percent') or 0.0)
    elif day_type == 'non_working_day' and settings:
        s_map = dict(settings) if hasattr(settings, 'keys') else (settings if isinstance(settings, dict) else {})
        mode = str(s_map.get('non_working_day_ot_pay_mode') or 'percent').strip().lower()
        if mode == 'multiplier':
            mult = float(s_map.get('non_working_day_ot_multiplier') or 1.0)
            if mult > 1.0:
                return round((mult - 1.0) * 100.0, 4)
        if s_map.get('non_working_day_ot_premium_active'):
            return float(s_map.get('non_working_day_ot_premium_percent') or 0.0)
    return 0.0


def get_overtime_day_type(user_id: int, overtime_date: str) -> str:
    """Public helper to classify overtime by shift schedule and calendar overrides."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return _get_overtime_day_type(conn.cursor(), user_id, overtime_date)


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
        def _get_val(r, idx, key):
            if isinstance(r, (dict, sqlite3.Row)):
                return r[key]
            return r[idx]
        work_date = datetime.strptime(overtime_date, "%Y-%m-%d").date()

        cursor.execute('''
            SELECT shift_id, working_days FROM employee_shift_history
            WHERE user_id = ? AND effective_from <= ?
            ORDER BY effective_from DESC, id DESC LIMIT 1
        ''', (user_id, overtime_date))
        history = cursor.fetchone()
        if history:
            shift_id, working_days = _get_val(history, 0, 'shift_id'), _get_val(history, 1, 'working_days')
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
            shift_id, working_days = _get_val(current, 0, 'shift_id'), _get_val(current, 1, 'working_days')

        cursor.execute('''
            SELECT calendar_date, override_type, scope_type, scope_id
            FROM special_date_overrides
            WHERE calendar_date = ?
              AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
              AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
        ''', (overtime_date, organization_id, branch_id))
        overrides = {
            (_get_val(row, 0, 'calendar_date'), _get_val(row, 2, 'scope_type'), _get_val(row, 3, 'scope_id')): _get_val(row, 1, 'override_type')
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


def _clone_shift_row(cursor: sqlite3.Cursor, source_id: int, organization_id: str, branch_id: str) -> Optional[int]:
    """Copy a shift into another tenant so companies do not share Evening/Morning rows."""
    cursor.execute(
        """
        SELECT name, start_time, end_time, grace_period_minutes, checkout_grace_period_minutes,
               working_days, is_default, active
        FROM shifts WHERE id = ?
        """,
        (source_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    s_name = row[0]
    existing = cursor.execute("SELECT 1 FROM shifts WHERE name = ?", (s_name,)).fetchone()
    if existing:
        s_name = f"{row[0]} ({organization_id})"
        existing2 = cursor.execute("SELECT 1 FROM shifts WHERE name = ?", (s_name,)).fetchone()
        if existing2:
            s_name = f"{row[0]} ({organization_id}_{source_id})"
    try:
        cursor.execute(
            """
            INSERT INTO shifts (
                name, start_time, end_time, grace_period_minutes, checkout_grace_period_minutes,
                working_days, is_default, active, organization_id, branch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                s_name, row[1], row[2], row[3] or 15, row[4] or 0,
                row[5] or json.dumps([0, 1, 2, 3, 4, 5]),
                row[6] or 0, 1 if row[7] is None else row[7],
                organization_id, branch_id or 'DEFAULT_BRANCH',
            ),
        )
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def _repair_shared_shift_tenants(cursor: sqlite3.Cursor) -> None:
    """Split shift rows that several organizations are using, then stamp the owner."""
    cursor.execute("SELECT id, COALESCE(NULLIF(organization_id, ''), 'DEFAULT_ORG') FROM shifts")
    for shift_id, shift_org in cursor.fetchall():
        cursor.execute(
            """
            SELECT DISTINCT COALESCE(NULLIF(organization_id, ''), 'DEFAULT_ORG'),
                   COALESCE(NULLIF(branch_id, ''), 'DEFAULT_BRANCH')
            FROM users
            WHERE active = 1 AND COALESCE(shift_id, 1) = ?
            """,
            (shift_id,),
        )
        tenants = [
            (org_id, branch_id)
            for org_id, branch_id in cursor.fetchall()
            if org_id and org_id != 'DEFAULT_ORG'
        ]
        if not tenants:
            continue
        if shift_id == 1:
            for org_id, branch_id in tenants:
                cursor.execute(
                    """
                    SELECT id FROM shifts
                    WHERE organization_id = ? AND name = (SELECT name FROM shifts WHERE id = 1)
                    LIMIT 1
                    """,
                    (org_id,),
                )
                owned = cursor.fetchone()
                owned_id = owned[0] if owned else _clone_shift_row(cursor, 1, org_id, branch_id)
                if owned_id:
                    cursor.execute(
                        """
                        UPDATE users SET shift_id = ?
                        WHERE organization_id = ? AND COALESCE(shift_id, 1) = 1
                        """,
                        (owned_id, org_id),
                    )
            continue
        keep_org = shift_org if shift_org not in ('DEFAULT_ORG', '') else tenants[0][0]
        extra_tenants = [item for item in tenants if item[0] != keep_org]
        if shift_org in ('DEFAULT_ORG', ''):
            keep = tenants[0]
            extra_tenants = tenants[1:]
            cursor.execute(
                "UPDATE shifts SET organization_id = ?, branch_id = ? WHERE id = ?",
                (keep[0], keep[1], shift_id),
            )
        for org_id, branch_id in extra_tenants:
            new_id = _clone_shift_row(cursor, shift_id, org_id, branch_id)
            if not new_id:
                continue
            cursor.execute(
                """
                UPDATE users SET shift_id = ?
                WHERE organization_id = ? AND COALESCE(shift_id, 1) = ?
                """,
                (new_id, org_id, shift_id),
            )


def _ensure_organization_default_shift(cursor: sqlite3.Cursor, organization_id: str, branch_id: Optional[str] = None) -> None:
    """Empty companies get their own 09:00 Morning. They never inherit another org's Evening."""
    if not organization_id or organization_id in ('all', 'DEFAULT_ORG'):
        return
    cursor.execute(
        "SELECT COUNT(*) FROM shifts WHERE organization_id = ? AND COALESCE(active, 1) = 1",
        (organization_id,),
    )
    if cursor.fetchone()[0] > 0:
        return

    # Check if PostgreSQL has shifts for this tenant before creating a default
    if postgres_is_configured():
        try:
            import postgres_mirror as cloud_pg
            pg_shifts = cloud_pg.fetch_pg_all_shifts(organization_id, branch_id)
            if pg_shifts:
                cursor.execute(
                    "SELECT COUNT(*) FROM shifts WHERE organization_id = ? AND COALESCE(active, 1) = 1",
                    (organization_id,),
                )
                if cursor.fetchone()[0] > 0:
                    return
        except Exception as err:
            logger.warning("Could not check PG shifts: %s", err)

    s_name = 'Morning Shift'
    existing = cursor.execute("SELECT 1 FROM shifts WHERE name = ?", (s_name,)).fetchone()
    if existing:
        s_name = f"Morning Shift ({organization_id})"
        existing2 = cursor.execute("SELECT 1 FROM shifts WHERE name = ?", (s_name,)).fetchone()
        if existing2:
            s_name = f"Morning Shift ({organization_id}_default)"
    try:
        cursor.execute(
            """
            INSERT INTO shifts (
                name, start_time, end_time, grace_period_minutes, checkout_grace_period_minutes,
                working_days, is_default, active, organization_id, branch_id
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
            """,
            (
                s_name, '09:00', '17:00', 15, 0,
                json.dumps([0, 1, 2, 3, 4, 5]),
                organization_id, branch_id or 'DEFAULT_BRANCH',
            ),
        )
    except sqlite3.IntegrityError:
        return
    new_id = cursor.lastrowid
    # LAYER 3 PROTECTION: Only assign default shift to users who genuinely have NO shift assignment (shift_id IS NULL)
    # NEVER overwrite an employee's existing valid custom shift!
    cursor.execute(
        """
        UPDATE users SET shift_id = ?
        WHERE organization_id = ? AND active = 1 AND shift_id IS NULL
        """,
        (new_id, organization_id),
    )


def _rehome_users_on_foreign_shifts(cursor: sqlite3.Cursor, organization_id: str, branch_id: Optional[str] = None) -> None:
    """If a staff member still points at another company's shift, give this company its own copy."""
    if not organization_id or organization_id in ('all', 'DEFAULT_ORG'):
        return
    cursor.execute(
        """
        SELECT u.id, u.shift_id
        FROM users u
        LEFT JOIN shifts s ON s.id = u.shift_id
        WHERE u.active = 1 AND u.organization_id = ?
          AND u.shift_id IS NOT NULL
          AND (s.id IS NULL OR COALESCE(s.organization_id, '') != ?)
        """,
        (organization_id, organization_id),
    )
    stuck_users = cursor.fetchall()
    for user_id, source_shift_id in stuck_users:
        # Check if source_shift_id exists authoritatively in PostgreSQL for this org first
        if postgres_is_configured() and source_shift_id:
            try:
                import postgres_mirror as cloud_pg
                pg_s = cloud_pg.fetch_pg_shift(source_shift_id, organization_id)
                if pg_s:
                    continue  # Shift exists in PG for this org and was hydrated; no need to rehome
            except Exception:
                pass
        cursor.execute(
            "SELECT name FROM shifts WHERE id = ?",
            (source_shift_id,),
        )
        named = cursor.fetchone()
        owned_id = None
        if named:
            cursor.execute(
                "SELECT id FROM shifts WHERE organization_id = ? AND name = ? LIMIT 1",
                (organization_id, named[0]),
            )
            owned = cursor.fetchone()
            owned_id = owned[0] if owned else _clone_shift_row(
                cursor, source_shift_id, organization_id, branch_id or 'DEFAULT_BRANCH'
            )
        if not owned_id:
            cursor.execute(
                "SELECT id FROM shifts WHERE organization_id = ? AND COALESCE(active, 1) = 1 ORDER BY id ASC LIMIT 1",
                (organization_id,),
            )
            fallback = cursor.fetchone()
            owned_id = fallback[0] if fallback else None
        if owned_id:
            cursor.execute("UPDATE users SET shift_id = ? WHERE id = ?", (owned_id, user_id))


def get_all_shifts(
    active_only: bool = True,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> List[Dict]:
    pg_shift_ids = None
    if organization_id and organization_id not in ('all', 'DEFAULT_ORG'):
        if postgres_is_configured():
            try:
                import postgres_mirror as cloud_pg
                pg_shifts = cloud_pg.fetch_pg_all_shifts(organization_id, branch_id, active_only)
                if pg_shifts is not None:
                    pg_shift_ids = {int(str(s.get("id"))) for s in pg_shifts if s.get("id") is not None}
            except Exception as error:
                logger.warning("Postgres unreachable for shifts; falling back to SQLite cache: %s", error)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            _repair_shared_shift_tenants(cursor)
            if organization_id and organization_id not in ('all', 'DEFAULT_ORG'):
                _ensure_organization_default_shift(cursor, organization_id, branch_id)
                _rehome_users_on_foreign_shifts(cursor, organization_id, branch_id)
            conn.commit()
            where_clauses = []
            params = []
            join_conditions = [
                "COALESCE(u.shift_id, 1) = s.id",
                "u.active = 1",
            ]
            join_params = []
            if active_only:
                where_clauses.append("s.active = 1")
            if not organization_id or organization_id == 'all':
                return []
            if organization_id == 'DEFAULT_ORG':
                where_clauses.append("(s.organization_id = 'DEFAULT_ORG' OR s.organization_id IS NULL OR s.organization_id = '')")
                join_conditions.append("COALESCE(NULLIF(u.organization_id, ''), 'DEFAULT_ORG') = 'DEFAULT_ORG'")
            else:
                where_clauses.append("s.organization_id = ?")
                params.append(organization_id)
                join_conditions.append("u.organization_id = ?")
                join_params.append(organization_id)
            if branch_id and branch_id != 'all':
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

        shift_list = [{
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
        if pg_shift_ids is not None:
            shift_list = [s for s in shift_list if s['id'] in pg_shift_ids]
        return shift_list
    except Exception as e:
        logger.error(f"Failed to fetch shifts: {e}")
        return []


def set_default_shift(shift_id: int, organization_id: Optional[str] = None) -> bool:
    """Set a specific shift as the default for one organization only."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id and organization_id not in ('all', 'DEFAULT_ORG'):
                cursor.execute(
                    "SELECT id FROM shifts WHERE id = ? AND organization_id = ?",
                    (shift_id, organization_id),
                )
                if not cursor.fetchone():
                    return False
                cursor.execute("UPDATE shifts SET is_default = 0 WHERE organization_id = ?", (organization_id,))
                cursor.execute(
                    "UPDATE shifts SET is_default = 1 WHERE id = ? AND organization_id = ?",
                    (shift_id, organization_id),
                )
            else:
                cursor.execute("UPDATE shifts SET is_default = 0 WHERE organization_id = 'DEFAULT_ORG' OR organization_id IS NULL OR id = 1")
                cursor.execute("UPDATE shifts SET is_default = 1 WHERE id = ?", (shift_id,))
            conn.commit()
            logger.info(f"✓ Shift ID #{shift_id} set as default shift")
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
        import postgres_mirror as cloud_pg
        if cloud_pg.postgres_is_configured():
            pg_res = cloud_pg.fetch_pg_shift(shift_id, organization_id)
            if pg_res is False:
                return None
    except Exception as e:
        logger.warning("Postgres unreachable for get_shift_by_id; falling back to SQLite cache: %s", e)
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
                if organization_id and organization_id != 'all':
                    if organization_id == 'DEFAULT_ORG':
                        if shift_dict['organization_id'] not in ('DEFAULT_ORG', None, ''):
                            return None
                    elif shift_dict['organization_id'] != organization_id:
                        return None
                return shift_dict
    except Exception as e:
        logger.error(f"Failed to fetch shift ID {shift_id}: {e}")
    return None


def is_shift_ended_for_time(shift_end_str: str, current_time: Any = None) -> bool:
    """Helper to determine whether a shift has ended given its end time string."""
    try:
        from datetime import datetime, time
        if current_time is None:
            current_time = datetime.now().time()
        elif isinstance(current_time, datetime):
            current_time = current_time.time()
        elif isinstance(current_time, str):
            parts = [int(p) for p in current_time.split(":")[:2]]
            current_time = time(parts[0], parts[1])
        parts = [int(p) for p in shift_end_str.split(":")[:2]]
        shift_end = time(parts[0], parts[1])
        return current_time > shift_end
    except Exception:
        return True


def validate_half_day_leave_duration(leave_start: str, leave_end: str, shift_start: str, shift_end: str) -> bool:
    """Validate that half-day leave duration is <= 50% of the shift duration."""
    try:
        def to_mins(t_str: str) -> int:
            parts = [int(p) for p in t_str.split(":")[:2]]
            return parts[0] * 60 + parts[1]
        s_start = to_mins(shift_start)
        s_end = to_mins(shift_end)
        shift_dur = (s_end - s_start) if s_end >= s_start else (s_end + 1440 - s_start)
        l_start = to_mins(leave_start)
        l_end = to_mins(leave_end)
        leave_dur = (l_end - l_start) if l_end >= l_start else (l_end + 1440 - l_start)
        return leave_dur <= (shift_dur / 2.0)
    except Exception:
        return True


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
            if organization_id and organization_id != 'all':
                cursor.execute("SELECT organization_id FROM shifts WHERE id = ?", (shift_id,))
                s_row = cursor.fetchone()
                if not s_row or (s_row[0] or 'DEFAULT_ORG') != organization_id:
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
                full_shift = cursor.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
                if full_shift:
                    cols = [col[0] for col in cursor.description]
                    shift_dict = dict(zip(cols, full_shift))
                    _cloud_mirror("mirror_shift", shift_dict)
            return updated
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to update shift ID {shift_id}: {e}")
        return False


def delete_shift(shift_id: int, organization_id: Optional[str] = None) -> bool:
    """Delete shift (reassigns users of this shift back to shift ID 1), ensuring tenant authorization."""
    if shift_id == 1:
        logger.warning("Cannot delete the shared template Morning Shift")
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id and organization_id != 'all':
                cursor.execute("SELECT organization_id FROM shifts WHERE id = ?", (shift_id,))
                s_row = cursor.fetchone()
                if not s_row and postgres_is_configured():
                    try:
                        import postgres_mirror as cloud_pg
                        cloud_pg.fetch_pg_shift(shift_id, organization_id)
                        cursor.execute("SELECT organization_id FROM shifts WHERE id = ?", (shift_id,))
                        s_row = cursor.fetchone()
                    except Exception:
                        pass
                if not s_row or (s_row[0] or '') != organization_id:
                    return False
                cursor.execute(
                    "SELECT COUNT(*) FROM shifts WHERE organization_id = ? AND COALESCE(active, 1) = 1",
                    (organization_id,),
                )
                if cursor.fetchone()[0] <= 1:
                    logger.warning("Cannot delete the only shift for organization %s", organization_id)
                    return False
            cursor.execute("SELECT id FROM users WHERE shift_id = ?", (shift_id,))
            reassigned_users = [row[0] for row in cursor.fetchall()]
            fallback_id = 1
            if organization_id and organization_id != 'all':
                cursor.execute(
                    "SELECT id FROM shifts WHERE organization_id = ? AND id != ? ORDER BY id ASC LIMIT 1",
                    (organization_id, shift_id),
                )
                fallback = cursor.fetchone()
                if fallback:
                    fallback_id = fallback[0]
            cursor.execute('UPDATE users SET shift_id = ? WHERE shift_id = ?', (fallback_id, shift_id))
            for user_id in reassigned_users:
                _record_employee_shift_history(cursor, user_id, fallback_id)
            cursor.execute('DELETE FROM shifts WHERE id = ?', (shift_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            if deleted:
                try:
                    if postgres_is_configured():
                        import postgres_mirror as cloud_pg
                        cloud_pg.delete_shift(shift_id, organization_id=organization_id, fallback_shift_id=fallback_id)
                except Exception as pg_err:
                    logger.warning("Failed to mirror shift deletion to cloud: %s", pg_err)
                logger.info(f"✓ Shift ID {shift_id} deleted successfully.")
            return deleted
    except Exception as e:
        logger.error(f"Failed to delete shift ID {shift_id}: {e}")
        return False


def assign_user_shift(user_id: int, shift_id: int, effective_from: Optional[str] = None, confirm_shift_reclassify: bool = False, organization_id: Optional[str] = None) -> bool:
    """Assign a shift to an individual user immediately in the database, verifying reclassification impact."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            effective_date = _normalize_shift_effective_date(effective_from)
            cursor.execute(
                "SELECT COALESCE(shift_id, 1), organization_id, COALESCE(branch_id, 'DEFAULT_BRANCH') FROM users WHERE id = ? AND active = 1",
                (user_id,),
            )
            current_shift = cursor.fetchone()
            if not current_shift:
                return False
            if organization_id and (current_shift[1] or '') != organization_id:
                logger.warning(
                    f"Blocked assigning shift to user {user_id}: belongs to {current_shift[1]}, not requested {organization_id}"
                )
                return False
            cursor.execute("SELECT organization_id FROM shifts WHERE id = ?", (shift_id,))
            shift_row = cursor.fetchone()
            if not shift_row and postgres_is_configured() and current_shift[1]:
                try:
                    import postgres_mirror as cloud_pg
                    cloud_pg.fetch_pg_shift(shift_id, current_shift[1])
                    cursor.execute("SELECT organization_id FROM shifts WHERE id = ?", (shift_id,))
                    shift_row = cursor.fetchone()
                except Exception:
                    pass
            if not shift_row or (shift_row[0] or '') != (current_shift[1] or ''):
                logger.warning(
                    "Blocked assigning shift %s to user %s (shift is not owned by their organization)",
                    shift_id, user_id,
                )
                return False
            
            # SH-01: Check if change affects dates with processed attendance
            if shift_id is not None and (shift_id != current_shift[0] or effective_from is not None):
                affected_month = effective_date[:7]
                org_id = current_shift[1] or 'DEFAULT_ORG'
                branch_id = current_shift[2] or 'DEFAULT_BRANCH'
                if payroll_month_is_finalized(affected_month, org_id, branch_id):
                    raise ValueError(PAYROLL_LOCK_MESSAGE)
                past_logs = cursor.execute(
                    "SELECT COUNT(*) FROM attendance WHERE user_id = ? AND date(timestamp) >= ?",
                    (user_id, effective_date),
                ).fetchone()[0]
                if past_logs and not confirm_shift_reclassify:
                    raise ValueError(
                        "Changing this shift effective date reclassifies processed attendance. "
                        "Confirm to continue."
                    )

            is_current = effective_date <= datetime.now().date().isoformat()
            if is_current:
                cursor.execute("UPDATE users SET shift_id = ? WHERE id = ? AND active = 1", (shift_id, user_id))
            updated = True
            if effective_date > datetime.now().date().isoformat() or current_shift[0] != shift_id or effective_from is not None:
                _record_employee_shift_history(cursor, user_id, shift_id, effective_date)
            conn.commit()
            if updated:
                try:
                    if postgres_is_configured():
                        import postgres_mirror as cloud_pg
                        cloud_pg.update_employees_shift_pg([user_id], shift_id, organization_id=current_shift[1])
                except Exception as pg_err:
                    logger.warning("Failed to mirror shift assignment to cloud: %s", pg_err)
                logger.info(f"✓ Shift ID {shift_id} assigned to User ID {user_id}")
            return updated
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Failed to assign shift {shift_id} to user {user_id}: {e}")
        return False


def bulk_assign_shifts(
    user_ids: Union[str, List[int]],
    shift_id: int,
    effective_from: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> Tuple[bool, int]:
    """Bulk assign shift to employees in one organization only."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id and organization_id not in ('all', 'DEFAULT_ORG'):
                cursor.execute(
                    "SELECT id FROM shifts WHERE id = ? AND organization_id = ?",
                    (shift_id, organization_id),
                )
                if not cursor.fetchone():
                    if postgres_is_configured():
                        try:
                            import postgres_mirror as cloud_pg
                            cloud_pg.fetch_pg_shift(shift_id, organization_id)
                            cursor.execute(
                                "SELECT id FROM shifts WHERE id = ? AND organization_id = ?",
                                (shift_id, organization_id),
                            )
                        except Exception:
                            pass
                    if not cursor.fetchone():
                        return False, 0
            effective_date = _normalize_shift_effective_date(effective_from)
            org_filter = ""
            org_params: List[Any] = []
            if organization_id and organization_id not in ('all',):
                org_filter = " AND organization_id = ?"
                org_params = [organization_id]
            if user_ids == 'all' or not user_ids or (isinstance(user_ids, list) and len(user_ids) == 0):
                cursor.execute(
                    f"SELECT id, COALESCE(shift_id, 1) FROM users WHERE active = 1{org_filter}",
                    org_params,
                )
                selected_users = cursor.fetchall()
                if effective_date <= datetime.now().date().isoformat():
                    cursor.execute(
                        f"UPDATE users SET shift_id = ? WHERE active = 1{org_filter}",
                        (shift_id, *org_params),
                    )
            else:
                placeholders = ','.join('?' for _ in user_ids)
                cursor.execute(
                    f"SELECT id, COALESCE(shift_id, 1) FROM users WHERE id IN ({placeholders}) AND active = 1{org_filter}",
                    (*user_ids, *org_params),
                )
                selected_users = cursor.fetchall()
                if effective_date <= datetime.now().date().isoformat():
                    cursor.execute(
                        f"UPDATE users SET shift_id = ? WHERE id IN ({placeholders}) AND active = 1{org_filter}",
                        (shift_id, *user_ids, *org_params),
                    )
            count = len(selected_users)
            for user_id, current_shift_id in selected_users:
                if effective_date > datetime.now().date().isoformat() or current_shift_id != shift_id or effective_from is not None:
                    _record_employee_shift_history(cursor, user_id, shift_id, effective_date)
            conn.commit()
            if count > 0:
                try:
                    if postgres_is_configured():
                        import postgres_mirror as cloud_pg
                        assigned_uids = [u[0] for u in selected_users]
                        cloud_pg.update_employees_shift_pg(assigned_uids, shift_id, organization_id=organization_id)
                except Exception as pg_err:
                    logger.warning("Failed to mirror bulk shift assignment to cloud: %s", pg_err)
            logger.info(f"✓ Bulk shift ID {shift_id} assigned to {count} staff members")
            return True, count
    except Exception as e:
        logger.error(f"Failed to bulk assign shift: {e}")
        return False, 0


def _overtime_date_is_future(ot_date: str) -> bool:
    try:
        parsed = datetime.strptime(str(ot_date)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return False
    return parsed > date.today()


def _record_automatic_checkout_overtime(
    cursor: sqlite3.Cursor,
    user_id: int,
    user_name: str,
    checkout_time: Union[datetime, str],
) -> float:
    if isinstance(checkout_time, str):
        cleaned_str = checkout_time.strip().replace('T', ' ').split('.')[0]
        try:
            checkout_dt = datetime.strptime(cleaned_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                checkout_dt = datetime.strptime(cleaned_str[:16], '%Y-%m-%d %H:%M')
            except ValueError:
                checkout_dt = datetime.strptime(cleaned_str[:10], '%Y-%m-%d')
    else:
        checkout_dt = checkout_time
    actual_date = checkout_dt.strftime('%Y-%m-%d')
    if _overtime_date_is_future(actual_date):
        return 0.0
    actual_time = checkout_dt.strftime('%H:%M')
    if not user_name or not str(user_name).strip():
        cursor.execute("SELECT name FROM users WHERE id = ?", (user_id,))
        u_name_row = cursor.fetchone()
        user_name = u_name_row[0] if u_name_row else f"User {user_id}"
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
        ex_id, ex_status = existing[0], (existing[1] or 'pending').lower()
        if ex_status == 'pending':
            cursor.execute(
                "UPDATE overtime SET hours = ?, reason = ? WHERE id = ?",
                (overtime_hours, reason, ex_id),
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


def _restore_worked_leave_if_applicable(cursor, user_id: int, log_date: str) -> bool:
    """
    LV-05: When an employee with approved leave is subsequently recorded as present on that date,
    restore their leave entitlement unless payroll for that month is already finalized.
    Safe against duplicate calls (idempotent via restored_dates tracking).
    """
    try:
        month_str = log_date[:7]
        org_id, branch_id = _user_tenant(cursor, user_id)
        if payroll_month_is_finalized(month_str, org_id, branch_id):
            return False

        cursor.execute("""
            SELECT id, leave_type, leave_duration, start_date, end_date, status,
                   paid_status, unpaid_status, restored_dates, reason
            FROM leave_requests
            WHERE user_id = ?
              AND status IN ('approved', 'partially_approved')
              AND start_date <= ? AND end_date >= ?
        """, (user_id, log_date, log_date))
        rows = cursor.fetchall()
        for lr in rows:
            if isinstance(lr, (tuple, list)):
                leave_id = lr[0]
                lr_duration = lr[2]
                lr_start = lr[3]
                lr_end = lr[4]
                lr_status = lr[5]
                lr_restored_dates = lr[8]
                lr_reason = lr[9]
            else:
                leave_id = lr['id']
                lr_duration = lr['leave_duration']
                lr_start = lr['start_date']
                lr_end = lr['end_date']
                lr_status = lr['status']
                lr_restored_dates = lr['restored_dates']
                lr_reason = lr['reason']

            if str(lr_duration).lower() == 'half':
                continue

            restored = [d.strip() for d in str(lr_restored_dates or '').split(',') if d.strip()]
            if log_date not in restored:
                restored.append(log_date)
                restored_str = ','.join(sorted(restored))
                all_restored = False
                try:
                    s_dt = datetime.strptime(lr_start, '%Y-%m-%d')
                    e_dt = datetime.strptime(lr_end, '%Y-%m-%d')
                    total_req_days = (e_dt - s_dt).days + 1
                    all_restored = len(restored) >= total_req_days
                except Exception:
                    pass
                new_status = 'cancelled_worked' if all_restored else lr_status
                cursor.execute("""
                    UPDATE leave_requests
                    SET restored_dates = ?,
                        status = ?,
                        paid_status = CASE WHEN ? = 'cancelled_worked' THEN 'cancelled_worked' ELSE paid_status END,
                        unpaid_status = CASE WHEN ? = 'cancelled_worked' THEN 'cancelled_worked' ELSE unpaid_status END,
                        reason = CASE WHEN reason IS NULL OR reason = ''
                                      THEN '[Leave entitlement restored: employee worked on ' || ? || ']'
                                      ELSE reason || ' [Entitlement restored for ' || ? || ']' END
                    WHERE id = ?
                """, (restored_str, new_status, new_status, new_status, log_date, log_date, leave_id))
                logger.info(f"LV-05: Restored leave entitlement for user #{user_id} on {log_date} (leave request #{leave_id})")
        return True
    except Exception as e:
        logger.error(f"Error checking leave restoration for user {user_id} on {log_date}: {e}")
        return False


def _unrestore_worked_leave_if_applicable(cursor, user_id: int, log_date: str) -> bool:
    """
    ATT-02: When attendance changes from Present to Absent, reverse any previous leave restoration
    for that date so leave deduction and leave balances remain consistent.
    """
    try:
        month_str = log_date[:7]
        org_id, branch_id = _user_tenant(cursor, user_id)
        if payroll_month_is_finalized(month_str, org_id, branch_id):
            return False

        cursor.execute("""
            SELECT id, status, restored_dates
            FROM leave_requests
            WHERE user_id = ?
              AND start_date <= ? AND end_date >= ?
        """, (user_id, log_date, log_date))
        rows = cursor.fetchall()
        for lr in rows:
            if isinstance(lr, (tuple, list)):
                leave_id = lr[0]
                lr_status = lr[1]
                lr_restored_dates = lr[2]
            else:
                leave_id = lr['id']
                lr_status = lr['status']
                lr_restored_dates = lr['restored_dates']

            restored = [d.strip() for d in str(lr_restored_dates or '').split(',') if d.strip()]
            if log_date in restored:
                restored.remove(log_date)
                restored_str = ','.join(sorted(restored))
                new_status = 'approved' if lr_status == 'cancelled_worked' else lr_status
                cursor.execute("""
                    UPDATE leave_requests
                    SET restored_dates = ?, status = ?
                    WHERE id = ?
                """, (restored_str, new_status, leave_id))
                logger.info(f"ATT-02: Re-applied leave deduction for user #{user_id} on {log_date} (removed from restored_dates of leave #{leave_id})")
        return True
    except Exception as e:
        logger.error(f"Error un-restoring leave for user {user_id} on {log_date}: {e}")
        return False


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
            log_date_str = attendance_time.strftime('%Y-%m-%d')
            cursor.execute(
                'DELETE FROM manual_absent WHERE user_id = ? AND date = date(?)',
                (user_id, log_date_str)
            )
            _restore_worked_leave_if_applicable(cursor, user_id, log_date_str)
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
            overtime_hours = 0.0
            if checkout_camera_id:
                overtime_hours = _record_automatic_checkout_overtime(
                    cursor, user_id, detected_name, attendance_time
                )
            else:
                cursor.execute(
                    "SELECT MIN(timestamp) FROM attendance WHERE user_id = ? AND date(timestamp) = ?",
                    (user_id, log_date_str)
                )
                first_att = cursor.fetchone()
                if first_att and first_att[0] and str(first_att[0]) < attendance_time.strftime('%Y-%m-%d %H:%M:%S'):
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
    if organization_id and organization_id not in ('all', 'DEFAULT_ORG'):
        if postgres_is_configured():
            try:
                import postgres_mirror as cloud_pg
                cloud_pg.fetch_pg_attendance_logs(organization_id, branch_id, limit)
            except Exception as error:
                logger.warning("Postgres unreachable for attendance logs; falling back to SQLite cache: %s", error)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            query = '''
                SELECT a.id, COALESCE(u.name, a.detected_name, 'Unknown') as name, a.detected_name, a.confidence, a.timestamp, a.source, a.location, a.device_id
                FROM attendance a
                LEFT JOIN users u ON a.user_id = u.id
                WHERE 1=1
            '''
            params = []
            if organization_id and organization_id != 'all':
                if organization_id == 'DEFAULT_ORG':
                    query += " AND (a.organization_id = 'DEFAULT_ORG' OR a.organization_id IS NULL OR u.organization_id = 'DEFAULT_ORG' OR u.organization_id IS NULL)"
                else:
                    query += " AND (a.organization_id = ? OR (a.organization_id IS NULL AND u.organization_id = ?))"
                    params.extend([organization_id, organization_id])
            if branch_id and branch_id != 'all':
                if branch_id == 'DEFAULT_BRANCH':
                    query += " AND (a.branch_id = 'DEFAULT_BRANCH' OR a.branch_id IS NULL OR u.branch_id = 'DEFAULT_BRANCH' OR u.branch_id IS NULL)"
                else:
                    query += " AND (a.branch_id = ? OR (a.branch_id IS NULL AND u.branch_id = ?))"
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
    """Get list of distinct active departments from authoritative departments table, falling back to assigned users."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if organization_id:
                if organization_id == 'DEFAULT_ORG':
                    cursor.execute("""
                        SELECT DISTINCT name FROM departments 
                        WHERE active = 1 AND (organization_id = ? OR organization_id IS NULL OR organization_id = '')
                        UNION
                        SELECT DISTINCT department FROM users 
                        WHERE active = 1 AND department IS NOT NULL AND department != '' 
                          AND (organization_id = ? OR organization_id IS NULL OR organization_id = '')
                        ORDER BY 1 ASC
                    """, (organization_id, organization_id))
                else:
                    cursor.execute("""
                        SELECT DISTINCT name FROM departments 
                        WHERE active = 1 AND organization_id = ?
                        UNION
                        SELECT DISTINCT department FROM users 
                        WHERE active = 1 AND department IS NOT NULL AND department != '' 
                          AND organization_id = ?
                        ORDER BY 1 ASC
                    """, (organization_id, organization_id))
            else:
                cursor.execute("""
                    SELECT DISTINCT name FROM departments WHERE active = 1
                    UNION
                    SELECT DISTINCT department FROM users WHERE active = 1 AND department IS NOT NULL AND department != ''
                    ORDER BY 1 ASC
                """)
            rows = cursor.fetchall()
            return [r[0] for r in rows if r[0]]
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

            if organization_id and organization_id not in ('all', 'DEFAULT_ORG'):
                if postgres_is_configured():
                    try:
                        import postgres_mirror as cloud_pg
                        cloud_pg.fetch_pg_attendance_logs(organization_id, branch_id, limit=500)
                    except Exception as error:
                        logger.warning("Postgres unreachable for attendance logs in report: %s", error)

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
                td_str = str(target_date).strip()
                if ' to ' in td_str:
                    s_part, e_part = td_str.split(' to ', 1)
                    start_date, end_date = s_part.strip(), e_part.strip()
                elif ' - ' in td_str and not td_str.startswith('-') and len(td_str) > 7:
                    s_part, e_part = td_str.split(' - ', 1)
                    start_date, end_date = s_part.strip(), e_part.strip()
                elif len(td_str) == 7 and td_str.count('-') == 1:
                    try:
                        m_dt = datetime.strptime(td_str, "%Y-%m").date()
                        start_date = m_dt.strftime("%Y-%m-01")
                        if m_dt.month == 12:
                            next_m = m_dt.replace(year=m_dt.year + 1, month=1)
                        else:
                            next_m = m_dt.replace(month=m_dt.month + 1)
                        end_date = (next_m - timedelta(days=1)).strftime("%Y-%m-%d")
                    except ValueError:
                        start_date = td_str
                        end_date = td_str
                elif td_str.lower() in [
                    'january', 'february', 'march', 'april', 'may', 'june',
                    'july', 'august', 'september', 'october', 'november', 'december'
                ]:
                    try:
                        cur_year = datetime.now().year
                        m_dt = datetime.strptime(f"{td_str} {cur_year}", "%B %Y").date()
                        start_date = m_dt.strftime("%Y-%m-01")
                        if m_dt.month == 12:
                            next_m = m_dt.replace(year=m_dt.year + 1, month=1)
                        else:
                            next_m = m_dt.replace(month=m_dt.month + 1)
                        end_date = (next_m - timedelta(days=1)).strftime("%Y-%m-%d")
                    except ValueError:
                        start_date = td_str
                        end_date = td_str
                else:
                    start_date = td_str
                    end_date = td_str
                is_multi_day = (start_date != end_date)
            else:
                end_date = end_date or start_date
                start_date = start_date or end_date
                is_multi_day = (start_date != end_date)

            # Strict normalization to calendar day (YYYY-MM-DD), removing time or timezone differences
            start_date = str(start_date).strip().split('T')[0].split(' ')[0][:10]
            end_date = str(end_date).strip().split('T')[0].split(' ')[0][:10]
            d1_date = datetime.strptime(start_date, "%Y-%m-%d").date()
            d2_date = datetime.strptime(end_date, "%Y-%m-%d").date()
            if d1_date > d2_date:
                d1_date, d2_date = d2_date, d1_date
                start_date, end_date = end_date, start_date

            # Inclusive calendar day count: (end - start) + 1
            total_days = (d2_date - d1_date).days + 1
            date_list = [(d1_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(total_days)]
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
                s_val = str(shift_id).strip()
                user_query += """ AND (
                    u.shift_id = ? OR LOWER(TRIM(s.name)) = LOWER(TRIM(?))
                    OR EXISTS (
                        SELECT 1 FROM employee_shift_history esh
                        JOIN shifts eshs ON esh.shift_id = eshs.id
                        WHERE esh.user_id = u.id AND (esh.shift_id = ? OR LOWER(TRIM(eshs.name)) = LOWER(TRIM(?)))
                    )
                )"""
                user_params.extend([s_val, s_val, s_val, s_val])
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
                        'total_days': total_days,
                        'duration_days': total_days,
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

            # ATT-01: Sargable range query using indexed timestamp boundaries with strict tenant & camera isolation
            start_ts = f"{start_date} 00:00:00"
            end_ts = f"{end_date} 23:59:59.999999"

            att_conditions = [
                f"user_id IN ({placeholders})",
                "timestamp >= ?",
                "timestamp <= ?"
            ]
            att_params = list(user_ids) + [start_ts, end_ts]

            if report_org and report_org != 'all':
                if report_org == 'DEFAULT_ORG':
                    att_conditions.append("(organization_id = 'DEFAULT_ORG' OR organization_id IS NULL)")
                else:
                    att_conditions.append("(organization_id = ? OR organization_id IS NULL)")
                    att_params.append(report_org)
                    # Cross-tenant camera isolation: Never allow cameras explicitly tagged with a different organization prefix
                    att_conditions.append("(device_id IS NULL OR device_id NOT LIKE 'CAM-ORG-%' OR device_id LIKE ?)")
                    att_params.append(f"%{report_org}%")

            if branch_id and branch_id != 'all':
                if branch_id == 'DEFAULT_BRANCH':
                    att_conditions.append("(branch_id = 'DEFAULT_BRANCH' OR branch_id IS NULL)")
                else:
                    att_conditions.append("(branch_id = ? OR branch_id IS NULL)")
                    att_params.append(branch_id)

            att_where_sql = " AND ".join(att_conditions)
            cursor.execute(
                f'''
                SELECT user_id, timestamp, location, device_id, checkout_location, checkout_camera_id,
                       source, notes, marked_by, shift_id
                FROM attendance
                WHERE {att_where_sql}
                ORDER BY user_id, timestamp
                ''',
                att_params,
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
                  SELECT lr.id, lr.user_id, lr.start_date, lr.end_date, lr.leave_type, lr.leave_duration,
                      lr.paid_days, lr.unpaid_days, lr.paid_through_date, lr.paid_status, lr.unpaid_status,
                      lr.status, lr.restored_dates,
                       COALESCE(lt.is_paid, CASE WHEN lr.leave_type = 'unpaid' THEN 0 ELSE 1 END) as is_paid
                FROM leave_requests lr
                LEFT JOIN leave_types lt ON lt.code = lr.leave_type OR lt.name = lr.leave_type
                WHERE lr.user_id IN ({placeholders})
                  AND (lr.status = 'approved' OR lr.paid_status = 'approved' OR lr.unpaid_status = 'approved')
                  AND lr.end_date >= ? AND lr.start_date <= ?
            ''', (*user_ids, start_date, end_date))
            leave_rows = cursor.fetchall()
            leave_map = {}
            approved_leave_dates = set()
            for lr in leave_rows:
                l_uid, l_start, l_end, l_type = lr['user_id'], lr['start_date'], lr['end_date'], lr['leave_type']
                is_half_leave = lr['leave_duration'] == 'half'
                explicit_allocation = not is_half_leave and lr['paid_days'] is not None and lr['unpaid_days'] is not None
                paid_days = int(lr['paid_days'] or 0) if explicit_allocation else None
                unpaid_days = int(lr['unpaid_days'] or 0) if explicit_allocation else None
                paid_through_date = lr['paid_through_date']
                if explicit_allocation and (paid_days or 0) > 0 and not paid_through_date:
                    p_rng, _ = split_leave_request_dates(l_uid, l_start, l_end, paid_days)
                    if p_rng:
                        paid_through_date = p_rng[1]
                leave_is_paid_default = bool(lr['is_paid'])

                restored_dates_raw = lr['restored_dates'] if 'restored_dates' in lr.keys() else ''
                restored_set = set(d.strip() for d in (restored_dates_raw or '').split(',') if d.strip())

                is_overall_approved = (str(lr['status']).lower() == 'approved')
                p_status = (lr['paid_status'] or ('approved' if is_overall_approved else 'pending')).lower()
                if is_overall_approved and p_status == 'pending':
                    p_status = 'approved'
                up_status = (lr['unpaid_status'] or ('approved' if is_overall_approved else 'pending')).lower()
                if is_overall_approved and up_status == 'pending':
                    up_status = 'approved'

                try:
                    cur_l = datetime.strptime(l_start, "%Y-%m-%d")
                    end_l = datetime.strptime(l_end, "%Y-%m-%d")
                    while cur_l <= end_l:
                        current_date = cur_l.strftime("%Y-%m-%d")
                        if is_overall_approved or p_status == 'approved' or up_status == 'approved':
                            approved_leave_dates.add((l_uid, current_date))
                        if current_date in restored_set:
                            cur_l += timedelta(days=1)
                            continue

                        if is_half_leave:
                            if leave_is_paid_default:
                                if p_status != 'approved':
                                    cur_l += timedelta(days=1)
                                    continue
                                is_paid_day = True
                            else:
                                if up_status != 'approved':
                                    cur_l += timedelta(days=1)
                                    continue
                                is_paid_day = False
                        elif explicit_allocation:
                            is_in_paid_range = (
                                unpaid_days == 0 or (
                                    paid_days > 0 and paid_through_date is not None and current_date <= paid_through_date
                                )
                            )
                            if is_in_paid_range:
                                if p_status != 'approved':
                                    cur_l += timedelta(days=1)
                                    continue
                                is_paid_day = True
                            else:
                                if up_status != 'approved':
                                    cur_l += timedelta(days=1)
                                    continue
                                is_paid_day = False
                        else:
                            if leave_is_paid_default:
                                if p_status != 'approved':
                                    cur_l += timedelta(days=1)
                                    continue
                                is_paid_day = True
                            else:
                                if up_status != 'approved':
                                    cur_l += timedelta(days=1)
                                    continue
                                is_paid_day = False

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
                    dept_stats[d] = {'total_staff': 0, 'attended_staff': 0, 'present_days': 0, 'absent_days': 0, 'late_days': 0, 'paid_leave_days': 0, 'unpaid_leave_days': 0}
                dept_stats[d]['total_staff'] += 1

            records = []
            total_present_days_all = 0
            total_late_days_all = 0
            total_absent_days_all = 0
            total_paid_leave_days_all = 0
            total_unpaid_leave_days_all = 0
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
                emp_unpaid_leave_days = 0

                for cur_date in date_list:
                    while history_index < len(shift_history) and shift_history[history_index]['effective_from'] <= cur_date:
                        effective_shift = shift_history[history_index]
                        history_index += 1
                    att = att_map.get((uid, cur_date))
                    schedule_known = False
                    if att and att.get('record_shift_id') and att['record_shift_id'] in all_shifts_lookup:
                        rec_shift = all_shifts_lookup[att['record_shift_id']]
                        shift_id_for_day = rec_shift['id']
                        day_shift_name = rec_shift['name']
                        shift_start = rec_shift['start_time']
                        shift_end = rec_shift['end_time']
                        grace_mins = int(rec_shift.get('grace_period_minutes') or 15)
                        shift_working_days = set(_normalize_shift_working_days(rec_shift.get('working_days')))
                        schedule_known = True
                    elif effective_shift is not None:
                        shift_id_for_day = effective_shift['shift_id']
                        day_shift_name = effective_shift['shift_name']
                        shift_start = effective_shift['start_time']
                        shift_end = effective_shift['end_time']
                        grace_mins = int(effective_shift['grace_period_minutes'] or 15)
                        shift_working_days = set(_normalize_shift_working_days(effective_shift['working_days']))
                        schedule_known = True
                    else:
                        # Support pre-go-live and assigned shifts: if employee has an assigned shift, use it (QA-15)
                        legacy_shift_id = u.get('shift_id')
                        fallback_shift = all_shifts_lookup.get(legacy_shift_id) if legacy_shift_id else None
                        if fallback_shift:
                            shift_id_for_day = legacy_shift_id
                            day_shift_name = fallback_shift.get('name')
                            shift_start = fallback_shift.get('start_time') or '09:00'
                            shift_end = fallback_shift.get('end_time') or '17:00'
                            grace_mins = int(fallback_shift.get('grace_period_minutes') or 15)
                            shift_working_days = set(_normalize_shift_working_days(fallback_shift.get('working_days')))
                            schedule_known = True
                        else:
                            shift_id_for_day = None
                            day_shift_name = None
                            shift_start = '08:00'
                            shift_end = '20:00'
                            grace_mins = 15
                            shift_working_days = set()
                            schedule_known = False
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

                    # LAYER 5: Standardized business employment start date precedence
                    employed_from = get_employee_employment_start_date(u) or ''
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

                    # Priority order: Explicit Admin Manual Absent > Actual Face Attendance > Approved Leave > Unexcused Absence
                    if is_manual_absent and is_scheduled_workday:
                        status = 'absent'
                        first_seen = None
                        last_seen = None
                        loc = '-'
                        dev = '-'
                        checkin_cam = '-'
                        checkout_cam = '-'
                        emp_absent_days += 1
                        emp_working_days += 1
                    elif att:
                        # LV-05: If employee actually worked on an approved leave day, ensure entitlement is restored
                        # Do NOT restore half-day leave when employee works, as half-day leave expects them to work the other half (QA-45)
                        if leave_info and not leave_info.get('is_half_leave'):
                            _restore_worked_leave_if_applicable(cursor, uid, cur_date)
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

                        if leave_info and leave_info.get('is_half_leave'):
                            emp_present_days += 0.5
                            if leave_info.get('is_paid'):
                                emp_leave_days += 0.5
                            else:
                                emp_unpaid_leave_days += 0.5
                        else:
                            emp_present_days += 1
                        if is_scheduled_workday:
                            emp_working_days += 1
                    elif is_scheduled_workday and leave_info:
                        if leave_info.get('is_half_leave'):
                            if leave_info.get('is_paid'):
                                emp_leave_days += 0.5
                                status = 'on_leave'
                            else:
                                emp_unpaid_leave_days += 0.5
                                status = 'on_leave_unpaid'
                            emp_absent_days += 0.5
                        elif leave_info['is_paid']:
                            status = 'on_leave'
                            emp_leave_days += 1
                        else:
                            status = 'on_leave_unpaid'
                            emp_unpaid_leave_days += 1
                        first_seen = None
                        last_seen = None
                        loc = '-'
                        dev = '-'
                        checkin_cam = '-'
                        checkout_cam = '-'
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
                            # QA-50 & Section C #3: Today is NOT counted as absent before the shift ends
                            is_cur_today = (cur_date == datetime.now().strftime('%Y-%m-%d'))
                            shift_ended = True
                            if is_cur_today:
                                now_dt = datetime.now()
                                now_mins = now_dt.hour * 60 + now_dt.minute
                                try:
                                    s_end_m = parse_time_to_minutes(shift_end)
                                    s_start_m = parse_time_to_minutes(shift_start)
                                    if s_end_m < s_start_m:  # overnight
                                        shift_ended = (now_mins >= s_end_m and now_mins < s_start_m)
                                    else:
                                        shift_ended = (now_mins >= s_end_m)
                                except Exception:
                                    shift_ended = True

                            if is_cur_today and not shift_ended:
                                status = 'not_checked_in'
                            else:
                                status = 'absent'
                                emp_absent_days += 1
                            first_seen = None
                            last_seen = None
                            loc = '-'
                            dev = '-'
                            checkin_cam = '-'
                            checkout_cam = '-'
                            emp_working_days += 1

                    # ATT-05: Determine Day Type from existing sources and structured labels
                    day_type = None
                    if override_type == 'holiday':
                        day_type = 'Public Holiday'
                    elif (uid, cur_date) in leave_map or (uid, cur_date) in approved_leave_dates:
                        day_type = 'Leave Day'
                    elif not is_scheduled_workday or override_type == 'off_day':
                        day_type = 'Rest Day'
                    else:
                        day_type = 'Regular Working Day'

                    normalized_day_type = (
                        'public_holiday' if day_type == 'Public Holiday' else
                        'leave_day' if day_type == 'Leave Day' else
                        'rest_day' if day_type == 'Rest Day' else
                        'regular_working_day'
                    )

                    # Presentation status display (ATT-05: Present — Public Holiday / Leave Day / Rest Day)
                    if att:
                        base_st = 'Present' if status == 'present' else ('Late' if status == 'late' else 'Checked Out')
                        if day_type in ('Public Holiday', 'Leave Day', 'Rest Day'):
                            status_display_val = f"{base_st} — {day_type}"
                        elif half_leave_active or (leave_info and leave_info.get('is_half_leave')):
                            status_display_val = f"{base_st} + Half Leave"
                        else:
                            status_display_val = base_st
                    else:
                        if half_leave_active or (leave_info and leave_info.get('is_half_leave')):
                            status_display_val = f"{status.replace('_', ' ').title()} + Half Leave"
                        elif status == 'on_leave':
                            status_display_val = 'On Leave'
                        elif status == 'on_leave_unpaid':
                            status_display_val = 'Unpaid Leave'
                        elif status == 'holiday':
                            status_display_val = 'Holiday'
                        elif status in ('weekend', 'off_day'):
                            status_display_val = 'Weekend / Off'
                        else:
                            status_display_val = 'Absent'

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
                        'attendance_status': status,
                        'day_type': normalized_day_type,
                        'day_type_raw': day_type,
                        'day_type_label': status_display_val,
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
                        'status_display': status_display_val,
                        'day_type': day_type,
                        'half_leave_count': 1 if (half_leave_active or (leave_info and leave_info.get('is_half_leave'))) else 0,
                        'half_leave_type': leave_info.get('leave_type') if (leave_info and leave_info.get('is_half_leave')) else None,
                        'is_half_leave': bool(half_leave_active or (leave_info and leave_info.get('is_half_leave'))),
                        'half_leave_is_paid': bool(leave_info and leave_info.get('is_paid')),
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
                dept_stats[dept]['paid_leave_days'] += emp_leave_days
                dept_stats[dept]['unpaid_leave_days'] += emp_unpaid_leave_days

                total_present_days_all += emp_present_days
                total_late_days_all += emp_late_days
                total_absent_days_all += emp_absent_days
                total_paid_leave_days_all += emp_leave_days
                total_unpaid_leave_days_all += emp_unpaid_leave_days

                effective_working_days = max(emp_working_days, 1) if emp_working_days > 0 else total_days
                emp_rate = round((emp_present_days / effective_working_days * 100), 1) if effective_working_days > 0 else 0.0

                # Filter matching logic
                if search_query and search_query.strip():
                    sq = search_query.strip().lower()
                    if not (sq in u['name'].lower() or sq in dept.lower() or sq in (u['position'] or '').lower() or sq in str(uid)):
                        continue

                if status_filter and status_filter.lower() != 'all':
                    sf = status_filter.lower().strip()
                    if not is_multi_day:
                        st_today = emp_daily_records[0].get('status', 'absent') if emp_daily_records else 'absent'
                        dt_today = (emp_daily_records[0].get('day_type') or '') if emp_daily_records else ''
                        if sf == 'present' and st_today not in ('present', 'checked_out'):
                            continue
                        elif sf == 'absent' and st_today != 'absent':
                            continue
                        elif sf == 'late' and st_today != 'late':
                            continue
                        elif sf == 'checked_out' and st_today != 'checked_out':
                            continue
                        elif sf in ('leave', 'on_leave') and st_today not in ('on_leave', 'on_leave_unpaid'):
                            continue
                        elif sf == 'paid_leave' and st_today != 'on_leave':
                            continue
                        elif sf == 'unpaid_leave' and st_today != 'on_leave_unpaid':
                            continue
                        elif sf in ('holiday', 'public_holiday') and dt_today != 'public_holiday':
                            continue
                        elif sf in ('rest_day', 'off_day') and dt_today != 'rest_day':
                            continue
                    else:
                        if sf == 'present' and emp_present_days == 0:
                            continue
                        elif sf == 'absent' and emp_absent_days == 0:
                            continue
                        elif sf == 'late' and emp_late_days == 0:
                            continue
                        elif sf == 'checked_out' and not any(d.get('status') == 'checked_out' for d in emp_daily_records):
                            continue
                        elif sf in ('leave', 'on_leave') and (emp_leave_days + emp_unpaid_leave_days) == 0:
                            continue
                        elif sf == 'paid_leave' and emp_leave_days == 0:
                            continue
                        elif sf == 'unpaid_leave' and emp_unpaid_leave_days == 0:
                            continue
                        elif sf in ('holiday', 'public_holiday') and not any(d.get('day_type') == 'public_holiday' for d in emp_daily_records):
                            continue
                        elif sf in ('rest_day', 'off_day') and not any(d.get('day_type') == 'rest_day' for d in emp_daily_records):
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
                    # ONE ROW PER EMPLOYEE (ATT-03: unique classification per day)
                    unpaid_leave_days = emp_unpaid_leave_days
                    total_leave_days = emp_leave_days + unpaid_leave_days
                    consistent_working_days = emp_working_days
                    consistent_absent_days = emp_absent_days
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
                        'working_days': consistent_working_days,
                        'present_days': emp_present_days,
                        'absent_days': consistent_absent_days,
                        'late_days': emp_late_days,
                        'leave_days': total_leave_days,
                        'paid_leave_days': emp_leave_days,
                        'unpaid_leave_days': unpaid_leave_days,
                        'total_leave_days': total_leave_days,
                        'payable_days': min(consistent_working_days, emp_present_days + emp_leave_days),
                        'attendance_rate': emp_rate,
                        'present_display': f"{emp_present_days} / {consistent_working_days}",
                        'absent_display': f"{consistent_absent_days} / {consistent_working_days}",
                        'daily_records': emp_daily_records
                    })
                else:
                    # Single day view: 1 row for that day
                    day_entry = emp_daily_records[0] if emp_daily_records else {}
                    st_val = day_entry.get('status', 'absent')
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
                        'status': st_val,
                        'attendance_status': st_val,
                        'day_type': day_entry.get('day_type'),
                        'day_type_raw': day_entry.get('day_type_raw') or day_entry.get('day_type'),
                        'day_type_label': day_entry.get('day_type_label') or day_entry.get('status_display'),
                        'status_display': day_entry.get('status_display'),
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
                        'paid_leave_days': int(st_val == 'on_leave'),
                        'unpaid_leave_days': int(st_val == 'on_leave_unpaid'),
                        'total_leave_days': int(st_val in ('on_leave', 'on_leave_unpaid')),
                        'leave_days': int(st_val in ('on_leave', 'on_leave_unpaid')),
                        'present_days': int(st_val in ('present', 'late', 'checked_out')),
                        'absent_days': int(st_val == 'absent'),
                        'payable_days': int(st_val in ('present', 'late', 'checked_out', 'on_leave')),
                        'working_days': int(bool(day_entry.get('scheduled_workday'))),
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
                    'paid_leave_days': ddata.get('paid_leave_days', 0),
                    'unpaid_leave_days': ddata.get('unpaid_leave_days', 0),
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
                    'working_days': record.get('working_days', 0),
                    'attendance_rate': record.get('attendance_rate', 0.0),
                    'status': record.get('status', 'absent'),
                    'day_type': record.get('day_type'),
                    'status_display': record.get('status_display'),
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
                    'total_days': total_days,
                    'duration_days': total_days,
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
                    'total_paid_leave_days': total_paid_leave_days_all,
                    'total_unpaid_leave_days': total_unpaid_leave_days_all,
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
            actual_date = target_date[:10] if target_date else datetime.now().date().isoformat()
            validate_employment_dates(cursor, user_id, actual_date, activity_name='Attendance')
            raise_if_payroll_month_locked(lock_day, organization_id or user_org, user_branch)
            user_branch = u_row[1] or 'DEFAULT_BRANCH'
            if organization_id and organization_id != 'all':
                if organization_id != 'DEFAULT_ORG' and user_org != organization_id:
                    return False
            actual_date = target_date[:10] if target_date else datetime.now().date().isoformat()
            if target_date:
                cursor.execute('''
                    DELETE FROM attendance 
                    WHERE user_id = ? AND date(timestamp) = ?
                ''', (user_id, actual_date))
                deleted_count = cursor.rowcount

                cursor.execute('''
                    INSERT OR REPLACE INTO manual_absent (user_id, date, organization_id, branch_id)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, actual_date, user_org, user_branch))
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

            # ATT-02: Re-apply leave deduction if this date was previously restored when employee was marked Present
            _unrestore_worked_leave_if_applicable(cursor, user_id, actual_date)

            # OT-02: When attendance becomes Absent, remove/cancel all associated overtime
            now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("""
                SELECT id, status, reason
                FROM overtime
                WHERE user_id = ? AND ot_date = ?
            """, (user_id, actual_date))
            for ot_row in cursor.fetchall():
                ot_id = ot_row[0] if isinstance(ot_row, (tuple, list)) else ot_row['id']
                ot_st = (ot_row[1] if isinstance(ot_row, (tuple, list)) else ot_row['status'] or 'pending').lower()
                ot_reason = str(ot_row[2] if isinstance(ot_row, (tuple, list)) else ot_row['reason'] or '')
                is_auto = (
                    ot_reason.startswith('Automatic camera checkout') or
                    ot_reason.startswith('Manual attendance checkout')
                )
                if is_auto and ot_st == 'pending':
                    cursor.execute("DELETE FROM overtime WHERE id = ?", (ot_id,))
                    logger.info(f"OT-02: Removed automatic pending overtime ID #{ot_id} for user #{user_id} on {actual_date} due to absent status")
                elif ot_st in ('pending', 'approved'):
                    cursor.execute("""
                        UPDATE overtime
                        SET status = 'cancelled_absent',
                            rejection_reason = 'Attendance marked absent',
                            rejected_by = 'System',
                            rejected_at = COALESCE(rejected_at, ?),
                            audit_trail = COALESCE(audit_trail || CHAR(10), '') || ?
                        WHERE id = ?
                    """, (now_ts, f"[{now_ts}] Status changed from '{ot_st}' to 'cancelled_absent' by System (attendance marked absent).", ot_id))
                    logger.info(f"OT-02: Cancelled overtime ID #{ot_id} ({ot_st}) for user #{user_id} on {actual_date} due to absent status")

            conn.commit()
        if postgres_is_configured():
            try:
                import postgres_mirror as cloud_pg
                cloud_pg.delete_pg_attendance_events(user_id, actual_date, organization_id=user_org)
            except Exception as pg_err:
                logger.warning("Failed to delete attendance events from cloud: %s", pg_err)
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
        actual_date = str(target_date)[:10]
        
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
            validate_employment_dates(cursor, user_id, actual_date, activity_name='Attendance')
            raise_if_payroll_month_locked(actual_date, organization_id or user_org, user_branch)
            if organization_id and organization_id != 'all':
                if organization_id != 'DEFAULT_ORG' and user_org != organization_id:
                    return False
            
            # Clear manual absent and restore worked leave entitlement
            cursor.execute('DELETE FROM manual_absent WHERE user_id = ? AND date = ?', (user_id, actual_date))
            _restore_worked_leave_if_applicable(cursor, user_id, actual_date)
            
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
                ot_hours = 0.0 if _overtime_date_is_future(actual_date) else calculate_eligible_overtime(shift_start, shift_end, co_clean, checkout_grace)
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

        if postgres_is_configured():
            try:
                import postgres_mirror as cloud_pg
                cloud_pg.delete_pg_manual_absent(user_id, actual_date, organization_id=user_org)
                cloud_pg.mirror_manual_attendance(user_id, actual_date, ci_clean, co_clean, shift_db_id, notes, marked_by, user_org, user_branch)
            except Exception as pg_err:
                logger.warning("Failed to sync manual attendance to cloud: %s", pg_err)

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
        import postgres_mirror as cloud_pg
        if cloud_pg.postgres_is_configured():
            pg_res = cloud_pg.fetch_pg_employee(user_id, organization_id)
            if pg_res is False:
                return None
    except Exception as e:
        logger.warning("Postgres unreachable for get_user_by_id; falling back to SQLite cache: %s", e)
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


def mobile_password_for_name(name: str) -> str:
    """Employee phone password: first name, lowercase, then @123. Salman -> salman@123."""
    for part in re.split(r'\s+', str(name or '').strip()):
        letters = re.sub(r'[^A-Za-z]', '', part).lower()
        if len(letters) >= 2:
            return f'{letters}@123'
    return ''


def assign_mobile_employee_password(user_id: int, organization_id: str, password: str) -> bool:
    """Store the generated phone password for one employee in this organization."""
    if not re.fullmatch(r'[a-z]+@123', str(password or '')):
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE users
                      SET password = NULL, password_hash = ?, mobile_login = 1
                    WHERE id = ? AND active = 1
                      AND COALESCE(organization_id, 'DEFAULT_ORG') = ?""",
                (generate_password_hash(password), int(user_id), organization_id or 'DEFAULT_ORG'),
            )
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Assign mobile password error: {e}")
        return False


def generate_mobile_credentials(
    organization_id: str,
    branch_id: Optional[str] = None,
    remaining_only: bool = False,
) -> Dict[str, Any]:
    """Give employees a phone login. Username is their email. Password is name@123.

    remaining_only keeps people who already have a mobile login and creates
    logins only for employees added after the last generate.
    """
    already_ids = set()
    if remaining_only:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """SELECT id FROM users
                   WHERE COALESCE(mobile_login, 0) = 1
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?""",
                (organization_id or 'DEFAULT_ORG',),
            ).fetchall()
            already_ids = {row[0] for row in rows}
    users = get_all_users(organization_id=organization_id, branch_id=branch_id)
    credentials = []
    skipped = []
    seen_emails = {}
    for user in users:
        email = str(user.get('email') or '').strip().lower()
        if email:
            seen_emails[email] = seen_emails.get(email, 0) + 1
    for user in users:
        name = str(user.get('name') or '').strip()
        email = str(user.get('email') or '').strip().lower()
        password = mobile_password_for_name(name)
        if not email:
            skipped.append({'name': name or 'Employee', 'reason': 'No email address'})
            continue
        if seen_emails.get(email, 0) > 1:
            skipped.append({'name': name or email, 'reason': 'Email is used by more than one employee'})
            continue
        if not password:
            skipped.append({'name': name or email, 'reason': 'Name cannot be turned into a password'})
            continue
        if remaining_only and int(user['id']) in already_ids:
            continue
        if not assign_mobile_employee_password(int(user['id']), organization_id, password):
            skipped.append({'name': name or email, 'reason': 'Password could not be saved'})
            continue
        credentials.append({
            'name': name,
            'username': email,
            'password': password,
        })
    return {'credentials': credentials, 'skipped': skipped}


def list_mobile_credentials(
    organization_id: str,
    branch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return phone logins already saved for this company. The password is the same name@123."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT name, email
                  FROM users
                 WHERE active = 1
                   AND COALESCE(mobile_login, 0) = 1
                   AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
            """
            params: List[Any] = [organization_id or 'DEFAULT_ORG']
            if branch_id and branch_id not in ('all', 'DEFAULT_BRANCH'):
                query += " AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?"
                params.append(branch_id)
            query += " ORDER BY name COLLATE NOCASE, id"
            rows = conn.execute(query, params).fetchall()
    except Exception as error:
        logger.error(f"List mobile credentials error: {error}")
        return {'credentials': []}
    credentials = []
    for row in rows:
        name = str(row['name'] or '').strip()
        email = str(row['email'] or '').strip().lower()
        password = mobile_password_for_name(name)
        if not email or not password:
            continue
        credentials.append({'name': name, 'username': email, 'password': password})
    return {'credentials': credentials}


def authenticate_employee_email(email: str, password: str) -> Optional[Dict]:
    """Log in with the employee email and the portal password (first name + @123)."""
    email_key = re.sub(r'\s+', '', str(email or '').strip().lower())
    pw_str = str(password or '').strip()
    if not email_key or not pw_str:
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM users").fetchall()
        matches = []
        for row in rows:
            user = dict(row)
            stored_email = re.sub(r'\s+', '', str(user.get('email') or '').strip().lower())
            if stored_email != email_key:
                continue
            password_hash = user.get('password_hash')
            expected = mobile_password_for_name(user.get('name'))
            hash_ok = False
            if password_hash:
                try:
                    hash_ok = check_password_hash(password_hash, pw_str)
                except Exception:
                    hash_ok = str(password_hash).strip() == pw_str
            name_ok = bool(expected) and pw_str.lower() == expected.lower()
            if hash_ok or name_ok:
                user.pop('password', None)
                user.pop('password_hash', None)
                matches.append(user)
        if not matches:
            return None

        def _login_rank(user: Dict) -> tuple:
            org = str(user.get('organization_id') or '').strip()
            real_org = 1 if org.startswith('ORG-') else 0
            mobile_flag = 1 if int(user.get('mobile_login') or 0) == 1 else 0
            try:
                user_no = int(user.get('id') or 0)
            except (TypeError, ValueError):
                user_no = 0
            return (real_org, mobile_flag, user_no)

        matches.sort(key=_login_rank, reverse=True)
        return matches[0]
    except Exception as e:
        logger.error(f"Employee email login error: {e}")
        return None


def _clock_label(value: Any) -> str:
    text = str(value or '').strip()
    if not text or text.lower() in {'none', 'null', '-', '--', '--:--', 'n/a'}:
        return ''
    if ' ' in text:
        text = text.split(' ')[-1]
    if 'T' in text:
        text = text.split('T')[-1]
    text = text.split('.')[0]
    parts = text.split(':')
    if len(parts) < 2:
        return ''
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return ''
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ''
    suffix = 'AM' if hour < 12 else 'PM'
    hour12 = hour % 12 or 12
    return f'{hour12:02d}:{minute:02d} {suffix}'


def _worked_label(first_seen: Any, last_seen: Any) -> str:
    def _parsed(value: Any):
        text = str(value or '').strip().split('.')[0].replace('T', ' ')
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None
    start = _parsed(first_seen)
    end = _parsed(last_seen)
    if not start or not end or end <= start:
        return ''
    minutes = int((end - start).total_seconds() // 60)
    hours, mins = divmod(minutes, 60)
    return f'{hours}h {mins}m'


def _current_shift_for_mobile(user_id: int, organization_id: str) -> Dict[str, str]:
    """This company's live shift for the employee. Never another company's leftover shift."""
    org = str(organization_id or '').strip()
    empty = {'shift_name': '', 'shift_start': '', 'shift_end': ''}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT s.name, s.start_time, s.end_time
                  FROM users u
                  JOIN shifts s
                    ON s.id = u.shift_id
                   AND s.organization_id = u.organization_id
                 WHERE u.id = ? AND u.active = 1
                   AND (? = '' OR u.organization_id = ?)
                """,
                (int(user_id), org, org),
            ).fetchone()
            if not row and org:
                row = conn.execute(
                    """
                    SELECT name, start_time, end_time
                      FROM shifts
                     WHERE organization_id = ? AND COALESCE(active, 1) = 1
                     ORDER BY COALESCE(is_default, 0) DESC, id DESC
                     LIMIT 1
                    """,
                    (org,),
                ).fetchone()
    except Exception as error:
        logger.error(f"Mobile shift lookup error: {error}")
        return empty
    if not row:
        return empty
    return {
        'shift_name': str(row['name'] or '').strip(),
        'shift_start': str(row['start_time'] or '').strip(),
        'shift_end': str(row['end_time'] or '').strip(),
    }


def get_latest_employee_punch(user_id: int, organization_id: str, branch_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve the most recent punch event for the employee today with greeting message."""
    today_str = date.today().isoformat()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT a.id, a.user_id, a.timestamp, a.source, a.location, a.device_id,
                       u.name
                FROM attendance a
                LEFT JOIN users u ON a.user_id = u.id
                WHERE a.user_id = ?
                  AND a.timestamp >= ?
                  AND (a.organization_id = ? OR a.organization_id IS NULL OR a.organization_id = 'DEFAULT_ORG')
                ORDER BY a.timestamp DESC, a.id DESC
                LIMIT 1
                """,
                (int(user_id), f"{today_str} 00:00:00", organization_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            punch_time_raw = str(row['timestamp'] or '')
            punch_clock = _clock_label(punch_time_raw) or punch_time_raw
            full_name = str(row['name'] or 'Employee').strip()
            first_name = full_name.split()[0] if full_name else 'there'
            
            cursor.execute(
                """
                SELECT COUNT(*) FROM attendance
                WHERE user_id = ? AND timestamp >= ?
                """,
                (int(user_id), f"{today_str} 00:00:00"),
            )
            count = int(cursor.fetchone()[0] or 1)
            
            try:
                dt = datetime.fromisoformat(punch_time_raw.replace('Z', ''))
                hour = dt.hour
            except Exception:
                hour = datetime.now().hour

            if hour < 12:
                greeting = "Good morning"
            elif hour < 17:
                greeting = "Good afternoon"
            else:
                greeting = "Good evening"

            if count <= 1:
                punch_type = "in"
                title = "Check-in Confirmed"
                message = f"{greeting}, {first_name}! Checked in at {punch_clock}."
            else:
                punch_type = "out"
                title = "Check-out Confirmed"
                message = f"Checked out at {punch_clock}. Have a great day, {first_name}!"

            return {
                'id': row['id'],
                'user_id': row['user_id'],
                'employee_name': full_name,
                'first_name': first_name,
                'timestamp': punch_time_raw,
                'punch_time': punch_clock,
                'punch_type': punch_type,
                'punch_count': count,
                'location': row['location'] or 'Office Entrance',
                'device_id': row['device_id'] or 'Camera #1',
                'title': title,
                'message': message,
            }
    except Exception as e:
        logger.error(f"Error fetching latest employee punch: {e}")
        return None


def employee_mobile_attendance(user_id: int, organization_id: str, branch_id: str) -> Dict[str, Any]:
    """Today's attendance for one employee, using the report the portal already builds."""
    user = get_user_by_id(int(user_id), organization_id=organization_id) or {}
    report = get_detailed_attendance_report(
        target_date='today',
        user_id=int(user_id),
        organization_id=organization_id,
        branch_id=branch_id,
    )
    row = (report.get('records') or [{}])[0] if isinstance(report, dict) else {}
    raw_status = str(row.get('status') or 'absent').strip().lower()
    if raw_status == 'checked_out':
        status = 'Checked Out'
    elif raw_status in {'present', 'late'}:
        status = 'Present'
    elif raw_status in {'on_leave', 'on_leave_unpaid'}:
        status = 'On Leave'
    else:
        status = 'Absent'
    shift = _current_shift_for_mobile(int(user_id), organization_id)
    shift_start = shift.get('shift_start') or ''
    shift_end = shift.get('shift_end') or ''
    start_label = _clock_label(shift_start) or shift_start
    end_label = _clock_label(shift_end) or shift_end
    timing = f'{start_label} - {end_label}' if start_label and end_label else ''
    punch_info = get_latest_employee_punch(int(user_id), organization_id, branch_id)
    return {
        'name': row.get('name') or user.get('name') or '',
        'designation': row.get('position') or user.get('position') or '',
        'department': row.get('department') or user.get('department') or '',
        'shift_name': shift.get('shift_name') or '',
        'shift_timing': timing,
        'shift_start': shift_start,
        'shift_end': shift_end,
        'check_in': _clock_label(row.get('first_seen')),
        'check_out': _clock_label(row.get('last_seen')),
        'raw_check_in': row.get('first_seen'),
        'raw_check_out': row.get('last_seen'),
        'status': status,
        'working_time': _worked_label(row.get('first_seen'), row.get('last_seen')),
        'date': row.get('date') or report.get('start_date') or '',
        'latest_punch': punch_info,
    }


def employee_mobile_month_attendance(user_id: int, organization_id: str, branch_id: str, target_month: Optional[str] = None) -> Dict[str, Any]:
    """Return each day's attendance for the employee for this month."""
    if not target_month:
        target_month = datetime.now().strftime("%Y-%m")

    try:
        y, m = target_month.split('-')
        start_date = f"{y}-{m}-01"
        import calendar
        _, last_day = calendar.monthrange(int(y), int(m))
        end_date = f"{y}-{m}-{last_day:02d}"
    except Exception:
        start_date = datetime.now().strftime("%Y-%m-01")
        end_date = datetime.now().strftime("%Y-%m-%d")

    report = get_detailed_attendance_report(
        start_date=start_date,
        end_date=end_date,
        user_id=int(user_id),
        organization_id=organization_id,
        branch_id=branch_id,
    )
    records = report.get('records') or []
    rec = records[0] if records else {}
    daily_records = rec.get('daily_records') or []

    days = []
    for day in daily_records:
        raw_st = str(day.get('status') or 'absent').strip().lower()
        if raw_st == 'checked_out':
            status = 'Checked Out'
        elif raw_st in {'present', 'late'}:
            status = 'Present'
        elif raw_st in {'on_leave', 'on_leave_unpaid'}:
            status = 'On Leave'
        elif raw_st in {'holiday', 'off_day', 'weekend'}:
            status = 'Off Day' if raw_st == 'off_day' else ('Holiday' if raw_st == 'holiday' else 'Weekend')
        else:
            status = 'Absent'

        days.append({
            'date': day.get('date'),
            'date_formatted': day.get('date_formatted') or day.get('date'),
            'day': day.get('day') or '',
            'check_in': _clock_label(day.get('first_seen')) or '--:--',
            'check_out': _clock_label(day.get('last_seen')) or '--:--',
            'status': status,
            'working_time': _worked_label(day.get('first_seen'), day.get('last_seen')) or '--',
        })

    return {
        'month': target_month,
        'present_days': rec.get('present_days', 0),
        'absent_days': rec.get('absent_days', 0),
        'late_days': rec.get('late_days', 0),
        'leave_days': rec.get('leave_days', 0),
        'working_days': rec.get('working_days', 0),
        'days': days,
    }


def employee_mobile_profile(user_id: int, organization_id: str, branch_id: str, company_name: str = '') -> Dict[str, Any]:
    """Return employee profile details for mobile view."""
    user = get_user_by_id(int(user_id), organization_id=organization_id) or {}
    shift = _current_shift_for_mobile(int(user_id), organization_id)
    shift_start = shift.get('shift_start') or ''
    shift_end = shift.get('shift_end') or ''
    start_label = _clock_label(shift_start) or shift_start
    end_label = _clock_label(shift_end) or shift_end
    timing = f'{start_label} - {end_label}' if start_label and end_label else ''
    return {
        'id': user.get('id'),
        'name': user.get('name') or '',
        'email': user.get('email') or '',
        'phone': user.get('phone') or '',
        'designation': user.get('position') or 'Staff Member',
        'department': user.get('department') or 'General',
        'shift_name': shift.get('shift_name') or 'Morning Shift',
        'shift_timing': timing,
        'company': company_name or 'QuantEye Attendance',
        'join_date': user.get('join_date') or user.get('enrollment_date') or '',
    }


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
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    status: str = 'pending',
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
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
            u_org = organization_id or (u_row[0] if u_row else None) or 'DEFAULT_ORG'
            u_branch = branch_id or (u_row[1] if u_row else None) or 'DEFAULT_BRANCH'
            raise_if_payroll_month_locked(s_str, u_org, u_branch)
            cursor.execute(
                     '''INSERT INTO leave_requests (
                                user_id, user_name, leave_type, leave_duration, start_date, end_date, reason,
                                paid_days, unpaid_days, quota_days, paid_through_date, quota_through_date,
                                half_leave_quota_type_id, half_leave_quota_charge, organization_id, branch_id,
                                start_time, end_time, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                         (user_id, user_name, leave_type, leave_duration, s_str, e_str, reason,
                      paid_days, unpaid_days, quota_days, paid_through_date, quota_through_date,
                      half_leave_quota_type_id, int(bool(half_leave_quota_charge)), u_org, u_branch,
                      start_time, end_time, status)
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
                "status": status,
                "organization_id": u_org,
                "branch_id": u_branch,
            })
            return leave_id
    except ValueError:
        raise
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
            for _, start_date, _, _ in requests:
                raise_if_payroll_month_locked(start_date, u_org, u_branch)
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
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Add leave request segments error: {e}")
        return None


def get_leave_requests(user_id: Optional[int] = None, status: Optional[str] = None, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict]:
    """Get list of leave requests optionally filtered by user_id, status, organization_id, and branch_id."""
    if organization_id and organization_id not in ('all', 'DEFAULT_ORG'):
        if postgres_is_configured():
            try:
                import postgres_mirror as cloud_pg
                cloud_pg.fetch_pg_leave_requests(organization_id, branch_id, user_id, status)
            except Exception as error:
                logger.warning("Postgres unreachable for leave requests; falling back to SQLite cache: %s", error)
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
                st_lower = status.strip().lower()
                if st_lower == 'active':
                    query += " AND LOWER(COALESCE(status, 'pending')) IN ('pending', 'approved', 'partially_approved') AND status != 'cancelled_worked'"
                else:
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

                # LV-06: Derive approved, pending, rejected, paid approved, unpaid approved breakdowns
                p_days = int(leave_request.get('paid_days') or 0)
                u_days = int(leave_request.get('unpaid_days') or 0)
                st = str(leave_request.get('status') or 'pending').lower()
                p_st = str(leave_request.get('paid_status') or ('approved' if st == 'approved' else 'pending')).lower()
                u_st = str(leave_request.get('unpaid_status') or ('approved' if st == 'approved' else 'pending')).lower()

                paid_appr = p_days if p_st == 'approved' else 0
                unpaid_appr = u_days if u_st == 'approved' else 0
                paid_pend = p_days if p_st == 'pending' else 0
                unpaid_pend = u_days if u_st == 'pending' else 0
                paid_rej = p_days if p_st == 'rejected' else 0
                unpaid_rej = u_days if u_st == 'rejected' else 0

                leave_request['paid_approved_days'] = paid_appr
                leave_request['unpaid_approved_days'] = unpaid_appr
                leave_request['approved_days'] = paid_appr + unpaid_appr
                leave_request['pending_days'] = paid_pend + unpaid_pend
                leave_request['rejected_days'] = paid_rej + unpaid_rej
            return requests
    except Exception as e:
        logger.error(f"Get leave requests error: {e}")
        return []


def get_leave_by_id(leave_id: int, organization_id: Optional[str] = None) -> Optional[Dict]:
    """Retrieve single leave request by id with tenant scoping."""
    try:
        import postgres_mirror as cloud_pg
        if cloud_pg.postgres_is_configured():
            pg_res = cloud_pg.fetch_pg_leave(leave_id, organization_id)
            if pg_res is False:
                return None
    except Exception as e:
        logger.warning("Postgres unreachable for get_leave_by_id; falling back to SQLite cache: %s", e)
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


def update_leave_status(
    leave_id: int,
    status: str = 'approved',
    approved_by: str = 'Admin',
    organization_id: Optional[str] = None,
    portion: Optional[str] = None,
    rejection_reason: Optional[str] = None,
) -> bool:
    """Update status unless it would create overlapping active leave, verifying tenant authorization.
    Supports portion-based approval (LV-01, LV-02) and rejection reasons (LV-03)."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('BEGIN IMMEDIATE')
            cursor.execute(
                """SELECT user_id, start_date, end_date, organization_id, branch_id,
                          paid_days, unpaid_days, paid_status, unpaid_status, status, leave_type
                   FROM leave_requests WHERE id = ?""",
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
                leave['branch_id'] if 'branch_id' in leave.keys() else None,
            )

            norm_status = str(status or 'approved').strip().lower()
            norm_portion = str(portion or 'all').strip().lower() if portion else 'all'
            
            current_paid_status = leave['paid_status'] or ('approved' if leave['status'] == 'approved' else 'pending')
            current_unpaid_status = leave['unpaid_status'] or ('approved' if leave['status'] == 'approved' else 'pending')
            paid_days = int(leave['paid_days'] or 0) if leave['paid_days'] is not None else (0 if leave['leave_type'] == 'unpaid' else 1)
            unpaid_days = int(leave['unpaid_days'] or 0) if leave['unpaid_days'] is not None else (1 if leave['leave_type'] == 'unpaid' else 0)

            if norm_portion == 'paid':
                new_paid_status = norm_status
                new_unpaid_status = current_unpaid_status
            elif norm_portion == 'unpaid':
                new_paid_status = current_paid_status
                new_unpaid_status = norm_status
            else:
                new_paid_status = norm_status
                new_unpaid_status = norm_status

            # Derive overall status
            if unpaid_days > 0 and paid_days > 0:
                if new_paid_status == 'approved' and new_unpaid_status == 'approved':
                    overall_status = 'approved'
                elif new_paid_status == 'rejected' and new_unpaid_status == 'rejected':
                    overall_status = 'rejected'
                elif new_paid_status == 'approved' or new_unpaid_status == 'approved':
                    overall_status = 'partially_approved'
                elif new_paid_status == 'rejected' or new_unpaid_status == 'rejected':
                    overall_status = 'partially_rejected'
                else:
                    overall_status = 'pending'
            elif unpaid_days > 0:
                overall_status = new_unpaid_status
            else:
                overall_status = new_paid_status

            if overall_status in ('pending', 'approved', 'partially_approved'):
                conflict = _find_active_leave_overlap(
                    cursor, leave['user_id'], leave['start_date'], leave['end_date'], exclude_id=leave_id
                )
                if conflict:
                    logger.warning(
                        f"Cannot set leave request #{leave_id} to {overall_status}: dates overlap active request #{conflict['id']}"
                    )
                    return False

            reason_val = str(rejection_reason).strip() if (rejection_reason and norm_status == 'rejected') else None
            cursor.execute(
                """UPDATE leave_requests
                   SET status = ?, paid_status = ?, unpaid_status = ?, approved_by = ?,
                       rejection_reason = COALESCE(?, rejection_reason)
                   WHERE id = ?""",
                (overall_status, new_paid_status, new_unpaid_status, approved_by, reason_val, leave_id)
            )
            conn.commit()
            updated = cursor.rowcount > 0
            if updated:
                full_leave = cursor.execute("SELECT * FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()
                if full_leave:
                    _cloud_mirror("mirror_leave", dict(full_leave))
            return updated
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


def update_company_holiday(
    holiday_id: int,
    name: str,
    holiday_date: str,
    end_date: Optional[str] = None,
    description: str = '',
    is_recurring: int = 0,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> bool:
    """Update an existing company holiday within its owning tenant and branch."""
    try:
        if not end_date:
            end_date = holiday_date
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            query = """
                UPDATE company_holidays
                   SET name = ?, holiday_date = ?, end_date = ?, description = ?, is_recurring = ?
                 WHERE id = ?
            """
            params: List[Any] = [name, holiday_date, end_date, description, is_recurring, holiday_id]
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
        logger.error(f"Update company holiday error: {e}")
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
                               half_leave_quota_type_id, half_leave_quota_charge, restored_dates FROM leave_requests
                        WHERE user_id = ? AND (status = 'approved' OR paid_status = 'approved') AND (leave_type = ? OR leave_type = ?)
                          AND end_date >= ? AND start_date <= ?
                    """, (u['id'], lt['code'], lt['name'], cycle_start, cycle_end))
                    actual_used = 0.0
                    for request_row in cursor.fetchall():
                        restored_dates_raw = request_row['restored_dates'] if 'restored_dates' in request_row.keys() else ''
                        restored_set = set(d.strip() for d in (restored_dates_raw or '').split(',') if d.strip())
                        if request_row['leave_duration'] == 'half':
                            if request_row['start_date'] in restored_set:
                                continue
                            if request_row['half_leave_quota_charge'] and request_row['half_leave_quota_type_id'] == lt['id']:
                                actual_used += 1.0
                            else:
                                actual_used += 0.5
                            continue
                        clipped_start = max(request_row['start_date'], cycle_start)
                        clipped_end = min(request_row['end_date'], cycle_end)
                        if request_row['quota_days'] is None:
                            scheduled_days = _count_scheduled_leave_days(cursor, u['id'], clipped_start, clipped_end)
                            restored_in_range = sum(1 for d in restored_set if clipped_start <= d <= clipped_end)
                            actual_used += max(0.0, scheduled_days - restored_in_range)
                        else:
                            quota_end = min(clipped_end, request_row['quota_through_date'] or request_row['end_date'])
                            if quota_end >= clipped_start:
                                scheduled_days = _count_scheduled_leave_days(cursor, u['id'], clipped_start, quota_end)
                                restored_in_range = sum(1 for d in restored_set if clipped_start <= d <= quota_end)
                                actual_used += max(0.0, scheduled_days - restored_in_range)
                    cursor.execute("""
                        SELECT COUNT(*) FROM leave_requests
                        WHERE user_id = ? AND (status = 'approved' OR paid_status = 'approved') AND leave_duration = 'half'
                          AND half_leave_quota_charge = 1 AND half_leave_quota_type_id = ?
                          AND (leave_type != ? AND leave_type != ?)
                          AND end_date >= ? AND start_date <= ?
                    """, (u['id'], lt['id'], lt['code'], lt['name'], cycle_start, cycle_end))
                    external_half_quota_charges = int(cursor.fetchone()[0] or 0)
                    actual_used += external_half_quota_charges
                    used_val = float(actual_used)

                    half_rule_res = resolve_half_leave_rule(
                        cursor, u['id'], lt, datetime(year, reference_month, 1)
                    )
                    half_leave_count = half_rule_res['used_count']
                    half_leave_remaining = half_rule_res['remaining_count']
                    half_allowance = half_rule_res['allowance']

                    # LV-08: Distinguish standard entitlement from employee-specific and request-specific quota overrides
                    standard_quota = float(lt.get('yearly_allowance') or 0.0)
                    allocated_override = round(alloc - standard_quota, 2) if alloc != standard_quota else 0.0

                    cursor.execute("""
                        SELECT id, start_date, end_date, quota_days, paid_days, leave_type, reason
                        FROM leave_requests
                        WHERE user_id = ? AND (status = 'approved' OR paid_status = 'approved')
                          AND (leave_type = ? OR leave_type = ? OR leave_type = 'excess_paid')
                          AND end_date >= ? AND start_date <= ?
                          AND (
                              (paid_days > quota_days AND quota_days IS NOT NULL)
                              OR leave_type = 'excess_paid'
                          )
                    """, (u['id'], lt['code'], lt['name'], cycle_start, cycle_end))
                    req_overrides = []
                    request_override_days = 0.0
                    for ro in cursor.fetchall():
                        o_days = float((ro['paid_days'] or 0) - (ro['quota_days'] or 0)) if ro['leave_type'] != 'excess_paid' else float(ro['paid_days'] or 0)
                        if o_days > 0:
                            request_override_days += o_days
                            req_overrides.append({
                                'request_id': ro['id'],
                                'start_date': ro['start_date'],
                                'end_date': ro['end_date'],
                                'override_days': o_days,
                                'reason': ro['reason'] or ''
                            })
                    total_override_days = round(max(allocated_override, 0.0) + request_override_days, 2)

                    u_balances['balances'][lt['code']] = {
                        'type_name': lt['name'],
                        'standard_quota': standard_quota,
                        'allocated': alloc,
                        'allocated_override': allocated_override,
                        'request_overrides': req_overrides,
                        'total_override_days': total_override_days,
                        'used': used_val,
                        'remaining': max(0.0, alloc - used_val),
                        'half_leave_count': half_leave_count,
                        'half_leave_remaining': half_leave_remaining,
                        'half_leave_enabled': half_rule_res['half_leave_enabled'],
                        'half_leave_mode': half_rule_res.get('half_leave_mode', lt.get('half_leave_mode', 'separate')),
                        'half_leave_allowance_count': half_allowance,
                        'half_leave_allowance_months': half_rule_res.get('months', int(lt.get('half_leave_allowance_months') or 12)),
                        'half_leave_period': half_rule_res.get('period', 'monthly'),
                        'half_leave_period_text': half_rule_res.get('period_text', 'this month'),
                        'half_leave_winning_source': half_rule_res.get('winning_source'),
                        'half_leave_quota_text': f"Half leave: {half_leave_count} used, {half_leave_remaining} remaining {half_rule_res.get('period_text', 'this month')}" if half_rule_res['half_leave_enabled'] else f"Half leave is not allowed for {lt['name']}.",
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


def resolve_half_leave_rule(
    cursor: sqlite3.Cursor,
    user_id: int,
    leave_type: Union[Dict[str, Any], str, int],
    reference_date: Union[datetime, date, str],
) -> Dict[str, Any]:
    """
    Authoritative half-leave rule resolution implementing strict precedence:
    PERSON -> DEPARTMENT -> COMPANY -> LEAVE TYPE DEFAULT.
    A higher-level rule replaces lower-level rules without mixing.
    QA-39 & QA-40 Authoritative Function.
    """
    if isinstance(reference_date, str):
        try:
            ref_dt = datetime.strptime(reference_date[:10], '%Y-%m-%d')
        except Exception:
            ref_dt = datetime.now()
    elif isinstance(reference_date, date) and not isinstance(reference_date, datetime):
        ref_dt = datetime(reference_date.year, reference_date.month, reference_date.day)
    elif isinstance(reference_date, datetime):
        ref_dt = reference_date
    else:
        ref_dt = datetime.now()

    org_id, branch_id = _user_tenant(cursor, user_id)
    ensure_payroll_settings_row(cursor, org_id, branch_id)

    # Fetch user details
    cursor.execute("SELECT department FROM users WHERE id = ?", (user_id,))
    u_row = cursor.fetchone()
    department_name = (u_row['department'] if hasattr(u_row, 'keys') else u_row[0]) if u_row else ''
    department_name = str(department_name or '').strip()
    department_id = None
    if department_name:
        cursor.execute(
            """SELECT id FROM departments
               WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
                 AND (COALESCE(organization_id, 'DEFAULT_ORG') = ? OR COALESCE(organization_id, 'DEFAULT_ORG') = 'DEFAULT_ORG')
               ORDER BY CASE WHEN COALESCE(organization_id, 'DEFAULT_ORG') = ? THEN 0 ELSE 1 END
               LIMIT 1""",
            (department_name, org_id, org_id),
        )
        d_row = cursor.fetchone()
        if d_row:
            department_id = int(d_row['id'] if hasattr(d_row, 'keys') else d_row[0])

    # Resolve leave type dict
    lt_dict = None
    if isinstance(leave_type, dict):
        lt_dict = dict(leave_type)
    elif isinstance(leave_type, int):
        cursor.execute("SELECT * FROM leave_types WHERE id = ?", (leave_type,))
        r = cursor.fetchone()
        if r: lt_dict = dict(r)
    else:
        cursor.execute(
            "SELECT * FROM leave_types WHERE (code = ? OR name = ?) AND COALESCE(organization_id, 'DEFAULT_ORG') = ? LIMIT 1",
            (str(leave_type), str(leave_type), org_id)
        )
        r = cursor.fetchone()
        if r: lt_dict = dict(r)

    if not lt_dict:
        return {
            'allowed': False,
            'reason_code': 'invalid_leave_type',
            'message': 'Leave type not found.',
            'winning_source': None,
            'half_leave_enabled': False,
            'allowance': 0,
            'used_count': 0,
            'remaining_count': 0,
            'period': 'monthly',
            'period_text': 'this month',
            'leave_type_name': str(leave_type or 'Leave'),
            'is_paid': 1,
        }

    leave_name = lt_dict['name']
    lt_code = lt_dict['code']
    is_paid = int(lt_dict.get('is_paid', 1))

    # Precedence: PERSON -> DEPARTMENT -> COMPANY -> LEAVE TYPE DEFAULT
    winning_rule = None

    # 1. PERSON
    cursor.execute("""
        SELECT * FROM payroll_scoped_rules
        WHERE policy_type = 'half_leave_quota' AND scope_type = 'employee' AND scope_id = ? AND active = 1
          AND (COALESCE(organization_id, 'DEFAULT_ORG') = ? OR COALESCE(organization_id, 'DEFAULT_ORG') = 'DEFAULT_ORG')
        ORDER BY CASE WHEN COALESCE(organization_id, 'DEFAULT_ORG') = ? THEN 0 ELSE 1 END LIMIT 1
    """, (user_id, org_id, org_id))
    p_row = cursor.fetchone()
    if p_row:
        p_dict = dict(p_row)
        winning_rule = {
            'source': 'person',
            'allowance': max(1, int(p_dict.get('half_leaves_per_quota_day') or 2)),
            'mode': str(p_dict.get('half_leave_quota_mode') or 'category').lower(),
            'period': 'monthly',
            'period_text': 'this month',
            'months': 1,
            'target_name': 'Employee',
        }

    # 2. DEPARTMENT (if no person rule)
    if not winning_rule and department_id is not None:
        cursor.execute("""
            SELECT * FROM payroll_scoped_rules
            WHERE policy_type = 'half_leave_quota' AND scope_type = 'department' AND scope_id = ? AND active = 1
              AND (COALESCE(organization_id, 'DEFAULT_ORG') = ? OR COALESCE(organization_id, 'DEFAULT_ORG') = 'DEFAULT_ORG')
            ORDER BY CASE WHEN COALESCE(organization_id, 'DEFAULT_ORG') = ? THEN 0 ELSE 1 END LIMIT 1
        """, (department_id, org_id, org_id))
        d_row = cursor.fetchone()
        if d_row:
            d_dict = dict(d_row)
            winning_rule = {
                'source': 'department',
                'allowance': max(1, int(d_dict.get('half_leaves_per_quota_day') or 2)),
                'mode': str(d_dict.get('half_leave_quota_mode') or 'category').lower(),
                'period': 'monthly',
                'period_text': 'this month',
                'months': 1,
                'target_name': 'Department',
            }

    # 3. COMPANY (if no person or department rule)
    if not winning_rule:
        cursor.execute("""
            SELECT * FROM payroll_scoped_rules
            WHERE policy_type = 'half_leave_quota' AND scope_type IN ('company', 'org') AND active = 1
              AND (COALESCE(organization_id, 'DEFAULT_ORG') = ? OR COALESCE(organization_id, 'DEFAULT_ORG') = 'DEFAULT_ORG')
            ORDER BY CASE WHEN COALESCE(organization_id, 'DEFAULT_ORG') = ? THEN 0 ELSE 1 END LIMIT 1
        """, (org_id, org_id))
        c_row = cursor.fetchone()
        if c_row:
            c_dict = dict(c_row)
            winning_rule = {
                'source': 'company',
                'allowance': max(1, int(c_dict.get('half_leaves_per_quota_day') or 2)),
                'mode': str(c_dict.get('half_leave_quota_mode') or 'category').lower(),
                'period': 'monthly',
                'period_text': 'this month',
                'months': 1,
                'target_name': 'Company',
            }
        else:
            cursor.execute(
                "SELECT half_leave_quota_mode, half_leaves_per_quota_day FROM payroll_settings WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ? AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?",
                (org_id, branch_id)
            )
            s_row = cursor.fetchone()
            if s_row and s_row[0] in ('monthly_cap', 'monthly_convert', 'company'):
                winning_rule = {
                    'source': 'company',
                    'allowance': max(1, int(s_row[1] or 2)),
                    'mode': str(s_row[0]).lower(),
                    'period': 'monthly',
                    'period_text': 'this month',
                    'months': 1,
                    'target_name': 'Company',
                }

    # 4. LEAVE TYPE DEFAULT (if no person, department, or company rule)
    if not winning_rule:
        half_enabled = bool(lt_dict.get('half_leave_enabled'))
        half_allowance = int(lt_dict.get('half_leave_allowance_count') or 0)
        half_mode = str(lt_dict.get('half_leave_mode') or 'separate').lower()
        half_months = max(1, min(12, int(lt_dict.get('half_leave_allowance_months') or 12)))
        if half_enabled and (half_allowance > 0 or half_mode == 'convert'):
            period_str = 'monthly' if half_months == 1 else f"{half_months}-month cycle"
            period_text = 'this month' if half_months == 1 else f"in this {half_months}-month cycle"
            winning_rule = {
                'source': 'leave_type',
                'allowance': half_allowance,
                'mode': half_mode,
                'period': period_str,
                'period_text': period_text,
                'months': half_months,
                'target_name': 'Leave Type',
            }

    # If NO winning rule exists or half leave is disabled
    if not winning_rule:
        msg = "Half leave is not set up for this department yet." if (department_id or department_name) else f"Half leave is not allowed for {leave_name}."
        return {
            'allowed': False,
            'reason_code': 'half_leave_not_allowed',
            'message': msg,
            'winning_source': None,
            'half_leave_enabled': False,
            'allowance': 0,
            'used_count': 0,
            'remaining_count': 0,
            'period': 'monthly',
            'period_text': 'this month',
            'leave_type_name': leave_name,
            'is_paid': is_paid,
        }

    # Winning rule exists: compute cycle window and usage
    if winning_rule['period'] == 'monthly':
        month_start = ref_dt.date().replace(day=1)
        if month_start.month == 12:
            next_month = month_start.replace(year=month_start.year + 1, month=1)
        else:
            next_month = month_start.replace(month=month_start.month + 1)
        cycle_start = month_start.isoformat()
        cycle_end = (next_month - timedelta(days=1)).isoformat()
    else:
        cycle_start, cycle_end = _leave_allowance_cycle_window(
            ref_dt, 'custom', winning_rule.get('months', 12)
        )

    if winning_rule['mode'] == 'monthly_cap':
        cursor.execute("""
            SELECT COUNT(*) FROM leave_requests
            WHERE user_id = ? AND leave_duration = 'half' AND LOWER(status) IN ('pending', 'approved')
              AND end_date >= ? AND start_date <= ?
        """, (user_id, cycle_start, cycle_end))
    else:
        cursor.execute("""
            SELECT COUNT(*) FROM leave_requests
            WHERE user_id = ? AND (leave_type = ? OR leave_type = ?)
              AND leave_duration = 'half' AND LOWER(status) IN ('pending', 'approved')
              AND end_date >= ? AND start_date <= ?
        """, (user_id, lt_code, leave_name, cycle_start, cycle_end))

    used_count = int(cursor.fetchone()[0] or 0)
    allowance = winning_rule['allowance']
    remaining_count = max(0, allowance - used_count)
    is_exhausted = (used_count >= allowance)
    period_str = 'month' if winning_rule['period'] == 'monthly' else winning_rule['period']

    return {
        'allowed': not is_exhausted,
        'reason_code': 'half_leave_allowance_reached' if is_exhausted else 'ok',
        'message': f"{leave_name} half-leave allowance reached ({allowance} occurrence(s) per {period_str})." if is_exhausted else 'OK',
        'winning_source': winning_rule['source'],
        'half_leave_enabled': True,
        'half_leave_mode': winning_rule['mode'],
        'allowance': allowance,
        'used_count': used_count,
        'remaining_count': remaining_count,
        'period': winning_rule['period'],
        'period_text': winning_rule['period_text'],
        'cycle_start': cycle_start,
        'cycle_end': cycle_end,
        'leave_type_name': leave_name,
        'is_paid': is_paid,
        'months': winning_rule.get('months', 1),
        'target_id': lt_dict.get('half_leave_quota_type_id'),
        'conversion_ratio': max(1, int(lt_dict.get('half_leaves_per_quota_day') or 2)),
    }


def get_half_leave_rule(
    user_id: int,
    leave_type_id_or_code: Any,
    reference_date: Optional[Union[datetime, date, str]] = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Convenience wrapper for resolve_half_leave_rule."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        return resolve_half_leave_rule(
            cursor,
            user_id,
            leave_type_id_or_code,
            reference_date or datetime.now(),
        )


def check_leave_quota(
    user_id: Optional[int] = None,
    leave_type_code: str = 'casual',
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    exclude_id: Optional[int] = None,
    leave_duration: str = 'full',
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
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
            if user_id:
                org_id, _branch_id = _user_tenant(cursor, user_id)
            else:
                org_id, _branch_id = _tenant_ids(organization_id, branch_id)
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
                rule_res = resolve_half_leave_rule(cursor, user_id, lt_dict, reference_date)
                if not rule_res['allowed']:
                    return {
                        'allowed': False,
                        'reason_code': rule_res['reason_code'],
                        'message': rule_res['message'],
                        'requested_days': 1,
                        'quota_days_requested': 0,
                        'remaining_days': rule_res['remaining_count'],
                        'leave_type_name': leave_name,
                        'half_leave_enabled': rule_res['half_leave_enabled'],
                        'half_leave_mode': rule_res.get('half_leave_mode'),
                        'half_leave_count': rule_res['used_count'],
                        'half_leave_allowance_count': rule_res['allowance'],
                    }

                # Check if employee still has full-day quota for paid leave
                allocated = float(lt_dict.get('yearly_allowance', 10.0))
                if user_id:
                    cursor.execute(
                        "SELECT allocated_days FROM employee_leave_balances WHERE user_id = ? AND leave_type_id = ? AND year = ?",
                        (user_id, lt_dict['id'], reference_date.year)
                    )
                    bal_row = cursor.fetchone()
                    if bal_row:
                        allocated = float(bal_row['allocated_days'])

                allowance_period = lt_dict.get('allowance_period', 'yearly')
                cycle_start, cycle_end = _leave_allowance_cycle_window(
                    reference_date, allowance_period, lt_dict.get('allowance_months')
                )
                cursor.execute("""
                    SELECT id, start_date, end_date, quota_days, quota_through_date, leave_duration,
                           half_leave_quota_type_id, half_leave_quota_charge, restored_dates FROM leave_requests
                    WHERE user_id = ? AND (status = 'approved' OR paid_status = 'approved') AND (leave_type = ? OR leave_type = ?)
                      AND end_date >= ? AND start_date <= ?
                """, (user_id, lt_dict['code'], leave_name, cycle_start, cycle_end))
                already_used = 0.0
                for req in cursor.fetchall():
                    if exclude_id and req['id'] == exclude_id:
                        continue
                    if req['leave_duration'] == 'half':
                        already_used += 0.5
                    else:
                        try:
                            clipped_start = max(req['start_date'], cycle_start)
                            clipped_end = min(req['end_date'], cycle_end)
                            already_used += max(0.0, _count_scheduled_leave_days(cursor, user_id, clipped_start, clipped_end))
                        except Exception:
                            already_used += 1.0

                remaining_full = max(0.0, allocated - already_used)
                if is_paid == 1 and remaining_full <= 0:
                    rem_str = int(remaining_full) if remaining_full.is_integer() else remaining_full
                    return {
                        'allowed': False,
                        'reason_code': 'quota_exceeded',
                        'message': f"{leave_name} only has {rem_str} allowed paid days for this employee. You cannot request half-day leave. Please adjust the dates or file under Unpaid Leave (LWP).",
                        'requested_days': 1,
                        'quota_days_requested': 1,
                        'remaining_days': 0,
                        'allocated_days': allocated,
                        'used_days': already_used,
                        'leave_type_name': leave_name,
                        'is_paid': 1,
                    }

                existing_half_count = rule_res['used_count']
                conversion_ratio = rule_res.get('conversion_ratio', 2)
                half_mode = rule_res.get('half_leave_mode', 'category')
                half_months = rule_res.get('months', 1)

                if half_mode != 'convert':
                    return {
                        'allowed': True,
                        'message': 'OK',
                        'requested_days': 1,
                        'quota_days_requested': 0,
                        'remaining_days': rule_res['remaining_count'],
                        'leave_type_name': leave_name,
                        'is_paid': is_paid,
                        'half_leave_enabled': True,
                        'half_leave_mode': half_mode,
                        'half_leave_count': existing_half_count + 1,
                        'half_leave_allowance_count': rule_res['allowance'],
                        'half_leaves_per_month': rule_res['allowance'] if rule_res['period'] == 'monthly' else None,
                        'half_leave_allowance_months': half_months,
                    }

                # Mode is convert
                target_id = rule_res.get('target_id') or lt_dict.get('half_leave_quota_type_id')
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
                           half_leave_quota_type_id, half_leave_quota_charge, restored_dates FROM leave_requests
                    WHERE user_id = ? AND (status = 'approved' OR paid_status = 'approved') AND (leave_type = ? OR leave_type = ?)
                      AND end_date >= ? AND start_date <= ?
                """, (user_id, lt_dict['code'], lt_dict['name'], cycle_start, cycle_end))
                approved_requests = cursor.fetchall()

                for req in approved_requests:
                    if exclude_id and req['id'] == exclude_id:
                        continue
                    restored_dates_raw = req['restored_dates'] if 'restored_dates' in req.keys() else ''
                    restored_set = set(d.strip() for d in (restored_dates_raw or '').split(',') if d.strip())
                    if req['leave_duration'] == 'half':
                        if req['start_date'] in restored_set:
                            continue
                        if req['half_leave_quota_charge'] and req['half_leave_quota_type_id'] == lt_dict['id']:
                            already_used += 1.0
                        else:
                            already_used += 0.5
                        continue
                    try:
                        clipped_start = max(req['start_date'], cycle_start)
                        clipped_end = min(req['end_date'], cycle_end)
                        restored_in_range = sum(1 for d in restored_set if clipped_start <= d <= clipped_end)
                        if req['quota_days'] is None:
                            already_used += max(0.0, _count_scheduled_leave_days(cursor, user_id, clipped_start, clipped_end) - restored_in_range)
                        else:
                            quota_end = min(clipped_end, req['quota_through_date'] or req['end_date'])
                            if quota_end >= clipped_start:
                                already_used += max(0.0, _count_scheduled_leave_days(cursor, user_id, clipped_start, quota_end) - restored_in_range)
                    except Exception:
                        already_used += 1.0
                cursor.execute("""
                    SELECT COUNT(*) FROM leave_requests
                    WHERE user_id = ? AND (status = 'approved' OR paid_status = 'approved') AND leave_duration = 'half'
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



def _get_following_workday_attendance_status(cursor: sqlite3.Cursor, user_id: int, ot_date: str, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> Optional[str]:
    """If the following scheduled workday has already elapsed, return its attendance status (QA-05)."""
    try:
        ot_dt = datetime.strptime(ot_date, '%Y-%m-%d').date()
        today = date.today()
        for i in range(1, 8):
            next_d = ot_dt + timedelta(days=i)
            if next_d > today:
                return None
            next_d_str = next_d.isoformat()
            day_type = _get_overtime_day_type(cursor, user_id, next_d_str)
            if day_type == 'regular':
                return get_user_attendance_status(user_id, next_d_str, organization_id=organization_id, branch_id=branch_id)
        return None
    except Exception:
        return None


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
            validate_employment_dates(cursor, user_id, ot_date, activity_name='Overtime')
            next_day_att = _get_following_workday_attendance_status(cursor, user_id, ot_date, organization_id=organization_id, branch_id=branch_id)
            if next_day_att == 'absent':
                raise ValueError('Overtime cannot be applied because the employee was absent on the following workday.')
            raise_if_payroll_month_locked(ot_date, organization_id, branch_id)
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
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Add overtime error: {e}")
        return None


def calculate_employee_ot_rate(
    conn_or_cursor,
    user_id: int,
    as_of_date: Optional[str] = None,
    basic_salary: Optional[float] = None,
    configured_rate: Optional[float] = None,
) -> float:
    """
    Authoritative Overtime Rate Calculation (QA-04 / QA-27).
    Formula:
        Monthly Working Hours = (Shift Hours Per Day * Scheduled Working Days Per Week * 52) / 12
        OT Rate = round(Basic Salary / Monthly Working Hours, 2)
    If configured_rate > 0, returns configured_rate.
    Otherwise calculates dynamically from employee shift on as_of_date.
    """
    if configured_rate is not None and float(configured_rate) > 0:
        return round(float(configured_rate), 2)

    cursor = conn_or_cursor.cursor() if hasattr(conn_or_cursor, 'cursor') else conn_or_cursor

    # Get salary and configured rate if not passed
    if basic_salary is None or (configured_rate is None and configured_rate != 0):
        row = cursor.execute("""
            SELECT COALESCE(sc.ot_rate, 0.0) as ot_rate,
                   COALESCE(sc.basic_salary, u.salary, 0.0) as basic_salary
            FROM users u
            LEFT JOIN salary_configs sc ON sc.user_id = u.id
            WHERE u.id = ?
        """, (user_id,)).fetchone()
        if row:
            if configured_rate is None:
                configured_rate = float(row[0] or 0.0)
            if basic_salary is None:
                basic_salary = float(row[1] or 0.0)
        else:
            if basic_salary is None:
                basic_salary = 0.0
            if configured_rate is None:
                configured_rate = 0.0

    if configured_rate and float(configured_rate) > 0:
        return round(float(configured_rate), 2)

    salary = float(basic_salary or 0.0)
    if salary <= 0:
        return 0.0

    date_filter = str(as_of_date)[:10] if as_of_date else None
    shift_row = None
    if date_filter:
        shift_row = cursor.execute("""
            SELECT start_time, end_time, working_days FROM employee_shift_history
            WHERE user_id = ? AND effective_from <= ?
            ORDER BY effective_from DESC, id DESC LIMIT 1
        """, (user_id, date_filter)).fetchone()

    if not shift_row:
        shift_row = cursor.execute("""
            SELECT start_time, end_time, working_days FROM employee_shift_history
            WHERE user_id = ?
            ORDER BY effective_from DESC, id DESC LIMIT 1
        """, (user_id,)).fetchone()

    if not shift_row:
        shift_row = cursor.execute("""
            SELECT s.start_time, s.end_time, s.working_days
            FROM users u
            JOIN shifts s ON s.id = COALESCE(u.shift_id, 1)
            WHERE u.id = ?
        """, (user_id,)).fetchone()

    monthly_hours = 208.0
    if shift_row and shift_row[0] and shift_row[1]:
        try:
            s_str = str(shift_row[0])[:5]
            e_str = str(shift_row[1])[:5]
            s_dt = datetime.strptime(s_str, '%H:%M')
            e_dt = datetime.strptime(e_str, '%H:%M')
            diff_hours = (e_dt - s_dt).total_seconds() / 3600.0
            if diff_hours <= 0:
                diff_hours += 24.0
            days_list = _normalize_shift_working_days(shift_row[2])
            days_per_wk = len(days_list) if days_list else 6
            monthly_hours = (diff_hours * days_per_wk * 52.0) / 12.0
        except Exception:
            monthly_hours = 208.0

    if monthly_hours <= 0:
        monthly_hours = 208.0

    return round(salary / monthly_hours, 2)


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
                       holiday_ot_pay_mode, holiday_ot_multiplier,
                       non_working_day_ot_pay_mode, non_working_day_ot_multiplier,
                       COALESCE(ot_hour_divisor, 208) AS ot_hour_divisor,
                       COALESCE(ot_rounding, 'round_rate_first') AS ot_rounding
                FROM payroll_settings
                WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                  AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
            ''', (emp_org, emp_branch)).fetchone()
            raw_rate = calculate_employee_ot_rate(cursor, user_id, as_of_date=overtime_date, basic_salary=salary, configured_rate=configured_rate)
            rounding = _normalize_ot_rounding(settings['ot_rounding'] if settings else None)
            base_rate = round(raw_rate, 2) if rounding == 'round_rate_first' else float(raw_rate)
            day_type = _get_overtime_day_type(cursor, user_id, overtime_date)
            holiday = _get_company_holiday_for_date(
                cursor, overtime_date, employee['organization_id'], employee['branch_id']
            ) if day_type == 'holiday' else None
            premium_percent = _get_overtime_premium_percent(
                cursor, overtime_date, day_type, settings, holiday, user_id=user_id
            )
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
    if organization_id and organization_id not in ('all', 'DEFAULT_ORG'):
        if postgres_is_configured():
            try:
                import postgres_mirror as cloud_pg
                cloud_pg.fetch_pg_overtime_records(organization_id, branch_id, user_id, status, start_date, end_date)
            except Exception as error:
                logger.warning("Postgres unreachable for overtime; falling back to SQLite cache: %s", error)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = """
                SELECT o.id, o.user_id, COALESCE(u.name, o.user_name) as user_name, o.ot_date, o.hours, o.reason, o.status, o.approved_by, o.created_at, o.rejection_reason,
                       o.rejected_by, o.rejected_at, o.audit_trail,
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
                if base_rate <= 0:
                    base_rate = calculate_employee_ot_rate(cursor, int(d['user_id']), as_of_date=str(d.get('ot_date')))
                emp_org = str(d.get('organization_id') or 'DEFAULT_ORG')
                emp_branch = str(d.get('branch_id') or 'DEFAULT_BRANCH')
                cache_key = (emp_org, emp_branch)
                if cache_key not in settings_cache:
                    ensure_payroll_settings_row(cursor, emp_org, emp_branch)
                    settings_cache[cache_key] = cursor.execute(
                        """SELECT holiday_ot_premium_percent, non_working_day_ot_premium_percent,
                                  holiday_ot_premium_active, non_working_day_ot_premium_active,
                                  holiday_ot_pay_mode, holiday_ot_multiplier,
                                  non_working_day_ot_pay_mode, non_working_day_ot_multiplier
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
                premium = _get_overtime_premium_percent(
                    cursor, str(d['ot_date']), day_type, settings_row, holiday, user_id=int(d['user_id'])
                )
                effective_rate = round(base_rate * (1 + premium / 100), 2)
                d['base_ot_rate'] = base_rate
                d['ot_day_type'] = day_type
                d['holiday_id'] = holiday.get('id') if holiday else None
                d['holiday_name'] = holiday.get('name') if holiday else None
                d['ot_premium_percent'] = premium
                d['ot_rate'] = effective_rate
                d['ot_amount'] = round(hours * effective_rate, 2)
                d['date'] = str(d.get('ot_date') or '')
                d['overtime_date'] = str(d.get('ot_date') or '')
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
        import postgres_mirror as cloud_pg
        if cloud_pg.postgres_is_configured():
            pg_res = cloud_pg.fetch_pg_overtime(ot_id, organization_id)
            if pg_res is False:
                return None
    except Exception as e:
        logger.warning("Postgres unreachable for get_overtime_by_id; falling back to SQLite cache: %s", e)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = """SELECT o.id, o.user_id, COALESCE(u.name, o.user_name) as user_name, o.ot_date, o.hours, o.reason, o.status, o.approved_by, o.created_at, o.rejection_reason,
                              o.rejected_by, o.rejected_at, o.audit_trail
                       FROM overtime o
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
            if not row:
                return None
            res_dict = dict(row)
            res_dict['date'] = str(res_dict.get('ot_date') or '')
            res_dict['overtime_date'] = str(res_dict.get('ot_date') or '')
            return res_dict
    except Exception as e:
        logger.error(f"Error fetching overtime record {ot_id}: {e}")
        return None


def get_user_attendance_status(user_id: int, target_date: str, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> Optional[str]:
    """Get an employee's resolved attendance status for one date with tenant scoping."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if not organization_id or not branch_id:
                cursor.execute("SELECT organization_id, branch_id FROM users WHERE id = ?", (user_id,))
                urow = cursor.fetchone()
                if urow:
                    organization_id = organization_id or urow[0]
                    branch_id = branch_id or urow[1]
            cursor.execute(
                "SELECT 1 FROM manual_absent WHERE user_id = ? AND date = ? LIMIT 1",
                (user_id, target_date)
            )
            if cursor.fetchone():
                return 'absent'

        report = get_detailed_attendance_report(
            target_date=target_date,
            user_id=user_id,
            organization_id=organization_id,
            branch_id=branch_id,
        )
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
    rejection_reason: Optional[str] = None,
) -> bool:
    """Update an overtime record status (pending -> approved/rejected, or approved -> rejected) while recording audit history."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            ot_cols = {r[1] for r in cursor.execute("PRAGMA table_info(overtime)").fetchall()}
            for col, col_type in [
                ('rejection_reason', 'TEXT'),
                ('rejected_by', 'TEXT'),
                ('rejected_at', 'TEXT'),
                ('audit_trail', 'TEXT'),
            ]:
                if col not in ot_cols:
                    cursor.execute(f"ALTER TABLE overtime ADD COLUMN {col} {col_type}")
                    ot_cols.add(col)

            row = cursor.execute(
                """SELECT ot_date, COALESCE(organization_id,'DEFAULT_ORG'), COALESCE(branch_id,'DEFAULT_BRANCH'),
                          COALESCE(status, 'pending'), rejection_reason, audit_trail
                   FROM overtime WHERE id = ?""",
                (ot_id,),
            ).fetchone()
            if not row:
                return False
            ot_date_val, org_val, branch_val, cur_status, existing_rej_reason, existing_audit = row
            cur_status = str(cur_status).lower()
            norm_new_status = str(status).lower()

            raise_if_payroll_month_locked(str(ot_date_val)[:10], org_val, branch_val)

            if norm_new_status == 'rejected':
                if cur_status not in ('pending', 'approved'):
                    return False
            elif norm_new_status == 'approved':
                if cur_status != 'pending':
                    return False
            else:
                return False

            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            audit_entry = f"[{now_str}] Status changed from '{cur_status}' to '{norm_new_status}' by '{approved_by}'."
            if rejection_reason:
                audit_entry += f" Reason: {rejection_reason}"
            new_audit = f"{existing_audit}\n{audit_entry}".strip() if existing_audit else audit_entry

            final_rej_reason = rejection_reason if rejection_reason is not None else existing_rej_reason
            if norm_new_status == 'rejected' and not final_rej_reason and cur_status == 'approved':
                final_rej_reason = f"Rejected post-approval by {approved_by}"

            rej_by = approved_by if norm_new_status == 'rejected' else None
            rej_at = now_str if norm_new_status == 'rejected' else None

            set_clauses = [
                "status = ?",
                "approved_by = ?",
                "rejection_reason = ?",
                "rejected_by = COALESCE(?, rejected_by)",
                "rejected_at = COALESCE(?, rejected_at)",
                "audit_trail = ?"
            ]
            params: List[Any] = [norm_new_status, approved_by, final_rej_reason, rej_by, rej_at, new_audit]

            where_statuses = "('pending', 'approved')" if norm_new_status == 'rejected' else "('pending')"
            query = f"""UPDATE overtime SET {', '.join(set_clauses)}
                       WHERE id = ? AND LOWER(COALESCE(status, 'pending')) IN {where_statuses}"""
            params.append(ot_id)

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
            updated = cursor.rowcount > 0
            if updated:
                conn.row_factory = sqlite3.Row
                full_ot = conn.execute(
                    "SELECT o.*, COALESCE(u.name, o.user_name) as user_name FROM overtime o LEFT JOIN users u ON u.id = o.user_id WHERE o.id = ?",
                    (ot_id,)
                ).fetchone()
                if full_ot:
                    _cloud_mirror("mirror_overtime", dict(full_ot))
            return updated
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
            configs = [dict(r) for r in cursor.fetchall()]
            for r in configs:
                if float(r.get('ot_rate') or 0.0) <= 0:
                    r['ot_rate'] = calculate_employee_ot_rate(cursor, r['user_id'], basic_salary=r.get('basic_salary'))
            return configs
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
            if not row:
                return None
            result = dict(row)
            # Determine applicable monthly working hours from employee's shift schedule
            cursor.execute('''
                SELECT start_time, end_time, working_days FROM employee_shift_history
                WHERE user_id = ?
                ORDER BY effective_from DESC, id DESC LIMIT 1
            ''', (user_id,))
            shift_row = cursor.fetchone()
            if not shift_row:
                cursor.execute('''
                    SELECT s.start_time, s.end_time, s.working_days
                    FROM users u
                    JOIN shifts s ON s.id = COALESCE(u.shift_id, 1)
                    WHERE u.id = ?
                ''', (user_id,))
                shift_row = cursor.fetchone()
            monthly_hours = 208.0
            if shift_row and shift_row['start_time'] and shift_row['end_time']:
                try:
                    s_str = str(shift_row['start_time'])[:5]
                    e_str = str(shift_row['end_time'])[:5]
                    s_dt = datetime.strptime(s_str, '%H:%M')
                    e_dt = datetime.strptime(e_str, '%H:%M')
                    diff_hours = (e_dt - s_dt).total_seconds() / 3600.0
                    if diff_hours <= 0:
                        diff_hours += 24.0
                    days_list = _normalize_shift_working_days(shift_row['working_days'])
                    days_per_wk = len(days_list) if days_list else 6
                    if days_per_wk == 6 and round(diff_hours, 1) == 8.0:
                        monthly_hours = 208.0
                    else:
                        monthly_hours = round((diff_hours * days_per_wk * 52.0) / 12.0, 2)
                except Exception:
                    monthly_hours = 208.0
            # Get tenant settings divisor
            cursor.execute('''
                SELECT COALESCE(ot_hour_divisor, 208) AS ot_hour_divisor
                FROM payroll_settings
                WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?
                  AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
            ''', _user_tenant(cursor, user_id))
            ps = cursor.fetchone()
            settings_divisor = float(ps['ot_hour_divisor']) if ps and ps['ot_hour_divisor'] else 208.0
            result['applicable_working_hours'] = monthly_hours if monthly_hours > 0 else settings_divisor
            result['ot_hour_divisor'] = settings_divisor
            return result
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
                '''INSERT INTO salary_configs (
                       user_id, basic_salary, allowances, deductions, ot_rate, updated_at,
                       organization_id, branch_id
                   )
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   basic_salary=excluded.basic_salary,
                   allowances=excluded.allowances,
                   deductions=excluded.deductions,
                   ot_rate=excluded.ot_rate,
                   organization_id=excluded.organization_id,
                   branch_id=excluded.branch_id,
                   updated_at=CURRENT_TIMESTAMP''',
                (user_id, b_sal, allow, deduct, ot, owner[1], owner[2])
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


def list_salary_config_tenant_report() -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM salary_config_tenant_report ORDER BY user_id").fetchall()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]


def preview_payroll_settings_impact(
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    proration_basis: Optional[str] = None,
    ot_rounding: Optional[str] = None,
    payroll_month: Optional[str] = None,
) -> Dict[str, Any]:
    month = payroll_month or date.today().strftime('%Y-%m')
    before = calculate_monthly_payroll(
        month, organization_id=organization_id, branch_id=branch_id, force_recalculate=True,
    )
    overrides = {}
    if proration_basis:
        overrides['proration_basis'] = proration_basis
    if ot_rounding:
        overrides['ot_rounding'] = ot_rounding
    after = calculate_monthly_payroll(
        month, organization_id=organization_id, branch_id=branch_id,
        settings_overrides=overrides or None, force_recalculate=True,
    )

    def _slim(payroll):
        if not isinstance(payroll, dict):
            return []
        return [
            {
                'employee_name': row.get('employee_name'),
                'basic_salary': row.get('basic_salary'),
                'net_salary': row.get('net_salary'),
                'overtime_amount': row.get('overtime_amount'),
                'overtime_rate': row.get('overtime_rate'),
            }
            for row in payroll.get('records') or []
        ]

    return {
        'month': month,
        'before_settings': {
            'proration_basis': before.get('proration_basis') if isinstance(before, dict) else None,
            'ot_rounding': before.get('ot_rounding') if isinstance(before, dict) else None,
        },
        'after_settings': {
            'proration_basis': proration_basis or (before.get('proration_basis') if isinstance(before, dict) else None),
            'ot_rounding': ot_rounding or (before.get('ot_rounding') if isinstance(before, dict) else None),
        },
        'before': _slim(before),
        'after': _slim(after),
        'finalized_unchanged': bool(get_monthly_payroll(month, organization_id=organization_id, branch_id=branch_id)),
    }


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
                if rate_divisor and rate_divisor != DEFAULT_OT_DIVISOR:
                    ot_rate = round(b_sal / rate_divisor, 2) if (b_sal > 0 and rate_divisor > 0) else 0.0
                else:
                    ot_rate = calculate_employee_ot_rate(conn, emp['id'], None, b_sal, None)
                cursor.execute("""
                    INSERT INTO salary_configs (
                        user_id, basic_salary, allowances, deductions, ot_rate, updated_at,
                        organization_id, branch_id
                    )
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                    ot_rate = excluded.ot_rate,
                    organization_id = excluded.organization_id,
                    branch_id = excluded.branch_id,
                    updated_at = CURRENT_TIMESTAMP
                """, (emp['id'], b_sal, float(emp['allowances'] or 0.0), float(emp['deductions'] or 0.0), ot_rate, org, branch))
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
        for key in PAYSLIP_LETTERHEAD_FIELDS:
            if key not in settings or settings.get(key) is None:
                settings[key] = DEFAULT_PAYROLL_SETTINGS.get(key) or ''
            else:
                settings[key] = str(settings.get(key) or '')
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
        basis = str(settings.get('proration_basis') or LEGACY_PRORATION_BASIS)
        rounding = _normalize_ot_rounding(settings.get('ot_rounding'))
        settings['legacy_calendar_proration'] = basis == 'calendar_days'
        settings['legacy_ot_round_rate_first'] = rounding == 'round_rate_first'
        if basis == 'calendar_days':
            settings['notices'].append(
                "Join/exit proration is calendar days. New organizations default to scheduled working days."
            )
        if rounding == 'round_rate_first':
            settings['notices'].append(
                "Overtime still rounds the hourly rate first. New organizations round the overtime amount at the end."
            )
        return settings


def get_payroll_scope_options(
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return real departments, job titles, shifts, people, and the current branch."""
    org, branch = _tenant_ids(organization_id, branch_id)
    empty = {
        'departments': [], 'designations': [], 'shifts': [],
        'employees': [], 'branches': [{'id': branch, 'name': branch}],
    }
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            employees = [dict(row) for row in conn.execute(
                """SELECT id, name,
                          COALESCE(department, '') AS department,
                          COALESCE(position, '') AS position,
                          COALESCE(shift_id, 0) AS shift_id
                   FROM users
                   WHERE active = 1
                     AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                     AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                   ORDER BY name COLLATE NOCASE""",
                (org, branch),
            ).fetchall()]
            table_depts = [dict(row) for row in conn.execute(
                """SELECT id, name FROM departments
                   WHERE active = 1
                     AND (
                       COALESCE(organization_id, 'DEFAULT_ORG') = ?
                       OR organization_id IS NULL OR organization_id = ''
                     )
                   ORDER BY name COLLATE NOCASE""",
                (org,),
            ).fetchall()]
            seen_dept = set()
            departments: List[Dict[str, Any]] = []
            for row in table_depts:
                name = str(row.get('name') or '').strip()
                if not name or name.lower() in seen_dept:
                    continue
                seen_dept.add(name.lower())
                departments.append({'id': row['id'], 'name': name})
            for emp in employees:
                name = str(emp.get('department') or '').strip()
                if not name or name.lower() in seen_dept:
                    continue
                seen_dept.add(name.lower())
                departments.append({'id': name, 'name': name})
            departments.sort(key=lambda row: str(row['name']).lower())

            table_titles = [dict(row) for row in conn.execute(
                """SELECT id, title FROM designations
                   WHERE active = 1
                     AND (
                       COALESCE(organization_id, 'DEFAULT_ORG') = ?
                       OR organization_id IS NULL OR organization_id = ''
                     )
                   ORDER BY title COLLATE NOCASE""",
                (org,),
            ).fetchall()]
            seen_title = set()
            designations: List[Dict[str, Any]] = []
            for row in table_titles:
                title = str(row.get('title') or '').strip()
                if not title or title.lower() in seen_title:
                    continue
                seen_title.add(title.lower())
                designations.append({'id': row['id'], 'name': title, 'title': title})
            for emp in employees:
                title = str(emp.get('position') or '').strip()
                if not title or title.lower() in seen_title:
                    continue
                seen_title.add(title.lower())
                designations.append({'id': title, 'name': title, 'title': title})
            designations.sort(key=lambda row: str(row['name']).lower())

            shifts = [dict(row) for row in conn.execute(
                """SELECT id, name FROM shifts
                   WHERE COALESCE(active, 1) = 1
                     AND (
                       COALESCE(organization_id, 'DEFAULT_ORG') = ?
                       OR organization_id IS NULL OR organization_id = ''
                     )
                   ORDER BY name COLLATE NOCASE""",
                (org,),
            ).fetchall()]
            if not shifts:
                shift_ids = sorted({int(emp.get('shift_id') or 0) for emp in employees if int(emp.get('shift_id') or 0)})
                if shift_ids:
                    placeholders = ','.join('?' * len(shift_ids))
                    shifts = [dict(row) for row in conn.execute(
                        f"SELECT id, name FROM shifts WHERE id IN ({placeholders}) ORDER BY name COLLATE NOCASE",
                        shift_ids,
                    ).fetchall()]

            branch_name = branch
            try:
                row = conn.execute(
                    "SELECT branch_name FROM branch_settings WHERE id = 1"
                ).fetchone()
                if row and row['branch_name']:
                    branch_name = str(row['branch_name'])
            except sqlite3.OperationalError:
                pass
            return {
                'departments': departments,
                'designations': designations,
                'shifts': shifts,
                'employees': employees,
                'branches': [{'id': branch, 'name': branch_name}],
            }
    except Exception as e:
        logger.error(f"Failed to load payroll rule scope options: {e}")
        return empty


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


def _resolve_scoped_target_id(cursor, org: str, branch: str, scope: str, scope_id) -> Optional[int]:
    """Map a person id or department name/id to the integer scoped-rule target."""
    if scope == 'employee':
        try:
            target_id = int(scope_id)
        except (TypeError, ValueError, OverflowError):
            return None
        row = cursor.execute(
            """SELECT id FROM users
               WHERE id = ?
                 AND COALESCE(organization_id, 'DEFAULT_ORG') = ?
                 AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?""",
            (target_id, org, branch),
        ).fetchone()
        return int(row[0]) if row else None
    name = str(scope_id or '').strip()
    if not name:
        return None
    try:
        as_id = int(name)
    except (TypeError, ValueError):
        as_id = None
    if as_id is not None:
        row = cursor.execute("SELECT id FROM departments WHERE id = ?", (as_id,)).fetchone()
        if row:
            return int(row[0])
    row = cursor.execute(
        "SELECT id FROM departments WHERE name = ? COLLATE NOCASE LIMIT 1",
        (name,),
    ).fetchone()
    if row:
        return int(row[0])
    try:
        cur = cursor.cursor() if hasattr(cursor, 'cursor') else cursor
        cur.execute(
            "INSERT INTO departments (name, code, description, organization_id, branch_id) VALUES (?, '', '', ?, ?)",
            (name, org, branch),
        )
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        row = cursor.execute(
            "SELECT id FROM departments WHERE name = ? COLLATE NOCASE LIMIT 1",
            (name,),
        ).fetchone()
        return int(row[0]) if row else None


class ScopedRuleSaveResult(dict):
    """Result dictionary that evaluates to True if success is True, False otherwise."""
    def __bool__(self):
        return bool(self.get('success'))


def save_scoped_half_leave_rule(
    scope_type: str,
    scope_id: Any,
    quota_mode: str,
    monthly_count: Optional[int] = None,
    half_leaves_per_quota_day: Optional[int] = None,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
) -> ScopedRuleSaveResult:
    scope = str(scope_type or '').strip().lower()
    mode = str(quota_mode or '').strip().lower()
    if scope not in ('department', 'employee', 'company', 'org') or mode not in ('category', 'monthly_cap', 'monthly_convert'):
        return ScopedRuleSaveResult({'success': False, 'error': 'Invalid rule values. Scope must be department, employee, or company.'})
    
    val = monthly_count if monthly_count is not None else half_leaves_per_quota_day
    try:
        count = int(val)
    except (TypeError, ValueError, OverflowError):
        return ScopedRuleSaveResult({'success': False, 'error': 'Monthly count must be a valid integer.'})
    if count < 1:
        return ScopedRuleSaveResult({'success': False, 'error': 'Monthly count must be at least 1.'})
    try:
        with sqlite3.connect(DB_PATH) as conn:
            org, branch = _tenant_ids(organization_id, branch_id)
            if scope in ('company', 'org'):
                save_mode = 'company' if mode == 'category' else mode
                ensure_payroll_settings_row(conn.cursor(), org, branch)
                existing = conn.execute('''
                    SELECT half_leave_quota_mode, half_leaves_per_quota_day FROM payroll_settings
                    WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ? AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                ''', (org, branch)).fetchone()
                if existing and existing[0] in (save_mode, mode) and existing[1] == count:
                    return ScopedRuleSaveResult({
                        'success': False,
                        'duplicate': True,
                        'target_name': 'Company',
                        'error': f'A half-day rule already exists for Company ({count} occurrence(s) per month).'
                    })
                conn.execute('''
                    UPDATE payroll_settings
                    SET half_leave_quota_mode = ?, half_leaves_per_quota_day = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ? AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
                ''', (save_mode, count, org, branch))
                conn.commit()
                return ScopedRuleSaveResult({
                    'success': True,
                    'target_name': 'Company',
                    'message': 'Half-day rule added for Company.',
                    'id': None
                })

            target_id = _resolve_scoped_target_id(conn, org, branch, scope, scope_id)
            if not target_id:
                return ScopedRuleSaveResult({'success': False, 'error': f'{scope.capitalize()} target not found.'})

            # Resolve target name for clear confirmation message (QA-43)
            target_name = None
            if scope == 'employee':
                u_row = conn.execute("SELECT name FROM users WHERE id = ?", (target_id,)).fetchone()
                target_name = u_row[0] if u_row and u_row[0] else f"Employee #{target_id}"
            elif scope in ('company', 'org'):
                target_name = "Company"
            else:
                d_row = conn.execute("SELECT name FROM departments WHERE id = ?", (target_id,)).fetchone()
                target_name = d_row[0] if d_row and d_row[0] else f"Department #{target_id}"

            # Check for identical duplicate rule (QA-43)
            existing = conn.execute('''
                SELECT id, half_leave_quota_mode, half_leaves_per_quota_day FROM payroll_scoped_rules
                WHERE policy_type = 'half_leave_quota' AND scope_type = ? AND scope_id = ?
                  AND active = 1 AND COALESCE(organization_id, 'DEFAULT_ORG') = ? AND COALESCE(branch_id, 'DEFAULT_BRANCH') = ?
            ''', (scope, target_id, org, branch)).fetchone()
            if existing and existing[1] == mode and existing[2] == count:
                return ScopedRuleSaveResult({
                    'success': False,
                    'duplicate': True,
                    'target_name': target_name,
                    'error': f'A half-day rule already exists for {target_name} ({count} occurrence(s) per month).'
                })

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
            return ScopedRuleSaveResult({
                'success': True,
                'target_name': target_name,
                'message': f'Half-day rule added for {target_name}.'
            })
    except Exception as e:
        logger.error(f"Failed to save scoped half-leave rule: {e}")
        return ScopedRuleSaveResult({'success': False, 'error': f'Failed to save scoped half-leave rule: {e}'})

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
    if p == 'monthly':
        p = 'month'
    elif p == 'daily':
        p = 'day'
    elif p == 'weekly':
        p = 'week'
    if scope not in ('department', 'employee') or p not in ('day', 'week', 'month', 'per_entry'):
        return False
    try:
        max_h = float(max_hours)
    except (TypeError, ValueError, OverflowError):
        return False
    if max_h <= 0:
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            org, branch = _tenant_ids(organization_id, branch_id)
            target_id = _resolve_scoped_target_id(conn, org, branch, scope, scope_id)
            if not target_id:
                return False
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
        premium = float(premium_percent)
    except (TypeError, ValueError, OverflowError):
        return False
    if not 0 <= premium <= 1000:
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            org, branch = _tenant_ids(organization_id, branch_id)
            target_id = _resolve_scoped_target_id(conn, org, branch, scope, scope_id)
            if not target_id:
                return False
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
    org_id, branch_id = _user_tenant(cursor, user_id)
    if department_name:
        cursor.execute(
            """SELECT id FROM departments
               WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
                 AND (COALESCE(organization_id, 'DEFAULT_ORG') = ? OR COALESCE(organization_id, 'DEFAULT_ORG') = 'DEFAULT_ORG')
               ORDER BY CASE WHEN COALESCE(organization_id, 'DEFAULT_ORG') = ? THEN 0 ELSE 1 END
               LIMIT 1""",
            (department_name, org_id, org_id),
        )
        department = cursor.fetchone()
        if department:
            department_id = int(department['id'] if hasattr(department, 'keys') else department[0])
    conditions = ["(scope_type = 'employee' AND scope_id = ?)"]
    rule_day_type = day_type if policy_type == 'overtime_premium' else ''
    params: List[Any] = [policy_type, rule_day_type, org_id, branch_id, int(user_id)]
    if department_id is not None:
        conditions.append("(scope_type = 'department' AND scope_id = ?)")
        params.append(department_id)
    query = f'''
        SELECT * FROM payroll_scoped_rules
        WHERE policy_type = ? AND day_type = ? AND active = 1
          AND (COALESCE(organization_id, 'DEFAULT_ORG') = ? OR COALESCE(organization_id, 'DEFAULT_ORG') = 'DEFAULT_ORG')
          AND (COALESCE(branch_id, 'DEFAULT_BRANCH') = ? OR COALESCE(branch_id, 'DEFAULT_BRANCH') = 'DEFAULT_BRANCH')
          AND ({' OR '.join(conditions)})
        ORDER BY CASE scope_type WHEN 'employee' THEN 0 ELSE 1 END,
                 CASE WHEN COALESCE(organization_id, 'DEFAULT_ORG') = ? THEN 0 ELSE 1 END
        LIMIT 1
    '''
    cursor.execute(query, (*params, org_id))
    row = cursor.fetchone()
    if not row:
        return None
    if hasattr(row, 'keys'):
        return dict(row)
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))


def get_scoped_payroll_rule(
    policy_type: str,
    user_id: Optional[int],
    day_type: str = '',
) -> Optional[Dict[str, Any]]:
    """Public helper to retrieve an active scoped rule for an employee or their department."""
    if not user_id:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return _get_scoped_payroll_rule(conn.cursor(), policy_type, int(user_id), day_type)


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
    deduct_unpaid_leave: Union[bool, Dict[str, Any]] = True,
    half_leave_quota_mode: str = 'category',
    half_leaves_per_quota_day: int = 2,
    holiday_ot_premium_percent: float = 0.0,
    non_working_day_ot_premium_percent: float = 0.0,
    organization_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    ot_hour_divisor: Optional[float] = None,
    proration_basis: Optional[str] = None,
    ot_rounding: Optional[str] = None,
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
    letterhead: Optional[Dict[str, Any]] = None,
) -> bool:
    """Save organization-owned payroll and half-leave quota policies."""
    if isinstance(deduct_unpaid_leave, dict):
        d = deduct_unpaid_leave
        return update_payroll_settings(
            deduct_unpaid_leave=bool(d.get('deduct_unpaid_leave', True)),
            half_leave_quota_mode=d.get('half_leave_quota_mode', half_leave_quota_mode),
            half_leaves_per_quota_day=d.get('half_leaves_per_quota_day', half_leaves_per_quota_day),
            holiday_ot_premium_percent=d.get('holiday_ot_premium_percent', holiday_ot_premium_percent),
            non_working_day_ot_premium_percent=d.get('non_working_day_ot_premium_percent', non_working_day_ot_premium_percent),
            organization_id=organization_id or d.get('organization_id'),
            branch_id=branch_id or d.get('branch_id'),
            ot_hour_divisor=d.get('ot_hour_divisor', ot_hour_divisor),
            proration_basis=d.get('proration_basis', proration_basis),
            ot_rounding=d.get('ot_rounding', ot_rounding),
            attendance_pay_mode=d.get('attendance_pay_mode', attendance_pay_mode),
            currency_code=d.get('currency_code', currency_code),
            decimal_places=d.get('decimal_places', decimal_places),
            holiday_ot_pay_mode=d.get('holiday_ot_pay_mode', holiday_ot_pay_mode),
            non_working_day_ot_pay_mode=d.get('non_working_day_ot_pay_mode', non_working_day_ot_pay_mode),
            holiday_ot_multiplier=d.get('holiday_ot_multiplier', holiday_ot_multiplier),
            non_working_day_ot_multiplier=d.get('non_working_day_ot_multiplier', non_working_day_ot_multiplier),
            holiday_ot_fixed_amount=d.get('holiday_ot_fixed_amount', holiday_ot_fixed_amount),
            non_working_day_ot_fixed_amount=d.get('non_working_day_ot_fixed_amount', non_working_day_ot_fixed_amount),
            off_day_attendance_pay_active=d.get('off_day_attendance_pay_active', off_day_attendance_pay_active),
            off_day_attendance_pay_mode=d.get('off_day_attendance_pay_mode', off_day_attendance_pay_mode),
            off_day_attendance_pay_multiplier=d.get('off_day_attendance_pay_multiplier', off_day_attendance_pay_multiplier),
            off_day_attendance_pay_fixed_amount=d.get('off_day_attendance_pay_fixed_amount', off_day_attendance_pay_fixed_amount),
            feature_flags=d.get('feature_flags', feature_flags),
            letterhead=d.get('letterhead', letterhead),
        )
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
    if basis in ('all_days', 'all_days_in_month', 'calendar'):
        basis = 'calendar_days'
    elif basis in ('working', 'work_days'):
        basis = 'working_days'
    elif basis in ('30', '30_days', '30days'):
        basis = '30_day'
    if basis not in VALID_PRORATION_BASES:
        raise ValueError('Choose calendar days, scheduled working days, or a 30-day month.')
    rounding = _normalize_ot_rounding(
        ot_rounding if ot_rounding is not None else current.get('ot_rounding')
    )
    if ot_rounding is not None and str(ot_rounding).strip().lower() not in VALID_OT_ROUNDING:
        raise ValueError('Choose round hourly rate first, or round the overtime amount at the end.')
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
        "ot_rounding = ?",
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
    letterhead = letterhead or {}
    letterhead_values = []
    for key in PAYSLIP_LETTERHEAD_FIELDS:
        if key in letterhead:
            incoming = letterhead.get(key)
        else:
            incoming = current.get(key)
        if incoming is None:
            text = str(DEFAULT_PAYROLL_SETTINGS.get(key) or '')
        else:
            text = str(incoming).strip()
        if len(text) > 240:
            text = text[:240]
        letterhead_values.append(text)
        assignments.append(f"{key} = ?")
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
        rounding,
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
        *letterhead_values,
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
        "ot_rounding",
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
        *PAYSLIP_LETTERHEAD_FIELDS,
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
            month_key = date.today().isoformat()[:7]
            previous_mode = _normalize_attendance_pay_mode(current.get('attendance_pay_mode'))
            if previous_mode != pay_mode:
                _write_payroll_audit(cur, 'attendance_pay_mode_changed', org, branch, month_key, 'Admin', {
                    'from': previous_mode, 'to': pay_mode,
                })
            prev_basis = str(current.get('proration_basis') or LEGACY_PRORATION_BASIS)
            if prev_basis != basis:
                _write_payroll_audit(cur, 'proration_basis_changed', org, branch, month_key, 'Admin', {
                    'from': prev_basis, 'to': basis,
                })
            prev_round = _normalize_ot_rounding(current.get('ot_rounding'))
            if prev_round != rounding:
                _write_payroll_audit(cur, 'ot_rounding_changed', org, branch, month_key, 'Admin', {
                    'from': prev_round, 'to': rounding,
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
    commit: bool = True,
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
            raw_id = item.get('user_id') or item.get('employee_id')
            raw_name = str(item.get('name') or '').strip()
            raw_email = str(item.get('email') or '').strip()
            raw_amount = item.get('amount')
            note = str(item.get('note') or '').strip()

            emp_label = raw_name or raw_email or (f"ID #{raw_id}" if raw_id else f"Row {index}")

            # Skip instruction/comment rows that might have slipped through
            identifier_str = f"{raw_id} {raw_name}".lower()
            if any(identifier_str.startswith(kw) for kw in ('instruction', 'match employees', 'amount is', 'off-days')):
                continue
            if not raw_id and not raw_name and not raw_email and raw_amount is None:
                continue

            # Robust numeric string conversion
            amount = None
            is_blank = False
            if raw_amount is None:
                is_blank = True
            elif isinstance(raw_amount, str) and raw_amount.strip() == '':
                is_blank = True
            elif isinstance(raw_amount, (int, float)):
                amount = float(raw_amount)
            elif isinstance(raw_amount, str):
                cleaned_amt = raw_amount.strip().replace(',', '')
                # Remove currency prefixes/suffixes
                cleaned_amt = re.sub(r'^[^\d.-]+|[^\d.]+$', '', cleaned_amt).strip()
                if cleaned_amt in ('', '-'):
                    is_blank = True
                else:
                    try:
                        amount = float(cleaned_amt)
                    except (TypeError, ValueError, OverflowError):
                        amount = None

            if is_blank:
                if source == 'excel':
                    # Excel template allows unused employees to be left blank or 0. Skip them.
                    continue
                errors.append({'row': index, 'employee': emp_label, 'field': 'amount', 'error': f"Row {index} ({emp_label}): Amount is required"})
                continue

            if amount is None:
                errors.append({'row': index, 'employee': emp_label, 'field': 'amount', 'error': f"Row {index} ({emp_label}): Amount must be numeric"})
                continue
            if amount < 0:
                errors.append({'row': index, 'employee': emp_label, 'field': 'amount', 'error': f"Row {index} ({emp_label}): Amount cannot be negative"})
                continue
            if amount == 0:
                if source == 'excel':
                    # Excel template instructions say "Leave 0 if none". Skip employees who receive no bonus.
                    continue
                errors.append({'row': index, 'employee': emp_label, 'field': 'amount', 'error': f"Row {index} ({emp_label}): Amount cannot be zero"})
                continue
            if amount > limit:
                errors.append({'row': index, 'employee': emp_label, 'field': 'amount', 'error': f"Row {index} ({emp_label}): Amount exceeds the organization bonus limit of {limit:g}"})
                continue
            user = None
            if raw_id not in (None, ''):
                try:
                    user = by_id.get(int(raw_id))
                except (TypeError, ValueError, OverflowError):
                    user = None
            if user is None and raw_email:
                user = by_email.get(raw_email.lower())
            if user is None and raw_name:
                candidates = by_name.get(raw_name.lower()) or []
                if len(candidates) == 1:
                    user = candidates[0]
            if user is None:
                errors.append({'row': index, 'employee': emp_label, 'field': 'employee_id', 'error': f"Row {index}: Unknown or deleted employee ({emp_label})"})
                continue
            uid = int(user['id'])
            if uid in seen:
                errors.append({'row': index, 'employee': emp_label, 'field': 'employee_id', 'error': f"Row {index}: Duplicate employee #{uid}; duplicates are rejected, not summed"})
                continue
            seen[uid] = index
            preview.append({'user_id': uid, 'employee_name': user.get('name'), 'amount': amount, 'note': note})

        if commit and not errors:
            cursor = conn.cursor()
            if source != 'excel' or not preview:
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
        'committed': bool(commit and not errors),
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
        # QA-42: Check duplicate name or code within tenant
        dup_row = conn.execute(
            """SELECT id, name, code FROM payroll_component_types
               WHERE COALESCE(organization_id,'DEFAULT_ORG')=? AND COALESCE(branch_id,'DEFAULT_BRANCH')=?
                 AND (LOWER(TRIM(name)) = LOWER(TRIM(?)) OR LOWER(TRIM(code)) = LOWER(TRIM(?)))
                 AND (? IS NULL OR id != ?)""",
            (org, branch, name.strip(), code.strip(), component_id, component_id)
        ).fetchone()
        if dup_row:
            if str(dup_row[1]).strip().lower() == name.strip().lower():
                raise ValueError(f"An item with this name already exists: '{name.strip()}'.")
            else:
                raise ValueError(f"An item with this code already exists: '{code.strip()}'.")

        try:
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
        except sqlite3.IntegrityError:
            raise ValueError("An item with this name or code already exists.")


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
        from payroll_extensions import _refresh_duplicate_component_report
        _refresh_duplicate_component_report(cursor)
        conn.commit()
        rows = cursor.execute(
            """SELECT d.*, t.name as component_name, t.code as component_code, u.name as employee_name
               FROM payroll_duplicate_component_report d
               LEFT JOIN payroll_component_types t ON t.id = d.component_type_id
               LEFT JOIN users u ON u.id = d.user_id
               WHERE COALESCE(d.organization_id,'DEFAULT_ORG')=?
                 AND COALESCE(d.branch_id,'DEFAULT_BRANCH')=?
               ORDER BY d.id""",
            (org, branch),
        ).fetchall()
        return [dict(row) for row in rows]


def list_employee_components(user_id: int, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> List[Dict[str, Any]]:
    org, branch = _tenant_ids(organization_id, branch_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # QA-56: Fetch employee basic salary to calculate estimated/resulting amount for percentage lines
        u_row = conn.execute("SELECT basic_salary FROM users WHERE id = ?", (int(user_id),)).fetchone()
        basic_sal = float(u_row[0]) if (u_row and u_row[0] is not None) else 0.0
        s_row = conn.execute("SELECT basic_salary FROM salary_configs WHERE user_id = ?", (int(user_id),)).fetchone()
        if s_row and s_row[0] is not None:
            basic_sal = float(s_row[0])

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
        result = []
        for row in rows:
            d = dict(row)
            d['basic_salary'] = basic_sal
            pct = float(d.get('percent') or 0)
            cm = str(d.get('calc_mode') or '')
            if pct > 0 or cm.startswith('percent'):
                d['calculated_amount'] = round(basic_sal * (pct / 100.0), 2)
            else:
                d['calculated_amount'] = float(d.get('amount') or 0)
            result.append(d)
        return result


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
    if scope in ('company', 'all'):
        scope = 'org'
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
    if min_amount is not None:
        try:
            min_val = float(min_amount)
            if min_val < 0:
                raise ValueError('Lowest amount limit cannot be negative')
            min_amount = min_val
        except (TypeError, ValueError) as exc:
            if 'negative' in str(exc): raise
            raise ValueError('Lowest amount limit must be numeric') from exc
    if max_amount is not None:
        try:
            max_val = float(max_amount)
            if max_val < 0:
                raise ValueError('Highest amount limit cannot be negative')
            max_amount = max_val
        except (TypeError, ValueError) as exc:
            if 'negative' in str(exc): raise
            raise ValueError('Highest amount limit must be numeric') from exc
    if min_amount is not None and max_amount is not None:
        if min_amount > max_amount:
            raise ValueError('Lowest amount limit cannot be greater than highest amount limit')

    try:
        amt_val = float(amount or 0)
        if amt_val < 0:
            raise ValueError('Amount cannot be negative')
        amount = amt_val
    except (TypeError, ValueError, OverflowError) as exc:
        if 'negative' in str(exc): raise
        raise ValueError('Amount must be numeric') from exc

    # QA-57: Rule precedence: If both amount and percent are provided, percent wins
    pct_val = float(percent or 0)
    if pct_val > 0 and (not calc_mode or calc_mode == 'fixed'):
        calc_mode = 'percent_basic'

    check_amt = amount
    if check_amt == 0 and type_id:
        with sqlite3.connect(DB_PATH) as conn_type:
            t_row = conn_type.execute("SELECT default_amount, calc_mode FROM payroll_component_types WHERE id = ?", (type_id,)).fetchone()
            if t_row and t_row[0] is not None and (not calc_mode or calc_mode == 'fixed'):
                check_amt = float(t_row[0])

    if calc_mode in ('percent_basic', 'percent_gross'):
        pct = float(percent or 0)
        if pct < 0 or pct > 100:
            raise ValueError('Percentage must be between 0 and 100')
    else:
        if min_amount is not None and check_amt < min_amount:
            raise ValueError(f'Amount cannot be less than lowest amount limit ({min_amount:g})')
        if max_amount is not None and check_amt > max_amount:
            raise ValueError(f'Amount cannot be greater than highest amount limit ({max_amount:g})')

    start = str(effective_from or '').strip()[:10]
    if not start:
        start = str(join_date or '').strip()[:10]
    if not start:
        raise ValueError('effective_from is required; it does not default to today. Use the employee start date or pick a date.')
    try:
        datetime.strptime(start, '%Y-%m-%d')
    except ValueError as exc:
        raise ValueError('effective_from must use YYYY-MM-DD') from exc

    if scope == 'employee' and uid > 0:
        with sqlite3.connect(DB_PATH) as conn_user:
            u_row = conn_user.execute("SELECT join_date FROM users WHERE id = ?", (uid,)).fetchone()
            if u_row and u_row[0]:
                emp_join = str(u_row[0])[:10]
                if emp_join and start < emp_join:
                    raise ValueError(f"Pay line start date ({start}) cannot precede employee joining date ({emp_join})")

    end = str(effective_to).strip()[:10] if effective_to else None
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        ensure_payroll_extension_tables(cursor)
        # QA-51: Robust duplicate check matching by employee user_id or group scope_id
        existing = [dict(r) for r in cursor.execute(
            """SELECT * FROM payroll_employee_components
               WHERE COALESCE(organization_id,'DEFAULT_ORG')=?
                 AND COALESCE(branch_id,'DEFAULT_BRANCH')=?
                 AND component_type_id=?
                 AND COALESCE(scope_type,'employee')=?
                 AND (
                     (COALESCE(scope_type,'employee') = 'employee' AND COALESCE(user_id,0)=?)
                     OR (COALESCE(scope_type,'employee') = 'org')
                     OR (COALESCE(scope_type,'employee') NOT IN ('employee', 'org') AND COALESCE(scope_id,'')=?)
                 )
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
                     AND status = 'Finalized'
                     AND payroll_month >= ?
                   ORDER BY payroll_month""",
                (org, branch, start[:7]),
            ).fetchall()
        ]
        if affected:
            for m in affected:
                raise_if_payroll_month_locked(f'{m}-01', org, branch)
            if not confirm_affected_months:
                return {
                    'ok': False,
                    'needs_confirmation': True,
                    'field': 'effective_from',
                    'error': 'This pay line affects finalized payroll periods. Confirm to save.',
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


def calculate_monthly_payroll(payroll_month: str | int | None = None, month: Optional[int] = None, organization_id: str = 'DEFAULT_ORG', branch_id: Optional[str] = None, settings_overrides: Optional[Dict[str, Any]] = None, force_recalculate: bool = False) -> Dict[str, Any] | list:
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
    if finalized_run and not settings_overrides and not force_recalculate:
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
    if settings_overrides:
        payroll_settings = dict(payroll_settings)
        payroll_settings.update({k: v for k, v in settings_overrides.items() if v is not None})
    deduct_unpaid_leave = bool(payroll_settings.get('deduct_unpaid_leave', 0))
    ot_hour_divisor = float(payroll_settings.get('ot_hour_divisor') or DEFAULT_OT_DIVISOR) or DEFAULT_OT_DIVISOR
    proration_basis = str(payroll_settings.get('proration_basis') or DEFAULT_PRORATION_BASIS)
    ot_rounding = _normalize_ot_rounding(payroll_settings.get('ot_rounding'))
    payroll_settings['ot_rounding'] = ot_rounding
    attendance_mode = _normalize_attendance_pay_mode(payroll_settings.get('attendance_pay_mode'))
    month_complete = period_end >= month_end
    decimal_places = int(payroll_settings.get('decimal_places') or DEFAULT_DECIMAL_PLACES)
    def _m(value):
        return _payroll_money(value, decimal_places)
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("PRAGMA busy_timeout = 60000")
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
                   u.join_date,
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
        cursor.execute('SELECT id, working_days FROM shifts')
        shift_days_by_id = {
            int(row['id']): _normalize_shift_working_days(row['working_days'])
            for row in cursor.fetchall()
        }
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
                'premium_percent': _get_overtime_premium_percent(
                    cursor, overtime_date, day_type, payroll_settings, holiday, user_id=overtime_user_id
                ),
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
        # LAYER 5: Standardized business employment start date precedence
        emp_start_str = get_employee_employment_start_date(employee)
        try:
            employed_from = datetime.strptime(emp_start_str, "%Y-%m-%d").date() if emp_start_str else month_start
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
        pattern_days = None
        for day in daily_records:
            wds = day.get('shift_working_days')
            if wds:
                pattern_days = wds
                break
        if not pattern_days:
            pattern_days = shift_days_by_id.get(int(employee.get('shift_id') or 0)) or default_working_days()
        days_by_date = {str(day.get('date')): day for day in daily_records}
        full_month_workdays = 0
        cursor_day = month_start
        while cursor_day <= month_end:
            rec = days_by_date.get(cursor_day.isoformat()) or {}
            if _is_scheduled_workday(cursor_day, pattern_days, rec.get('calendar_override')):
                full_month_workdays += 1
            cursor_day += timedelta(days=1)
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
        # PAY-01: Correctly calculate days accounting for half-day leaves (0.5 duration)
        present_days = 0.0
        late_days = 0.0
        approved_leave_days = 0.0
        approved_leave_days_period = 0.0
        unpaid_leave_days = 0.0
        unexcused_absent_days = 0.0

        for day in eligible_records:
            if not day.get('scheduled_workday', True):
                continue
            st = day.get('status')
            is_half = bool(day.get('is_half_leave'))
            half_is_paid = bool(day.get('half_leave_is_paid'))

            if st in ('present', 'checked_out'):
                if is_half:
                    present_days += 0.5
                    if half_is_paid:
                        approved_leave_days_period += 0.5
                    else:
                        unpaid_leave_days += 0.5
                else:
                    present_days += 1.0
            elif st == 'late':
                if is_half:
                    late_days += 0.5
                    if half_is_paid:
                        approved_leave_days_period += 0.5
                    else:
                        unpaid_leave_days += 0.5
                else:
                    late_days += 1.0
            elif st == 'on_leave':
                if is_half:
                    approved_leave_days_period += 0.5
                    unexcused_absent_days += 0.5
                else:
                    approved_leave_days_period += 1.0
            elif st == 'on_leave_unpaid':
                if is_half:
                    unpaid_leave_days += 0.5
                    unexcused_absent_days += 0.5
                else:
                    unpaid_leave_days += 1.0
            elif st == 'absent':
                unexcused_absent_days += 1.0

        for day in month_schedule_records:
            if not day.get('scheduled_workday', True):
                continue
            st = day.get('status')
            is_half = bool(day.get('is_half_leave'))
            half_is_paid = bool(day.get('half_leave_is_paid'))
            if st == 'on_leave':
                approved_leave_days += 0.5 if is_half else 1.0
            elif is_half and half_is_paid and st in ('present', 'checked_out', 'late'):
                approved_leave_days += 0.5

        half_leave_count = sum(int(day.get('half_leave_count') or 0) for day in eligible_records)
        absent_days = unexcused_absent_days + unpaid_leave_days
        payable_days = min(float(scheduled_workdays), round(present_days + late_days + approved_leave_days_period, 2))
        
        proration_factor = _payroll_proration_factor(
            proration_basis,
            eligible_start,
            schedule_end,
            month_start,
            month_end,
            month_scheduled_workdays,
            full_month_workdays,
        )
        if attendance_mode == 'payable_days' and proration_basis == 'working_days':
            month_scheduled_workdays = int(full_month_workdays or 0)
        
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
            unrounded_basic_salary = basic_salary
            if revision_ot > 0:
                employee['ot_rate'] = revision_ot
        else:
            # payable_days + working_days: earn divisor is the whole month (26), so
            # do not also scale basic by join/exit or joiners pay 17/17 of 60,000.
            scale = proration_factor
            if attendance_mode == 'payable_days' and proration_basis == 'working_days':
                scale = 1.0
            unrounded_basic_salary = max(0.0, float(employee['basic_salary'] or 0)) * scale
            basic_salary = _m(unrounded_basic_salary)
            lump_allowances = round(max(0.0, float(employee.get('allowances') or 0.0)) * scale, 2)
            lump_deductions = round(max(0.0, float(employee.get('custom_deductions') or 0.0)) * scale, 2)

        # QA-41: Save proration details and explanation for mid-month join/exit
        full_basic = float(employee['basic_salary'] or 0.0)
        is_prorated = False
        proration_days_paid = 0
        proration_method_days = 0
        proration_explanation = ''

        if (eligible_start > month_start or schedule_end < month_end) and full_basic > 0:
            import calendar as _cal
            _, total_days_in_month = _cal.monthrange(month_start.year, month_start.month)
            active_cal_days = max(0, (schedule_end - eligible_start).days + 1)
            
            p_basis = str(proration_basis or 'all_days').strip().lower()
            if p_basis == 'working_days':
                proration_days_paid = max(0, int(month_scheduled_workdays or 0))
                proration_method_days = max(1, int(full_month_workdays or 0))
                if proration_days_paid < proration_method_days:
                    is_prorated = True
                    fb_str = f"{full_basic:,.0f}" if full_basic.is_integer() else f"{full_basic:,.2f}"
                    bs_str = f"{basic_salary:,.2f}"
                    proration_explanation = f"Basic {fb_str}, paid {proration_days_paid} of {proration_method_days} days = {bs_str}"
            elif p_basis == '30_day':
                proration_days_paid = min(active_cal_days, 30)
                proration_method_days = 30
                if proration_days_paid < 30:
                    is_prorated = True
                    fb_str = f"{full_basic:,.0f}" if full_basic.is_integer() else f"{full_basic:,.2f}"
                    bs_str = f"{basic_salary:,.2f}"
                    proration_explanation = f"Basic {fb_str}, paid {proration_days_paid} of 30 days = {bs_str}"
            else:
                proration_days_paid = min(active_cal_days, total_days_in_month)
                proration_method_days = total_days_in_month
                if proration_days_paid < total_days_in_month:
                    is_prorated = True
                    fb_str = f"{full_basic:,.0f}" if full_basic.is_integer() else f"{full_basic:,.2f}"
                    bs_str = f"{basic_salary:,.2f}"
                    proration_explanation = f"Basic {fb_str}, paid {proration_days_paid} of {proration_method_days} days = {bs_str}"

        # Apply calendar-based premiums to approved overtime hours.
        overtime_entries = approved_ot_by_user.get(employee_id, [])
        if exited_on:
            overtime_entries = [entry for entry in overtime_entries if str(entry.get('ot_date') or '')[:10] <= exited_on.isoformat()]
        ot_hours = round(sum(entry['hours'] for entry in overtime_entries), 2)
        configured_ot_rate = float(employee.get('ot_rate') or 0.0)
        if configured_ot_rate > 0.0:
            ot_rate = _m(configured_ot_rate) if ot_rounding == 'round_rate_first' else float(configured_ot_rate)
        elif basic_salary > 0.0:
            raw_ot_rate = calculate_employee_ot_rate(cursor, employee_id, as_of_date=eligible_end.isoformat(), basic_salary=basic_salary)
            ot_rate = _m(raw_ot_rate) if ot_rounding == 'round_rate_first' else raw_ot_rate
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
        attendance_off_days_worked = len(adj_pack.get('remaining_off_days') or [])
        attendance_off_day_extra = _off_day_attendance_extra(
            daily_rate, attendance_off_days_worked, payroll_settings, decimal_places
        )
        off_day_extra_pay = _m(attendance_off_day_extra)
        off_days_worked = attendance_off_days_worked
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
        
        # QA-48: Compute absence rule deduction using unrounded basic internally and round once at final monetary step
        unrounded_absence_deduction = 0.0
        if unexcused_absent_days > 0 and matched_rule:
            if deduction_type == 'percentage' and deduction_percent is not None:
                unrounded_absence_deduction = unrounded_basic_salary * (deduction_percent / 100.0)
                absence_rule_deduction = round(unrounded_absence_deduction, 2)
            else:
                unrounded_absence_deduction = fixed_amount
                absence_rule_deduction = round(fixed_amount, 2)
        else:
            absence_rule_deduction = 0.0
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
        has_joining_date = bool(str(employee.get('join_date') or '').strip())
        if not has_joining_date:
            issues_remarks = 'Missing joining date; attendance deductions deferred'
            rule_missing = False
            absence_rule_deduction = 0.0
            half_leave_deduction = 0.0
            unpaid_leave_deduction = 0.0
            attendance_shortfall = 0.0
        elif attendance_mode == 'payable_days':
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
        total_emp_deductions = round(attendance_deduction + custom_deductions, 2)
        deduction_amount = total_emp_deductions

        # Gross = Basic + Allowances + Approved Overtime + Excel bonus + rest-day extra + adjustments/arrears
        gross_salary = _m(
            basic_salary + allowances + overtime_amount + bonus_amount + off_day_extra_pay + adjustment_amount
        )
        # QA-49: Preserve exact net and keep shortfall visible when cuts exceed pay
        shortfall = round(max(0.0, total_emp_deductions - gross_salary), 2)
        net_salary = round(gross_salary - total_emp_deductions, 2)

        if attendance_mode == 'payable_days':
            p_label = int(payable_days) if float(payable_days).is_integer() else round(payable_days, 1)
            matched_rule_label = f'Pay {p_label}/{int(month_scheduled_workdays)} working days'
        elif matched_rule:
            max_absent = matched_rule['max_absent_days']
            matched_rule_label = (
                f"{matched_rule['min_absent_days']}+ absent days" if max_absent is None
                else f"{matched_rule['min_absent_days']} absent days" if max_absent == matched_rule['min_absent_days']
                else f"{matched_rule['min_absent_days']}-{max_absent} absent days"
            )
        else:
            matched_rule_label = 'No absence deduction' if unexcused_absent_days == 0 else 'No matching rule'
        # PAY-02: Include only leave actually taken through the live period window in payroll
        paid_leave_days = approved_leave_days_period
        total_leave_days = paid_leave_days + unpaid_leave_days
        package = basic_salary + allowances
        earn_div = float(month_scheduled_workdays or 0)
        earned_so_far = _m(package * (float(payable_days) / earn_div) if earn_div else 0.0)
        remaining_days = max(0, int(month_scheduled_workdays or 0) - int(scheduled_workdays or 0))
        proj_val = min(float(month_scheduled_workdays or 0), float(payable_days or 0) + remaining_days)
        projected_payable = int(proj_val) if float(proj_val).is_integer() else round(proj_val, 1)
        projected_month_end = _m(package * (float(projected_payable) / earn_div) if earn_div else 0.0)

        # QA-48: Explicit missed-day rule metadata
        missed_day_details = None
        if matched_rule and unexcused_absent_days > 0:
            min_d = matched_rule['min_absent_days']
            max_d = matched_rule['max_absent_days']
            if max_d is None:
                bracket_str = f"{min_d}+ days"
            elif max_d == min_d:
                bracket_str = f"{min_d} days"
            else:
                bracket_str = f"{min_d}–{max_d} days"

            base_name = 'prorated basic' if is_prorated else 'basic'
            pct_label = f"{int(deduction_percent)}%" if deduction_percent is not None and deduction_percent.is_integer() else f"{deduction_percent}%"
            # QA-48: Name the rule, absent-day count, and percent so hand-verifiable:
            # Example: "Absent 14 days (6 to 31 days: 30% of 58,064.52) = 17,419.36"
            if deduction_type == 'percentage':
                explanation = f"Missed-day rule: {bracket_str} = {pct_label} of {base_name} (Absent {int(unexcused_absent_days)} days: {absence_rule_deduction:,.2f})"
            else:
                explanation = f"Missed-day rule: {bracket_str} = fixed {fixed_amount:,.2f} (Absent {int(unexcused_absent_days)} days)"
            missed_day_details = {
                'rule_label': bracket_str,
                'min_absent_days': min_d,
                'max_absent_days': max_d,
                'method': deduction_type,
                'percentage': deduction_percent,
                'fixed_amount': fixed_amount,
                'missed_days': unexcused_absent_days,
                'base_type': base_name,
                'base_amount': basic_salary,
                'unrounded_base': unrounded_basic_salary,
                'full_basic': full_basic,
                'unrounded_deduction': unrounded_absence_deduction,
                'final_deduction': absence_rule_deduction,
                'rounding_policy': 'Round half to even at final monetary step (2 decimal places)',
                'explanation': explanation,
            }

        # QA-47: Authoritative days_paid calculation for payslip header
        if attendance_mode == 'payable_days':
            days_paid = payable_days
        elif is_prorated:
            days_paid = proration_days_paid
        else:
            days_paid = month_scheduled_workdays or scheduled_workdays or proration_method_days

        rows.append({
            'employee_id': employee_id,
            'user_id': employee_id,
            'employee_name': employee['employee_name'],
            'department': employee['department'],
            'designation': employee['designation'] or 'Staff Member',
            'exit_date': employee.get('exit_date'),
            'has_joining_date': has_joining_date,
            'basic_salary': basic_salary,
            'is_prorated': 1 if is_prorated else 0,
            'unprorated_basic_salary': full_basic,
            'proration_days_paid': proration_days_paid,
            'proration_method_days': proration_method_days,
            'proration_explanation': proration_explanation,
            'days_paid': days_paid,
            'missed_day_details': missed_day_details,
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
            'equivalent_half_leave_days': round(half_leave_count * 0.5, 2),
            'half_leave_deduction': half_leave_deduction,
            'matched_half_leave_rule': (
                f"{matched_half_leave_rule['min_absent_days']}+ half-leave occurrences"
                if matched_half_leave_rule and matched_half_leave_rule['max_absent_days'] is None
                else f"{matched_half_leave_rule['min_absent_days']}-{matched_half_leave_rule['max_absent_days']} half-leave occurrences"
                if matched_half_leave_rule else 'No half-leave deduction'
            ),
            'absent_days': absent_days,
            'unexcused_absent_days': unexcused_absent_days,
            'late_days': late_days,
            'approved_leave_days': approved_leave_days_period,
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
            'attendance_deduction': attendance_deduction,
            'deduction_amount': deduction_amount,
            'deduction': deduction_amount,
            'total_deductions': total_emp_deductions,
            'shortfall': shortfall,
            'unrecovered_deductions': shortfall,
            'rule_missing': rule_missing,
            'issues_remarks': issues_remarks,
            'earned_so_far': earned_so_far,
            'projected_month_end': projected_month_end,
            'net_salary': net_salary,
            'net_payable': net_salary,
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
        'total_deduction': round(sum(row['deduction_amount'] + row['custom_deductions'] for row in rows), 2),
        'total_net_salary': round(sum(row['net_salary'] for row in rows), 2),
        'absence_rules_configured': absence_rules_configured,
        'notices': payroll_notices,
        'earned_so_far_label': f'Earned so far (through {period_end.isoformat()})',
        'projected_month_end_label': 'Projected month-end',
        'deactivated_policy': 'Included only when exit_date is set and on or after the month start. Deactivated with no exit date are excluded and listed in notices.',
        'proration_basis': proration_basis,
        'ot_rounding': ot_rounding,
        'snapshot': {
            'settings': {
                'attendance_pay_mode': attendance_mode,
                'proration_basis': proration_basis,
                'ot_rounding': ot_rounding,
                'ot_hour_divisor': ot_hour_divisor,
                'decimal_places': decimal_places,
                'deduct_unpaid_leave': deduct_unpaid_leave,
                'currency_code': payroll_settings.get('currency_code'),
            },
            'rules': active_rules,
            'pay_lines': extra_maps.get('assignments') if isinstance(extra_maps, dict) else [],
            'pay_line_types': list((extra_maps.get('types') or {}).values()) if isinstance(extra_maps, dict) else [],
            'rates': [
                {
                    'employee_id': row['employee_id'],
                    'basic_salary': row['basic_salary'],
                    'overtime_rate': row.get('overtime_rate'),
                    'allowances': row.get('allowances'),
                }
                for row in rows
            ],
            'attendance_counts': [
                {
                    'employee_id': row['employee_id'],
                    'present_days': row.get('present_days'),
                    'absent_days': row.get('absent_days'),
                    'payable_days': row.get('payable_days'),
                    'scheduled_workdays': row.get('scheduled_workdays'),
                    'month_scheduled_workdays': row.get('month_scheduled_workdays'),
                }
                for row in rows
            ],
        },
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
    if rows and not force_recalculate and not finalized_run:
        preview['payroll_run_id'] = _persist_draft_monthly_payroll(
            preview, organization_id=organization_id, branch_id=branch_id
        )
    else:
        preview['payroll_run_id'] = None
    finalized_run = get_monthly_payroll(
        payroll_month, organization_id=organization_id, branch_id=branch_id
    )
    if finalized_run and not settings_overrides and not force_recalculate:
        return finalized_run
    if month is not None and payroll_month.endswith(f"-{int(month):02d}"):
        return rows
    return preview


def _insert_payroll_records(cursor: sqlite3.Cursor, run_id: int, rows: List[Dict[str, Any]], finalized: bool, organization_id: str = 'DEFAULT_ORG', branch_id: Optional[str] = None) -> None:
    payroll_record_columns = {col[1] for col in cursor.execute("PRAGMA table_info(payroll_records)").fetchall()}
    for col_name, col_def in [
        ("is_prorated", "INTEGER NOT NULL DEFAULT 0"),
        ("unprorated_basic_salary", "REAL NOT NULL DEFAULT 0.0"),
        ("proration_days_paid", "INTEGER NOT NULL DEFAULT 0"),
        ("proration_method_days", "INTEGER NOT NULL DEFAULT 0"),
        ("proration_explanation", "TEXT DEFAULT ''"),
        ("days_paid", "REAL DEFAULT NULL"),
        ("missed_day_details", "TEXT DEFAULT ''"),
    ]:
        if col_name not in payroll_record_columns:
            cursor.execute(f"ALTER TABLE payroll_records ADD COLUMN {col_name} {col_def}")
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
            fixed_amount, deduction_amount, net_salary, status, organization_id, branch_id,
            is_prorated, unprorated_basic_salary, proration_days_paid, proration_method_days, proration_explanation,
            days_paid, missed_day_details
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        organization_id or 'DEFAULT_ORG', branch_id or 'DEFAULT_BRANCH',
        row.get('is_prorated', 0), row.get('unprorated_basic_salary', 0.0),
        row.get('proration_days_paid', 0), row.get('proration_method_days', 0),
        row.get('proration_explanation', ''),
        row.get('days_paid'),
        json.dumps(row.get('missed_day_details') or {})
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
        _cloud_mirror("mirror_payroll_run", {
            "id": run_id,
            "organization_id": organization_id,
            "branch_id": branch_id or "DEFAULT_BRANCH",
            "payroll_month": preview.get("payroll_month"),
            "period_start": preview.get("period_start"),
            "period_end": preview.get("period_end"),
            "employee_count": preview.get("employee_count", 0),
            "total_basic_salary": preview.get("total_basic_salary", 0.0),
            "total_deduction": preview.get("total_deduction", 0.0),
            "total_net_salary": preview.get("total_net_salary", 0.0),
            "status": "draft"
        }, preview.get("records", []))
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
    if organization_id and organization_id not in ('all', 'DEFAULT_ORG'):
        if postgres_is_configured():
            try:
                import postgres_mirror as cloud_pg
                pg_res = cloud_pg.fetch_pg_monthly_payroll(payroll_month, organization_id, branch_id)
                if pg_res is False:
                    return None
            except Exception as error:
                logger.warning("Postgres unreachable for monthly payroll; falling back to SQLite cache: %s", error)
    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("PRAGMA busy_timeout = 60000")
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
            try:
                record['missed_day_details'] = json.loads(record.get('missed_day_details') or '{}')
            except (TypeError, ValueError):
                record['missed_day_details'] = {}
            result['records'].append(record)
        raw_snap = result.get('snapshot_json')
        if raw_snap:
            try:
                result['snapshot'] = json.loads(raw_snap)
            except (TypeError, ValueError):
                result['snapshot'] = None
    result['is_finalized'] = True
    if result['records']:
        result['total_allowances'] = round(sum(float(row.get('allowances') or 0) for row in result['records']), 2)
        result['total_overtime'] = round(sum(float(row.get('overtime_amount') or 0) for row in result['records']), 2)
        result['total_bonus'] = round(sum(float(row.get('bonus_amount') or 0) for row in result['records']), 2)
        result['total_off_day_extra'] = round(sum(float(row.get('off_day_extra_pay') or 0) for row in result['records']), 2)
        result['total_deduction'] = round(sum(float(row.get('deduction_amount') or 0) + float(row.get('custom_deductions') or 0) for row in result['records']), 2)
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
    letterhead = {key: settings.get(key) or DEFAULT_PAYROLL_SETTINGS.get(key) or '' for key in PAYSLIP_LETTERHEAD_FIELDS}
    return {
        'payroll_month': payroll.get('payroll_month'),
        'period_start': payroll.get('period_start'),
        'period_end': payroll.get('period_end'),
        'status': payroll.get('status'),
        'is_finalized': payroll.get('is_finalized'),
        'currency_code': settings.get('currency_code') or DEFAULT_CURRENCY,
        'decimal_places': settings.get('decimal_places') if settings.get('decimal_places') is not None else DEFAULT_DECIMAL_PLACES,
        'finalized_at': payroll.get('finalized_at'),
        'finalized_by': payroll.get('finalized_by'),
        'letterhead': letterhead,
        'company': {
            'name': letterhead.get('payslip_company_name') or 'QIntellect Technologies',
            'branch': letterhead.get('payslip_address') or '',
            'city': '',
            'phone': letterhead.get('payslip_phone') or '',
            'email': letterhead.get('payslip_email') or '',
            'address': letterhead.get('payslip_address') or '',
        },
        'employee': record,
        'gross_salary': record.get('gross_salary'),
        'deductions': {
            'absence': record.get('absence_deduction') or 0,
            'unpaid_leave': record.get('unpaid_leave_deduction') or 0,
            'half_leave': record.get('half_leave_deduction') or 0,
            'attendance_shortfall': record.get('attendance_shortfall') or 0,
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
        with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA busy_timeout = 60000")
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
            snapshot_json = json.dumps(preview.get('snapshot') or {}, default=str)
            cursor.execute('''
                UPDATE payroll_runs SET status = 'Finalized', finalized_at = CURRENT_TIMESTAMP, finalized_by = ?,
                    snapshot_json = ?
                WHERE id = ?
            ''', (finalized_by, snapshot_json, run_id,))
            _write_payroll_audit(cursor, 'payroll_finalized', organization_id, branch_id, preview.get('payroll_month'), finalized_by, {
                'run_id': run_id,
                'employee_count': preview.get('employee_count'),
                'total_net_salary': preview.get('total_net_salary'),
                'excluded': preview.get('excluded') or [],
                'finalize_early': bool(preview.get('finalize_early')),
            })
            conn.commit()
            _cloud_mirror("mirror_payroll_run", {
                "id": run_id,
                "organization_id": organization_id,
                "branch_id": branch_id or "DEFAULT_BRANCH",
                "payroll_month": preview.get("payroll_month"),
                "period_start": preview.get("period_start"),
                "period_end": preview.get("period_end"),
                "employee_count": preview.get("employee_count", 0),
                "total_basic_salary": preview.get("total_basic_salary", 0.0),
                "total_deduction": preview.get("total_deduction", 0.0),
                "total_net_salary": preview.get("total_net_salary", 0.0),
                "status": "approved"
            }, preview.get("records", []))
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

def _sync_tenant_departments_and_designations(cursor: sqlite3.Cursor, organization_id: Optional[str] = None):
    """Ensure any active user's department and designation are properly seeded into the authoritative tables."""
    try:
        org_filter = "AND organization_id = ?" if organization_id and organization_id != 'DEFAULT_ORG' else ""
        params = (organization_id,) if organization_id and organization_id != 'DEFAULT_ORG' else ()

        cursor.execute(f"""
            SELECT DISTINCT COALESCE(organization_id, 'DEFAULT_ORG'), COALESCE(branch_id, 'DEFAULT_BRANCH'), department
            FROM users
            WHERE active = 1 AND department IS NOT NULL AND TRIM(department) != ''
            {org_filter}
        """, params)
        for org, br, dept_name in cursor.fetchall():
            dept_name = dept_name.strip()
            cursor.execute("SELECT id FROM departments WHERE name = ? AND organization_id = ?", (dept_name, org))
            if not cursor.fetchone():
                code = ''.join([w[0].upper() for w in dept_name.split() if w])[:6] or 'GEN'
                cursor.execute("""
                    INSERT INTO departments (name, code, description, active, organization_id, branch_id)
                    VALUES (?, ?, ?, 1, ?, ?)
                """, (dept_name, code, f'{dept_name} Department', org, br))

        cursor.execute(f"""
            SELECT DISTINCT COALESCE(u.organization_id, 'DEFAULT_ORG'), COALESCE(u.branch_id, 'DEFAULT_BRANCH'), u.department, u.position
            FROM users u
            WHERE u.active = 1 AND u.department IS NOT NULL AND TRIM(u.department) != ''
              AND u.position IS NOT NULL AND TRIM(u.position) != ''
              {org_filter.replace('organization_id', 'u.organization_id')}
        """, params)
        for org, br, dept_name, pos_title in cursor.fetchall():
            dept_name = dept_name.strip()
            pos_title = pos_title.strip()
            cursor.execute("SELECT id FROM departments WHERE name = ? AND organization_id = ?", (dept_name, org))
            dept_row = cursor.fetchone()
            if dept_row:
                dept_id = dept_row[0]
                cursor.execute("SELECT id FROM designations WHERE department_id = ? AND title = ? AND organization_id = ?", (dept_id, pos_title, org))
                if not cursor.fetchone():
                    cursor.execute("""
                        INSERT INTO designations (department_id, title, description, active, organization_id, branch_id)
                        VALUES (?, ?, ?, 1, ?, ?)
                    """, (dept_id, pos_title, f'{pos_title} in {dept_name}', org, br))
    except Exception as e:
        logger.warning(f"Self-healing departments/designations sync warning: {e}")

def get_all_departments_list(active_only: bool = False, organization_id: Optional[str] = None) -> List[Dict]:
    """Retrieve all departments with registered employee counts and designation counts, scoped by organization."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            _sync_tenant_departments_and_designations(cursor, organization_id)
            conn.commit()

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
            _sync_tenant_departments_and_designations(cursor, organization_id)
            conn.commit()
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

get_cameras = get_all_cameras

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
                heal_deleted_camera_assignments(cam_row[1] or organization_id)
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