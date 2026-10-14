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

    _with_cursor(_write)


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

    _with_cursor(_write)


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

    _with_cursor(_write)


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
            cur.execute(
                """
                SELECT id, employee_code, name, email, phone, is_active
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
                exists = sqlite_cur.execute(
                    "SELECT 1 FROM users WHERE id = ? AND organization_id = ? AND branch_id = ?",
                    (user_id, org_id, br_id),
                ).fetchone()
                if exists:
                    continue
                sqlite_cur.execute(
                    """
                    INSERT INTO users (
                        id, name, email, phone, notes, active, organization_id, branch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
