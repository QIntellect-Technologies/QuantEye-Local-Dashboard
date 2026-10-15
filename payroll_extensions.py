"""Optional payroll catalog, exceptions, revisions, audit, and documents.

All features default off except Excel bonuses (already live). Disabled flags
leave calculate_monthly_payroll math unchanged.
"""
from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from payroll_policy_schema import DEFAULT_CURRENCY, DEFAULT_DECIMAL_PLACES, money, tenant_ids

FEATURE_FLAGS = (
    "enable_named_components",
    "enable_tax",
    "enable_loans",
    "enable_advances",
    "enable_provident",
    "enable_insurance",
    "enable_bonuses",
    "enable_individual_adjustments",
    "enable_salary_revisions",
)

DEFAULT_FEATURE_FLAGS = {
    "enable_named_components": 0,
    "enable_tax": 0,
    "enable_loans": 0,
    "enable_advances": 0,
    "enable_provident": 0,
    "enable_insurance": 0,
    "enable_bonuses": 1,
    "enable_individual_adjustments": 0,
    "enable_salary_revisions": 0,
}

VALID_COMPONENT_KINDS = ("earning", "deduction")
VALID_COMPONENT_CATEGORIES = (
    "allowance", "bonus", "tax", "provident", "insurance", "loan", "advance", "other",
)
VALID_CALC_MODES = ("fixed", "percent_basic", "percent_gross", "per_working_day", "per_hour")
VALID_SCOPES = ("org", "branch", "shift", "department", "designation", "employee")
SCOPE_RANK = {
    "employee": 0,
    "designation": 1,
    "department": 2,
    "shift": 3,
    "branch": 4,
    "org": 5,
}
# percent_gross uses basic + non-percent-gross named earnings (no circular reference).
VALID_ADJ_TYPES = ("rest_day", "bonus", "earning", "deduction", "overtime", "arrears")
VALID_ADJ_MODES = ("multiplier", "fixed")

CATEGORY_FLAG = {
    "tax": "enable_tax",
    "loan": "enable_loans",
    "advance": "enable_advances",
    "provident": "enable_provident",
    "insurance": "enable_insurance",
    "bonus": "enable_bonuses",
}


def flag_on(settings: Any, name: str) -> bool:
    if not settings:
        return bool(DEFAULT_FEATURE_FLAGS.get(name))
    data = dict(settings) if not isinstance(settings, dict) and hasattr(settings, "keys") else (settings or {})
    if name not in data or data.get(name) is None:
        return bool(DEFAULT_FEATURE_FLAGS.get(name))
    return bool(int(data.get(name) or 0))


def ensure_payroll_extension_tables(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payroll_component_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
            branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
            name TEXT NOT NULL,
            code TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('earning', 'deduction')),
            category TEXT NOT NULL DEFAULT 'allowance',
            calc_mode TEXT NOT NULL DEFAULT 'fixed',
            default_amount REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(organization_id, branch_id, code)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payroll_employee_components (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
            branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
            user_id INTEGER NOT NULL,
            component_type_id INTEGER NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            percent REAL NOT NULL DEFAULT 0,
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS salary_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
            branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
            user_id INTEGER NOT NULL,
            effective_from TEXT NOT NULL,
            basic_salary REAL NOT NULL DEFAULT 0,
            allowances REAL NOT NULL DEFAULT 0,
            deductions REAL NOT NULL DEFAULT 0,
            ot_rate REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payroll_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
            branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
            user_id INTEGER NOT NULL,
            payroll_month TEXT NOT NULL,
            work_date TEXT,
            adj_type TEXT NOT NULL,
            pay_mode TEXT NOT NULL DEFAULT 'fixed',
            amount REAL NOT NULL DEFAULT 0,
            multiplier REAL NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            source_month TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payroll_duplicate_component_report (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
            branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
            user_id INTEGER,
            component_type_id INTEGER NOT NULL,
            assignment_ids TEXT NOT NULL DEFAULT '',
            overlap_from TEXT,
            overlap_to TEXT,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    _migrate_component_assignment_columns(cursor)
    _refresh_duplicate_component_report(cursor)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payroll_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id TEXT NOT NULL DEFAULT 'DEFAULT_ORG',
            branch_id TEXT NOT NULL DEFAULT 'DEFAULT_BRANCH',
            payroll_month TEXT,
            action TEXT NOT NULL,
            actor TEXT,
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _columns(cursor, table: str) -> set:
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_component_assignment_columns(cursor) -> None:
    """Idempotent. Rollback: DROP the added columns is not possible in SQLite; restore a backup."""
    type_cols = _columns(cursor, "payroll_component_types")
    for column, spec in (
        ("min_amount", "REAL"),
        ("max_amount", "REAL"),
        ("rounding", "TEXT NOT NULL DEFAULT 'cent'"),
    ):
        if column not in type_cols:
            cursor.execute(f"ALTER TABLE payroll_component_types ADD COLUMN {column} {spec}")
    asg_cols = _columns(cursor, "payroll_employee_components")
    for column, spec in (
        ("scope_type", "TEXT NOT NULL DEFAULT 'employee'"),
        ("scope_id", "TEXT"),
        ("excluded", "INTEGER NOT NULL DEFAULT 0"),
        ("min_amount", "REAL"),
        ("max_amount", "REAL"),
        ("rounding", "TEXT NOT NULL DEFAULT 'cent'"),
        ("calc_mode", "TEXT"),
    ):
        if column not in asg_cols:
            cursor.execute(f"ALTER TABLE payroll_employee_components ADD COLUMN {column} {spec}")
    cursor.execute(
        "UPDATE payroll_employee_components SET scope_type = 'employee' WHERE scope_type IS NULL OR scope_type = ''"
    )


def _dates_overlap(a_from: Optional[str], a_to: Optional[str], b_from: Optional[str], b_to: Optional[str]) -> bool:
    start_a = a_from or "0000-01-01"
    start_b = b_from or "0000-01-01"
    end_a = a_to or "9999-12-31"
    end_b = b_to or "9999-12-31"
    return start_a <= end_b and start_b <= end_a


def _refresh_duplicate_component_report(cursor) -> None:
    if not cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='payroll_employee_components'"
    ).fetchone():
        return
    cursor.execute("DELETE FROM payroll_duplicate_component_report")
    rows = [dict(r) for r in cursor.execute("SELECT * FROM payroll_employee_components WHERE active = 1").fetchall()]
    seen = set()
    for i, left in enumerate(rows):
        for right in rows[i + 1 :]:
            if int(left.get("component_type_id") or 0) != int(right.get("component_type_id") or 0):
                continue
            if str(left.get("organization_id")) != str(right.get("organization_id")):
                continue
            if str(left.get("branch_id")) != str(right.get("branch_id")):
                continue
            if str(left.get("scope_type") or "employee") != str(right.get("scope_type") or "employee"):
                continue
            if int(left.get("user_id") or 0) != int(right.get("user_id") or 0):
                continue
            if str(left.get("scope_id") or "") != str(right.get("scope_id") or ""):
                continue
            if int(left.get("excluded") or 0) or int(right.get("excluded") or 0):
                continue
            if not _dates_overlap(left.get("effective_from"), left.get("effective_to"), right.get("effective_from"), right.get("effective_to")):
                continue
            key = tuple(sorted((left["id"], right["id"])))
            if key in seen:
                continue
            seen.add(key)
            cursor.execute(
                """INSERT INTO payroll_duplicate_component_report (
                       organization_id, branch_id, user_id, component_type_id, assignment_ids, overlap_from, overlap_to
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    left.get("organization_id"),
                    left.get("branch_id"),
                    left.get("user_id"),
                    left.get("component_type_id"),
                    f"{left['id']},{right['id']}",
                    max(left.get("effective_from") or "", right.get("effective_from") or ""),
                    min(left.get("effective_to") or "9999-12-31", right.get("effective_to") or "9999-12-31"),
                ),
            )


def assignment_matches_employee(row: Dict[str, Any], employee: Dict[str, Any], org: str, branch: str) -> bool:
    scope = str(row.get("scope_type") or "employee").strip().lower()
    sid = str(row.get("scope_id") or "").strip().lower()
    if scope == "employee":
        return int(row.get("user_id") or 0) == int(employee.get("employee_id") or employee.get("user_id") or 0)
    if scope == "designation":
        return sid == str(employee.get("designation") or "").strip().lower()
    if scope == "department":
        return sid == str(employee.get("department") or "").strip().lower()
    if scope == "shift":
        return str(row.get("scope_id") or "") == str(employee.get("shift_id") or "")
    if scope == "branch":
        return sid == str(branch).strip().lower() or str(row.get("scope_id") or "") == str(branch)
    if scope == "org":
        return True
    return False


def resolve_named_assignments(
    all_assignments: Sequence[Dict[str, Any]],
    employee: Dict[str, Any],
    org: str,
    branch: str,
    as_of: date,
) -> List[Dict[str, Any]]:
    as_of_s = as_of.isoformat()
    excluded_types = set()
    candidates: List[Dict[str, Any]] = []
    for row in all_assignments:
        if not row.get("active", 1):
            continue
        start = str(row.get("effective_from") or "0000-01-01")[:10]
        end = str(row.get("effective_to") or "9999-12-31")[:10]
        if as_of_s < start or as_of_s > end:
            continue
        if not assignment_matches_employee(row, employee, org, branch):
            continue
        if int(row.get("excluded") or 0):
            excluded_types.add(int(row.get("component_type_id") or 0))
            continue
        candidates.append(dict(row))
    by_type: Dict[int, Dict[str, Any]] = {}
    for row in candidates:
        type_id = int(row.get("component_type_id") or 0)
        if type_id in excluded_types and str(row.get("scope_type") or "employee") != "employee":
            continue
        if type_id in excluded_types and str(row.get("scope_type") or "employee") == "employee":
            continue
        rank = SCOPE_RANK.get(str(row.get("scope_type") or "employee"), 9)
        current = by_type.get(type_id)
        if current is None or rank < SCOPE_RANK.get(str(current.get("scope_type") or "employee"), 9):
            by_type[type_id] = row
    return list(by_type.values())


def write_audit(cursor, action: str, organization_id: Optional[str], branch_id: Optional[str],
                payroll_month: Optional[str] = None, actor: Optional[str] = None, detail: Optional[Dict[str, Any]] = None) -> None:
    org, branch = tenant_ids(organization_id, branch_id)
    cursor.execute(
        """INSERT INTO payroll_audit_log (
               organization_id, branch_id, payroll_month, action, actor, detail
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (org, branch, payroll_month, action, actor or "Admin", json.dumps(detail or {})),
    )


def _as_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def package_as_of(
    revisions: Sequence[Dict[str, Any]],
    fallback: Dict[str, Any],
    as_of: date,
) -> Dict[str, float]:
    chosen = None
    for row in revisions:
        start = _as_date(row.get("effective_from"))
        if start and start <= as_of:
            if chosen is None or start >= (_as_date(chosen.get("effective_from")) or date.min):
                chosen = row
    src = chosen or fallback
    return {
        "basic_salary": float(src.get("basic_salary") or 0),
        "allowances": float(src.get("allowances") or 0),
        "deductions": float(src.get("deductions") or 0),
        "ot_rate": float(src.get("ot_rate") or 0),
    }


def prorate_packages(
    revisions: Sequence[Dict[str, Any]],
    fallback: Dict[str, Any],
    eligible_start: date,
    schedule_end: date,
    month_start: date,
    month_end: date,
    decimal_places: int,
    enabled: bool,
) -> Tuple[float, float, float, float]:
    """Return (basic, allowances, deductions, ot_rate) for the month slice."""
    if not enabled or not revisions:
        days_in_month = (month_end - month_start).days + 1
        active = max(0, (schedule_end - eligible_start).days + 1)
        factor = 1.0 if active >= days_in_month else (active / days_in_month if days_in_month else 1.0)
        return (
            money(float(fallback.get("basic_salary") or 0) * factor, decimal_places),
            money(float(fallback.get("allowances") or 0) * factor, decimal_places),
            money(float(fallback.get("deductions") or 0) * factor, decimal_places),
            money(float(fallback.get("ot_rate") or 0), decimal_places),
        )
    days_in_month = (month_end - month_start).days + 1 or 1
    basic = allow = deduct = 0.0
    current = eligible_start
    last_ot = float(fallback.get("ot_rate") or 0)
    while current <= schedule_end:
        pkg = package_as_of(revisions, fallback, current)
        basic += float(pkg["basic_salary"]) / days_in_month
        allow += float(pkg["allowances"]) / days_in_month
        deduct += float(pkg["deductions"]) / days_in_month
        last_ot = float(pkg["ot_rate"] or last_ot)
        current += timedelta(days=1)
    return money(basic, decimal_places), money(allow, decimal_places), money(deduct, decimal_places), money(last_ot, decimal_places)


def category_allowed(category: str, settings: Any) -> bool:
    flag = CATEGORY_FLAG.get(str(category or "").strip().lower())
    if not flag:
        return True
    return flag_on(settings, flag)


def named_component_totals(
    assignments: Sequence[Dict[str, Any]],
    types_by_id: Dict[int, Dict[str, Any]],
    basic_salary: float,
    as_of: date,
    settings: Any,
    decimal_places: int,
    employee: Optional[Dict[str, Any]] = None,
    organization_id: str = "DEFAULT_ORG",
    branch_id: str = "DEFAULT_BRANCH",
    working_days: float = 0.0,
    overtime_hours: float = 0.0,
) -> Tuple[float, float, List[Dict[str, Any]]]:
    resolved = list(assignments)
    if employee is not None:
        resolved = resolve_named_assignments(assignments, employee, organization_id, branch_id, as_of)
    prepared: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    for row in resolved:
        spec = types_by_id.get(int(row.get("component_type_id") or 0))
        if not spec or not spec.get("active", 1):
            continue
        if not category_allowed(spec.get("category") or "", settings):
            continue
        mode = str(row.get("calc_mode") or spec.get("calc_mode") or "fixed").strip().lower()
        prepared.append((row, spec, mode))

    def _amount_for(row: Dict[str, Any], spec: Dict[str, Any], mode: str, gross_base: float) -> float:
        if mode == "percent_basic":
            amount = basic_salary * float(row.get("percent") or spec.get("default_amount") or 0) / 100.0
        elif mode == "percent_gross":
            amount = gross_base * float(row.get("percent") or spec.get("default_amount") or 0) / 100.0
        elif mode == "per_working_day":
            amount = float(row.get("amount") or spec.get("default_amount") or 0) * float(working_days or 0)
        elif mode == "per_hour":
            amount = float(row.get("amount") or spec.get("default_amount") or 0) * float(overtime_hours or 0)
        else:
            amount = float(row.get("amount") if row.get("amount") not in (None, "") else spec.get("default_amount") or 0)
        lo = row.get("min_amount") if row.get("min_amount") not in (None, "") else spec.get("min_amount")
        hi = row.get("max_amount") if row.get("max_amount") not in (None, "") else spec.get("max_amount")
        if lo not in (None, ""):
            amount = max(amount, float(lo))
        if hi not in (None, "") and float(hi) > 0:
            amount = min(amount, float(hi))
        return money(max(0.0, amount), decimal_places)

    first_pass_earn = 0.0
    for row, spec, mode in prepared:
        if spec.get("kind") == "earning" and mode != "percent_gross":
            first_pass_earn += _amount_for(row, spec, mode, basic_salary)
    gross_base = basic_salary + first_pass_earn
    earnings = 0.0
    deductions = 0.0
    lines: List[Dict[str, Any]] = []
    for row, spec, mode in prepared:
        amount = _amount_for(row, spec, mode, gross_base)
        kind = spec.get("kind") or "earning"
        lines.append({
            "name": spec.get("name"),
            "code": spec.get("code"),
            "kind": kind,
            "category": spec.get("category"),
            "amount": amount,
            "scope_type": row.get("scope_type") or "employee",
        })
        if kind == "deduction":
            deductions += amount
        else:
            earnings += amount
    return money(earnings, decimal_places), money(deductions, decimal_places), lines


def apply_adjustments(
    adjustments: Sequence[Dict[str, Any]],
    daily_rate: float,
    off_day_dates: Sequence[str],
    decimal_places: int,
) -> Dict[str, Any]:
    extra_earn = 0.0
    extra_deduct = 0.0
    arrears = 0.0
    covered_off_days = set()
    lines: List[Dict[str, Any]] = []
    off_set = {str(d) for d in off_day_dates}
    for adj in adjustments:
        adj_type = str(adj.get("adj_type") or "").strip().lower()
        mode = str(adj.get("pay_mode") or "fixed").strip().lower()
        work_date = str(adj.get("work_date") or "").strip()
        if mode == "multiplier":
            amount = money(daily_rate * float(adj.get("multiplier") or 0), decimal_places)
        else:
            amount = money(float(adj.get("amount") or 0), decimal_places)
        if adj_type == "rest_day":
            covered_off_days.add(work_date or "")
            extra_earn += amount
        elif adj_type in ("bonus", "earning", "overtime"):
            extra_earn += amount
        elif adj_type == "deduction":
            extra_deduct += amount
        elif adj_type == "arrears":
            arrears += amount
            if amount >= 0:
                extra_earn += amount
            else:
                extra_deduct += abs(amount)
        lines.append({
            "type": adj_type,
            "date": work_date,
            "amount": amount,
            "note": adj.get("note") or "",
            "source_month": adj.get("source_month"),
        })
    remaining_off = [d for d in off_set if d not in covered_off_days]
    return {
        "extra_earn": money(extra_earn, decimal_places),
        "extra_deduct": money(extra_deduct, decimal_places),
        "arrears": money(arrears, decimal_places),
        "remaining_off_days": remaining_off,
        "lines": lines,
    }


def simple_pdf_bytes(title: str, lines: Sequence[str]) -> bytes:
    """Minimal one-page PDF without extra packages."""
    content_lines = ["BT /F1 14 Tf 50 780 Td 18 TL ({}) Tj T*".format(_pdf_escape(title))]
    content_lines.append("/F1 10 Tf 14 TL")
    for line in lines:
        content_lines.append("({}) Tj T*".format(_pdf_escape(line)))
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("latin-1", "replace")
    objs = []
    objs.append(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    objs.append(b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n")
    objs.append(
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
    )
    objs.append(b"4 0 obj << /Length %d >> stream\n" % len(stream) + stream + b"\nendstream endobj\n")
    objs.append(b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    parts = [b"%PDF-1.4\n"]
    offsets = [0]
    for obj in objs:
        offsets.append(sum(len(p) for p in parts))
        parts.append(obj)
    xref_pos = sum(len(p) for p in parts)
    xref = [b"xref\n0 6\n0000000000 65535 f \n"]
    for off in offsets[1:]:
        xref.append("{:010d} 00000 n \n".format(off).encode("ascii"))
    parts.extend(xref)
    parts.append(b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref_pos)
    return b"".join(parts)


def _pdf_escape(text: str) -> str:
    return str(text or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")[:120]


def payroll_excel_bytes(payroll: Dict[str, Any], currency: str = DEFAULT_CURRENCY) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payroll"
    headers = [
        "Employee ID", "Name", "Department", "Basic", "Allowances", "Overtime",
        "Bonus", "Rest-day extra", "Adjustments", "Arrears", "Gross", "Deductions", "Net", "Status",
    ]
    ws.append(["Payroll", payroll.get("payroll_month"), payroll.get("status"), currency])
    ws.append(headers)
    for cell in ws[2]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="0F2942", end_color="0F2942", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")
    for row in payroll.get("records") or []:
        ws.append([
            row.get("employee_id"),
            row.get("employee_name"),
            row.get("department"),
            float(row.get("basic_salary") or 0),
            float(row.get("allowances") or 0),
            float(row.get("overtime_amount") or 0),
            float(row.get("bonus_amount") or 0),
            float(row.get("off_day_extra_pay") or 0),
            float(row.get("adjustment_amount") or 0),
            float(row.get("arrears_amount") or 0),
            float(row.get("gross_salary") or 0),
            float(row.get("deduction_amount") or 0),
            float(row.get("net_salary") or 0),
            row.get("status"),
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def payslip_pdf_bytes(payslip: Dict[str, Any]) -> bytes:
    emp = payslip.get("employee") or {}
    currency = payslip.get("currency_code") or DEFAULT_CURRENCY
    lines = [
        "Month: {}".format(payslip.get("payroll_month")),
        "Period: {} to {}".format(payslip.get("period_start"), payslip.get("period_end")),
        "Status: {}".format(payslip.get("status")),
        "Employee: {}  #{}".format(emp.get("employee_name"), emp.get("employee_id")),
        "Department: {}".format(emp.get("department") or ""),
        "",
        "Basic: {} {}".format(currency, emp.get("basic_salary")),
        "Allowances: {} {}".format(currency, emp.get("allowances")),
        "Overtime: {} {}".format(currency, emp.get("overtime_amount")),
        "Bonus: {} {}".format(currency, emp.get("bonus_amount")),
        "Rest-day extra: {} {}".format(currency, emp.get("off_day_extra_pay")),
        "Adjustments: {} {}".format(currency, emp.get("adjustment_amount")),
        "Arrears: {} {}".format(currency, emp.get("arrears_amount")),
        "Gross: {} {}".format(currency, payslip.get("gross_salary")),
        "Named deductions: {} {}".format(currency, (payslip.get("deductions") or {}).get("other")),
        "Total deductions: {} {}".format(currency, (payslip.get("deductions") or {}).get("total") or (payslip.get("deductions") or {}).get("other")),
        "Absence: {} {}".format(currency, (payslip.get("deductions") or {}).get("absence")),
        "Unpaid leave: {} {}".format(currency, (payslip.get("deductions") or {}).get("unpaid_leave")),
        "Net payable: {} {}".format(currency, payslip.get("net_salary")),
    ]
    pay_lines = emp.get("pay_lines") or []
    if isinstance(pay_lines, str):
        try:
            pay_lines = json.loads(pay_lines)
        except (TypeError, ValueError):
            pay_lines = []
    for item in pay_lines:
        lines.append("- {} ({}): {} {}".format(item.get("name") or item.get("type"), item.get("kind") or item.get("category"), currency, item.get("amount")))
    return simple_pdf_bytes("Payslip", lines)
