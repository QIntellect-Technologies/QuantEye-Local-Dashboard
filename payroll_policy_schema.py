"""Tenant-scope migrations and shared payroll policy defaults.

Existing calculation behavior stays the default: OT divisor 208,
calendar-day proration, 2 decimal places, PKR.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

DEFAULT_ORG = "DEFAULT_ORG"
DEFAULT_BRANCH = "DEFAULT_BRANCH"
DEFAULT_OT_DIVISOR = 208.0
DEFAULT_PRORATION_BASIS = "calendar_days"
DEFAULT_CURRENCY = "PKR"
DEFAULT_DECIMAL_PLACES = 2
VALID_PRORATION_BASES = ("calendar_days", "working_days", "30_day")
VALID_OT_PAY_MODES = ("percent", "multiplier", "fixed_hour")
VALID_OFF_DAY_ATTENDANCE_MODES = ("multiplier", "fixed_day")
DEFAULT_EXAMPLE_MULTIPLIER = 1.5

DEFAULT_LEAVE_TYPES = (
    ("Casual Leave", "casual", 10.0, 1, "Short-term casual leave"),
    ("Sick Leave", "sick", 8.0, 1, "Medical or health related leave"),
    ("Annual / Paid Leave", "annual", 14.0, 1, "Earned annual paid leave"),
    ("Unpaid Leave (LWP)", "unpaid", 0.0, 0, "Leave without pay"),
    ("Excess Paid Leave", "excess_paid", 0.0, 1, "Approved paid leave beyond standard quota"),
)

DEFAULT_PAYROLL_SETTINGS = {
    "deduct_unpaid_leave": 0,
    "half_leave_quota_mode": "category",
    "half_leaves_per_quota_day": 2,
    "holiday_ot_premium_percent": 0.0,
    "non_working_day_ot_premium_percent": 0.0,
    "holiday_ot_premium_active": 0,
    "non_working_day_ot_premium_active": 0,
    "overtime_max_hours": 0.0,
    "overtime_max_period": "day",
    "ot_hour_divisor": DEFAULT_OT_DIVISOR,
    "proration_basis": DEFAULT_PRORATION_BASIS,
    "currency_code": DEFAULT_CURRENCY,
    "decimal_places": DEFAULT_DECIMAL_PLACES,
    "holiday_ot_pay_mode": "percent",
    "non_working_day_ot_pay_mode": "percent",
    "holiday_ot_multiplier": DEFAULT_EXAMPLE_MULTIPLIER,
    "non_working_day_ot_multiplier": DEFAULT_EXAMPLE_MULTIPLIER,
    "holiday_ot_fixed_amount": 0.0,
    "non_working_day_ot_fixed_amount": 0.0,
    "off_day_attendance_pay_active": 0,
    "off_day_attendance_pay_mode": "multiplier",
    "off_day_attendance_pay_multiplier": DEFAULT_EXAMPLE_MULTIPLIER,
    "off_day_attendance_pay_fixed_amount": 0.0,
}


def tenant_ids(organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> Tuple[str, str]:
    org = str(organization_id or DEFAULT_ORG).strip() or DEFAULT_ORG
    branch = str(branch_id or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
    if org == "all":
        org = DEFAULT_ORG
    if branch == "all":
        branch = DEFAULT_BRANCH
    return org, branch


def _settings_map(settings: Any) -> Dict[str, Any]:
    if not settings:
        return {}
    if isinstance(settings, dict):
        return settings
    if hasattr(settings, "keys"):
        return dict(settings)
    return {}


def overtime_entry_pay(
    ot_rate: float,
    hours: float,
    day_type: str,
    settings: Any,
    holiday: Optional[Dict[str, Any]] = None,
    decimal_places: int = DEFAULT_DECIMAL_PLACES,
) -> Tuple[float, float, float]:
    """Return (effective hourly rate, amount, equivalent extra percent) for approved OT hours.

    Default mode is percent so existing 50% holiday / 25% rest-day premiums stay identical.
    Multiplier 1.5 means 1.5× the normal overtime hourly rate. Fixed extra is per hour
    on top of the normal overtime rate.
    """
    rate = float(ot_rate or 0.0)
    qty = float(hours or 0.0)
    cfg = _settings_map(settings)
    if qty <= 0:
        return money(rate, decimal_places), 0.0, 0.0

    def _percent(percent: float) -> Tuple[float, float, float]:
        extra = float(percent or 0.0)
        effective = money(rate * (1 + extra / 100.0), decimal_places)
        return effective, money(qty * effective, decimal_places), extra

    if day_type == "holiday":
        if holiday and holiday.get("overtime_premium_percent") is not None:
            return _percent(holiday.get("overtime_premium_percent") or 0.0)
        if not cfg.get("holiday_ot_premium_active"):
            effective = money(rate, decimal_places)
            return effective, money(qty * effective, decimal_places), 0.0
        mode = str(cfg.get("holiday_ot_pay_mode") or "percent").strip().lower()
        if mode == "multiplier":
            factor = float(cfg.get("holiday_ot_multiplier") or 1.0)
            effective = money(rate * factor, decimal_places)
            return effective, money(qty * effective, decimal_places), (factor - 1.0) * 100.0
        if mode == "fixed_hour":
            extra = float(cfg.get("holiday_ot_fixed_amount") or 0.0)
            effective = money(rate + extra, decimal_places)
            equiv = (extra / rate * 100.0) if rate else 0.0
            return effective, money(qty * effective, decimal_places), equiv
        return _percent(cfg.get("holiday_ot_premium_percent") or 0.0)

    if day_type == "non_working_day":
        if not cfg.get("non_working_day_ot_premium_active"):
            effective = money(rate, decimal_places)
            return effective, money(qty * effective, decimal_places), 0.0
        mode = str(cfg.get("non_working_day_ot_pay_mode") or "percent").strip().lower()
        if mode == "multiplier":
            factor = float(cfg.get("non_working_day_ot_multiplier") or 1.0)
            effective = money(rate * factor, decimal_places)
            return effective, money(qty * effective, decimal_places), (factor - 1.0) * 100.0
        if mode == "fixed_hour":
            extra = float(cfg.get("non_working_day_ot_fixed_amount") or 0.0)
            effective = money(rate + extra, decimal_places)
            equiv = (extra / rate * 100.0) if rate else 0.0
            return effective, money(qty * effective, decimal_places), equiv
        return _percent(cfg.get("non_working_day_ot_premium_percent") or 0.0)

    effective = money(rate, decimal_places)
    return effective, money(qty * effective, decimal_places), 0.0


def off_day_attendance_extra(
    daily_rate: float,
    off_days_worked: int,
    settings: Any,
    decimal_places: int = DEFAULT_DECIMAL_PLACES,
) -> float:
    """Extra pay when an employee attends on a shift rest day / holiday with no overtime entry.

    Off days come from Shift Management and company holidays, not a hardcoded Sunday.
    Disabled by default so existing payroll numbers do not change.
    """
    cfg = _settings_map(settings)
    if not cfg.get("off_day_attendance_pay_active"):
        return 0.0
    count = int(off_days_worked or 0)
    if count <= 0:
        return 0.0
    mode = str(cfg.get("off_day_attendance_pay_mode") or "multiplier").strip().lower()
    if mode == "fixed_day":
        return money(count * float(cfg.get("off_day_attendance_pay_fixed_amount") or 0.0), decimal_places)
    factor = float(cfg.get("off_day_attendance_pay_multiplier") or 0.0)
    return money(count * float(daily_rate or 0.0) * factor, decimal_places)


def money(value: Any, places: Any = DEFAULT_DECIMAL_PLACES) -> float:
    try:
        digits = int(places)
    except (TypeError, ValueError):
        digits = DEFAULT_DECIMAL_PLACES
    if digits < 0:
        digits = 0
    if digits > 6:
        digits = 6
    return round(float(value or 0), digits)


def proration_factor(
    basis: Optional[str],
    eligible_start: date,
    schedule_end: date,
    month_start: date,
    month_end: date,
    eligible_workdays: int,
    full_month_workdays: int,
) -> float:
    mode = str(basis or DEFAULT_PRORATION_BASIS).strip().lower()
    if mode not in VALID_PRORATION_BASES:
        mode = DEFAULT_PRORATION_BASIS
    if mode == "working_days":
        denom = max(int(full_month_workdays or 0), 1)
        numer = max(int(eligible_workdays or 0), 0)
        return 1.0 if numer >= denom else numer / denom
    active_days = max(0, (schedule_end - eligible_start).days + 1)
    if mode == "30_day":
        return 1.0 if active_days >= 30 else active_days / 30.0
    import calendar as _calendar
    _, total_days_in_month = _calendar.monthrange(month_start.year, month_start.month)
    return 1.0 if active_days >= total_days_in_month else (active_days / total_days_in_month)


def _columns(cursor, table: str) -> Dict[str, int]:
    return {row[1]: idx for idx, row in enumerate(cursor.execute(f"PRAGMA table_info({table})").fetchall())}


def _create_sql(cursor, table: str) -> str:
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return (row[0] or "") if row else ""


def _unique_index_columns(cursor, table: str) -> Iterable[Sequence[str]]:
    for index in cursor.execute(f"PRAGMA index_list({table})").fetchall():
        if not index[2]:
            continue
        yield tuple(
            row[2]
            for row in cursor.execute(f"PRAGMA index_info('{index[1]}')").fetchall()
        )


def migrate_payroll_policy_tenant_scope(cursor) -> None:
    """Rebuild policy tables so each organization owns its own payroll rules."""
    _migrate_payroll_settings(cursor)
    _migrate_payroll_rules(cursor)
    _migrate_payroll_scoped_rules(cursor)
    _migrate_leave_types(cursor)
    _migrate_company_holidays(cursor)
    _migrate_special_date_overrides(cursor)
    _copy_leave_types_to_existing_orgs(cursor)


def _migrate_payroll_settings(cursor) -> None:
    exists = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='payroll_settings'"
    ).fetchone()
    if not exists:
        return
    cols = _columns(cursor, "payroll_settings")
    sql = _create_sql(cursor, "payroll_settings")
    needs_rebuild = (
        "CHECK(id = 1)" in sql.replace(" ", "")
        or "organization_id" not in cols
        or ("organization_id", "branch_id") not in set(_unique_index_columns(cursor, "payroll_settings"))
        and "UNIQUE" not in sql.upper()
    )
    # Unique(org, branch) may only appear after rebuild; also rebuild if id=1 singleton.
    singleton = "id=1" in sql.replace(" ", "").lower() or "id = 1" in sql
    if singleton or "organization_id" not in cols:
        needs_rebuild = True
    if needs_rebuild:
        cursor.execute("ALTER TABLE payroll_settings RENAME TO payroll_settings_legacy_tenant")
        cursor.execute(
            """
            CREATE TABLE payroll_settings (
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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(organization_id, branch_id)
            )
            """
        )
        legacy_cols = _columns(cursor, "payroll_settings_legacy_tenant")
        select_org = (
            "COALESCE(NULLIF(organization_id, ''), 'DEFAULT_ORG')"
            if "organization_id" in legacy_cols
            else "'DEFAULT_ORG'"
        )
        select_branch = (
            "COALESCE(NULLIF(branch_id, ''), 'DEFAULT_BRANCH')"
            if "branch_id" in legacy_cols
            else "'DEFAULT_BRANCH'"
        )
        optional = []
        for name, default in (
            ("deduct_unpaid_leave", "0"),
            ("half_leave_quota_mode", "'category'"),
            ("half_leaves_per_quota_day", "2"),
            ("holiday_ot_premium_percent", "0"),
            ("non_working_day_ot_premium_percent", "0"),
            ("holiday_ot_premium_active", "0"),
            ("non_working_day_ot_premium_active", "0"),
            ("overtime_max_hours", "0"),
            ("overtime_max_period", "'day'"),
            ("ot_hour_divisor", "208"),
            ("proration_basis", "'calendar_days'"),
            ("currency_code", "'PKR'"),
            ("decimal_places", "2"),
            ("updated_at", "CURRENT_TIMESTAMP"),
        ):
            optional.append(name if name in legacy_cols else default)
        cursor.execute(
            f"""
            INSERT INTO payroll_settings (
                organization_id, branch_id, deduct_unpaid_leave, half_leave_quota_mode,
                half_leaves_per_quota_day, holiday_ot_premium_percent,
                non_working_day_ot_premium_percent, holiday_ot_premium_active,
                non_working_day_ot_premium_active, overtime_max_hours, overtime_max_period,
                ot_hour_divisor, proration_basis, currency_code, decimal_places, updated_at
            )
            SELECT {select_org}, {select_branch}, {", ".join(optional)}
            FROM payroll_settings_legacy_tenant
            """
        )
        cursor.execute("DROP TABLE payroll_settings_legacy_tenant")
    cols = _columns(cursor, "payroll_settings")
    for column, column_type in (
        ("ot_hour_divisor", "REAL NOT NULL DEFAULT 208"),
        ("proration_basis", "TEXT NOT NULL DEFAULT 'calendar_days'"),
        ("currency_code", "TEXT NOT NULL DEFAULT 'PKR'"),
        ("decimal_places", "INTEGER NOT NULL DEFAULT 2"),
        ("organization_id", "TEXT NOT NULL DEFAULT 'DEFAULT_ORG'"),
        ("branch_id", "TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH'"),
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
    ):
        if column not in cols:
            cursor.execute(f"ALTER TABLE payroll_settings ADD COLUMN {column} {column_type}")
    cursor.execute(
        """
        INSERT OR IGNORE INTO payroll_settings (organization_id, branch_id, deduct_unpaid_leave)
        VALUES ('DEFAULT_ORG', 'DEFAULT_BRANCH', 0)
        """
    )


def _migrate_payroll_rules(cursor) -> None:
    if not cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='payroll_rules'").fetchone():
        return
    cols = _columns(cursor, "payroll_rules")
    for column, column_type in (
        ("organization_id", "TEXT NOT NULL DEFAULT 'DEFAULT_ORG'"),
        ("branch_id", "TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH'"),
    ):
        if column not in cols:
            cursor.execute(f"ALTER TABLE payroll_rules ADD COLUMN {column} {column_type}")
    cursor.execute(
        "UPDATE payroll_rules SET organization_id = 'DEFAULT_ORG' WHERE organization_id IS NULL OR organization_id = ''"
    )
    cursor.execute(
        "UPDATE payroll_rules SET branch_id = 'DEFAULT_BRANCH' WHERE branch_id IS NULL OR branch_id = ''"
    )


def _migrate_payroll_scoped_rules(cursor) -> None:
    if not cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='payroll_scoped_rules'"
    ).fetchone():
        return
    cols = _columns(cursor, "payroll_scoped_rules")
    unique_sets = list(_unique_index_columns(cursor, "payroll_scoped_rules"))
    tenant_unique = ("policy_type", "scope_type", "scope_id", "day_type", "organization_id", "branch_id")
    if "organization_id" in cols and tenant_unique in unique_sets:
        return
    cursor.execute("ALTER TABLE payroll_scoped_rules RENAME TO payroll_scoped_rules_legacy_tenant")
    cursor.execute(
        """
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
        """
    )
    legacy = _columns(cursor, "payroll_scoped_rules_legacy_tenant")
    org_sql = "COALESCE(NULLIF(organization_id, ''), 'DEFAULT_ORG')" if "organization_id" in legacy else "'DEFAULT_ORG'"
    branch_sql = "COALESCE(NULLIF(branch_id, ''), 'DEFAULT_BRANCH')" if "branch_id" in legacy else "'DEFAULT_BRANCH'"
    extra = "overtime_max_hours" if "overtime_max_hours" in legacy else "NULL"
    extra_p = "overtime_max_period" if "overtime_max_period" in legacy else "NULL"
    cursor.execute(
        f"""
        INSERT INTO payroll_scoped_rules (
            id, organization_id, branch_id, policy_type, scope_type, scope_id, day_type,
            half_leave_quota_mode, half_leaves_per_quota_day, overtime_premium_percent,
            overtime_max_hours, overtime_max_period, active, created_at, updated_at
        )
        SELECT id, {org_sql}, {branch_sql}, policy_type, scope_type, scope_id, day_type,
               half_leave_quota_mode, half_leaves_per_quota_day, overtime_premium_percent,
               {extra}, {extra_p}, active, created_at, updated_at
        FROM payroll_scoped_rules_legacy_tenant
        """
    )
    cursor.execute("DROP TABLE payroll_scoped_rules_legacy_tenant")


def _migrate_leave_types(cursor) -> None:
    if not cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leave_types'").fetchone():
        return
    sql = _create_sql(cursor, "leave_types")
    cols = _columns(cursor, "leave_types")
    global_name_unique = "name TEXT NOT NULL UNIQUE" in sql or any(
        columns == ("name",) for columns in _unique_index_columns(cursor, "leave_types")
    )
    if "organization_id" in cols and not global_name_unique:
        return
    cursor.execute("ALTER TABLE leave_types RENAME TO leave_types_legacy_tenant")
    cursor.execute(
        """
        CREATE TABLE leave_types (
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
        """
    )
    legacy = _columns(cursor, "leave_types_legacy_tenant")
    org_sql = "COALESCE(NULLIF(organization_id, ''), 'DEFAULT_ORG')" if "organization_id" in legacy else "'DEFAULT_ORG'"
    branch_sql = "COALESCE(NULLIF(branch_id, ''), 'DEFAULT_BRANCH')" if "branch_id" in legacy else "'DEFAULT_BRANCH'"
    def col(name, default):
        return name if name in legacy else default
    cursor.execute(
        f"""
        INSERT INTO leave_types (
            id, name, code, yearly_allowance, is_paid, description, active,
            allowance_period, allowance_months, half_leave_enabled, half_leave_mode,
            half_leave_allowance_count, half_leave_allowance_months, half_leaves_per_quota_day,
            half_leave_quota_type_id, organization_id, branch_id, created_at
        )
        SELECT id, name, code, yearly_allowance, is_paid, description, {col('active', '1')},
               {col('allowance_period', "'yearly'")}, {col('allowance_months', '12')},
               {col('half_leave_enabled', '0')}, {col('half_leave_mode', "'separate'")},
               {col('half_leave_allowance_count', '0')}, {col('half_leave_allowance_months', '12')},
               {col('half_leaves_per_quota_day', '2')}, {col('half_leave_quota_type_id', 'NULL')},
               {org_sql}, {branch_sql}, {col('created_at', 'CURRENT_TIMESTAMP')}
        FROM leave_types_legacy_tenant
        """
    )
    cursor.execute("DROP TABLE leave_types_legacy_tenant")


def _copy_leave_types_to_existing_orgs(cursor) -> None:
    """Duplicate DEFAULT_ORG leave types for orgs that already have users but no catalog."""
    try:
        orgs = [
            row[0]
            for row in cursor.execute(
                """
                SELECT DISTINCT COALESCE(NULLIF(organization_id, ''), 'DEFAULT_ORG')
                FROM users
                """
            ).fetchall()
        ]
    except Exception:
        return
    for org in orgs:
        if not org or org == DEFAULT_ORG:
            continue
        count = cursor.execute(
            "SELECT COUNT(*) FROM leave_types WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?",
            (org,),
        ).fetchone()[0]
        if count:
            continue
        source = cursor.execute(
            "SELECT * FROM leave_types WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?",
            (DEFAULT_ORG,),
        ).fetchall()
        if not source:
            continue
        source_ids = []
        new_ids = []
        col_names = [item[0] for item in cursor.description]
        for row in source:
            mapping = dict(zip(col_names, row)) if not hasattr(row, "keys") else dict(row)
            old_id = mapping["id"]
            cursor.execute(
                """
                INSERT INTO leave_types (
                    name, code, yearly_allowance, is_paid, description, active,
                    allowance_period, allowance_months, half_leave_enabled, half_leave_mode,
                    half_leave_allowance_count, half_leave_allowance_months,
                    half_leaves_per_quota_day, half_leave_quota_type_id,
                    organization_id, branch_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mapping.get("name"),
                    mapping.get("code"),
                    mapping.get("yearly_allowance", 12),
                    mapping.get("is_paid", 1),
                    mapping.get("description"),
                    mapping.get("active", 1),
                    mapping.get("allowance_period", "yearly"),
                    mapping.get("allowance_months", 12),
                    mapping.get("half_leave_enabled", 0),
                    mapping.get("half_leave_mode", "separate"),
                    mapping.get("half_leave_allowance_count", 0),
                    mapping.get("half_leave_allowance_months", 12),
                    mapping.get("half_leaves_per_quota_day", 2),
                    mapping.get("half_leave_quota_type_id"),
                    org,
                    mapping.get("branch_id") or DEFAULT_BRANCH,
                ),
            )
            source_ids.append(old_id)
            new_ids.append(cursor.lastrowid)
        remap = dict(zip(source_ids, new_ids))
        for old_id, new_id in remap.items():
            cursor.execute(
                """
                UPDATE employee_leave_balances
                SET leave_type_id = ?
                WHERE leave_type_id = ?
                  AND user_id IN (SELECT id FROM users WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?)
                """,
                (new_id, old_id, org),
            )


def _migrate_company_holidays(cursor) -> None:
    if not cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_holidays'"
    ).fetchone():
        return
    sql = _create_sql(cursor, "company_holidays")
    unique_sets = list(_unique_index_columns(cursor, "company_holidays"))
    tenant_unique = ("organization_id", "branch_id", "holiday_date")
    date_only = any(columns == ("holiday_date",) for columns in unique_sets) or "holiday_date DATE NOT NULL UNIQUE" in sql
    if not date_only and tenant_unique in unique_sets:
        return
    cursor.execute("ALTER TABLE company_holidays RENAME TO company_holidays_legacy_tenant")
    cursor.execute(
        """
        CREATE TABLE company_holidays (
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
        """
    )
    legacy = _columns(cursor, "company_holidays_legacy_tenant")
    org_sql = "COALESCE(NULLIF(organization_id, ''), 'DEFAULT_ORG')" if "organization_id" in legacy else "'DEFAULT_ORG'"
    branch_sql = "COALESCE(NULLIF(branch_id, ''), 'DEFAULT_BRANCH')" if "branch_id" in legacy else "'DEFAULT_BRANCH'"
    end_sql = "end_date" if "end_date" in legacy else "holiday_date"
    premium_sql = "overtime_premium_percent" if "overtime_premium_percent" in legacy else "NULL"
    cursor.execute(
        f"""
        INSERT INTO company_holidays (
            id, name, holiday_date, end_date, description, is_recurring,
            overtime_premium_percent, organization_id, branch_id, created_at
        )
        SELECT id, name, holiday_date, {end_sql}, description, is_recurring,
               {premium_sql}, {org_sql}, {branch_sql}, created_at
        FROM company_holidays_legacy_tenant
        """
    )
    cursor.execute("DROP TABLE company_holidays_legacy_tenant")


def _migrate_special_date_overrides(cursor) -> None:
    if not cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='special_date_overrides'"
    ).fetchone():
        return
    unique_sets = list(_unique_index_columns(cursor, "special_date_overrides"))
    tenant_unique = ("calendar_date", "scope_type", "scope_id", "organization_id", "branch_id")
    cols = _columns(cursor, "special_date_overrides")
    if "organization_id" in cols and tenant_unique in unique_sets:
        return
    cursor.execute("ALTER TABLE special_date_overrides RENAME TO special_date_overrides_legacy_tenant")
    cursor.execute(
        """
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
        """
    )
    legacy = _columns(cursor, "special_date_overrides_legacy_tenant")
    org_sql = "COALESCE(NULLIF(organization_id, ''), 'DEFAULT_ORG')" if "organization_id" in legacy else "'DEFAULT_ORG'"
    branch_sql = "COALESCE(NULLIF(branch_id, ''), 'DEFAULT_BRANCH')" if "branch_id" in legacy else "'DEFAULT_BRANCH'"
    scope_t = "scope_type" if "scope_type" in legacy else "'all'"
    scope_i = "scope_id" if "scope_id" in legacy else "0"
    cursor.execute(
        f"""
        INSERT INTO special_date_overrides (
            id, calendar_date, override_type, note, scope_type, scope_id,
            organization_id, branch_id, created_at, updated_at
        )
        SELECT id, calendar_date, override_type, note, {scope_t}, {scope_i},
               {org_sql}, {branch_sql}, created_at, updated_at
        FROM special_date_overrides_legacy_tenant
        """
    )
    cursor.execute("DROP TABLE special_date_overrides_legacy_tenant")


def seed_leave_types_if_empty(cursor, organization_id: str, branch_id: str = DEFAULT_BRANCH) -> None:
    org, branch = tenant_ids(organization_id, branch_id)
    count = cursor.execute(
        "SELECT COUNT(*) FROM leave_types WHERE COALESCE(organization_id, 'DEFAULT_ORG') = ?",
        (org,),
    ).fetchone()[0]
    if count:
        return
    cursor.executemany(
        """
        INSERT INTO leave_types (name, code, yearly_allowance, is_paid, description, organization_id, branch_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [(*item, org, branch) for item in DEFAULT_LEAVE_TYPES],
    )


def ensure_payroll_settings_row(cursor, organization_id: Optional[str], branch_id: Optional[str]) -> None:
    org, branch = tenant_ids(organization_id, branch_id)
    cursor.execute(
        """
        INSERT OR IGNORE INTO payroll_settings (organization_id, branch_id, deduct_unpaid_leave)
        VALUES (?, ?, 0)
        """,
        (org, branch),
    )
