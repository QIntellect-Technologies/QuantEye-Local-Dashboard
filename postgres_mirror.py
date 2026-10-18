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
from typing import Any, Dict, Optional
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

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id, str(user.get("organization_name") or ""))
        cur.execute(
            """
            INSERT INTO employees (
                id, organization_id, branch_id, employee_code, name, email, phone, is_active
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                name = EXCLUDED.name,
                email = EXCLUDED.email,
                phone = EXCLUDED.phone,
                is_active = EXCLUDED.is_active,
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
                bool(int(user.get("active", 1) or 1)),
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


def mirror_camera(camera: Dict[str, Any]) -> None:
    org_id = str(camera.get("organization_id") or "").strip()
    branch_id = str(camera.get("branch_id") or "BR-01").strip() or "BR-01"
    if not _real_tenant(org_id, branch_id):
        return
    camera_id = str(camera.get("camera_id") or camera.get("id") or "").strip()
    rtsp_url = str(camera.get("rtsp_url") or "").strip() or "rtsp://unconfigured"
    if not camera_id:
        return

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id)
        cur.execute(
            """
            INSERT INTO cameras (
                id, organization_id, branch_id, camera_name, camera_type, rtsp_url,
                location, room_name, is_active
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                camera_name = EXCLUDED.camera_name,
                camera_type = EXCLUDED.camera_type,
                rtsp_url = EXCLUDED.rtsp_url,
                location = EXCLUDED.location,
                room_name = EXCLUDED.room_name,
                is_active = EXCLUDED.is_active,
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
                bool(int(camera.get("active", 1) or 1)),
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
                start_date, end_date, status, reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                status = EXCLUDED.status,
                reason = EXCLUDED.reason
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
            ),
        )

    return _with_cursor(_write)


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
                id, organization_id, branch_id, employee_id, work_date, hours, reason, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                hours = EXCLUDED.hours,
                status = EXCLUDED.status,
                reason = EXCLUDED.reason
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
            ),
        )

    return _with_cursor(_write)


def mirror_shift(row: Dict[str, Any]) -> None:
    org_id = str(row.get("organization_id") or "").strip()
    branch_id = str(row.get("branch_id") or "BR-01").strip() or "BR-01"
    if not _real_tenant(org_id, branch_id):
        return
    shift_id = str(row.get("id") or "").strip()
    if not shift_id:
        return

    def _write(cur):
        _ensure_tenant(cur, org_id, branch_id)
        cur.execute(
            """
            INSERT INTO shifts (
                id, organization_id, branch_id, name, start_time, end_time, grace_minutes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, id) DO UPDATE SET
                name = EXCLUDED.name,
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                grace_minutes = EXCLUDED.grace_minutes
            """,
            (
                shift_id,
                org_id,
                branch_id,
                row.get("name") or f"Shift {shift_id}",
                row.get("start_time") or "09:00",
                row.get("end_time") or "18:00",
                int(row.get("grace_period_minutes") or row.get("grace_minutes") or 15),
            ),
        )

    return _with_cursor(_write)


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
    """Copy Postgres employee rows into SQLite when the local cache is missing them.

    Face embeddings are never stored in Postgres and are not created here.
    """
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
                       COALESCE(password_hash, '') as password_hash,
                       COALESCE(mobile_login, 0) as mobile_login
                FROM employees
                WHERE organization_id = %s AND branch_id = %s
                """,
                (org_id, br_id),
            )
            rows = list(cur.fetchall() or [])
        if not rows:
            return 0
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            for row in rows:
                try:
                    user_id = int(str(row.get("id") or "").strip())
                except (TypeError, ValueError):
                    continue
                pw_hash = row.get("password_hash") or None
                mob_login = int(row.get("mobile_login", 0) or 0)
                exists = sqlite_cur.execute(
                    "SELECT 1 FROM users WHERE id = ? AND organization_id = ? AND branch_id = ?",
                    (user_id, org_id, br_id),
                ).fetchone()
                if exists:
                    if pw_hash or mob_login:
                        sqlite_cur.execute(
                            "UPDATE users SET password_hash = COALESCE(?, password_hash), mobile_login = MAX(?, COALESCE(mobile_login, 0)) WHERE id = ? AND organization_id = ?",
                            (pw_hash, mob_login, user_id, org_id),
                        )
                    continue
                sqlite_cur.execute(
                    """
                    INSERT INTO users (
                        id, name, email, phone, notes, active, organization_id, branch_id, password_hash, mobile_login
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    ),
                )
                inserted += 1
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
                ))
            if rec_tuples:
                execute_values(
                    cur,
                    """
                    INSERT INTO payroll_records (
                        id, organization_id, branch_id, payroll_run_id, employee_id,
                        basic_salary, allowances, deductions, net_salary,
                        scheduled_workdays, present_days, absent_days, late_days,
                        approved_leave_days, payable_days
                    ) VALUES %s
                    ON CONFLICT (organization_id, id) DO NOTHING
                    """,
                    rec_tuples,
                    page_size=500,
                )

    return _with_cursor(_write)
