"""
Cloud source of truth for Local Dashboard.

When DATABASE_URL is set, Postgres holds staff, cameras, attendance, shifts,
leave, overtime, and payroll. SQLite remains a working cache so frozen
log_attendance / InsightFace stay unchanged. Face embeddings never leave SQLite/EXE.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from logger_config import get_logger

logger = get_logger("quanteye.postgres_mirror")

_LOCAL_PG_HOSTS = {"127.0.0.1", "localhost", "::1"}
_PLACEHOLDER_ORGS = {"", "DEFAULT_ORG", "DEFAULT_BRANCH"}


def _production_runtime() -> bool:
    return bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("APP_ENV", "").lower() == "production"
        or os.environ.get("FLASK_ENV", "").lower() == "production"
    )


def postgres_is_configured() -> bool:
    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or "").strip()
    if db_url:
        try:
            host = (urlparse(db_url).hostname or "").strip().lower()
        except Exception:
            host = ""
        if _production_runtime() and host in _LOCAL_PG_HOSTS:
            return False
        return True
    host = (os.environ.get("PGHOST") or "").strip()
    if not host:
        return False
    if _production_runtime() and host.lower() in _LOCAL_PG_HOSTS:
        return False
    return True


def _connect():
    if not postgres_is_configured():
        return None
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError:
        logger.warning("psycopg2 is not installed; cloud Postgres mirror is skipped.")
        return None
    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or "").strip()
    timeout = int(os.environ.get("PGCONNECT_TIMEOUT", "5") or "5")
    try:
        if db_url:
            conn = psycopg2.connect(db_url, connect_timeout=timeout)
        else:
            conn = psycopg2.connect(
                host=os.environ.get("PGHOST"),
                port=os.environ.get("PGPORT", "5432"),
                dbname=os.environ.get("PGDATABASE", "quanteye_cloud"),
                user=os.environ.get("PGUSER", "postgres"),
                password=os.environ.get("PGPASSWORD", ""),
                connect_timeout=timeout,
            )
        conn.autocommit = True
        conn.cursor_factory = RealDictCursor
        return conn
    except Exception as error:
        logger.warning("Could not connect to cloud Postgres: %s", error)
        return None


def _real_tenant(org_id: Optional[str], branch_id: Optional[str] = None) -> bool:
    org = str(org_id or "").strip()
    if org in _PLACEHOLDER_ORGS:
        return False
    if branch_id is not None and str(branch_id).strip() in _PLACEHOLDER_ORGS:
        return False
    return True


def _ensure_tenant(cur, org_id: str, branch_id: str, org_name: str = "") -> None:
    cur.execute(
        """
        INSERT INTO organizations (id, name, status)
        VALUES (%s, %s, 'Active')
        ON CONFLICT (id) DO NOTHING
        """,
        (org_id, org_name or org_id),
    )
    cur.execute(
        """
        INSERT INTO branches (id, organization_id, name, access)
        VALUES (%s, %s, %s, 'Active')
        ON CONFLICT (organization_id, id) DO NOTHING
        """,
        (branch_id, org_id, "Main Branch"),
    )


def _with_cursor(work):
    if not postgres_is_configured():
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            work(cur)
        return True
    except Exception as error:
        logger.warning("Cloud Postgres mirror write failed: %s", error)
        return False
    finally:
        conn.close()


def _with_read_cursor(work):
    if not postgres_is_configured():
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            return work(cur)
    except Exception as error:
        logger.warning("Cloud Postgres read failed: %s", error)
        return None
    finally:
        conn.close()


def mirror_employee(user: Dict[str, Any]) -> None:
    org_id = str(user.get("organization_id") or "").strip()
    branch_id = str(user.get("branch_id") or "BR-01").strip() or "BR-01"
    if not _real_tenant(org_id, branch_id):
        return
    user_id = str(user.get("id") or "")
    if not user_id:
        return
    name = str(user.get("name") or "Employee")
    code = str(user.get("employee_code") or user_id)
    shift_val = user.get("shift_id")
    shift_id = str(shift_val).strip() if shift_val is not None and str(shift_val).strip() else None
    person_type = str(user.get("person_type") or "staff").strip()
    class_id = user.get("class_id")
    roll_number = str(user.get("roll_number") or "").strip() or None
    guardian_name = str(user.get("guardian_name") or "").strip() or None
    guardian_phone = str(user.get("guardian_phone") or "").strip() or None

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id, str(user.get("organization_name") or ""))
        cur.execute(
            """
            INSERT INTO employees (
                id, organization_id, branch_id, employee_code, name, email, phone, is_active, shift_id,
                person_type, class_id, roll_number, guardian_name, guardian_phone
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                name = EXCLUDED.name,
                email = EXCLUDED.email,
                phone = EXCLUDED.phone,
                is_active = EXCLUDED.is_active,
                shift_id = COALESCE(EXCLUDED.shift_id, employees.shift_id),
                person_type = EXCLUDED.person_type,
                class_id = EXCLUDED.class_id,
                roll_number = EXCLUDED.roll_number,
                guardian_name = EXCLUDED.guardian_name,
                guardian_phone = EXCLUDED.guardian_phone,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                org_id,
                branch_id,
                code,
                name,
                user.get("email") or "",
                user.get("phone") or "",
                bool(int(user["active"]) if user.get("active") is not None else True),
                shift_id,
                person_type,
                class_id,
                roll_number,
                guardian_name,
                guardian_phone,
            ),
        )

    return _with_cursor(_write)


def delete_employee(user_id: int, organization_id: Optional[str] = None) -> None:
    def _write(cur):
        if organization_id and _real_tenant(organization_id):
            cur.execute(
                "DELETE FROM employees WHERE organization_id = %s AND id = %s",
                (organization_id, str(user_id)),
            )
        else:
            cur.execute("DELETE FROM employees WHERE id = %s", (str(user_id),))

    _with_cursor(_write)


def mirror_shift(shift: Dict[str, Any]) -> None:
    org_id = str(shift.get("organization_id") or "").strip()
    branch_id = str(shift.get("branch_id") or "BR-01").strip() or "BR-01"
    if not _real_tenant(org_id, branch_id):
        return
    shift_id = str(shift.get("id") or "").strip()
    if not shift_id:
        return
    name = str(shift.get("name") or "Shift").strip()
    start_time = str(shift.get("start_time") or "09:00").strip()[:8]
    end_time = str(shift.get("end_time") or "17:00").strip()[:8]
    try:
        grace = int(shift.get("grace_period_minutes") or shift.get("grace_minutes") or 15)
    except (ValueError, TypeError):
        grace = 15

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id)
        cur.execute(
            """
            INSERT INTO shifts (
                id, organization_id, branch_id, name, start_time, end_time, grace_minutes,
                working_days, checkout_grace_period_minutes, is_default, active
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                name = EXCLUDED.name,
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                grace_minutes = EXCLUDED.grace_minutes,
                working_days = EXCLUDED.working_days,
                checkout_grace_period_minutes = EXCLUDED.checkout_grace_period_minutes,
                is_default = EXCLUDED.is_default,
                active = EXCLUDED.active
            """,
            (
                shift_id,
                org_id,
                branch_id,
                name,
                start_time,
                end_time,
                grace,
                str(shift.get("working_days") or '[0,1,2,3,4,5]'),
                int(shift.get("checkout_grace_period_minutes") or 0),
                int(shift.get("is_default") or 0),
                int(shift.get("active") if shift.get("active") is not None else 1),
            ),
        )

    return _with_cursor(_write)


def delete_shift(shift_id: int, organization_id: Optional[str] = None, fallback_shift_id: Optional[int] = None) -> None:
    def _write(cur):
        if fallback_shift_id is not None:
            if organization_id and _real_tenant(organization_id):
                cur.execute(
                    "UPDATE employees SET shift_id = %s WHERE organization_id = %s AND shift_id = %s",
                    (str(fallback_shift_id), organization_id, str(shift_id)),
                )
            else:
                cur.execute(
                    "UPDATE employees SET shift_id = %s WHERE shift_id = %s",
                    (str(fallback_shift_id), str(shift_id)),
                )
        if organization_id and _real_tenant(organization_id):
            cur.execute(
                "DELETE FROM shifts WHERE organization_id = %s AND id = %s",
                (organization_id, str(shift_id)),
            )
        else:
            cur.execute("DELETE FROM shifts WHERE id = %s", (str(shift_id),))

    _with_cursor(_write)


def update_employees_shift_pg(user_ids: List[int], shift_id: int, organization_id: Optional[str] = None) -> None:
    if not user_ids:
        return
    def _write(cur):
        str_uids = [str(uid) for uid in user_ids]
        if organization_id and _real_tenant(organization_id):
            cur.execute(
                """
                UPDATE employees
                   SET shift_id = %s, updated_at = CURRENT_TIMESTAMP
                 WHERE (id = ANY(%s) OR employee_code = ANY(%s))
                   AND organization_id = %s
                """,
                (str(shift_id), str_uids, str_uids, organization_id),
            )
        else:
            cur.execute(
                """
                UPDATE employees
                   SET shift_id = %s, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ANY(%s) OR employee_code = ANY(%s)
                """,
                (str(shift_id), str_uids, str_uids),
            )
    _with_cursor(_write)


def delete_pg_attendance_events(user_id: int, date_str: str, organization_id: Optional[str] = None) -> None:
    if not user_id or not date_str:
        return
    def _write(cur):
        str_uid = str(user_id).strip()
        d_str = str(date_str)[:10]
        if organization_id and _real_tenant(organization_id):
            cur.execute(
                """
                DELETE FROM attendance_events
                 WHERE organization_id = %s
                   AND (employee_id = %s OR employee_id = %s)
                   AND date(timestamp) = %s
                """,
                (organization_id, str_uid, f"EMP-{str_uid}", d_str),
            )
        else:
            cur.execute(
                """
                DELETE FROM attendance_events
                 WHERE (employee_id = %s OR employee_id = %s)
                   AND date(timestamp) = %s
                """,
                (str_uid, f"EMP-{str_uid}", d_str),
            )
    _with_cursor(_write)


def delete_pg_manual_absent(user_id: int, date_str: str, organization_id: Optional[str] = None) -> None:
    if not user_id or not date_str:
        return
    def _write(cur):
        str_uid = str(user_id).strip()
        d_str = str(date_str)[:10]
        cur.execute(
            """
            DELETE FROM manual_absent
             WHERE (user_id = %s OR user_id = %s)
               AND date = %s
            """,
            (str_uid, f"EMP-{str_uid}", d_str),
        )
    _with_cursor(_write)


def mirror_manual_attendance(user_id: int, date_str: str, check_in_time: str, check_out_time: Optional[str] = None, shift_id: int = 1, notes: str = "", marked_by: str = "Admin", organization_id: str = "DEFAULT_ORG", branch_id: str = "DEFAULT_BRANCH") -> None:
    if not user_id or not date_str or not check_in_time:
        return
    def _write(cur):
        str_uid = str(user_id).strip()
        d_str = str(date_str)[:10]
        ts_in = f"{d_str} {check_in_time}:00"
        cur.execute(
            """
            INSERT INTO attendance_events (organization_id, branch_id, employee_id, event_type, timestamp, confidence, source)
            VALUES (%s, %s, %s, 'CHECK_IN', %s, 1.0, 'manual')
            """,
            (organization_id, branch_id, str_uid, ts_in),
        )
        if check_out_time:
            ts_out = f"{d_str} {check_out_time}:00"
            cur.execute(
                """
                INSERT INTO attendance_events (organization_id, branch_id, employee_id, event_type, timestamp, confidence, source)
                VALUES (%s, %s, %s, 'CHECK_OUT', %s, 1.0, 'manual')
                """,
                (organization_id, branch_id, str_uid, ts_out),
            )
    _with_cursor(_write)


def mirror_camera(camera: Dict[str, Any]) -> None:
    org_id = str(camera.get("organization_id") or "").strip()
    branch_id = str(camera.get("branch_id") or "BR-01").strip() or "BR-01"
    if not _real_tenant(org_id, branch_id):
        return
    camera_id = str(camera.get("camera_id") or camera.get("id") or "").strip()
    rtsp_url = str(camera.get("rtsp_url") or "").strip() or "rtsp://unconfigured"
    if not camera_id:
        return
    camera_scope = str(camera.get("camera_scope") or "gate").strip()
    assigned_class_id = camera.get("assigned_class_id")

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id)
        cur.execute(
            """
            INSERT INTO cameras (
                id, organization_id, branch_id, camera_name, camera_type, rtsp_url,
                location, room_name, is_active, camera_scope, assigned_class_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                camera_name = EXCLUDED.camera_name,
                camera_type = EXCLUDED.camera_type,
                rtsp_url = EXCLUDED.rtsp_url,
                location = EXCLUDED.location,
                room_name = EXCLUDED.room_name,
                is_active = EXCLUDED.is_active,
                camera_scope = EXCLUDED.camera_scope,
                assigned_class_id = EXCLUDED.assigned_class_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                camera_id,
                org_id,
                branch_id,
                camera.get("name") or camera_id,
                camera.get("camera_type") or "rtsp",
                rtsp_url,
                camera.get("room_name") or "",
                camera.get("room_name") or "",
                bool(int(camera["active"]) if camera.get("active") is not None else True),
                camera_scope,
                assigned_class_id,
            ),
        )

    return _with_cursor(_write)


def delete_camera(camera_id: str, organization_id: Optional[str] = None) -> None:
    def _write(cur):
        if organization_id and _real_tenant(organization_id):
            cur.execute(
                "DELETE FROM cameras WHERE organization_id = %s AND id = %s",
                (organization_id, str(camera_id)),
            )
        else:
            cur.execute("DELETE FROM cameras WHERE id = %s", (str(camera_id),))

    _with_cursor(_write)


def _map_event_type(raw: str) -> str:
    value = str(raw or "CHECK_IN").upper().replace(" ", "_")
    mapping = {
        "CHECKIN": "CHECK_IN",
        "CHECK_IN": "CHECK_IN",
        "CHECKOUT": "CHECK_OUT",
        "CHECK_OUT": "CHECK_OUT",
        "BREAKIN": "BREAK_IN",
        "BREAK_IN": "BREAK_IN",
        "BREAKOUT": "BREAK_OUT",
        "BREAK_OUT": "BREAK_OUT",
    }
    return mapping.get(value, "RECOGNITION")


def mirror_attendance_event(event: Dict[str, Any]) -> None:
    org_id = str(event.get("organization_id") or "").strip()
    branch_id = str(event.get("branch_id") or "BR-01").strip() or "BR-01"
    if not _real_tenant(org_id, branch_id):
        return
    event_id = str(event.get("event_id") or event.get("id") or "").strip()
    employee_id = str(event.get("user_id") or event.get("employee_id") or "").strip()
    if not event_id or not employee_id:
        return
    stamp = event.get("timestamp") or datetime.now(timezone.utc).isoformat()

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id)
        cur.execute(
            """
            INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (organization_id, id) DO NOTHING
            """,
            (employee_id, org_id, branch_id, employee_id, event.get("name") or "Employee"),
        )
        cur.execute(
            """
            INSERT INTO attendance_events (
                id, organization_id, branch_id, employee_id, camera_id, timestamp,
                event_type, confidence, sync_status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'synced')
            ON CONFLICT (organization_id, id) DO NOTHING
            """,
            (
                event_id,
                org_id,
                branch_id,
                employee_id,
                event.get("camera_id") or "portal",
                stamp,
                _map_event_type(event.get("event_type")),
                float(event.get("confidence") or 1.0),
            ),
        )

    return _with_cursor(_write)


def _map_leave_type(raw: str) -> str:
    value = str(raw or "casual").strip().lower()
    if value in {"annual", "sick", "casual", "unpaid"}:
        return value
    if "sick" in value:
        return "sick"
    if "annual" in value or "vacation" in value:
        return "annual"
    if "unpaid" in value:
        return "unpaid"
    return "casual"


def mirror_leave(row: Dict[str, Any]) -> None:
    org_id = str(row.get("organization_id") or "").strip()
    branch_id = str(row.get("branch_id") or "BR-01").strip() or "BR-01"
    if not _real_tenant(org_id, branch_id):
        return
    leave_id = str(row.get("id") or "").strip()
    employee_id = str(row.get("user_id") or row.get("employee_id") or "").strip()
    if not leave_id or not employee_id:
        return
    status = str(row.get("status") or "pending").lower()
    if status not in {"pending", "approved", "rejected", "cancelled"}:
        status = "pending"

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id)
        cur.execute(
            """
            INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (organization_id, id) DO NOTHING
            """,
            (employee_id, org_id, branch_id, employee_id, str(row.get("user_name") or f"Employee {employee_id}")),
        )
        cur.execute(
            """
            INSERT INTO leave_requests (
                id, organization_id, branch_id, employee_id, leave_type,
                start_date, end_date, status, reason, paid_days, unpaid_days,
                start_time, end_time, leave_duration, rejection_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                status = EXCLUDED.status,
                reason = EXCLUDED.reason,
                leave_type = EXCLUDED.leave_type,
                start_date = EXCLUDED.start_date,
                end_date = EXCLUDED.end_date,
                paid_days = EXCLUDED.paid_days,
                unpaid_days = EXCLUDED.unpaid_days,
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                leave_duration = EXCLUDED.leave_duration,
                rejection_reason = EXCLUDED.rejection_reason
            """,
            (
                leave_id,
                org_id,
                branch_id,
                employee_id,
                _map_leave_type(row.get("leave_type")),
                row.get("start_date"),
                row.get("end_date"),
                status,
                row.get("reason") or "",
                float(row.get("paid_days") or 0.0),
                float(row.get("unpaid_days") or 0.0),
                str(row.get("start_time") or ""),
                str(row.get("end_time") or ""),
                str(row.get("leave_duration") or "full"),
                str(row.get("rejection_reason") or ""),
            ),
        )

    return _with_cursor(_write)


def delete_pg_leave(leave_id: int, organization_id: Optional[str] = None) -> None:
    if not leave_id:
        return
    def _write(cur):
        str_id = str(leave_id).strip()
        if organization_id and _real_tenant(organization_id):
            cur.execute(
                "DELETE FROM leave_requests WHERE id = %s AND organization_id = %s",
                (str_id, organization_id),
            )
        else:
            cur.execute("DELETE FROM leave_requests WHERE id = %s", (str_id,))
    _with_cursor(_write)


def mirror_overtime(row: Dict[str, Any]) -> None:
    org_id = str(row.get("organization_id") or "").strip()
    branch_id = str(row.get("branch_id") or "BR-01").strip() or "BR-01"
    if not _real_tenant(org_id, branch_id):
        return
    ot_id = str(row.get("id") or "").strip()
    employee_id = str(row.get("user_id") or row.get("employee_id") or "").strip()
    hours = float(row.get("hours") or 0)
    if not ot_id or not employee_id or hours <= 0:
        return
    status = str(row.get("status") or "pending").lower()
    if status not in {"pending", "approved", "rejected", "cancelled", "paid"}:
        status = "pending"

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id)
        cur.execute(
            """
            INSERT INTO employees (id, organization_id, branch_id, employee_code, name, is_active)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (organization_id, id) DO NOTHING
            """,
            (employee_id, org_id, branch_id, employee_id, str(row.get("user_name") or f"Employee {employee_id}")),
        )
        cur.execute(
            """
            INSERT INTO overtime_records (
                id, organization_id, branch_id, employee_id, work_date, hours, reason, status,
                rejection_reason, rejected_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                hours = EXCLUDED.hours,
                status = EXCLUDED.status,
                reason = EXCLUDED.reason,
                rejection_reason = EXCLUDED.rejection_reason,
                rejected_by = EXCLUDED.rejected_by
            """,
            (
                ot_id,
                org_id,
                branch_id,
                employee_id,
                row.get("ot_date") or row.get("work_date"),
                hours,
                row.get("reason") or "",
                status,
                str(row.get("rejection_reason") or ""),
                str(row.get("rejected_by") or ""),
            ),
        )

    return _with_cursor(_write)


def delete_pg_overtime(ot_id: int, organization_id: Optional[str] = None) -> None:
    if not ot_id:
        return
    def _write(cur):
        str_id = str(ot_id).strip()
        if organization_id and _real_tenant(organization_id):
            cur.execute(
                "DELETE FROM overtime_records WHERE id = %s AND organization_id = %s",
                (str_id, organization_id),
            )
        else:
            cur.execute("DELETE FROM overtime_records WHERE id = %s", (str_id,))
    _with_cursor(_write)


def mirror_company_holiday(holiday: Dict[str, Any]) -> None:
    org_id = str(holiday.get("organization_id") or "").strip()
    branch_id = str(holiday.get("branch_id") or "DEFAULT_BRANCH").strip() or "DEFAULT_BRANCH"
    if not _real_tenant(org_id, branch_id):
        return
    hid = str(holiday.get("id") or "").strip()
    if not hid:
        return
    h_date = str(holiday.get("holiday_date") or "").strip()
    if not h_date:
        return
    name = str(holiday.get("name") or "Holiday").strip()
    end_date = str(holiday.get("end_date") or h_date).strip()
    desc = str(holiday.get("description") or "").strip()
    is_rec = int(holiday.get("is_recurring") or 0)
    ot_prem = float(holiday.get("overtime_premium_percent") or 0.0)

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id)
        cur.execute(
            """
            INSERT INTO company_holidays (
                id, organization_id, branch_id, name, holiday_date, end_date, description, is_recurring, overtime_premium_percent
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                name = EXCLUDED.name,
                holiday_date = EXCLUDED.holiday_date,
                end_date = EXCLUDED.end_date,
                description = EXCLUDED.description,
                is_recurring = EXCLUDED.is_recurring,
                overtime_premium_percent = EXCLUDED.overtime_premium_percent
            """,
            (hid, org_id, branch_id, name, h_date, end_date, desc, is_rec, ot_prem),
        )

    return _with_cursor(_write)


def delete_pg_company_holiday(holiday_id: int, organization_id: Optional[str] = None, branch_id: Optional[str] = None) -> None:
    def _write(cur):
        if organization_id and _real_tenant(organization_id):
            cur.execute(
                "DELETE FROM company_holidays WHERE organization_id = %s AND id = %s",
                (organization_id, str(holiday_id)),
            )
        else:
            cur.execute("DELETE FROM company_holidays WHERE id = %s", (str(holiday_id),))

    _with_cursor(_write)


def fetch_pg_company_holidays(organization_id: str, branch_id: Optional[str] = None) -> Optional[int]:
    """Authoritatively fetch company holidays for a tenant from PostgreSQL into SQLite cache."""
    if not postgres_is_configured() or not _real_tenant(organization_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            query = """
                SELECT id, organization_id, branch_id, name, holiday_date::text,
                       COALESCE(end_date::text, holiday_date::text) as end_date,
                       description, is_recurring, overtime_premium_percent
                FROM company_holidays
                WHERE organization_id = %s
            """
            params = [organization_id]
            if branch_id and branch_id != 'all':
                query += " AND (branch_id = %s OR branch_id = 'DEFAULT_BRANCH')"
                params.append(branch_id)
            cur.execute(query, params)
            rows = list(cur.fetchall() or [])

        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            for r in rows:
                try:
                    hid = int(str(r.get("id") or "").strip())
                except (ValueError, TypeError):
                    continue
                sqlite_cur.execute(
                    """
                    INSERT INTO company_holidays (
                        id, organization_id, branch_id, name, holiday_date, end_date, description, is_recurring, overtime_premium_percent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(organization_id, branch_id, holiday_date) DO UPDATE SET
                        name = excluded.name,
                        end_date = excluded.end_date,
                        description = excluded.description,
                        is_recurring = excluded.is_recurring,
                        overtime_premium_percent = excluded.overtime_premium_percent
                    """,
                    (
                        hid,
                        organization_id,
                        r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                        r.get("name") or "Holiday",
                        r.get("holiday_date"),
                        r.get("end_date"),
                        r.get("description") or "",
                        int(r.get("is_recurring") or 0),
                        float(r.get("overtime_premium_percent") or 0.0),
                    ),
                )
            sqlite_conn.commit()
        return len(rows)
    except Exception as error:
        logger.warning("Could not fetch company holidays from Postgres: %s", error)
        return None
    finally:
        conn.close()


def mirror_mobile_credentials(user_id: int, organization_id: str, password_hash: str) -> None:
    org_id = str(organization_id or "").strip()
    def _write(cur):
        try:
            cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)")
            cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS mobile_login INT DEFAULT 0")
        except Exception:
            pass
        if org_id and _real_tenant(org_id):
            cur.execute(
                """
                UPDATE employees
                   SET password_hash = %s, mobile_login = 1, updated_at = CURRENT_TIMESTAMP
                 WHERE (id = %s OR employee_code = %s)
                   AND organization_id = %s
                """,
                (password_hash, str(user_id), str(user_id), org_id),
            )
        else:
            cur.execute(
                """
                UPDATE employees
                   SET password_hash = %s, mobile_login = 1, updated_at = CURRENT_TIMESTAMP
                 WHERE (id = %s OR employee_code = %s)
                """,
                (password_hash, str(user_id), str(user_id)),
            )
    _with_cursor(_write)


def hydrate_branch_employees(organization_id: str, branch_id: str) -> int:
    """Copy Postgres employee rows into SQLite and prune stale SQLite users for the tenant."""
    org_id = str(organization_id or "").strip()
    br_id = str(branch_id or "").strip()
    if not postgres_is_configured() or not _real_tenant(org_id, br_id):
        return 0
    conn = _connect()
    if conn is None:
        return 0
    inserted = 0
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)")
                cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS mobile_login INT DEFAULT 0")
            except Exception:
                pass
            cur.execute(
                """
                SELECT id, employee_code, name, email, phone, is_active,
                       shift_id,
                       COALESCE(password_hash, '') as password_hash,
                       COALESCE(mobile_login, 0) as mobile_login,
                       created_at::text as created_at,
                       COALESCE(person_type, 'staff') as person_type,
                       class_id,
                       roll_number,
                       guardian_name,
                       guardian_phone
                FROM employees
                WHERE organization_id = %s AND branch_id = %s
                """,
                (org_id, br_id),
            )
            rows = list(cur.fetchall() or [])
        if not rows:
            return 0
        pg_ids = set()
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            for row in rows:
                try:
                    user_id = int(str(row.get("id") or "").strip())
                except (TypeError, ValueError):
                    continue
                pg_ids.add(user_id)
                pw_hash = row.get("password_hash") or None
                mob_login = int(row.get("mobile_login", 0) or 0)
                row_created = str(row.get("created_at") or "").split("+")[0].strip() or None
                join_date_val = row_created.split()[0] if row_created else None
                s_id_raw = row.get("shift_id")
                try:
                    emp_shift_id = int(str(s_id_raw).strip()) if s_id_raw is not None and str(s_id_raw).strip() != "" else None
                except (ValueError, TypeError):
                    emp_shift_id = None
                p_type = row.get("person_type") or 'staff'
                c_id = row.get("class_id")
                r_num = row.get("roll_number")
                g_name = row.get("guardian_name")
                g_phone = row.get("guardian_phone")
                exists = sqlite_cur.execute(
                    "SELECT 1 FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
                if exists:
                    sqlite_cur.execute(
                        """
                        UPDATE users SET
                            name = ?,
                            email = COALESCE(?, email),
                            phone = COALESCE(?, phone),
                            active = ?,
                            shift_id = COALESCE(?, shift_id),
                            organization_id = ?,
                            branch_id = ?,
                            password_hash = COALESCE(?, password_hash),
                            mobile_login = MAX(?, COALESCE(mobile_login, 0)),
                            created_at = COALESCE(?, created_at),
                            join_date = CASE WHEN join_date IS NULL OR join_date = '' OR (join_date > ? AND ? IS NOT NULL) THEN COALESCE(?, join_date) ELSE join_date END,
                            enrollment_date = CASE WHEN enrollment_date IS NULL OR enrollment_date = '' OR (enrollment_date > ? AND ? IS NOT NULL) THEN COALESCE(?, enrollment_date) ELSE enrollment_date END,
                            person_type = COALESCE(?, person_type),
                            class_id = COALESCE(?, class_id),
                            roll_number = COALESCE(?, roll_number),
                            guardian_name = COALESCE(?, guardian_name),
                            guardian_phone = COALESCE(?, guardian_phone)
                        WHERE id = ?
                        """,
                        (
                            row.get("name"),
                            row.get("email"),
                            row.get("phone"),
                            1 if row.get("is_active", True) else 0,
                            emp_shift_id,
                            org_id,
                            br_id,
                            pw_hash,
                            mob_login,
                            row_created,
                            join_date_val,
                            join_date_val,
                            join_date_val,
                            row_created,
                            row_created,
                            row_created,
                            p_type,
                            c_id,
                            r_num,
                            g_name,
                            g_phone,
                            user_id,
                        ),
                    )
                    continue
                sqlite_cur.execute(
                    """
                    INSERT INTO users (
                        id, name, email, phone, notes, active, shift_id, organization_id, branch_id, password_hash, mobile_login, created_at, join_date, enrollment_date,
                        person_type, class_id, roll_number, guardian_name, guardian_phone
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        row.get("name") or f"User-{user_id}",
                        row.get("email") or "",
                        row.get("phone") or "",
                        row.get("employee_code") or f"EMP-{user_id}",
                        1 if row.get("is_active", True) else 0,
                        emp_shift_id,
                        org_id,
                        br_id,
                        pw_hash,
                        mob_login,
                        row_created,
                        join_date_val,
                        row_created,
                        p_type,
                        c_id,
                        r_num,
                        g_name,
                        g_phone,
                    ),
                )
            # LAYER 1 PROTECTION: A temporary absence of an employee from PostgreSQL
            # MUST NEVER cause that employee to be deleted from SQLite cache.
            # Retain local records with their tenant/branch identity intact.
            sqlite_cur.execute(
                "SELECT id FROM users WHERE organization_id = ? AND branch_id = ?",
                (org_id, br_id),
            )
            for cached_id_row in sqlite_cur.fetchall():
                cid = cached_id_row[0]
                if cid not in pg_ids:
                    logger.debug(
                        "Employee %s in %s/%s retained locally (not deleted due to temporary PG absence).",
                        cid, org_id, br_id
                    )

            # Purge any cross-tenant or corrupted attendance records with mismatched camera tags for this organization
            if org_id and org_id != 'DEFAULT_ORG':
                sqlite_cur.execute(
                    "DELETE FROM attendance WHERE organization_id = ? AND device_id LIKE 'CAM-ORG-%' AND device_id NOT LIKE ?",
                    (org_id, f"%{org_id}%"),
                )
                if pg_ids:
                    placeholders = ",".join("?" for _ in pg_ids)
                    # Purge any stale attendance rows belonging to other tenants mapped to this org's user IDs
                    sqlite_cur.execute(
                        f"DELETE FROM attendance WHERE user_id IN ({placeholders}) AND organization_id IS NOT NULL AND organization_id != ? AND organization_id != 'DEFAULT_ORG'",
                        (*pg_ids, org_id),
                    )
                    # Backfill organization_id for any orphan attendance records of this tenant's users
                    sqlite_cur.execute(
                        f"UPDATE attendance SET organization_id = ?, branch_id = COALESCE(branch_id, ?) WHERE user_id IN ({placeholders}) AND (organization_id IS NULL OR organization_id = '' OR organization_id = 'DEFAULT_ORG') AND (device_id IS NULL OR device_id NOT LIKE 'CAM-ORG-%' OR device_id LIKE ?)",
                        (org_id, br_id, *pg_ids, f"%{org_id}%"),
                    )
            sqlite_conn.commit()
        if inserted:
            logger.info(
                "Hydrated %s employees from Postgres into SQLite for %s/%s.",
                inserted,
                org_id,
                br_id,
            )
        return inserted
    except Exception as error:
        logger.warning("Could not hydrate employees from Postgres: %s", error)
        return 0
    finally:
        conn.close()


def fetch_pg_branch_employee_ids(organization_id: str, branch_id: Optional[str] = None) -> Optional[Set[int]]:
    """Fetch the set of employee IDs belonging to a tenant from PostgreSQL."""
    if not postgres_is_configured() or not _real_tenant(organization_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            query = "SELECT id FROM employees WHERE organization_id = %s"
            params = [organization_id]
            if branch_id and branch_id != 'all':
                query += " AND branch_id = %s"
                params.append(branch_id)
            cur.execute(query, params)
            rows = cur.fetchall()
            ids = set()
            for r in rows:
                try:
                    ids.add(int(str(r.get("id"))))
                except (ValueError, TypeError):
                    pass
            return ids
    except Exception as e:
        logger.warning("Could not fetch employee IDs from Postgres: %s", e)
        return None
    finally:
        conn.close()


def fetch_pg_employee(user_id: int, organization_id: Optional[str] = None) -> Any:
    """Authoritatively fetch a single employee from PostgreSQL into SQLite cache."""
    if not postgres_is_configured() or not user_id:
        return None
    org_id = organization_id
    if not org_id or not _real_tenant(org_id):
        try:
            with sqlite3.connect(_sqlite_path()) as s_conn:
                row = s_conn.execute("SELECT organization_id FROM users WHERE id = ?", (user_id,)).fetchone()
                if row and row[0]:
                    org_id = row[0]
        except Exception:
            pass
    if not org_id or not _real_tenant(org_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, organization_id, branch_id, employee_code, name, email, phone,
                       is_active, COALESCE(password_hash, '') as password_hash,
                       COALESCE(mobile_login, 0) as mobile_login,
                       COALESCE(person_type, 'staff') as person_type,
                       class_id, roll_number, guardian_name, guardian_phone
                FROM employees
                WHERE id = %s AND organization_id = %s
                """,
                (str(user_id), org_id),
            )
            row = cur.fetchone()
        if not row:
            # LAYER 1 & 2 PROTECTION: Temporary absence from PostgreSQL must NEVER
            # delete the local employee record. Only explicit authorized manager actions can delete.
            return False
        
        org_id = row.get("organization_id") or "DEFAULT_ORG"
        br_id = row.get("branch_id") or "BR-01"
        pw_hash = row.get("password_hash") or None
        mob_login = int(row.get("mobile_login", 0) or 0)
        p_type = row.get("person_type") or 'staff'
        c_id = row.get("class_id")
        r_num = row.get("roll_number")
        g_name = row.get("guardian_name")
        g_phone = row.get("guardian_phone")
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            exists = sqlite_cur.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
            if exists:
                sqlite_cur.execute(
                    """
                    UPDATE users SET
                        name = ?,
                        email = COALESCE(?, email),
                        phone = COALESCE(?, phone),
                        active = ?,
                        organization_id = COALESCE(?, organization_id),
                        branch_id = COALESCE(?, branch_id),
                        notes = COALESCE(?, notes),
                        password_hash = COALESCE(?, password_hash),
                        mobile_login = MAX(?, COALESCE(mobile_login, 0)),
                        person_type = COALESCE(?, person_type),
                        class_id = COALESCE(?, class_id),
                        roll_number = COALESCE(?, roll_number),
                        guardian_name = COALESCE(?, guardian_name),
                        guardian_phone = COALESCE(?, guardian_phone)
                    WHERE id = ?
                    """,
                    (
                        row.get("name"),
                        row.get("email"),
                        row.get("phone"),
                        1 if row.get("is_active", True) else 0,
                        org_id,
                        br_id,
                        row.get("employee_code"),
                        pw_hash,
                        mob_login,
                        p_type,
                        c_id,
                        r_num,
                        g_name,
                        g_phone,
                        user_id,
                    ),
                )
            else:
                sqlite_cur.execute(
                    """
                    INSERT INTO users (
                        id, name, email, phone, notes, active, organization_id, branch_id, password_hash, mobile_login,
                        person_type, class_id, roll_number, guardian_name, guardian_phone
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        row.get("name") or f"User-{user_id}",
                        row.get("email") or "",
                        row.get("phone") or "",
                        row.get("employee_code") or f"EMP-{user_id}",
                        1 if row.get("is_active", True) else 0,
                        org_id,
                        br_id,
                        pw_hash,
                        mob_login,
                        p_type,
                        c_id,
                        r_num,
                        g_name,
                        g_phone,
                    ),
                )
            sqlite_conn.commit()
            sqlite_conn.row_factory = sqlite3.Row
            full_user = sqlite_conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(full_user) if full_user else dict(row)
    except Exception as error:
        logger.warning("Could not fetch employee from Postgres: %s", error)
        return None
    finally:
        conn.close()


def fetch_pg_user_by_name(name: str, organization_id: Optional[str] = None) -> Any:
    """Authoritatively fetch an employee by name from PostgreSQL and update SQLite cache."""
    if not postgres_is_configured() or not name:
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)")
                cur.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS mobile_login INT DEFAULT 0")
            except Exception:
                pass
            if organization_id and _real_tenant(organization_id):
                cur.execute(
                    """
                    SELECT id, organization_id, branch_id, employee_code, name, email, phone,
                           is_active, COALESCE(password_hash, '') as password_hash,
                           COALESCE(mobile_login, 0) as mobile_login,
                           COALESCE(shift_id, '1') as shift_id
                    FROM employees
                    WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s)) AND organization_id = %s
                    LIMIT 1
                    """,
                    (name, organization_id),
                )
            else:
                cur.execute(
                    """
                    SELECT id, organization_id, branch_id, employee_code, name, email, phone,
                           is_active, COALESCE(password_hash, '') as password_hash,
                           COALESCE(mobile_login, 0) as mobile_login,
                           COALESCE(shift_id, '1') as shift_id
                    FROM employees
                    WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
                    LIMIT 1
                    """,
                    (name,),
                )
            row = cur.fetchone()
        if not row:
            return False
        
        try:
            user_id = int(str(row.get("id") or "").strip())
        except (ValueError, TypeError):
            user_id = 0
        org_id = row.get("organization_id") or "DEFAULT_ORG"
        br_id = row.get("branch_id") or "BR-01"
        pw_hash = row.get("password_hash") or None
        mob_login = int(row.get("mobile_login", 0) or 0)
        try:
            shift_id = int(str(row.get("shift_id") or "1"))
        except (ValueError, TypeError):
            shift_id = 1

        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            exists = sqlite_cur.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
            if exists:
                sqlite_cur.execute(
                    """
                    UPDATE users SET
                        name = ?,
                        email = COALESCE(?, email),
                        phone = COALESCE(?, phone),
                        active = ?,
                        organization_id = COALESCE(?, organization_id),
                        branch_id = COALESCE(?, branch_id),
                        notes = COALESCE(?, notes),
                        password_hash = COALESCE(?, password_hash),
                        mobile_login = MAX(?, COALESCE(mobile_login, 0)),
                        shift_id = COALESCE(?, shift_id)
                    WHERE id = ?
                    """,
                    (
                        row.get("name"),
                        row.get("email"),
                        row.get("phone"),
                        1 if row.get("is_active", True) else 0,
                        org_id,
                        br_id,
                        row.get("employee_code"),
                        pw_hash,
                        mob_login,
                        shift_id,
                        user_id,
                    ),
                )
            else:
                sqlite_cur.execute(
                    """
                    INSERT INTO users (
                        id, name, email, phone, notes, active, organization_id, branch_id, password_hash, mobile_login, shift_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        row.get("name"),
                        row.get("email") or "",
                        row.get("phone") or "",
                        row.get("employee_code") or f"EMP-{user_id}",
                        1 if row.get("is_active", True) else 0,
                        org_id,
                        br_id,
                        pw_hash,
                        mob_login,
                        shift_id,
                    ),
                )
            sqlite_conn.commit()
            sqlite_conn.row_factory = sqlite3.Row
            full_user = sqlite_conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(full_user) if full_user else dict(row)
    except Exception as error:
        logger.warning("Could not fetch employee by name from Postgres: %s", error)
        return None
    finally:
        conn.close()


def fetch_pg_shift(shift_id: int, organization_id: Optional[str] = None) -> Any:
    """Authoritatively fetch a single shift from PostgreSQL into SQLite cache."""
    if not postgres_is_configured() or not shift_id or not organization_id or not _real_tenant(organization_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, organization_id, branch_id, name,
                       to_char(start_time, 'HH24:MI') as start_time,
                       to_char(end_time, 'HH24:MI') as end_time,
                       grace_minutes
                FROM shifts
                WHERE id = %s AND organization_id = %s
                """,
                (str(shift_id), organization_id),
            )
            row = cur.fetchone()
        if not row:
            # LAYER 3 PROTECTION: Temporary absence from PostgreSQL must NEVER delete local shift
            return False
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            s_name = row.get("name") or f"Shift {shift_id}"
            exists_name = sqlite_cur.execute("SELECT id FROM shifts WHERE name = ? AND id != ?", (s_name, shift_id)).fetchone()
            if exists_name:
                s_name = f"{s_name} ({organization_id})"
                exists_name2 = sqlite_cur.execute("SELECT id FROM shifts WHERE name = ? AND id != ?", (s_name, shift_id)).fetchone()
                if exists_name2:
                    s_name = f"{row.get('name') or 'Shift'} ({organization_id}_{shift_id})"
            exists = sqlite_cur.execute("SELECT 1 FROM shifts WHERE id = ?", (shift_id,)).fetchone()
            if exists:
                sqlite_cur.execute(
                    """
                    UPDATE shifts SET
                        name = ?,
                        start_time = ?,
                        end_time = ?,
                        grace_period_minutes = ?,
                        active = 1,
                        organization_id = ?,
                        branch_id = COALESCE(?, branch_id)
                    WHERE id = ?
                    """,
                    (
                        s_name,
                        row.get("start_time") or "09:00",
                        row.get("end_time") or "17:00",
                        row.get("grace_minutes", 15),
                        organization_id,
                        row.get("branch_id") or "DEFAULT_BRANCH",
                        shift_id,
                    ),
                )
            else:
                sqlite_cur.execute(
                    """
                    INSERT INTO shifts (id, name, start_time, end_time, grace_period_minutes, active, organization_id, branch_id)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        shift_id,
                        s_name,
                        row.get("start_time") or "09:00",
                        row.get("end_time") or "17:00",
                        row.get("grace_minutes", 15),
                        organization_id,
                        row.get("branch_id") or "DEFAULT_BRANCH",
                    ),
                )
            sqlite_conn.commit()
            sqlite_conn.row_factory = sqlite3.Row
            full_shift = sqlite_conn.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
            return dict(full_shift) if full_shift else dict(row)
    except Exception as error:
        logger.warning("Could not fetch shift from Postgres: %s", error)
        return None
    finally:
        conn.close()


def fetch_pg_all_shifts(organization_id: str, branch_id: Optional[str] = None, active_only: bool = True) -> Optional[List[Dict[str, Any]]]:
    """Authoritatively fetch all shifts for a tenant from PostgreSQL and update SQLite cache."""
    if not postgres_is_configured() or not _real_tenant(organization_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            query = """
                SELECT id, organization_id, branch_id, name,
                       to_char(start_time, 'HH24:MI') as start_time,
                       to_char(end_time, 'HH24:MI') as end_time,
                       grace_minutes
                FROM shifts
                WHERE organization_id = %s
            """
            params = [organization_id]
            if branch_id and branch_id != 'all':
                query += " AND branch_id = %s"
                params.append(branch_id)
            cur.execute(query, params)
            rows = list(cur.fetchall() or [])
        
        pg_ids = set()
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            for r in rows:
                try:
                    sid = int(str(r.get("id") or "").strip())
                except (ValueError, TypeError):
                    continue
                pg_ids.add(sid)
                s_name = r.get("name") or f"Shift {sid}"
                exists_name = sqlite_cur.execute("SELECT id FROM shifts WHERE name = ? AND id != ?", (s_name, sid)).fetchone()
                if exists_name:
                    s_name = f"{s_name} ({organization_id})"
                    exists_name2 = sqlite_cur.execute("SELECT id FROM shifts WHERE name = ? AND id != ?", (s_name, sid)).fetchone()
                    if exists_name2:
                        s_name = f"{r.get('name') or 'Shift'} ({organization_id}_{sid})"
                exists = sqlite_cur.execute("SELECT 1 FROM shifts WHERE id = ?", (sid,)).fetchone()
                if exists:
                    sqlite_cur.execute(
                        """
                        UPDATE shifts SET
                            name = ?,
                            start_time = ?,
                            end_time = ?,
                            grace_period_minutes = ?,
                            organization_id = ?,
                            branch_id = COALESCE(?, branch_id)
                        WHERE id = ?
                        """,
                        (
                            s_name,
                            r.get("start_time") or "09:00",
                            r.get("end_time") or "17:00",
                            r.get("grace_minutes", 15),
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                            sid,
                        ),
                    )
                else:
                    sqlite_cur.execute(
                        """
                        INSERT INTO shifts (id, name, start_time, end_time, grace_period_minutes, active, organization_id, branch_id)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            sid,
                            s_name,
                            r.get("start_time") or "09:00",
                            r.get("end_time") or "17:00",
                            r.get("grace_minutes", 15),
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                        ),
                    )
            # LAYER 3 PROTECTION: Retain local shifts safely without destructive deletion
            sqlite_conn.commit()
        return rows
    except Exception as error:
        logger.warning("Could not fetch shifts from Postgres: %s", error)
        return None
    finally:
        conn.close()


def fetch_pg_leave(leave_id: int, organization_id: Optional[str] = None) -> Any:
    """Authoritatively fetch a single leave from PostgreSQL into SQLite cache."""
    if not postgres_is_configured() or not leave_id:
        return None
    org_id = organization_id
    if not org_id or not _real_tenant(org_id):
        try:
            with sqlite3.connect(_sqlite_path()) as s_conn:
                row = s_conn.execute("SELECT organization_id FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()
                if row and row[0]:
                    org_id = row[0]
        except Exception:
            pass
    if not org_id or not _real_tenant(org_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, organization_id, branch_id, employee_id, leave_type,
                       start_date::text as start_date, end_date::text as end_date,
                       status, reason, paid_days, unpaid_days, start_time, end_time,
                       leave_duration, rejection_reason
                FROM leave_requests
                WHERE id = %s AND organization_id = %s
                """,
                (str(leave_id), org_id),
            )
            row = cur.fetchone()
        if not row:
            try:
                with sqlite3.connect(_sqlite_path()) as s_conn:
                    s_conn.execute("DELETE FROM leave_requests WHERE id = ? AND organization_id = ?", (leave_id, org_id))
                    s_conn.commit()
            except Exception:
                pass
            return False
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            raw_st = str(row.get("status") or "pending").strip().lower()
            sub_st = 'approved' if raw_st == 'approved' else ('rejected' if raw_st == 'rejected' else 'pending')
            dur = str(row.get("leave_duration") or "full").strip().lower()
            exists = sqlite_cur.execute("SELECT 1 FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()
            if exists:
                sqlite_cur.execute(
                    """
                    UPDATE leave_requests SET
                        status = ?,
                        paid_status = ?,
                        unpaid_status = ?,
                        reason = ?,
                        leave_type = COALESCE(?, leave_type),
                        start_date = COALESCE(?, start_date),
                        end_date = COALESCE(?, end_date),
                        leave_duration = COALESCE(?, leave_duration),
                        paid_days = COALESCE(?, paid_days),
                        unpaid_days = COALESCE(?, unpaid_days),
                        start_time = COALESCE(?, start_time),
                        end_time = COALESCE(?, end_time),
                        rejection_reason = COALESCE(?, rejection_reason),
                        organization_id = ?,
                        branch_id = COALESCE(?, branch_id)
                    WHERE id = ?
                    """,
                    (
                        raw_st,
                        sub_st,
                        sub_st,
                        row.get("reason") or "",
                        row.get("leave_type"),
                        row.get("start_date"),
                        row.get("end_date"),
                        dur,
                        row.get("paid_days"),
                        row.get("unpaid_days"),
                        row.get("start_time"),
                        row.get("end_time"),
                        row.get("rejection_reason"),
                        org_id,
                        row.get("branch_id") or "DEFAULT_BRANCH",
                        leave_id,
                    ),
                )
            else:
                try:
                    user_id = int(str(row.get("employee_id") or "").strip())
                except (ValueError, TypeError):
                    user_id = 0
                sqlite_cur.execute(
                    """
                    INSERT INTO leave_requests (
                        id, user_id, leave_type, start_date, end_date, status, paid_status, unpaid_status,
                        leave_duration, paid_days, unpaid_days, start_time, end_time, rejection_reason,
                        reason, organization_id, branch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        leave_id,
                        user_id,
                        row.get("leave_type") or "casual",
                        row.get("start_date"),
                        row.get("end_date"),
                        raw_st,
                        sub_st,
                        sub_st,
                        dur,
                        row.get("paid_days"),
                        row.get("unpaid_days"),
                        row.get("start_time"),
                        row.get("end_time"),
                        row.get("rejection_reason"),
                        row.get("reason") or "",
                        org_id,
                        row.get("branch_id") or "DEFAULT_BRANCH",
                    ),
                )
            sqlite_conn.commit()
            sqlite_conn.row_factory = sqlite3.Row
            full_row = sqlite_conn.execute("SELECT * FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()
            return dict(full_row) if full_row else dict(row)
    except Exception as error:
        logger.warning("Could not fetch leave from Postgres: %s", error)
        return None
    finally:
        conn.close()


def fetch_pg_leave_requests(organization_id: str, branch_id: Optional[str] = None, user_id: Optional[int] = None, status: Optional[str] = None) -> Optional[int]:
    """Authoritatively fetch leave requests for a tenant from PostgreSQL and update SQLite cache."""
    if not postgres_is_configured() or not _real_tenant(organization_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            query = """
                SELECT id, organization_id, branch_id, employee_id, leave_type,
                       start_date::text as start_date, end_date::text as end_date,
                       status, reason, paid_days, unpaid_days, start_time, end_time,
                       leave_duration, rejection_reason
                FROM leave_requests
                WHERE organization_id = %s
            """
            params = [organization_id]
            if branch_id and branch_id != 'all':
                query += " AND branch_id = %s"
                params.append(branch_id)
            if user_id:
                query += " AND employee_id = %s"
                params.append(str(user_id))
            if status:
                st = status.strip().lower()
                if st == 'active':
                    query += " AND LOWER(status) IN ('pending', 'approved', 'partially_approved')"
                else:
                    query += " AND LOWER(status) = %s"
                    params.append(st)
            cur.execute(query, params)
            rows = list(cur.fetchall() or [])
        
        pg_ids = set()
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            for r in rows:
                try:
                    lid = int(str(r.get("id") or "").strip())
                except (ValueError, TypeError):
                    continue
                pg_ids.add(lid)
                try:
                    uid = int(str(r.get("employee_id") or "").strip())
                except (ValueError, TypeError):
                    uid = 0
                raw_st = str(r.get("status") or "pending").strip().lower()
                sub_st = 'approved' if raw_st == 'approved' else ('rejected' if raw_st == 'rejected' else 'pending')
                dur = str(r.get("leave_duration") or "full").strip().lower()
                exists = sqlite_cur.execute("SELECT 1 FROM leave_requests WHERE id = ?", (lid,)).fetchone()
                if exists:
                    sqlite_cur.execute(
                        """
                        UPDATE leave_requests SET
                            status = ?,
                            paid_status = ?,
                            unpaid_status = ?,
                            reason = ?,
                            leave_type = COALESCE(?, leave_type),
                            start_date = COALESCE(?, start_date),
                            end_date = COALESCE(?, end_date),
                            leave_duration = COALESCE(?, leave_duration),
                            paid_days = COALESCE(?, paid_days),
                            unpaid_days = COALESCE(?, unpaid_days),
                            start_time = COALESCE(?, start_time),
                            end_time = COALESCE(?, end_time),
                            rejection_reason = COALESCE(?, rejection_reason),
                            organization_id = ?,
                            branch_id = COALESCE(?, branch_id)
                        WHERE id = ?
                        """,
                        (
                            raw_st,
                            sub_st,
                            sub_st,
                            r.get("reason") or "",
                            r.get("leave_type"),
                            r.get("start_date"),
                            r.get("end_date"),
                            dur,
                            r.get("paid_days"),
                            r.get("unpaid_days"),
                            r.get("start_time"),
                            r.get("end_time"),
                            r.get("rejection_reason"),
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                            lid,
                        ),
                    )
                else:
                    sqlite_cur.execute(
                        """
                        INSERT INTO leave_requests (
                            id, user_id, leave_type, start_date, end_date, status, paid_status, unpaid_status,
                            leave_duration, paid_days, unpaid_days, start_time, end_time, rejection_reason,
                            reason, organization_id, branch_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            lid,
                            uid,
                            r.get("leave_type") or "casual",
                            r.get("start_date"),
                            r.get("end_date"),
                            raw_st,
                            sub_st,
                            sub_st,
                            dur,
                            r.get("paid_days"),
                            r.get("unpaid_days"),
                            r.get("start_time"),
                            r.get("end_time"),
                            r.get("rejection_reason"),
                            r.get("reason") or "",
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                        ),
                    )
            # Prune phantom leave requests in SQLite only during an UNFILTERED fetch for the tenant
            if not user_id and not status:
                sqlite_query = "SELECT id FROM leave_requests WHERE organization_id = ?"
                sq_params = [organization_id]
                if branch_id and branch_id != 'all':
                    sqlite_query += " AND branch_id = ?"
                    sq_params.append(branch_id)
                sqlite_cur.execute(sqlite_query, sq_params)
                cached_ids = [row[0] for row in sqlite_cur.fetchall()]
                for cid in cached_ids:
                    if cid not in pg_ids:
                        sqlite_cur.execute("DELETE FROM leave_requests WHERE id = ? AND organization_id = ?", (cid, organization_id))
            sqlite_conn.commit()
        return len(rows)
    except Exception as error:
        logger.warning("Could not fetch leave requests from Postgres: %s", error)
        return None
    finally:
        conn.close()


def fetch_pg_overtime(ot_id: int, organization_id: Optional[str] = None) -> Any:
    """Authoritatively fetch a single overtime record from PostgreSQL into SQLite cache."""
    if not postgres_is_configured() or not ot_id:
        return None
    org_id = organization_id
    if not org_id or not _real_tenant(org_id):
        try:
            with sqlite3.connect(_sqlite_path()) as s_conn:
                row = s_conn.execute("SELECT organization_id FROM overtime WHERE id = ?", (ot_id,)).fetchone()
                if row and row[0]:
                    org_id = row[0]
        except Exception:
            pass
    if not org_id or not _real_tenant(org_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, organization_id, branch_id, employee_id, work_date::text as work_date, hours, reason, status FROM overtime_records WHERE id = %s AND organization_id = %s",
                (str(ot_id), org_id),
            )
            row = cur.fetchone()
        if not row:
            try:
                with sqlite3.connect(_sqlite_path()) as s_conn:
                    s_conn.execute("DELETE FROM overtime WHERE id = ? AND organization_id = ?", (ot_id, org_id))
                    s_conn.commit()
            except Exception:
                pass
            return False
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            exists = sqlite_cur.execute("SELECT 1 FROM overtime WHERE id = ?", (ot_id,)).fetchone()
            if exists:
                sqlite_cur.execute(
                    """
                    UPDATE overtime SET
                        status = ?,
                        hours = ?,
                        reason = ?,
                        ot_date = COALESCE(?, ot_date),
                        organization_id = ?,
                        branch_id = COALESCE(?, branch_id)
                    WHERE id = ?
                    """,
                    (
                        row.get("status"),
                        float(row.get("hours") or 0),
                        row.get("reason"),
                        row.get("work_date"),
                        org_id,
                        row.get("branch_id") or "DEFAULT_BRANCH",
                        ot_id,
                    ),
                )
            else:
                try:
                    user_id = int(str(row.get("employee_id") or "").strip())
                except (ValueError, TypeError):
                    user_id = 0
                sqlite_cur.execute(
                    """
                    INSERT INTO overtime (id, user_id, ot_date, hours, reason, status, organization_id, branch_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ot_id,
                        user_id,
                        str(row.get("work_date")),
                        float(row.get("hours") or 0),
                        row.get("reason") or "",
                        row.get("status"),
                        org_id,
                        row.get("branch_id") or "DEFAULT_BRANCH",
                    ),
                )
            sqlite_conn.commit()
            sqlite_conn.row_factory = sqlite3.Row
            full_row = sqlite_conn.execute("SELECT * FROM overtime WHERE id = ?", (ot_id,)).fetchone()
            return dict(full_row) if full_row else dict(row)
    except Exception as error:
        logger.warning("Could not fetch overtime from Postgres: %s", error)
        return None
    finally:
        conn.close()


def fetch_pg_overtime_records(
    organization_id: str,
    branch_id: Optional[str] = None,
    user_id: Optional[int] = None,
    status: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[int]:
    """Authoritatively fetch overtime records for a tenant from PostgreSQL and update SQLite cache."""
    if not postgres_is_configured() or not _real_tenant(organization_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            query = """
                SELECT id, organization_id, branch_id, employee_id,
                       work_date::text as work_date, hours, reason, status
                FROM overtime_records
                WHERE organization_id = %s
            """
            params = [organization_id]
            if branch_id and branch_id != 'all':
                query += " AND branch_id = %s"
                params.append(branch_id)
            if user_id:
                query += " AND employee_id = %s"
                params.append(str(user_id))
            if status and status.lower() != 'all':
                query += " AND LOWER(status) = %s"
                params.append(status.lower())
            if start_date:
                query += " AND work_date >= %s"
                params.append(start_date)
            if end_date:
                query += " AND work_date <= %s"
                params.append(end_date)
            cur.execute(query, params)
            rows = list(cur.fetchall() or [])
        
        pg_ids = set()
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            for r in rows:
                try:
                    oid = int(str(r.get("id") or "").strip())
                except (ValueError, TypeError):
                    continue
                pg_ids.add(oid)
                try:
                    uid = int(str(r.get("employee_id") or "").strip())
                except (ValueError, TypeError):
                    uid = 0
                exists = sqlite_cur.execute("SELECT 1 FROM overtime WHERE id = ?", (oid,)).fetchone()
                if exists:
                    sqlite_cur.execute(
                        """
                        UPDATE overtime SET
                            status = ?,
                            hours = ?,
                            reason = ?,
                            ot_date = COALESCE(?, ot_date),
                            organization_id = ?,
                            branch_id = COALESCE(?, branch_id)
                        WHERE id = ?
                        """,
                        (
                            r.get("status"),
                            float(r.get("hours") or 0.0),
                            r.get("reason") or "",
                            r.get("work_date"),
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                            oid,
                        ),
                    )
                else:
                    sqlite_cur.execute(
                        """
                        INSERT INTO overtime (id, user_id, ot_date, hours, reason, status, organization_id, branch_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            oid,
                            uid,
                            r.get("work_date"),
                            float(r.get("hours") or 0.0),
                            r.get("reason") or "",
                            r.get("status") or "pending",
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                        ),
                    )
            # Prune phantom overtime in SQLite only during an UNFILTERED fetch for the tenant
            if not user_id and not status and not start_date and not end_date:
                sqlite_query = "SELECT id FROM overtime WHERE organization_id = ?"
                sq_params = [organization_id]
                if branch_id and branch_id != 'all':
                    sqlite_query += " AND branch_id = ?"
                    sq_params.append(branch_id)
                sqlite_cur.execute(sqlite_query, sq_params)
                cached_ids = [row[0] for row in sqlite_cur.fetchall()]
                for cid in cached_ids:
                    if cid not in pg_ids:
                        cur_row = sqlite_cur.execute(
                            "SELECT id, user_id, user_name, ot_date, hours, reason, status, organization_id, branch_id FROM overtime WHERE id = ?",
                            (cid,)
                        ).fetchone()
                        if cur_row and str(cur_row[6] or "").lower() == "pending":
                            try:
                                mirror_overtime({
                                    "id": cur_row[0],
                                    "user_id": cur_row[1],
                                    "user_name": cur_row[2],
                                    "ot_date": cur_row[3],
                                    "hours": cur_row[4],
                                    "reason": cur_row[5],
                                    "status": cur_row[6],
                                    "organization_id": cur_row[7] or organization_id,
                                    "branch_id": cur_row[8] or branch_id,
                                })
                                continue
                            except Exception:
                                pass
                        sqlite_cur.execute("DELETE FROM overtime WHERE id = ? AND organization_id = ?", (cid, organization_id))
            sqlite_conn.commit()
        return len(rows)
    except Exception as error:
        logger.warning("Could not fetch overtime from Postgres: %s", error)
        return None
    finally:
        conn.close()


def fetch_pg_monthly_payroll(payroll_month: str, organization_id: str, branch_id: Optional[str] = None) -> Any:
    """Authoritatively fetch a finalized monthly payroll run and its records from PostgreSQL and update SQLite cache."""
    if not postgres_is_configured() or not _real_tenant(organization_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, organization_id, branch_id, payroll_month,
                       period_start::text, period_end::text, employee_count,
                       total_basic_salary, total_deduction, status, total_amount
                FROM payroll_runs
                WHERE organization_id = %s AND payroll_month = %s
                """,
                (organization_id, payroll_month),
            )
            run = cur.fetchone()
            if not run:
                with sqlite3.connect(_sqlite_path()) as sqlite_conn:
                    sqlite_conn.execute("UPDATE payroll_runs SET status = 'Reopened' WHERE organization_id = ? AND payroll_month = ?", (organization_id, payroll_month))
                    sqlite_conn.execute("DELETE FROM payroll_records WHERE payroll_run_id IN (SELECT id FROM payroll_runs WHERE organization_id = ? AND payroll_month = ?)", (organization_id, payroll_month))
                    sqlite_conn.execute("DELETE FROM payroll_runs WHERE organization_id = ? AND payroll_month = ?", (organization_id, payroll_month))
                    sqlite_conn.commit()
                return False
            
            run_id = str(run.get("id"))
            cur.execute(
                """
                SELECT id, organization_id, branch_id, payroll_run_id, employee_id,
                       basic_salary, allowances, deductions, net_salary,
                       scheduled_workdays, present_days, absent_days, late_days,
                       approved_leave_days, payable_days,
                       is_prorated, unprorated_basic_salary, proration_days_paid, proration_method_days, proration_explanation,
                       COALESCE(gross_salary, 0.0) as gross_salary,
                       COALESCE(overtime_amount, 0.0) as overtime_amount,
                       COALESCE(overtime_hours, 0.0) as overtime_hours,
                       COALESCE(bonus_amount, 0.0) as bonus_amount,
                       COALESCE(arrears_amount, 0.0) as arrears_amount,
                       COALESCE(adjustment_amount, 0.0) as adjustment_amount,
                       COALESCE(unpaid_leave_deduction, 0.0) as unpaid_leave_deduction,
                       COALESCE(off_day_extra_pay, 0.0) as off_day_extra_pay,
                       COALESCE(custom_deductions, 0.0) as custom_deductions,
                       COALESCE(paid_leave_days, 0) as paid_leave_days,
                       COALESCE(unpaid_leave_days, 0) as unpaid_leave_days,
                       COALESCE(total_leave_days, 0) as total_leave_days
                FROM payroll_records
                WHERE organization_id = %s AND payroll_run_id = %s
                """,
                (organization_id, run_id),
            )
            records = list(cur.fetchall() or [])
        
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            stat = "Finalized" if str(run.get("status") or "").lower() in ("approved", "paid", "finalized") else "Draft"
            branch = run.get("branch_id") or branch_id or "DEFAULT_BRANCH"
            existing = sqlite_cur.execute(
                "SELECT id FROM payroll_runs WHERE organization_id = ? AND branch_id = ? AND payroll_month = ?",
                (organization_id, branch, payroll_month)
            ).fetchone()

            target_run_id = existing[0] if existing else None
            if target_run_id is not None:
                sqlite_cur.execute("UPDATE payroll_runs SET status = 'Reopened' WHERE id = ?", (target_run_id,))
                sqlite_cur.execute(
                    """
                    UPDATE payroll_runs SET
                        total_net_salary = ?,
                        employee_count = ?,
                        total_basic_salary = ?,
                        total_deduction = ?,
                        period_start = ?,
                        period_end = ?
                    WHERE id = ?
                    """,
                    (
                        float(run.get("total_amount") or 0.0),
                        int(run.get("employee_count") or len(records)),
                        float(run.get("total_basic_salary") or 0.0),
                        float(run.get("total_deduction") or 0.0),
                        run.get("period_start"),
                        run.get("period_end"),
                        target_run_id,
                    ),
                )
            else:
                sqlite_cur.execute(
                    """
                    INSERT INTO payroll_runs (
                        organization_id, branch_id, payroll_month, period_start, period_end,
                        employee_count, total_basic_salary, total_deduction, total_net_salary, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Draft')
                    """,
                    (
                        organization_id,
                        branch,
                        payroll_month,
                        run.get("period_start"),
                        run.get("period_end"),
                        int(run.get("employee_count") or len(records)),
                        float(run.get("total_basic_salary") or 0.0),
                        float(run.get("total_deduction") or 0.0),
                        float(run.get("total_amount") or 0.0),
                    ),
                )
                target_run_id = sqlite_cur.lastrowid

            pg_rec_eids = set()
            for rec in records:
                try:
                    eid = int(str(rec.get("employee_id") or "0"))
                except (ValueError, TypeError):
                    eid = 0
                if not eid:
                    continue
                pg_rec_eids.add(eid)
                u_row = sqlite_cur.execute("SELECT name FROM users WHERE id = ?", (eid,)).fetchone()
                ename = u_row[0] if u_row and u_row[0] else f"Employee {eid}"
                ded_val = float(rec.get("deductions") or rec.get("deduction_amount") or 0.0)
                sqlite_cur.execute(
                    """
                    INSERT INTO payroll_records (
                        payroll_run_id, employee_id, employee_name, organization_id, branch_id,
                        basic_salary, allowances, deductions, deduction_amount, net_salary,
                        scheduled_workdays, present_days, absent_days, late_days,
                        approved_leave_days, payable_days,
                        is_prorated, unprorated_basic_salary, proration_days_paid, proration_method_days, proration_explanation,
                        gross_salary, overtime_amount, overtime_hours, bonus_amount, arrears_amount, adjustment_amount,
                        unpaid_leave_deduction, off_day_extra_pay, custom_deductions, paid_leave_days, unpaid_leave_days, total_leave_days
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(payroll_run_id, employee_id) DO UPDATE SET
                        organization_id = excluded.organization_id,
                        branch_id = excluded.branch_id,
                        basic_salary = excluded.basic_salary,
                        allowances = excluded.allowances,
                        deductions = excluded.deductions,
                        deduction_amount = excluded.deduction_amount,
                        net_salary = excluded.net_salary,
                        payable_days = excluded.payable_days,
                        is_prorated = excluded.is_prorated,
                        unprorated_basic_salary = excluded.unprorated_basic_salary,
                        proration_days_paid = excluded.proration_days_paid,
                        proration_method_days = excluded.proration_method_days,
                        proration_explanation = excluded.proration_explanation,
                        gross_salary = excluded.gross_salary,
                        overtime_amount = excluded.overtime_amount,
                        overtime_hours = excluded.overtime_hours,
                        bonus_amount = excluded.bonus_amount,
                        arrears_amount = excluded.arrears_amount,
                        adjustment_amount = excluded.adjustment_amount,
                        unpaid_leave_deduction = excluded.unpaid_leave_deduction,
                        off_day_extra_pay = excluded.off_day_extra_pay,
                        custom_deductions = excluded.custom_deductions,
                        paid_leave_days = excluded.paid_leave_days,
                        unpaid_leave_days = excluded.unpaid_leave_days,
                        total_leave_days = excluded.total_leave_days
                    """,
                    (
                        target_run_id,
                        eid,
                        ename,
                        organization_id,
                        branch,
                        float(rec.get("basic_salary") or 0.0),
                        float(rec.get("allowances") or 0.0),
                        ded_val,
                        ded_val,
                        float(rec.get("net_salary") or 0.0),
                        int(rec.get("scheduled_workdays") or 0),
                        int(float(rec.get("present_days") or 0)),
                        int(float(rec.get("absent_days") or 0)),
                        int(float(rec.get("late_days") or 0)),
                        int(float(rec.get("approved_leave_days") or 0)),
                        float(rec.get("payable_days") or 0.0),
                        int(rec.get("is_prorated") or 0),
                        float(rec.get("unprorated_basic_salary") or 0.0),
                        int(rec.get("proration_days_paid") or 0),
                        int(rec.get("proration_method_days") or 0),
                        str(rec.get("proration_explanation") or ""),
                        float(rec.get("gross_salary") or 0.0),
                        float(rec.get("overtime_amount") or 0.0),
                        float(rec.get("overtime_hours") or 0.0),
                        float(rec.get("bonus_amount") or 0.0),
                        float(rec.get("arrears_amount") or 0.0),
                        float(rec.get("adjustment_amount") or 0.0),
                        float(rec.get("unpaid_leave_deduction") or 0.0),
                        float(rec.get("off_day_extra_pay") or 0.0),
                        float(rec.get("custom_deductions") or 0.0),
                        int(rec.get("paid_leave_days") or 0),
                        int(rec.get("unpaid_leave_days") or 0),
                        int(rec.get("total_leave_days") or 0),
                    ),
                )
            if target_run_id:
                sqlite_cur.execute("SELECT id, employee_id FROM payroll_records WHERE payroll_run_id = ?", (target_run_id,))
                for crid_row in sqlite_cur.fetchall():
                    if crid_row[1] not in pg_rec_eids:
                        sqlite_cur.execute("DELETE FROM payroll_records WHERE id = ?", (crid_row[0],))
                sqlite_cur.execute("UPDATE payroll_runs SET status = ? WHERE id = ?", (stat, target_run_id))
            sqlite_conn.commit()
        return dict(run)
    except Exception as error:
        logger.warning("Could not fetch monthly payroll from Postgres: %s", error)
        return None
    finally:
        conn.close()


def fetch_pg_attendance_logs(organization_id: str, branch_id: Optional[str] = None, limit: int = 100) -> Optional[int]:
    """Authoritatively fetch attendance events for a tenant from PostgreSQL and update SQLite cache."""
    if not postgres_is_configured() or not _real_tenant(organization_id):
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            query = """
                SELECT id, organization_id, branch_id, employee_id, camera_id,
                       timestamp::text as timestamp, event_type, confidence
                FROM attendance_events
                WHERE organization_id = %s
            """
            params = [organization_id]
            if branch_id and branch_id != 'all':
                query += " AND branch_id = %s"
                params.append(branch_id)
            query += " ORDER BY timestamp DESC LIMIT %s"
            params.append(limit)
            cur.execute(query, params)
            rows = list(cur.fetchall() or [])
        
        pg_ids = set()
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            for r in rows:
                raw_id = str(r.get("id") or "").strip()
                if raw_id.startswith("EVT-"):
                    continue
                try:
                    uid = int(str(r.get("employee_id") or "").strip())
                except (ValueError, TypeError):
                    uid = 0
                if not uid:
                    continue
                user_name = "Employee"
                u_row = sqlite_cur.execute("SELECT name FROM users WHERE id = ?", (uid,)).fetchone()
                if u_row and u_row[0]:
                    user_name = u_row[0]
                ts_raw = str(r.get("timestamp") or "")
                ts_clean = ts_raw.replace("T", " ").split("+")[0].split(".")[0]
                
                # If marked manual_absent, do not re-insert attendance logs
                is_manual_absent = sqlite_cur.execute(
                    "SELECT 1 FROM manual_absent WHERE user_id = ? AND date = ?",
                    (uid, ts_clean[:10]),
                ).fetchone()
                if is_manual_absent:
                    continue

                # QA-50: Do not overwrite or revert manual attendance records
                has_manual = sqlite_cur.execute(
                    "SELECT id FROM attendance WHERE user_id = ? AND date(timestamp) = ? AND source = 'manual'",
                    (uid, ts_clean[:10]),
                ).fetchall()
                if has_manual:
                    for m_row in has_manual:
                        pg_ids.add(m_row[0])
                    continue

                exists = sqlite_cur.execute(
                    "SELECT id, source FROM attendance WHERE user_id = ? AND timestamp LIKE ?",
                    (uid, f"{ts_clean}%"),
                ).fetchone()
                if exists:
                    aid = exists[0]
                    if exists[1] == 'manual':
                        pg_ids.add(aid)
                        continue
                    pg_ids.add(aid)
                    sqlite_cur.execute(
                        """
                        UPDATE attendance SET
                            confidence = ?,
                            timestamp = ?,
                            device_id = ?,
                            location = ?,
                            organization_id = ?,
                            branch_id = COALESCE(?, branch_id)
                        WHERE id = ?
                        """,
                        (
                            float(r.get("confidence") or 1.0),
                            ts_clean,
                            r.get("camera_id") or "CAM-01",
                            r.get("camera_id") or "Entrance",
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                            aid,
                        ),
                    )
                else:
                    sqlite_cur.execute(
                        """
                        INSERT INTO attendance (user_id, detected_name, confidence, timestamp, source, location, device_id, organization_id, branch_id)
                        VALUES (?, ?, ?, ?, 'camera', ?, ?, ?, ?)
                        """,
                        (
                            uid,
                            user_name,
                            float(r.get("confidence") or 1.0),
                            ts_clean,
                            r.get("camera_id") or "Entrance",
                            r.get("camera_id") or "CAM-01",
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                        ),
                    )
                    aid = sqlite_cur.lastrowid
                    if aid:
                        pg_ids.add(aid)
            sqlite_conn.commit()
        return len(rows)
    except Exception as error:
        logger.warning("Could not fetch attendance logs from Postgres: %s", error)
        return None
    finally:
        conn.close()


def _sqlite_path() -> str:
    from config import DB_PATH
    return DB_PATH


def _fetch_sqlite_user(user_id: int) -> Optional[Dict[str, Any]]:
    try:
        with sqlite3.connect(_sqlite_path()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def mirror_user_id(user_id: Optional[int]) -> None:
    if not user_id:
        return
    row = _fetch_sqlite_user(int(user_id))
    if row:
        mirror_employee(row)


def mirror_camera_row(cam_db_id: Optional[int] = None, camera_id: Optional[str] = None) -> None:
    try:
        with sqlite3.connect(_sqlite_path()) as conn:
            conn.row_factory = sqlite3.Row
            if cam_db_id is not None:
                row = conn.execute("SELECT * FROM cameras WHERE id = ?", (cam_db_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM cameras WHERE camera_id = ?", (camera_id,)
                ).fetchone()
            if row:
                mirror_camera(dict(row))
    except Exception as error:
        logger.warning("Could not load camera for Postgres mirror: %s", error)


def mirror_class_row(class_id: Optional[int] = None) -> None:
    try:
        with sqlite3.connect(_sqlite_path()) as conn:
            conn.row_factory = sqlite3.Row
            if class_id is not None:
                row = conn.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
                if row:
                    mirror_class(dict(row))
    except Exception as error:
        logger.warning("Could not load class for Postgres mirror: %s", error)


def mirror_class(cls: Dict[str, Any]) -> None:
    org_id = str(cls.get("organization_id") or "").strip()
    branch_id = str(cls.get("branch_id") or "BR-01").strip() or "BR-01"
    if not _real_tenant(org_id, branch_id):
        return
    class_id = cls.get("id")
    name = str(cls.get("name") or "").strip()
    section = str(cls.get("section") or "A").strip()
    if not name:
        return
    shift_id = cls.get("shift_id")
    room_number = cls.get("room_number")
    teacher_id = cls.get("teacher_id")
    active = int(cls.get("active", 1) or 1)

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id)
        cur.execute(
            """
            INSERT INTO classes (
                id, organization_id, branch_id, name, section, shift_id, room_number, teacher_id, active
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                organization_id = EXCLUDED.organization_id,
                branch_id = EXCLUDED.branch_id,
                name = EXCLUDED.name,
                section = EXCLUDED.section,
                shift_id = EXCLUDED.shift_id,
                room_number = EXCLUDED.room_number,
                teacher_id = EXCLUDED.teacher_id,
                active = EXCLUDED.active
            """,
            (class_id, org_id, branch_id, name, section, shift_id, room_number, teacher_id, active),
        )

    return _with_cursor(_write)


def delete_class(class_id: int, organization_id: Optional[str] = None) -> None:
    def _write(cur):
        if organization_id and _real_tenant(organization_id):
            cur.execute(
                "UPDATE employees SET class_id = NULL WHERE organization_id = %s AND class_id = %s",
                (organization_id, class_id),
            )
            cur.execute(
                "UPDATE cameras SET assigned_class_id = NULL WHERE organization_id = %s AND assigned_class_id = %s",
                (organization_id, class_id),
            )
            cur.execute(
                "DELETE FROM classes WHERE organization_id = %s AND id = %s",
                (organization_id, class_id),
            )
        else:
            cur.execute("UPDATE employees SET class_id = NULL WHERE class_id = %s", (class_id,))
            cur.execute("UPDATE cameras SET assigned_class_id = NULL WHERE assigned_class_id = %s", (class_id,))
            cur.execute("DELETE FROM classes WHERE id = %s", (class_id,))

    _with_cursor(_write)


def hydrate_branch_classes(organization_id: str, branch_id: str) -> int:
    """Synchronize classes from Postgres into SQLite cache."""
    org_id = str(organization_id or "").strip()
    br_id = str(branch_id or "").strip()
    if not postgres_is_configured() or not _real_tenant(org_id, br_id):
        return 0
    conn = _connect()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, organization_id, branch_id, name, section, shift_id, room_number, teacher_id, active
                FROM classes
                WHERE organization_id = %s AND branch_id = %s
                """,
                (org_id, br_id),
            )
            rows = list(cur.fetchall() or [])
        if not rows:
            return 0
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            for r in rows:
                c_id = r.get("id")
                c_name = r.get("name")
                c_sec = r.get("section") or "A"
                c_shift = r.get("shift_id")
                c_room = r.get("room_number")
                c_teacher = r.get("teacher_id")
                c_active = int(r.get("active", 1) or 1)
                sqlite_cur.execute(
                    """
                    INSERT INTO classes (id, organization_id, branch_id, name, section, shift_id, room_number, teacher_id, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(organization_id, branch_id, name, section) DO UPDATE SET
                        shift_id = excluded.shift_id,
                        room_number = excluded.room_number,
                        teacher_id = excluded.teacher_id,
                        active = excluded.active
                    """,
                    (c_id, org_id, br_id, c_name, c_sec, c_shift, c_room, c_teacher, c_active),
                )
            sqlite_conn.commit()
        return len(rows)
    except Exception as error:
        logger.warning("Could not hydrate classes from Postgres: %s", error)
        return 0
    finally:
        conn.close()


def bootstrap_cloud_postgres() -> None:
    """Push existing SQLite tenant rows into empty Postgres; never copy embeddings."""
    if not postgres_is_configured():
        logger.info("PostgreSQL is not configured; Local SQLite remains the working cache.")
        return
    conn = _connect()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM employees")
            employee_count = int((cur.fetchone() or {}).get("n") or 0)
        if employee_count > 0:
            logger.info("Cloud Postgres already has %s employees; SQLite stays a cache.", employee_count)
            return
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_conn.row_factory = sqlite3.Row
            users = [
                dict(row)
                for row in sqlite_conn.execute(
                    "SELECT * FROM users WHERE organization_id IS NOT NULL AND organization_id != '' AND organization_id != 'DEFAULT_ORG'"
                ).fetchall()
            ]
            cameras = [
                dict(row)
                for row in sqlite_conn.execute(
                    "SELECT * FROM cameras WHERE organization_id IS NOT NULL AND organization_id != '' AND organization_id != 'DEFAULT_ORG'"
                ).fetchall()
            ]
            shifts = [
                dict(row)
                for row in sqlite_conn.execute(
                    "SELECT * FROM shifts WHERE organization_id IS NOT NULL AND organization_id != '' AND organization_id != 'DEFAULT_ORG'"
                ).fetchall()
            ]
        for user in users:
            mirror_employee(user)
        for camera in cameras:
            mirror_camera(camera)
        for shift in shifts:
            mirror_shift(shift)
        logger.info(
            "Bootstrapped cloud Postgres from Local SQLite (%s staff, %s cameras).",
            len(users),
            len(cameras),
        )
    except Exception as error:
        logger.warning("Postgres bootstrap from SQLite skipped: %s", error)
    finally:
        conn.close()


def mirror_payroll_run(run: Dict[str, Any], records: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Synchronize monthly payroll run and its records to PostgreSQL."""
    org_id = str(run.get("organization_id") or "").strip()
    branch_id = str(run.get("branch_id") or "BR-01").strip() or "BR-01"
    if not _real_tenant(org_id, branch_id):
        return False
    run_id = str(run.get("id") or "").strip()
    if not run_id:
        return False
    stat = str(run.get("status") or "draft").lower()
    if stat not in {"draft", "approved", "paid", "cancelled"}:
        stat = "draft"

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id)
        cur.execute(
            """
            INSERT INTO payroll_runs (
                id, organization_id, branch_id, payroll_month, period_start, period_end,
                employee_count, total_basic_salary, total_deduction, status, total_amount
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                status = EXCLUDED.status,
                total_amount = EXCLUDED.total_amount,
                employee_count = EXCLUDED.employee_count,
                total_basic_salary = EXCLUDED.total_basic_salary,
                total_deduction = EXCLUDED.total_deduction
            """,
            (
                run_id,
                org_id,
                branch_id,
                run.get("payroll_month") or "2026-10",
                run.get("period_start") or "2026-10-01",
                run.get("period_end") or "2026-10-31",
                int(run.get("employee_count") or 0),
                float(run.get("total_basic_salary") or 0.0),
                float(run.get("total_deduction") or 0.0),
                stat,
                float(run.get("total_net_salary") or run.get("total_amount") or 0.0),
            ),
        )
        if records:
            from psycopg2.extras import execute_values
            rec_tuples = []
            for r in records:
                eid = str(r.get("employee_id") or "")
                if not eid:
                    continue
                rid = str(r.get("id") or f"{run_id}_{eid}")
                rec_tuples.append((
                    rid,
                    org_id,
                    branch_id,
                    run_id,
                    eid,
                    float(r.get("basic_salary") or 0.0),
                    float(r.get("allowances") or 0.0),
                    float(r.get("deduction_amount") or r.get("deductions") or 0.0),
                    float(r.get("net_salary") or 0.0),
                    int(r.get("scheduled_workdays") or 0),
                    int(float(r.get("present_days") or 0)),
                    int(float(r.get("absent_days") or 0)),
                    int(float(r.get("late_days") or 0)),
                    int(float(r.get("approved_leave_days") or 0)),
                    float(r.get("payable_days") or 0.0),
                    int(r.get("is_prorated") or 0),
                    float(r.get("unprorated_basic_salary") or 0.0),
                    int(r.get("proration_days_paid") or 0),
                    int(r.get("proration_method_days") or 0),
                    str(r.get("proration_explanation") or ""),
                    float(r.get("gross_salary") or 0.0),
                    float(r.get("overtime_amount") or 0.0),
                    float(r.get("overtime_hours") or 0.0),
                    float(r.get("overtime_rate") or 0.0),
                    float(r.get("bonus_amount") or 0.0),
                    float(r.get("arrears_amount") or 0.0),
                    float(r.get("adjustment_amount") or 0.0),
                    float(r.get("unpaid_leave_deduction") or 0.0),
                    float(r.get("off_day_extra_pay") or 0.0),
                    float(r.get("custom_deductions") or 0.0),
                    int(r.get("paid_leave_days") or 0),
                    int(r.get("unpaid_leave_days") or 0),
                    int(r.get("total_leave_days") or 0),
                    float(r.get("days_paid")) if r.get("days_paid") is not None else None,
                    str(r.get("status") or "Finalized"),
                ))
            if rec_tuples:
                eids = list({t[4] for t in rec_tuples if t[4]})
                if eids:
                    emp_tuples = [
                        (eid_val, org_id, branch_id, eid_val, f"Employee {eid_val}")
                        for eid_val in eids
                    ]
                    execute_values(
                        cur,
                        """
                        INSERT INTO employees (id, organization_id, branch_id, employee_code, name)
                        VALUES %s
                        ON CONFLICT (organization_id, id) DO NOTHING
                        """,
                        emp_tuples,
                        page_size=500,
                    )
                execute_values(
                    cur,
                    """
                    INSERT INTO payroll_records (
                        id, organization_id, branch_id, payroll_run_id, employee_id,
                        basic_salary, allowances, deductions, net_salary,
                        scheduled_workdays, present_days, absent_days, late_days,
                        approved_leave_days, payable_days,
                        is_prorated, unprorated_basic_salary, proration_days_paid, proration_method_days, proration_explanation,
                        gross_salary, overtime_amount, overtime_hours, overtime_rate,
                        bonus_amount, arrears_amount, adjustment_amount, unpaid_leave_deduction,
                        off_day_extra_pay, custom_deductions, paid_leave_days, unpaid_leave_days,
                        total_leave_days, days_paid, status
                    ) VALUES %s
                    ON CONFLICT (organization_id, id) DO UPDATE SET
                        basic_salary = EXCLUDED.basic_salary,
                        allowances = EXCLUDED.allowances,
                        deductions = EXCLUDED.deductions,
                        net_salary = EXCLUDED.net_salary,
                        gross_salary = EXCLUDED.gross_salary,
                        overtime_amount = EXCLUDED.overtime_amount,
                        bonus_amount = EXCLUDED.bonus_amount,
                        arrears_amount = EXCLUDED.arrears_amount,
                        adjustment_amount = EXCLUDED.adjustment_amount,
                        unpaid_leave_deduction = EXCLUDED.unpaid_leave_deduction,
                        off_day_extra_pay = EXCLUDED.off_day_extra_pay,
                        custom_deductions = EXCLUDED.custom_deductions
                    """,
                    rec_tuples,
                    page_size=500,
                )

    return _with_cursor(_write)


def fetch_pg_organization_info(organization_id: str) -> Optional[Dict[str, Any]]:
    """Fetch enabled modules and company name for an organization from PostgreSQL."""
    if not postgres_is_configured() or not organization_id or organization_id in _PLACEHOLDER_ORGS:
        return None

    def _read(cur):
        cur.execute(
            """
            SELECT id, name, portal_slug FROM organizations
            WHERE id = %s OR LOWER(id) = LOWER(%s) OR LOWER(COALESCE(portal_slug, '')) = LOWER(%s)
            LIMIT 1
            """,
            (organization_id, organization_id, organization_id)
        )
        row = cur.fetchone()
        org_name = ""
        resolved_id = organization_id
        if row:
            if isinstance(row, dict):
                org_name = str(row.get('name') or '').strip()
                resolved_id = str(row.get('id') or organization_id).strip()
            else:
                org_name = str(row[1]).strip() if len(row) > 1 and row[1] else ""
                resolved_id = str(row[0]).strip() if len(row) > 0 and row[0] else organization_id

        cur.execute(
            """
            SELECT module_id FROM organization_modules
            WHERE (organization_id = %s OR LOWER(organization_id) = LOWER(%s))
              AND (is_enabled IS TRUE OR is_enabled IS NULL)
            """,
            (resolved_id, resolved_id)
        )
        modules = []
        for r in cur.fetchall():
            if isinstance(r, dict):
                mod = r.get('module_id')
            else:
                mod = r[0] if len(r) > 0 else None
            if mod:
                modules.append(str(mod).strip().lower())
        return {
            "name": org_name,
            "modules": modules,
        }

    try:
        return _with_read_cursor(_read)
    except Exception as e:
        logger.debug("Could not fetch organization info from postgres: %s", e)
        return None


def fetch_pg_organization_modules(organization_id: str) -> Optional[List[str]]:
    """Fetch enabled modules for an organization from PostgreSQL organization_modules table."""
    info = fetch_pg_organization_info(organization_id)
    if info is not None:
        return info.get("modules")
    return None

