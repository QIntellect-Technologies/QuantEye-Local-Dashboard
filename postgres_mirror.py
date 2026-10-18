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
                exists = sqlite_cur.execute(
                    "SELECT 1 FROM users WHERE id = ? AND organization_id = ? AND branch_id = ?",
                    (user_id, org_id, br_id),
                ).fetchone()
                if exists:
                    sqlite_cur.execute(
                        """
                        UPDATE users SET
                            name = ?,
                            email = COALESCE(?, email),
                            phone = COALESCE(?, phone),
                            active = ?,
                            password_hash = COALESCE(?, password_hash),
                            mobile_login = MAX(?, COALESCE(mobile_login, 0))
                        WHERE id = ? AND organization_id = ?
                        """,
                        (
                            row.get("name"),
                            row.get("email"),
                            row.get("phone"),
                            1 if row.get("is_active", True) else 0,
                            pw_hash,
                            mob_login,
                            user_id,
                            org_id,
                        ),
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
            # Prune cached SQLite users in this org/branch that do not exist in PostgreSQL
            sqlite_cur.execute(
                "SELECT id FROM users WHERE organization_id = ? AND branch_id = ?",
                (org_id, br_id),
            )
            for cached_id_row in sqlite_cur.fetchall():
                cid = cached_id_row[0]
                if cid not in pg_ids:
                    sqlite_cur.execute(
                        "DELETE FROM users WHERE id = ? AND organization_id = ? AND branch_id = ?",
                        (cid, org_id, br_id),
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
                       COALESCE(mobile_login, 0) as mobile_login
                FROM employees
                WHERE id = %s AND organization_id = %s
                """,
                (str(user_id), org_id),
            )
            row = cur.fetchone()
        if not row:
            try:
                with sqlite3.connect(_sqlite_path()) as s_conn:
                    s_conn.execute("DELETE FROM users WHERE id = ? AND organization_id = ?", (user_id, org_id))
                    s_conn.commit()
            except Exception:
                pass
            return False
        
        org_id = row.get("organization_id") or "DEFAULT_ORG"
        br_id = row.get("branch_id") or "BR-01"
        pw_hash = row.get("password_hash") or None
        mob_login = int(row.get("mobile_login", 0) or 0)
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
                        mobile_login = MAX(?, COALESCE(mobile_login, 0))
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
                        user_id,
                    ),
                )
            else:
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
            try:
                with sqlite3.connect(_sqlite_path()) as s_conn:
                    s_conn.execute("DELETE FROM shifts WHERE id = ? AND organization_id = ?", (shift_id, organization_id))
                    s_conn.commit()
            except Exception:
                pass
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
            # Prune phantom shifts in SQLite that do not exist in PostgreSQL for this tenant
            sqlite_cur.execute("SELECT id FROM shifts WHERE organization_id = ?", (organization_id,))
            cached_ids = [row[0] for row in sqlite_cur.fetchall()]
            for cid in cached_ids:
                if cid not in pg_ids:
                    sqlite_cur.execute("DELETE FROM shifts WHERE id = ? AND organization_id = ?", (cid, organization_id))
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
                "SELECT id, organization_id, branch_id, employee_id, leave_type, start_date::text as start_date, end_date::text as end_date, status, reason FROM leave_requests WHERE id = %s AND organization_id = %s",
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
            exists = sqlite_cur.execute("SELECT 1 FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()
            if exists:
                sqlite_cur.execute(
                    """
                    UPDATE leave_requests SET
                        status = ?,
                        reason = ?,
                        leave_type = COALESCE(?, leave_type),
                        start_date = COALESCE(?, start_date),
                        end_date = COALESCE(?, end_date),
                        organization_id = ?,
                        branch_id = COALESCE(?, branch_id)
                    WHERE id = ?
                    """,
                    (
                        row.get("status"),
                        row.get("reason"),
                        row.get("leave_type"),
                        row.get("start_date"),
                        row.get("end_date"),
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
                    INSERT INTO leave_requests (id, user_id, leave_type, start_date, end_date, status, reason, organization_id, branch_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        leave_id,
                        user_id,
                        row.get("leave_type"),
                        str(row.get("start_date")),
                        str(row.get("end_date")),
                        row.get("status"),
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
                       status, reason
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
                exists = sqlite_cur.execute("SELECT 1 FROM leave_requests WHERE id = ?", (lid,)).fetchone()
                if exists:
                    sqlite_cur.execute(
                        """
                        UPDATE leave_requests SET
                            status = ?,
                            reason = ?,
                            leave_type = COALESCE(?, leave_type),
                            start_date = COALESCE(?, start_date),
                            end_date = COALESCE(?, end_date),
                            organization_id = ?,
                            branch_id = COALESCE(?, branch_id)
                        WHERE id = ?
                        """,
                        (
                            r.get("status"),
                            r.get("reason") or "",
                            r.get("leave_type"),
                            r.get("start_date"),
                            r.get("end_date"),
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                            lid,
                        ),
                    )
                else:
                    sqlite_cur.execute(
                        """
                        INSERT INTO leave_requests (id, user_id, leave_type, start_date, end_date, status, reason, organization_id, branch_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            lid,
                            uid,
                            r.get("leave_type") or "casual",
                            r.get("start_date"),
                            r.get("end_date"),
                            r.get("status") or "pending",
                            r.get("reason") or "",
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                        ),
                    )
            # Prune phantom leave requests in SQLite that do not exist in PostgreSQL for this tenant
            sqlite_query = "SELECT id FROM leave_requests WHERE organization_id = ?"
            sq_params = [organization_id]
            if user_id:
                sqlite_query += " AND user_id = ?"
                sq_params.append(user_id)
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
            # Prune phantom overtime in SQLite that do not exist in PostgreSQL for this tenant
            sqlite_query = "SELECT id FROM overtime WHERE organization_id = ?"
            sq_params = [organization_id]
            if user_id:
                sqlite_query += " AND user_id = ?"
                sq_params.append(user_id)
            sqlite_cur.execute(sqlite_query, sq_params)
            cached_ids = [row[0] for row in sqlite_cur.fetchall()]
            for cid in cached_ids:
                if cid not in pg_ids:
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
                       is_prorated, unprorated_basic_salary, proration_days_paid, proration_method_days, proration_explanation
                FROM payroll_records
                WHERE organization_id = %s AND payroll_run_id = %s
                """,
                (organization_id, run_id),
            )
            records = list(cur.fetchall() or [])
        
        with sqlite3.connect(_sqlite_path()) as sqlite_conn:
            sqlite_cur = sqlite_conn.cursor()
            stat = "Finalized" if str(run.get("status") or "").lower() in ("approved", "paid", "finalized") else "Draft"
            sqlite_cur.execute(
                """
                INSERT INTO payroll_runs (
                    id, organization_id, branch_id, payroll_month, period_start, period_end,
                    employee_count, total_basic_salary, total_deductions, total_net_salary, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    total_net_salary = excluded.total_net_salary,
                    employee_count = excluded.employee_count,
                    total_basic_salary = excluded.total_basic_salary,
                    total_deductions = excluded.total_deductions
                """,
                (
                    run_id,
                    organization_id,
                    run.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                    payroll_month,
                    run.get("period_start"),
                    run.get("period_end"),
                    int(run.get("employee_count") or len(records)),
                    float(run.get("total_basic_salary") or 0.0),
                    float(run.get("total_deduction") or 0.0),
                    float(run.get("total_amount") or 0.0),
                    stat,
                ),
            )
            pg_rec_ids = set()
            for rec in records:
                rid = str(rec.get("id"))
                pg_rec_ids.add(rid)
                try:
                    eid = int(str(rec.get("employee_id") or "0"))
                except (ValueError, TypeError):
                    eid = 0
                sqlite_cur.execute(
                    """
                    INSERT INTO payroll_records (
                        id, organization_id, branch_id, payroll_run_id, employee_id,
                        basic_salary, allowances, deductions, net_salary,
                        scheduled_workdays, present_days, absent_days, late_days,
                        approved_leave_days, payable_days,
                        is_prorated, unprorated_basic_salary, proration_days_paid, proration_method_days, proration_explanation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        basic_salary = excluded.basic_salary,
                        allowances = excluded.allowances,
                        deductions = excluded.deductions,
                        net_salary = excluded.net_salary,
                        payable_days = excluded.payable_days,
                        is_prorated = excluded.is_prorated,
                        unprorated_basic_salary = excluded.unprorated_basic_salary,
                        proration_days_paid = excluded.proration_days_paid,
                        proration_method_days = excluded.proration_method_days,
                        proration_explanation = excluded.proration_explanation
                    """,
                    (
                        rid,
                        organization_id,
                        rec.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                        run_id,
                        eid,
                        float(rec.get("basic_salary") or 0.0),
                        float(rec.get("allowances") or 0.0),
                        float(rec.get("deductions") or 0.0),
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
                    ),
                )
            sqlite_cur.execute("SELECT id FROM payroll_records WHERE payroll_run_id = ?", (run_id,))
            for crid_row in sqlite_cur.fetchall():
                if crid_row[0] not in pg_rec_ids:
                    sqlite_cur.execute("DELETE FROM payroll_records WHERE id = ?", (crid_row[0],))
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
                try:
                    aid = int(str(r.get("id") or "").strip())
                except (ValueError, TypeError):
                    continue
                pg_ids.add(aid)
                try:
                    uid = int(str(r.get("employee_id") or "").strip())
                except (ValueError, TypeError):
                    uid = 0
                user_name = "Employee"
                u_row = sqlite_cur.execute("SELECT name FROM users WHERE id = ?", (uid,)).fetchone()
                if u_row and u_row[0]:
                    user_name = u_row[0]
                exists = sqlite_cur.execute("SELECT 1 FROM attendance WHERE id = ?", (aid,)).fetchone()
                if exists:
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
                            r.get("timestamp"),
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
                        INSERT INTO attendance (id, user_id, detected_name, confidence, timestamp, source, location, device_id, organization_id, branch_id)
                        VALUES (?, ?, ?, ?, ?, 'camera', ?, ?, ?, ?)
                        """,
                        (
                            aid,
                            uid,
                            user_name,
                            float(r.get("confidence") or 1.0),
                            r.get("timestamp"),
                            r.get("camera_id") or "Entrance",
                            r.get("camera_id") or "CAM-01",
                            organization_id,
                            r.get("branch_id") or branch_id or "DEFAULT_BRANCH",
                        ),
                    )
            sqlite_cur.execute("SELECT id FROM attendance WHERE organization_id = ? ORDER BY timestamp DESC LIMIT ?", (organization_id, limit))
            cached_ids = [row[0] for row in sqlite_cur.fetchall()]
            for cid in cached_ids:
                if cid not in pg_ids:
                    sqlite_cur.execute("DELETE FROM attendance WHERE id = ? AND organization_id = ?", (cid, organization_id))
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
                ))
            if rec_tuples:
                execute_values(
                    cur,
                    """
                    INSERT INTO payroll_records (
                        id, organization_id, branch_id, payroll_run_id, employee_id,
                        basic_salary, allowances, deductions, net_salary,
                        scheduled_workdays, present_days, absent_days, late_days,
                        approved_leave_days, payable_days,
                        is_prorated, unprorated_basic_salary, proration_days_paid, proration_method_days, proration_explanation
                    ) VALUES %s
                    ON CONFLICT (organization_id, id) DO NOTHING
                    """,
                    rec_tuples,
                    page_size=500,
                )

    return _with_cursor(_write)
