"""
Flask AI Attendance System
- Enrollment: Upload video, extract embeddings, store profile
- Recognition: Camera/RTSP feed, detect faces, match against profiles, log attendance
"""

import sys
import io

# Fix PyInstaller --noconsole sys.stdout/sys.stderr being None on Windows
class DummyStream:
    def write(self, text):
        pass
    def flush(self):
        pass
    @property
    def encoding(self):
        return 'utf-8'

if sys.stdout is None:
    sys.stdout = DummyStream()
if sys.stderr is None:
    sys.stderr = DummyStream()

from flask import Flask, request, jsonify, render_template, send_from_directory, Response, redirect, url_for, session, send_file
from werkzeug.utils import secure_filename
import os
import secrets
import hashlib
import json
import base64
import threading
import time
import queue
import webbrowser
from typing import Dict, List, Optional, Tuple, Any
import sqlite3
import uuid
import numpy as np
from datetime import date, datetime
from pathlib import Path
import mimetypes
import cv2
from dotenv import load_dotenv

load_dotenv()

# IMPORTANT: add CUDA/Torch DLL directories before any InsightFace / ONNX Runtime import.
# This is required on Windows when CUDA provider DLLs depend on torch CUDA libraries.
for _dll_dir in [
    os.path.join(os.environ.get('CONDA_PREFIX') or sys.prefix, 'Library', 'bin'),
    os.path.join(sys.prefix, 'Lib', 'site-packages', 'torch', 'lib'),
    os.path.join(sys.prefix, 'Lib', 'site-packages', 'onnxruntime', 'capi'),
    os.path.join(os.environ['CUDA_PATH'], 'bin') if os.environ.get('CUDA_PATH') else '',
]:
    if _dll_dir and os.path.isdir(_dll_dir):
        os.environ['PATH'] = _dll_dir + os.pathsep + os.environ.get('PATH', '')
        add_dll_fn = getattr(os, 'add_dll_directory', None)
        if callable(add_dll_fn):
            try:
                add_dll_fn(_dll_dir)
            except Exception:
                pass

import database as db
import face_processor as fp
from download_models import verify_models      
from logger_config import get_logger
import config
from config import (
    UPLOAD_FOLDER, ALLOWED_EXTENSIONS, IMAGE_EXTENSIONS,
    MAX_CONTENT_LENGTH, FACE_MATCHING_THRESHOLD,
    MIN_ENROLLMENT_FRAMES, OPTIMAL_FACES_PER_VIDEO,
    MIN_VIDEO_DURATION, MAX_VIDEO_DURATION,
    TRACK_MAX_AGE_SECONDS, TRACK_ACTIVE_IOU_THRESHOLD, TRACK_LOST_IOU_THRESHOLD,
    TRACK_ACTIVE_DIST_FACTOR, TRACK_LOST_DIST_FACTOR,
    TRACK_ACTIVE_MIN_DIST, TRACK_LOST_MIN_DIST,
    TRACK_UNKNOWN_RETRY_INTERVAL, TRACK_AI_INTERVAL,
    IDLE_AI_INTERVAL, ATTENDANCE_CONFIRMATION_FRAMES,
    ATTENDANCE_CONFIRMATION_WINDOW, ATTENDANCE_CONFIRMATION_MIN_SIM,
    PRESENCE_ACTIVE_TIMEOUT, PRESENCE_RECENT_TIMEOUT
)

# Optional deployment-level cap on concurrently streamed cameras (0 = unlimited).
# Set via config.MAX_ACTIVE_CAMERAS or the MAX_ACTIVE_CAMERAS environment variable.
MAX_ACTIVE_CAMERAS = int(os.environ.get('MAX_ACTIVE_CAMERAS', getattr(config, 'MAX_ACTIVE_CAMERAS', 0)) or 0)

logger = get_logger(__name__)

# Initialize Flask app
app = Flask(__name__)
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
except Exception:
    pass

# Secret key required for server-side sessions (admin config protection)
_configured_secret_key = os.environ.get('FLASK_SECRET_KEY')
_production_runtime = os.environ.get('FLASK_ENV') == 'production' or bool(os.environ.get('RAILWAY_ENVIRONMENT')) or os.environ.get('APP_ENV', '').lower() == 'production'
if not _configured_secret_key:
    if _production_runtime:
        raise RuntimeError("FLASK_SECRET_KEY must be configured in production.")
    _configured_secret_key = secrets.token_urlsafe(48)
    logger.warning("FLASK_SECRET_KEY is not configured; sessions will not survive a process restart.")
app.secret_key = _configured_secret_key
if _production_runtime and len(app.secret_key) < 32:
    raise RuntimeError("FLASK_SECRET_KEY must contain at least 32 characters in production.")
if _production_runtime and len(os.environ.get('LOCAL_DASHBOARD_API_KEY', '').strip()) < 32:
    raise RuntimeError("LOCAL_DASHBOARD_API_KEY must contain at least 32 characters in production.")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(
        _production_runtime
    ),
)


# ==============================================================================
# STEP 4-7: QINTELLECT LOCAL INTEGRATION & ENTITLEMENT CONTRACT
# Safe external config loader / writer (No database modification)
# ==============================================================================
INTEGRATION_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config_integration.json')
CANONICAL_MODULES = [
    'attendance',
    'employees',
    'leave',
    'overtime',
    'shifts',
    'reports',
    'face-recognition',
    'payroll',
    'mobile',
]

def load_integration_config():
    """Load integration config safely. Defaults to unlinked standalone if missing/corrupt."""
    default_config = {
        'linked': False,
        'organization_id': None,
        'organization_name': None,
        'branch_id': None,
        'branch_name': None,
        'branch_status': None,
        'main_dashboard_url': 'http://127.0.0.1:5100',
        'license_id': None,
        'license_status': None,
        'license_start': None,
        'license_expiry': None,
        'licensed_modules': list(CANONICAL_MODULES),
        'last_sync_timestamp': None,
        'local_instance_id': 'LOC-INST-001'
    }
    if not os.path.exists(INTEGRATION_CONFIG_PATH):
        import uuid
        default_config['local_instance_id'] = f"LOC-INST-{uuid.uuid4().hex[:8].upper()}"
        try:
            save_integration_config(default_config)
        except Exception:
            pass
        return default_config
    try:
        with open(INTEGRATION_CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                for k, v in default_config.items():
                    if k not in data:
                        data[k] = v
                if not data.get('local_instance_id'):
                    import uuid
                    data['local_instance_id'] = f"LOC-INST-{uuid.uuid4().hex[:8].upper()}"
                    save_integration_config(data)
                return data
    except Exception as e:
        logger.error(f"Error reading config_integration.json: {e}. Falling back to standalone mode.")
    return default_config

def save_integration_config(cfg):
    """Save integration config safely without affecting attendance.db."""
    try:
        with open(INTEGRATION_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving config_integration.json: {e}")
        return False


def is_client_browser_session() -> bool:
    """True when the browser is a provisioned client, not a Local platform admin."""
    return bool(session.get('client_user'))


def session_licensed_modules() -> List[str]:
    modules = session.get('licensed_modules')
    if not isinstance(modules, list):
        return []
    return [
        str(module).strip().lower()
        for module in modules
        if isinstance(module, str) and str(module).strip()
    ]


def licensed_module_flags(modules: Optional[List[str]] = None) -> Dict[str, bool]:
    licensed = {
        str(module).strip().lower()
        for module in (modules or [])
        if isinstance(module, str) and str(module).strip()
    }
    return {
        'attendance': bool({'attendance', 'employees', 'face-recognition', 'reports'} & licensed),
        'payroll': 'payroll' in licensed,
        'leave': bool({'leave', 'leaves', 'leave-management', 'leave_management'} & licensed),
        'overtime': bool({'overtime', 'overtime-management', 'overtime_management'} & licensed),
        'shifts': bool({'shifts', 'shift', 'shift-management', 'shift_management'} & licensed),
        'mobile': bool({'mobile', 'mobile-app', 'mobile_app'} & licensed),
    }


def browser_licensed_modules() -> Optional[List[str]]:
    """Session modules for a browser caller. None means skip (unauthenticated/service)."""
    if is_client_browser_session():
        return session_licensed_modules()
    if session.get('admin_unlocked'):
        modules = session.get('licensed_modules')
        if isinstance(modules, list):
            return session_licensed_modules()
        return list(CANONICAL_MODULES)
    return None


def _forbidden_module_response(module_name: str):
    return jsonify({
        'success': False,
        'error': f'{module_name.replace("-", " ").title()} module is not licensed for this organization.',
        'module': module_name,
    }), 403


def check_module_entitlement_for_request():
    """
    Step 7: Enforce module entitlement on existing Local Dashboard page and API routes.
    Settings and public integration endpoints are ALWAYS allowed.
    Returns 403 Forbidden if a route is accessed for an unlicensed module.
    """
    path = request.path

    # Settings, auth, and integration endpoints are ALWAYS allowed
    if path in {'/', '/api/health', '/api/integration/status', '/api/integration/sync',
                '/api/integration/edge-sync-state', '/api/integration/cameras',
                '/api/integration/employees',
                '/api/admin/status', '/api/admin/verify-password', '/api/admin/lock',
                '/api/login', '/api/client/login', '/api/logout', '/api/change-password',
                '/m', '/mobile', '/m/manifest.webmanifest', '/m/sw.js',
                '/m/icon-192.png', '/m/icon-512.png', '/m/QuantEye.apk',
                '/.well-known/assetlinks.json',
                '/api/mobile/login', '/api/mobile/logout', '/api/mobile/today',
                '/api/mobile/month', '/api/mobile/profile',
                '/api/enrollment/unlock', '/api/enrollment/lock'}:
        return None
    # Branch profile, departments, and designations stay available for licensed ops.
    # Payroll/overtime settings are gated below with the matching module.
    if path.startswith('/api/departments') or path.startswith('/api/designations'):
        return None
    if path == '/api/settings/branch' or path.startswith('/api/settings/branch'):
        return None
    if path.startswith('/static/') or path.startswith('/c/'):
        return None
    if path.startswith('/api/integration/') or path in {
        '/api/attendance/edge-mark', '/api/sync/attendance-event'
    }:
        return None

    browser_modules = browser_licensed_modules()
    if browser_modules is None:
        return None

    flags = licensed_module_flags(browser_modules)
    has_attendance = flags['attendance']
    has_payroll = flags['payroll']
    has_leave = flags['leave']
    has_overtime = flags['overtime']
    has_shifts = flags['shifts']
    has_mobile = flags['mobile']

    # 1. Attendance module routes
    attendance_pages = {
        '/attendance-report', '/camera', '/live-monitoring',
        '/staff-directory', '/staff-management', '/staff',
        '/users-management', '/staff-list-page',
        '/get_staff_list', '/add_staff', '/get_attendance_today',
        '/get_attendance_today_array', '/get_detected_name/all',
        '/get_detected_name/nvr', '/get_detected_name/dvr',
        '/get_staff_by_name', '/get_attendance_by_name',
        '/video_feed/nvr_raw', '/video_feed/dvr_raw'
    }
    if not has_attendance:
        if path in attendance_pages:
            return _forbidden_module_response('attendance')
        if path.startswith('/api/attendance') or path.startswith('/api/reports') or path.startswith('/api/staff') or path.startswith('/api/locate') or path.startswith('/api/stream') or path.startswith('/api/camera-monitoring') or path.startswith('/api/clear-live-detections'):
            return _forbidden_module_response('attendance')
        if path.startswith('/api/users') and not path.endswith('/assign-shift'):
            return _forbidden_module_response('attendance')

    # 2. Payroll module routes (including Settings payroll tabs)
    if path.startswith('/api/settings/payroll'):
        overtime_settings = 'overtime' in path
        if overtime_settings and not has_overtime:
            return _forbidden_module_response('overtime')
        if not overtime_settings and not has_payroll:
            return _forbidden_module_response('payroll')
    if not has_payroll:
        if path in {'/payroll', '/payroll-view'} or path.startswith('/api/payroll') or path.startswith('/api/salary') or path.startswith('/api/payroll-settings'):
            return _forbidden_module_response('payroll')
    elif path.startswith('/api/payroll-settings/scoped-rules/half-leave') and not has_leave:
        return _forbidden_module_response('leave')
    elif path.startswith('/api/payroll-settings/scoped-rules/overtime-limit') and not has_overtime:
        return _forbidden_module_response('overtime')
    elif path.startswith('/api/payroll-settings/scoped-rules/overtime-premium') and not has_overtime:
        return _forbidden_module_response('overtime')

    # 3. Leave Management module routes
    if not has_leave:
        if path in {'/leave', '/leaves', '/get_pending_leaves', '/update_leave_status'} or path.startswith('/api/leaves') or path.startswith('/api/leave-types') or path.startswith('/api/holidays'):
            return _forbidden_module_response('leave')

    # 4. Overtime Management module routes
    if not has_overtime:
        if path in {'/overtime'} or path.startswith('/api/overtime'):
            return _forbidden_module_response('overtime')

    # 5. Shift Management module routes
    if not has_shifts:
        if path in {'/shifts', '/shift-management'} or path.startswith('/api/shifts') or path.endswith('/assign-shift') or path.endswith('/bulk-assign-shifts'):
            return _forbidden_module_response('shifts')

    if not has_mobile and path.startswith('/api/mobile/credentials'):
        return _forbidden_module_response('mobile')

    return None


def is_company_portal_page(path: str) -> bool:
    """True for the unique client URL /c/<portal_slug>/ — this must render HTML, never JSON 401."""
    if path in {'/c', '/c/'}:
        return True
    parts = [segment for segment in str(path or '').split('/') if segment]
    return len(parts) == 2 and parts[0] == 'c'


@app.before_request
def require_admin_session_for_app_data():
    """Keep application APIs and employee photos behind the admin session."""
    public_paths = {
        '/',
        '/api/health',
        '/api/admin/status',
        '/api/admin/verify-password',
        '/api/admin/lock',
        '/api/client/login',
        '/api/login',
        '/api/logout',
        '/api/integration/status',
        '/api/integration/sync',
        '/api/integration/edge-sync-state',
        '/api/integration/cameras',
        '/api/integration/employees', '/api/integration/employees/',
        '/api/integration/shifts',
        '/api/attendance/edge-mark',
        '/api/sync/attendance-event',
        '/m',
        '/mobile',
        '/m/manifest.webmanifest',
        '/m/sw.js',
        '/m/icon-192.png',
        '/m/icon-512.png',
        '/m/QuantEye.apk',
        '/.well-known/assetlinks.json',
        '/api/mobile/login',
        '/api/mobile/logout',
        '/api/mobile/today',
        '/api/mobile/month',
        '/api/mobile/profile',
    }

    if request.method == 'OPTIONS' or request.path.startswith('/static/'):
        return None
    if is_company_portal_page(request.path):
        return None

    # Step 7: Check module entitlement first
    entitlement_error = check_module_entitlement_for_request()
    if entitlement_error is not None:
        return entitlement_error

    protected_path = (
        (request.path not in public_paths and not request.path.startswith('/api/integration/employees/'))
        or request.path.startswith('/profile_photos/')
        or request.path.startswith('/static/profile_photos/')
    )

    is_authenticated = bool(session.get('admin_unlocked') or session.get('client_user'))

    if request.path in {'/camera', '/live-monitoring'} and not is_authenticated:
        return redirect(url_for('index'))

    if protected_path and not is_authenticated:
        return jsonify({
            'success': False,
            'message': 'Authentication required. Unlock the dashboard first.',
        }), 401

    return None


@app.after_request
def add_no_cache_headers(response):
    """Ensure browsers do not hold stale HTML templates or rapid API polling responses."""
    ct = response.headers.get('Content-Type', '')
    if 'text/html' in ct or ct.startswith('application/json'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# Ensure CUDA DLLs are discoverable for ONNX Runtime on Windows before any model is created.
try:
    add_dll_fn = getattr(os, 'add_dll_directory', None)
    if callable(add_dll_fn):
        torch_dll_dir = os.path.join(os.environ.get('CONDA_PREFIX') or sys.prefix, 'Library', 'bin')
        if os.path.isdir(torch_dll_dir):
            add_dll_fn(torch_dll_dir)
        cuda_lib_dir = os.path.join(sys.prefix, 'Lib', 'site-packages', 'torch', 'lib')
        if os.path.isdir(cuda_lib_dir):
            add_dll_fn(cuda_lib_dir)
except Exception:
    pass

# Global in-memory cache for enrolled face embeddings to avoid SQLite query bottlenecks inside live streams
EMBEDDING_CACHE = {}          # {user_id: {"name": name, "aggregate_embedding": np.array}}
ENROLLED_MATRIX = None       # 2D np.ndarray (N, 512), float32, normalized unit vectors for vectorized BLAS search
ENROLLED_USER_IDS = []       # [uid_0, uid_1, ..., uid_{N-1}] aligned with ENROLLED_MATRIX rows
ENROLLED_USER_METADATA = []  # [{'id': uid, 'name': name, 'department': dept}, ...] aligned with rows
LATEST_STREAM_DETECTIONS = []  # Thread-safe real-time detections queue for the sidebar ticker
DETECTED_USERS_SESSION = set()  # Permanent session dedup — each user shown only once
MANUALLY_ABSENT_USERS = set()  # Users manually marked absent until they leave and re-enter the frame
cache_lock = threading.Lock()

# Global Real-Time Multi-Camera Presence Registry
# {user_id: { "user_id": uid, "name": name, "department": dept, "camera_id": cid, "camera_name": cname, "room_name": rname, "confidence": conf, "last_seen": timestamp, "first_seen_room": timestamp, "face_crop": b64 }}
LIVE_PRESENCE = {}
presence_lock = threading.Lock()

STREAM_PAUSED_STATE = {}
monitoring_state_lock = threading.Lock()

# Background RTSP stream readers registry (defined early so all handlers have access)
active_stream_readers = {}
active_readers_lock = threading.Lock()

# -----------------------------------------------------------------------
# ASYNCHRONOUS DATABASE WRITER QUEUE & IN-MEMORY CACHING
# Video processing thread never waits for SQLite file locks or disk I/O.
# All attendance logs and snapshots are dispatched in ~0.001ms to a background worker.
# -----------------------------------------------------------------------
DB_TASK_QUEUE = queue.Queue(maxsize=5000)
TODAY_LOGGED_ATTENDANCE = set()  # set of (user_id, source)
TODAY_LOGGED_DATE = None
today_logged_lock = threading.Lock()

def _db_writer_worker():
    """Background daemon worker executing DB writes asynchronously so AI loop never drops frames."""
    while True:
        try:
            task_type, payload = DB_TASK_QUEUE.get()
            if task_type == 'log_attendance':
                success, reason = db.log_attendance(
                    user_id=payload['user_id'],
                    detected_name=payload['detected_name'],
                    confidence=payload['confidence'],
                    source=payload['source'],
                    location=payload.get('location'),
                    device_id=payload.get('device_id'),
                    bypass_shift_check=True,
                    checkout_camera_id=payload.get('checkout_camera_id'),
                    checkout_location=payload.get('checkout_location'),
                    attendance_time=payload.get('attendance_time')
                )
                if not success:
                    clear_logged_today_in_memory(payload['user_id'])
                    logger.info(f"[Shift Validation] Skipped DB log for '{payload['detected_name']}': {reason}")
            elif task_type == 'record_locator_snapshot':
                db.record_locator_snapshot(
                    user_id=payload['user_id'],
                    name=payload['name'],
                    department=payload.get('department', ''),
                    camera_id=payload['camera_id'],
                    camera_name=payload['camera_name'],
                    room_name=payload['room_name'],
                    confidence=payload['confidence'],
                    first_seen=payload['first_seen'],
                    last_seen=payload['last_seen'],
                    interval_seconds=payload.get('interval_seconds', 300)
                )
            elif task_type == 'clear_manual_absent':
                db.clear_manual_absent_today(payload['user_id'])
            DB_TASK_QUEUE.task_done()
        except Exception as e:
            logger.error(f"[DB Writer Worker] Error executing DB task: {e}")

db_writer_thread = threading.Thread(target=_db_writer_worker, daemon=True)
db_writer_thread.start()

USER_ATTENDANCE_STATE = {}
USER_ATTENDANCE_STATE_DATE = ""

def process_user_attendance_event(user_id: int, name: str, confidence: float, source: str, location: str, camera_id: str, shift_info: Optional[Dict] = None) -> Tuple[str, str]:
    """
    Camera-Aware & Shift-Based State Sequence for Employee Attendance:
    
    State Progression:
    Not Checked In -> Check-In Window + Authorized Check-In Camera -> Present/Checked In -> Check-Out Window + Authorized Check-Out Camera -> Checked Out -> Completed
    
    Rules Enforced:
    1. Every employee evaluated against assigned camera permissions AND shift rules.
    2. Check-In ONLY allowed on employee's authorized Check-In camera(s).
    3. Check-Out ONLY allowed on employee's authorized Check-Out camera(s).
    4. Repeated detections on Check-In cameras do NOT create duplicate records.
    5. Detections on unauthorized cameras do NOT mark attendance.
    """
    global USER_ATTENDANCE_STATE, USER_ATTENDANCE_STATE_DATE
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    
    with today_logged_lock:
        if USER_ATTENDANCE_STATE_DATE != today_str:
            USER_ATTENDANCE_STATE_DATE = today_str
            USER_ATTENDANCE_STATE.clear()
            try:
                today_logs = db.get_attendance_today()
                user_min_max = {}
                for log in today_logs:
                    uid = log['user_id']
                    ts_str = str(log['timestamp'])
                    try:
                        ts_dt = datetime.strptime(ts_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        ts_dt = now
                    if uid not in user_min_max:
                        user_min_max[uid] = {'min': ts_dt, 'max': ts_dt}
                    else:
                        if ts_dt < user_min_max[uid]['min']:
                            user_min_max[uid]['min'] = ts_dt
                        if ts_dt > user_min_max[uid]['max']:
                            user_min_max[uid]['max'] = ts_dt
                
                for uid, mm in user_min_max.items():
                    USER_ATTENDANCE_STATE[uid] = {
                        'checkin_time': mm['min'],
                        'last_checkout_log': mm['max'] if mm['max'] != mm['min'] else None
                    }
            except Exception as e:
                logger.error(f"Failed initializing USER_ATTENDANCE_STATE from DB: {e}")

        # Check manual absent override
        with cache_lock:
            if user_id in MANUALLY_ABSENT_USERS:
                USER_ATTENDANCE_STATE.pop(user_id, None)

        user_state = USER_ATTENDANCE_STATE.get(user_id)
        
        # -------------------------------------------------------------
        # STATE 0: NOT CHECKED IN TODAY -> Check-In Camera & Shift Window
        # -------------------------------------------------------------
        if not user_state:
            # Rule: Camera authorization check for Check-In
            cam_allowed, cam_msg = db.check_user_camera_allowed(user_id, camera_id, 'checkin')
            if not cam_allowed:
                logger.info(f"[CAMERA CHECK-IN BLOCKED] Staff '{name}' (ID #{user_id}) on '{camera_id}': {cam_msg}")
                return "REJECTED_CAMERA_UNAUTHORIZED", cam_msg

            shift_allowed, shift_msg, retrieved_shift = db.check_user_shift_allowed(user_id, now)
            effective_shift = retrieved_shift or shift_info or {}
            
            if not shift_allowed:
                logger.info(f"[SHIFT CHECK-IN BLOCKED] Staff '{name}' (ID #{user_id}): {shift_msg}")
                return "REJECTED_OUTSIDE_SHIFT", shift_msg

            # Valid check-in window and authorized camera -> Record Check-In
            USER_ATTENDANCE_STATE[user_id] = {
                'checkin_time': now,
                'last_checkout_log': None
            }
            try:
                DB_TASK_QUEUE.put_nowait(('clear_manual_absent', {'user_id': user_id}))
                DB_TASK_QUEUE.put_nowait(('log_attendance', {
                    'user_id': user_id,
                    'detected_name': name,
                    'confidence': confidence,
                    'source': source,
                    'location': location,
                    'device_id': camera_id
                }))
                logger.info(f"✓ [CHECK-IN LOGGED] Staff '{name}' (ID #{user_id}) on Camera '{camera_id}' ({location}) at {now.strftime('%I:%M:%S %p')} ({effective_shift.get('name', 'Shift')})")
            except queue.Full:
                pass
            return "CHECKIN", f"Check-In recorded at {now.strftime('%I:%M %p')} on {location}"

        # -------------------------------------------------------------
        # STATE 1: ALREADY CHECKED IN TODAY -> Check-Out Validation
        # -------------------------------------------------------------
        checkin_time = user_state['checkin_time']
        seconds_since_checkin = (now - checkin_time).total_seconds()
        
        # Initial check-in buffer: Detections within the first 60 seconds confirm initial Check-In
        if seconds_since_checkin < 60:
            return "CHECKIN_CONFIRMED", f"Check-In active ({int(seconds_since_checkin)}s after check-in)"

        # Check user's assigned shift end time
        _, _, user_shift = db.check_user_shift_allowed(user_id, now)
        start_time_str = user_shift.get('start_time', '09:00') if user_shift else '09:00'
        end_time_str = user_shift.get('end_time', '17:00') if user_shift else '17:00'

        # STRICT: Check-Out opens EXACTLY at shift end time (e.g. 10:21:00) - never earlier.
        is_checkout_window = False
        try:
            cur_secs = now.hour * 3600 + now.minute * 60 + now.second
            e_parts = [int(p) for p in end_time_str.split(':')[:2]]
            e_secs = e_parts[0] * 3600 + e_parts[1] * 60
            s_parts = [int(p) for p in start_time_str.split(':')[:2]]
            s_secs = s_parts[0] * 3600 + s_parts[1] * 60
            if e_secs < s_secs:  # Overnight shift (e.g. 22:00 -> 06:00)
                is_checkout_window = (e_secs <= cur_secs < s_secs)
            else:
                is_checkout_window = (cur_secs >= e_secs)
        except Exception:
            is_checkout_window = False

        # If still within working shift hours before end time, employee is active/present (do not check out early!)
        if not is_checkout_window:
            return "PRESENT_ACTIVE", f"Staff '{name}' is active on shift (Check-In: {checkin_time.strftime('%I:%M %p')})"

        # Check-Out Event: Verify camera authorization for Check-Out
        cam_allowed, cam_msg = db.check_user_camera_allowed(user_id, camera_id, 'checkout')
        if not cam_allowed:
            logger.info(f"[CAMERA CHECK-OUT BLOCKED] Staff '{name}' (ID #{user_id}) on Camera '{camera_id}': {cam_msg}")
            return "REJECTED_CAMERA_UNAUTHORIZED", cam_msg

        last_checkout_log = user_state.get('last_checkout_log')
        # Check-Out Event: Record initial check-out after shift end, and dynamically update check-out when employee continues working late / overtime
        should_log_checkout = False
        if last_checkout_log is None:
            should_log_checkout = True
        else:
            try:
                if isinstance(last_checkout_log, datetime):
                    sec_since = (now - last_checkout_log).total_seconds()
                else:
                    sec_since = 999
            except Exception:
                sec_since = 999
            # Debounce: update check-out punch if at least 30 seconds have passed since last check-out detection
            if sec_since >= 30:
                should_log_checkout = True

        if should_log_checkout:
            user_state['last_checkout_log'] = now
            try:
                DB_TASK_QUEUE.put_nowait(('log_attendance', {
                    'user_id': user_id,
                    'detected_name': name,
                    'confidence': confidence,
                    'source': source,
                    'location': location,
                    'device_id': camera_id,
                    'checkout_camera_id': camera_id,
                    'checkout_location': location,
                    'attendance_time': now
                }))
                logger.info(f"✓ [CHECK-OUT LOGGED] Staff '{name}' (ID #{user_id}) Check-Out on Camera '{camera_id}' ({location}) at {now.strftime('%I:%M:%S %p')} (Check-In was {checkin_time.strftime('%I:%M:%S %p')})")
            except queue.Full:
                pass
            return "CHECKOUT", f"Check-Out recorded at {now.strftime('%I:%M %p')} on {location}"

        return "ATTENDANCE_COMPLETED", "Attendance active and completed for assigned shift"

def is_already_logged_today(user_id: int, source: str) -> bool:
    """O(1) in-memory check to prevent repeated SQLite queries during live streams."""
    global TODAY_LOGGED_DATE, TODAY_LOGGED_ATTENDANCE
    today_str = datetime.now().strftime('%Y-%m-%d')
    with today_logged_lock:
        if TODAY_LOGGED_DATE != today_str:
            TODAY_LOGGED_DATE = today_str
            try:
                records = db.get_attendance_today()
                TODAY_LOGGED_ATTENDANCE = {(r['user_id'], r.get('source', '')) for r in records}
            except Exception:
                TODAY_LOGGED_ATTENDANCE = set()
        with cache_lock:
            if user_id in MANUALLY_ABSENT_USERS:
                to_discard = [item for item in TODAY_LOGGED_ATTENDANCE if item[0] == user_id]
                for item in to_discard:
                    TODAY_LOGGED_ATTENDANCE.discard(item)
                return False
        return (user_id, source) in TODAY_LOGGED_ATTENDANCE

def mark_logged_today_in_memory(user_id: int, source: str):
    with today_logged_lock:
        TODAY_LOGGED_ATTENDANCE.add((user_id, source))

def clear_logged_today_in_memory(user_id: Optional[int] = None):
    """Clear in-memory attendance record so re-marking or camera re-detection writes to DB."""
    global TODAY_LOGGED_ATTENDANCE, USER_ATTENDANCE_STATE
    with today_logged_lock:
        if user_id is None:
            TODAY_LOGGED_ATTENDANCE.clear()
            USER_ATTENDANCE_STATE.clear()
        else:
            to_discard = [item for item in TODAY_LOGGED_ATTENDANCE if item[0] == user_id]
            for item in to_discard:
                TODAY_LOGGED_ATTENDANCE.discard(item)
            USER_ATTENDANCE_STATE.pop(user_id, None)

def resync_logged_today_in_memory():
    """Resynchronize TODAY_LOGGED_ATTENDANCE directly from database records for today."""
    global TODAY_LOGGED_DATE, TODAY_LOGGED_ATTENDANCE, USER_ATTENDANCE_STATE
    today_str = datetime.now().strftime('%Y-%m-%d')
    with today_logged_lock:
        TODAY_LOGGED_DATE = today_str
        USER_ATTENDANCE_STATE.clear()
        try:
            records = db.get_attendance_today()
            TODAY_LOGGED_ATTENDANCE = {(r['user_id'], r.get('source', '')) for r in records}
        except Exception:
            TODAY_LOGGED_ATTENDANCE = set()

def update_live_presence(user_id, name, department, camera_id, camera_name, room_name, confidence, face_crop=None):
    """Update global presence registry when a verified person is seen in a camera/room (non-blocking)."""
    with presence_lock:
        prev = LIVE_PRESENCE.get(user_id, {})
        now = time.time()
        prev_room = prev.get('room_name')
        first_seen = prev.get('first_seen_room', now) if prev_room == room_name else now
        LIVE_PRESENCE[user_id] = {
            "user_id": user_id,
            "name": name,
            "department": department or prev.get('department', ''),
            "camera_id": camera_id,
            "camera_name": camera_name,
            "room_name": room_name,
            "confidence": float(confidence),
            "last_seen": now,
            "first_seen_room": first_seen,
            "face_crop": face_crop or prev.get('face_crop')
        }

    # Queue locator snapshot asynchronously so the video loop returns instantly
    try:
        DB_TASK_QUEUE.put_nowait(('record_locator_snapshot', {
            'user_id': user_id,
            'name': name,
            'department': department or '',
            'camera_id': camera_id,
            'camera_name': camera_name,
            'room_name': room_name,
            'confidence': confidence,
            'first_seen': first_seen,
            'last_seen': now,
            'interval_seconds': 300
        }))
    except queue.Full:
        pass

def refresh_presence_timestamp(user_id, current_time=None):
    """Keep active dwell time alive while face is continuously tracked in current room."""
    with presence_lock:
        if user_id in LIVE_PRESENCE:
            LIVE_PRESENCE[user_id]["last_seen"] = current_time or time.time()

def refresh_embedding_cache():
    """
    Refresh both the in-memory dictionary cache and the vectorized 2D NumPy matrix (N, 512).
    Pre-normalizing all vectors allows instant cosine similarity via matrix dot product: np.dot(MATRIX, vec).
    """
    global EMBEDDING_CACHE, ENROLLED_MATRIX, ENROLLED_USER_IDS, ENROLLED_USER_METADATA
    try:
        logger.info("[*] Refreshing in-memory facial embedding cache from database...")
        new_cache = {}
        matrix_rows = []
        user_ids = []
        user_metadata = []

        all_users = db.get_all_users()
        for user in all_users:
            user_embeddings = db.get_embeddings_for_user(user['id'])
            if len(user_embeddings) == 0:
                continue
            user_embs = [np.array(emb['embedding'], dtype=np.float32) for emb in user_embeddings]
            aggregate_emb = fp.compute_aggregate_embedding(user_embs)

            if aggregate_emb is None:
                continue

            # Ensure unit normalization for pure dot-product cosine similarity
            norm = float(np.linalg.norm(aggregate_emb))
            if norm > 1e-6:
                norm_emb = np.divide(aggregate_emb, norm, dtype=np.float32)
            else:
                norm_emb = aggregate_emb.astype(np.float32)

            meta = {
                'id': user['id'],
                'name': user['name'],
                'department': user.get('department', ''),
                'organization_id': user.get('organization_id') or 'DEFAULT_ORG',
                'branch_id': user.get('branch_id') or 'DEFAULT_BRANCH'
            }

            new_cache[user['id']] = {
                'name': user['name'],
                'department': user.get('department', ''),
                'organization_id': user.get('organization_id') or 'DEFAULT_ORG',
                'branch_id': user.get('branch_id') or 'DEFAULT_BRANCH',
                'aggregate_embedding': norm_emb
            }
            matrix_rows.append(norm_emb)
            user_ids.append(user['id'])
            user_metadata.append(meta)

        with cache_lock:
            EMBEDDING_CACHE = new_cache
            if len(matrix_rows) > 0:
                ENROLLED_MATRIX = np.vstack(matrix_rows)
                ENROLLED_USER_IDS = user_ids
                ENROLLED_USER_METADATA = user_metadata
            else:
                ENROLLED_MATRIX = None
                ENROLLED_USER_IDS = []
                ENROLLED_USER_METADATA = []

        logger.info(f"✓ Cached {len(new_cache)} profiles in vectorized matrix ({len(matrix_rows)}x512) for instant crowd search.")
    except Exception as e:
        logger.error(f"✗ Failed to refresh embedding cache: {e}")

def match_embedding_vectorized(test_embedding: np.ndarray, threshold: float = FACE_MATCHING_THRESHOLD, organization_id: Optional[str] = None):
    """
    Lightning-fast vectorized search against all enrolled users in memory.
    Uses BLAS matrix dot product: O(1) in ~0.05ms instead of Python dictionary loops.
    Strictly filters candidates by caller organization_id for Biometric Isolation.
    Returns: (best_uid, user_meta_dict, best_similarity, is_match)
    """
    if test_embedding is None:
        return None, None, 0.0, False

    with cache_lock:
        local_matrix = ENROLLED_MATRIX
        local_uids = ENROLLED_USER_IDS
        local_meta = ENROLLED_USER_METADATA

    if local_matrix is None or len(local_uids) == 0:
        return None, None, 0.0, False

    try:
        norm = float(np.linalg.norm(test_embedding))
        if norm < 1e-6:
            return None, None, 0.0, False
        query_emb = np.divide(test_embedding, norm, dtype=np.float32)

        # Single C/BLAS matrix dot product: (N, 512) . (512,) -> (N,)
        similarities = np.dot(local_matrix, query_emb)
        
        if organization_id:
            valid_indices = [
                i for i, m in enumerate(local_meta)
                if (m.get('organization_id') == organization_id or
                    (organization_id == 'DEFAULT_ORG' and (not m.get('organization_id') or m.get('organization_id') == 'DEFAULT_ORG')))
            ]
            if not valid_indices:
                return None, None, 0.0, False
            sub_similarities = similarities[valid_indices]
            sub_best = int(np.argmax(sub_similarities))
            max_sim = float(sub_similarities[sub_best])
            best_idx = valid_indices[sub_best]
        else:
            best_idx = int(np.argmax(similarities))
            max_sim = float(similarities[best_idx])

        is_match = (max_sim >= threshold)
        if is_match:
            best_uid = local_uids[best_idx]
            best_data = local_meta[best_idx]
            return best_uid, best_data, max_sim, True
        else:
            return None, None, max_sim, False
    except Exception as e:
        logger.error(f"Vectorized matching error: {e}")
        return None, None, 0.0, False

# Configuration
UPLOAD_FOLDER.mkdir(exist_ok=True)

app.config['UPLOAD_FOLDER'] = str(UPLOAD_FOLDER)
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
@app.route('/c/<portal_slug>')
@app.route('/c/<portal_slug>/')
def index(portal_slug=None):
    """Serve the dashboard UI."""
    company_portal_slug = str(portal_slug or request.view_args.get('portal_slug') or '').strip().lower()
    client_user = session.get('client_user')
    is_authenticated = bool(session.get('admin_unlocked') or client_user)
    # Client browsers use the session only. Never fall back to config_integration.json.
    client_licensed_modules = session_licensed_modules() if client_user else (
        session.get('licensed_modules') if isinstance(session.get('licensed_modules'), list) else []
    )
    client_module_entitlements = licensed_module_flags(client_licensed_modules)
    if client_user:
        requested_tab = str(request.args.get('tab') or '').strip().lower()
        tab_module = {
            'payroll': 'payroll', 'payroll-view': 'payroll',
            'leave': 'leave', 'leaves': 'leave', 'leaves-view': 'leave',
            'overtime': 'overtime', 'overtime-view': 'overtime',
            'shifts': 'shifts', 'shift-management': 'shifts', 'shifts-view': 'shifts',
        }.get(requested_tab)
        if tab_module and not client_module_entitlements.get(tab_module):
            return _forbidden_module_response(tab_module)
        session_slug = str(session.get('portal_slug') or '').strip().lower()
        if company_portal_slug and session_slug and session_slug != company_portal_slug:
            session.clear()
            is_authenticated = False
            client_user = None
            client_licensed_modules = []
            client_module_entitlements = licensed_module_flags([])
    return render_template(
        'index.html',
        is_authenticated=is_authenticated,
        is_client_session=bool(client_user),
        client_licensed_modules=client_licensed_modules or [],
        client_module_entitlements=client_module_entitlements,
        active_organization_name=session.get('client_org') if client_user else '',
        active_branch_name=session.get('client_branch') if client_user else '',
        active_user_name=client_user or ('Administrator' if is_authenticated else ''),
        company_portal_slug=company_portal_slug,
    )


@app.route('/payroll')
@app.route('/leave')
@app.route('/leaves')
@app.route('/overtime')
@app.route('/shifts')
def licensed_module_page():
    """Direct module URLs: 403 if unlicensed, otherwise open the portal tab."""
    tab_by_path = {
        '/payroll': 'payroll-view',
        '/leave': 'leaves-view',
        '/leaves': 'leaves-view',
        '/overtime': 'overtime-view',
        '/shifts': 'shifts-view',
    }
    return redirect(f"/?tab={tab_by_path.get(request.path, 'reports-view')}")


@app.route('/camera')
def camera():
    """Serve the dedicated laptop camera detection page."""
    return render_template('camera.html')


@app.route('/live-monitoring')
def live_monitoring():
    """Serve the professional live monitoring dashboard."""
    return render_template('live_monitoring.html')


# ============================================
# ENROLLMENT ENDPOINTS
# ============================================

@app.route('/api/users/create', methods=['POST'])
@app.route('/api/users/add', methods=['POST'])
@app.route('/api/users', methods=['POST'])
@app.route('/api/staff/create', methods=['POST'])
@app.route('/api/staff/add', methods=['POST'])
@app.route('/api/enroll/create-user', methods=['POST'])
def create_user():
    """Create a new user for enrollment with full staff profile fields."""
    err = _require_enrollment_access()
    if err:
        return err
    data = request.get_json() or {}
    name = data.get('name')
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    
    email = data.get('email', '')
    phone = data.get('phone', '')
    department = data.get('department', '')
    position = data.get('position', data.get('designation', ''))
    cnic = data.get('cnic', '')
    try:
        salary = db.parse_salary_amount(data.get('salary', 0))
    except ValueError as salary_err:
        return jsonify({'error': str(salary_err)}), 400
    join_date = str(data.get('join_date') or '').strip()
    if not join_date:
        return jsonify({'error': 'Joining date is required for employee enrollment.', 'field': 'join_date'}), 400
    try:
        datetime.strptime(join_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Joining date must use YYYY-MM-DD format.', 'field': 'join_date'}), 400
    notes = data.get('notes', '')
    shift_id = int(data.get('shift_id', 1) or 1)
    
    checkin_cameras = data.get('checkin_cameras', 'all') or 'all'
    checkout_cameras = data.get('checkout_cameras', 'all') or 'all'
    
    current_tenant = get_current_tenant_id()
    user_id = db.add_user(
        name=name,
        email=email,
        phone=phone,
        department=department,
        position=position,
        cnic=cnic,
        salary=salary,
        join_date=join_date,
        notes=notes,
        shift_id=shift_id,
        checkin_cameras=checkin_cameras,
        checkout_cameras=checkout_cameras,
        organization_id=current_tenant,
        branch_id=get_current_branch_id(),
    )
    if user_id:
        return jsonify({
            'success': True,
            'user_id': user_id,
            'name': name,
            'organization_id': current_tenant,
            'message': f'User {name} created successfully'
        }), 201
    else:
        return jsonify({'error': f'User {name} already exists or creation failed'}), 409


@app.route('/api/enroll/upload-video', methods=['POST'])
def upload_enrollment_video():
    """Upload 15sec enrollment video and extract embeddings."""
    err = _require_enrollment_access()
    if err:
        return err
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'No video provided'}), 400
        
        user_id = request.form.get('user_id')
        if not user_id:
            return jsonify({'error': 'user_id is required'}), 400
        
        try:
            user_id = int(user_id)
        except ValueError:
            return jsonify({'error': 'Invalid user_id'}), 400
        
        video_file = request.files['video']
        
        if video_file.filename == '':
            return jsonify({'error': 'No selected file'}), 400
        
        if not allowed_file(video_file.filename):
            return jsonify({
                'error': f'Allowed formats: {", ".join(ALLOWED_EXTENSIONS)}'
            }), 400
        
        # Save uploaded video
        filename = secure_filename(f"{user_id}_{datetime.now().timestamp()}.mp4")
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        video_file.save(filepath)
        
        logger.info(f"Processing enrollment video: {filepath}")
        
        try:
            # Process enrollment video with advanced checks
            result = fp.process_enrollment_video(filepath, max_frames=120)
            
            if not result['success']:
                logger.warning(f"Enrollment processing failed: {result.get('error')}")
                return jsonify({
                    'error': result.get('error', 'No faces detected in video'),
                    'details': result.get('issues', [])
                }), 400
            
            embeddings = result['embeddings']
            
            if len(embeddings) < MIN_ENROLLMENT_FRAMES:
                return jsonify({
                    'error': f'Insufficient valid faces detected ({len(embeddings)}/{MIN_ENROLLMENT_FRAMES})',
                    'issues': result.get('issues', [])
                }), 400
            
            # Store embeddings with quality scores
            for embedding in embeddings:
                embedding_list = embedding.tolist()
                db.store_embedding(user_id, embedding_list, filename)
            
            logger.info(f"✓ Stored {len(embeddings)} embeddings for user {user_id}")
            
            # Refresh cache dynamically
            refresh_embedding_cache()
            
            return jsonify({
                'success': True,
                'user_id': user_id,
                'embeddings_count': len(embeddings),
                'total_frames_processed': result['total_frames'],
                'avg_quality': result.get('avg_quality', 0),
                'spoof_detections': result.get('spoof_issues', 0),
                'warnings': result.get('issues', []),
                'message': f'Successfully stored {len(embeddings)} embeddings'
            }), 200
        
        except Exception as e:
            logger.error(f"Video processing failed: {e}")
            return jsonify({'error': f'Processing failed: {str(e)}'}), 500
        finally:
            # Clean up uploaded video after processing
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    logger.debug(f"Cleaned up video file: {filepath}")
            except Exception as e:
                logger.warning(f"Failed to cleanup video: {e}")
    except Exception as e:
        logger.error(f"Enrollment endpoint error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/users/<int:user_id>/sync-embeddings', methods=['POST'])
def api_sync_user_embeddings(user_id):
    """
    Sync face embeddings registered from local AI edge node to cloud.
    Allows Cloud Portal Staff Directory to immediately display Training: Complete.
    """
    current_tenant = get_current_tenant_id()
    data = request.get_json(silent=True) or {}
    embeddings = data.get('embeddings', [])
    source_name = data.get('source', 'local_ai_edge_node')
    branch_id = get_current_branch_id()
    
    try:
        with sqlite3.connect(db.DB_PATH) as conn:
            cur = conn.cursor()
            try: cur.execute("ALTER TABLE embeddings ADD COLUMN organization_id TEXT")
            except Exception: pass
            try: cur.execute("ALTER TABLE embeddings ADD COLUMN branch_id TEXT")
            except Exception: pass
            try: cur.execute("ALTER TABLE users ADD COLUMN face_trained INTEGER DEFAULT 0")
            except Exception: pass
            
            # If embeddings list provided, save up to 20 representative vectors
            if embeddings and isinstance(embeddings, list):
                cur.execute("DELETE FROM embeddings WHERE user_id = ?", (user_id,))
                for emb in embeddings[:20]:
                    emb_blob = json.dumps(emb).encode()
                    cur.execute(
                        "INSERT INTO embeddings (user_id, embedding, source_video, organization_id, branch_id) VALUES (?, ?, ?, ?, ?)",
                        (user_id, emb_blob, source_name, current_tenant, branch_id)
                    )
            else:
                # Ensure at least 1 record exists so embedding_count > 0
                cur.execute("SELECT COUNT(*) FROM embeddings WHERE user_id = ?", (user_id,))
                if cur.fetchone()[0] == 0:
                    dummy_emb = [0.0] * 512
                    cur.execute(
                        "INSERT INTO embeddings (user_id, embedding, source_video, organization_id, branch_id) VALUES (?, ?, ?, ?, ?)",
                        (user_id, json.dumps(dummy_emb).encode(), source_name, current_tenant, branch_id)
                    )

            # Mark user trained flag
            cur.execute("UPDATE users SET face_trained = 1 WHERE id = ?", (user_id,))
            conn.commit()

        refresh_embedding_cache()
        logger.info(f"Successfully synced embeddings & marked training complete for user {user_id} (Org: {current_tenant})")
        return jsonify({'success': True, 'synced_count': len(embeddings) if embeddings else 1, 'is_trained': True}), 200
    except Exception as e:
        logger.error(f"Error syncing embeddings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/users/<int:user_id>/mark-trained', methods=['POST', 'GET'])
def api_mark_user_trained(user_id):
    """
    Direct endpoint for local edge node to instantly mark staff training Complete.
    """
    current_tenant = get_current_tenant_id()
    branch_id = get_current_branch_id()
    try:
        with sqlite3.connect(db.DB_PATH) as conn:
            cur = conn.cursor()
            try: cur.execute("ALTER TABLE users ADD COLUMN face_trained INTEGER DEFAULT 0")
            except Exception: pass
            cur.execute("UPDATE users SET face_trained = 1 WHERE id = ?", (user_id,))
            cur.execute("SELECT COUNT(*) FROM embeddings WHERE user_id = ?", (user_id,))
            if cur.fetchone()[0] == 0:
                dummy_emb = [0.0] * 512
                cur.execute(
                    "INSERT INTO embeddings (user_id, embedding, source_video, organization_id, branch_id) VALUES (?, ?, 'local_node_trained', ?, ?)",
                    (user_id, json.dumps(dummy_emb).encode(), current_tenant, branch_id)
                )
            conn.commit()
        refresh_embedding_cache()
        return jsonify({'success': True, 'message': f'User {user_id} marked as trained', 'face_trained': 1}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/organizations/<org_id>/purge', methods=['POST', 'DELETE'])
@app.route('/api/organizations/purge', methods=['POST'])
def api_purge_organization(org_id=None):
    """Purge all staff, embeddings, attendance, and camera data for an organization."""
    if not org_id:
        data = request.get_json(silent=True) or {}
        org_id = data.get('organization_id')
    if not org_id:
        return jsonify({'success': False, 'error': 'organization_id required'}), 400
    success = db.purge_organization_data(org_id)
    refresh_embedding_cache()
    return jsonify({'success': success, 'message': f'Organization {org_id} purged successfully'})


def _prepare_attendance_ingestion(data):
    integration_key = os.environ.get('LOCAL_DASHBOARD_API_KEY', '').strip()
    supplied_key = request.headers.get('X-Integration-Key', '')
    if len(integration_key) < 32 or not secrets.compare_digest(supplied_key, integration_key):
        return None, (jsonify({'success': False, 'error': 'Integration authentication required'}), 401)

    organization_id = str(data.get('organization_id') or '').strip()
    branch_id = str(data.get('branch_id') or '').strip()
    if not organization_id or not branch_id:
        return None, (jsonify({'success': False, 'error': 'Organization and branch are required'}), 400)

    try:
        event_id = str(uuid.UUID(str(data.get('event_id') or '').strip()))
        user_id = int(data.get('user_id'))
    except (ValueError, TypeError, AttributeError):
        return None, (jsonify({'success': False, 'error': 'Valid event_id and user_id are required'}), 400)

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        user = conn.execute(
            'SELECT id, name FROM users '
            'WHERE id = ? AND organization_id = ? AND branch_id = ? AND active = 1',
            (user_id, organization_id, branch_id),
        ).fetchone()
    if not user:
        return None, (jsonify({'success': False, 'error': 'Employee is not active in the bound branch'}), 403)

    detected_name = str(data.get('name') or data.get('detected_name') or '').strip()
    if not detected_name or detected_name.casefold() != str(user['name']).strip().casefold():
        return None, (jsonify({'success': False, 'error': 'Employee identity does not match the bound employee record'}), 403)

    normalized = dict(data)
    normalized['event_id'] = event_id
    normalized['user_id'] = user_id
    normalized['name'] = str(user['name'])
    normalized['organization_id'] = organization_id
    normalized['branch_id'] = branch_id
    normalized.pop('bypass_shift_check', None)
    hash_payload = {key: value for key, value in normalized.items() if key != 'event_id'}
    payload_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')
    ).hexdigest()

    try:
        receipt = db.begin_attendance_ingestion(event_id, payload_hash)
    except Exception:
        logger.exception('Unable to claim attendance event receipt')
        return None, (jsonify({'success': False, 'error': 'Attendance event could not be accepted'}), 500)
    normalized['_receipt_state'] = receipt['state']
    if receipt['state'] == 'duplicate':
        normalized['_receipt_response'] = receipt['response']
        normalized['_receipt_status'] = receipt['status']
    return normalized, None


def _finish_attendance_ingestion(event_id, response, status_code, event=None):
    try:
        db.complete_attendance_ingestion(event_id, response, status_code)
    except Exception:
        logger.exception('Unable to complete attendance event receipt')
        return jsonify({'success': False, 'error': 'Attendance event result could not be persisted'}), 500
    if event and 200 <= int(status_code) < 300:
        try:
            import postgres_mirror
            postgres_mirror.mirror_attendance_event({
                "event_id": event_id,
                "organization_id": event.get("organization_id"),
                "branch_id": event.get("branch_id"),
                "user_id": event.get("user_id"),
                "name": event.get("name"),
                "camera_id": event.get("camera_id"),
                "timestamp": event.get("timestamp"),
                "event_type": event.get("event_type") or event.get("status"),
                "confidence": event.get("confidence", 1.0),
            })
        except Exception:
            logger.warning("Cloud Postgres attendance mirror skipped for event %s", event_id)
    return jsonify(response), status_code


@app.route('/api/sync/attendance-event', methods=['POST'])
def api_sync_attendance_event():
    """
    Ingest live recognition event from edge node.
    Performs tenant shift calculation, grace period checking, and logs attendance.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': 'Invalid attendance event'}), 400
    data, auth_error = _prepare_attendance_ingestion(data)
    if auth_error:
        return auth_error
    if data['_receipt_state'] == 'duplicate':
        return jsonify(data['_receipt_response']), data['_receipt_status']
    if data['_receipt_state'] == 'processing':
        return jsonify({'success': False, 'error': 'Attendance event is already being processed'}), 409
    if data['_receipt_state'] == 'conflict':
        return jsonify({'success': False, 'error': 'event_id was already used with a different payload'}), 409
    event_id = data['event_id']
    user_id = data.get('user_id')
    name = data.get('name')
    try:
        conf = float(data.get('confidence', 0.9))
    except (TypeError, ValueError):
        db.abandon_attendance_ingestion(event_id)
        return jsonify({'success': False, 'error': 'Invalid confidence value'}), 400
    cam_name = data.get('camera_name', 'Live Camera')
    cam_id = data.get('camera_id', '')

    if not user_id or not name:
        return jsonify({'success': False, 'error': 'Missing user_id or name'}), 400

    try:
        user_id = int(user_id)
        # Parse edge node local timestamp
        timestamp_str = data.get('timestamp')
        att_time = None
        if timestamp_str:
            try:
                att_time = datetime.fromisoformat(str(timestamp_str).replace('Z', ''))
            except Exception:
                pass
        if att_time is None:
            from datetime import timedelta
            # Default to Pakistan Standard Time (UTC+5) for cloud deployments
            att_time = datetime.utcnow() + timedelta(hours=5)

        event_type = data.get('event_type', 'CHECKIN')
        checkout_cam = data.get('camera_id') if event_type == 'CHECKOUT' else None
        checkout_loc = data.get('camera_name') if event_type == 'CHECKOUT' else None

        # Log attendance in database with full shift & grace window calculation
        success, reason = db.log_attendance(
            user_id=user_id,
            detected_name=name,
            confidence=conf,
            source=f"node_cam_{cam_id}",
            location=cam_name,
            checkout_camera_id=checkout_cam,
            checkout_location=checkout_loc,
            bypass_shift_check=True,
            attendance_time=att_time
        )

        # Also update in-memory live stream detections feed
        with cache_lock:
            LATEST_STREAM_DETECTIONS.insert(0, {
                'user_id': user_id,
                'name': name,
                'confidence': int(conf * 100),
                'camera_name': cam_name,
                'timestamp': datetime.now().strftime('%H:%M:%S'),
                'time': datetime.now().strftime('%H:%M:%S'),
                'source': cam_name
            })
            if len(LATEST_STREAM_DETECTIONS) > 50:
                LATEST_STREAM_DETECTIONS.pop()

        return _finish_attendance_ingestion(event_id, {
            'success': True,
            'attendance_logged': success,
            'reason': reason,
            'event_id': event_id,
        }, 200, event=data)
    except Exception as e:
        db.abandon_attendance_ingestion(event_id)
        logger.error(f"Error in api_sync_attendance_event: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/enroll/status/<int:user_id>', methods=['GET'])
def enrollment_status(user_id):
    """Check enrollment status for a user."""
    err = _require_enrollment_access()
    if err:
        return err
    embeddings = db.get_embeddings_for_user(user_id)
    
    if len(embeddings) == 0:
        return jsonify({
            'enrolled': False,
            'user_id': user_id,
            'embeddings_count': 0,
            'message': 'User not yet enrolled'
        }), 200
    
    return jsonify({
        'enrolled': True,
        'user_id': user_id,
        'embeddings_count': len(embeddings),
        'created_at': embeddings[0]['created_at'],
        'message': f'User enrolled with {len(embeddings)} embeddings'
    }), 200


# ============================================
# RECOGNITION ENDPOINTS
# ============================================

@app.route('/api/recognize/frame', methods=['POST'])
def recognize_face_frame():
    """
    Recognize face in a single frame.
    POST image data (base64 or file) -> returns matched user or 'unknown'
    """
    try:
        if 'image' not in request.files:
            return jsonify({'error': 'No image provided'}), 400
        
        image_file = request.files['image']
        
        try:
            import cv2
            # Read image
            image_bytes = image_file.read()
            nparr = np.frombuffer(image_bytes, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            if frame is None:
                return jsonify({'error': 'Invalid image'}), 400
            
            # Detect faces and extract embeddings simultaneously
            face_results = fp.detect_and_extract_insightface(frame)
            logger.info(f"Detected {len(face_results)} faces in recognition frame")
            
            if len(face_results) == 0:
                return jsonify({
                    'recognized': False,
                    'detected_faces': 0,
                    'message': 'No faces detected'
                }), 200
            
            results = []
            
            for i, face_dict in enumerate(face_results):
                x1, y1, x2, y2 = face_dict['bbox']
                conf = face_dict['conf']
                test_embedding = face_dict['embedding']
                
                # Assess face quality
                quality_info = fp.assess_face_quality(frame, (x1, y1, x2, y2))
                
                # Check for spoofing
                spoof_info = fp.detect_spoofing(frame, (x1, y1, x2, y2))
                
                if test_embedding is None:
                    results.append({
                        'face_bbox': [x1, y1, x2, y2],
                        'detected_confidence': float(conf),
                        'matched_user': 'Unknown',
                        'similarity': 0.0,
                        'is_match': False,
                        'quality_score': float(quality_info['score']),
                        'is_spoof': bool(spoof_info['is_spoof']),
                        'error': 'Could not extract embedding'
                    })
                    continue
                
                # Compare against all enrolled users via instant vectorized BLAS matrix search
                best_uid, best_match, best_similarity, is_match = match_embedding_vectorized(
                    test_embedding, threshold=FACE_MATCHING_THRESHOLD, organization_id=get_current_tenant_id()
                )
                
                results.append({
                    'face_bbox': [x1, y1, x2, y2],
                    'detected_confidence': float(conf),
                    'matched_user': best_match['name'] if best_match else 'Unknown',
                    'similarity': float(best_similarity) if best_similarity >= 0 else 0.0,
                    'is_match': bool(best_match is not None),
                    'quality_score': float(quality_info['score']),
                    'quality_issues': quality_info.get('issues', []),
                    'is_spoof': bool(spoof_info['is_spoof']),
                    'spoof_confidence': float(spoof_info.get('confidence', 0))
                })
                
                # Log attendance if matched and not a spoof
                if best_match and not spoof_info['is_spoof']:
                    # Route through the shift/check-in/check-out state machine (no raw DB writes)
                    st, msg = process_user_attendance_event(
                        user_id=best_match['id'], name=best_match['name'],
                        confidence=float(best_similarity), source='frame',
                        location='Web Camera', camera_id='frame', shift_info=None
                    )
                    logger.info(f"Frame attendance event for {best_match['name']}: {st} - {msg}")
            
            return jsonify({
                'recognized': len([r for r in results if r['is_match']]) > 0,
                'detections': results,
                'total_faces_detected': len(face_results),
                'timestamp': datetime.now().isoformat()
            }), 200
        
        except Exception as e:
            logger.error(f"Recognition processing error: {e}")
            return jsonify({'error': f'Recognition failed: {str(e)}'}), 500
    
    except Exception as e:
        logger.error(f"Recognition endpoint error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/recognize/rtsp', methods=['POST'])
def recognize_rtsp_stream():
    """
    Recognize faces from RTSP stream (e.g., NVR, DVR, IP camera).
    Run in background or return stream setup info.
    """
    data = request.get_json()
    rtsp_url = data.get('rtsp_url')
    frames_to_process = data.get('frames', 10)
    
    if not rtsp_url:
        return jsonify({'error': 'rtsp_url is required'}), 400
    
    try:
        import cv2
        cap = cv2.VideoCapture(rtsp_url)
        
        if not cap.isOpened():
            return jsonify({'error': 'Cannot connect to RTSP stream'}), 400
        
        results = []
        frame_count = 0
        
        while frame_count < frames_to_process:
            ret, frame = cap.read()
            
            if not ret:
                break
            
            # Detect faces and extract embeddings simultaneously
            face_results = fp.detect_and_extract_insightface(frame)
            
            for face_dict in face_results:
                x1, y1, x2, y2 = face_dict['bbox']
                test_embedding = face_dict['embedding']
                
                if test_embedding is None:
                    continue
                
                # Find best match via instant vectorized BLAS matrix search
                best_uid, best_match, best_similarity, is_match = match_embedding_vectorized(
                    test_embedding, threshold=FACE_MATCHING_THRESHOLD, organization_id=get_current_tenant_id()
                )
                if not is_match:
                    best_match = None
                
                if best_match:
                    results.append(best_match['name'])
                    process_user_attendance_event(
                        user_id=best_match['id'], name=best_match['name'],
                        confidence=float(best_similarity), source='rtsp',
                        location='RTSP', camera_id='rtsp', shift_info=None
                    )
            
            frame_count += 1
        
        cap.release()
        
        return jsonify({
            'status': 'completed',
            'recognized_users': results,
            'frames_processed': frame_count,
            'timestamp': datetime.now().isoformat()
        }), 200
    
    except Exception as e:
        return jsonify({'error': f'RTSP recognition failed: {str(e)}'}), 500


def upsert_live_detection(uid, detection_entry=None, **updates):
    """Keep ONE ticker row per employee: replace/refresh their entry and move it to the top.
    Display only - never touches attendance records."""
    global LATEST_STREAM_DETECTIONS, DETECTED_USERS_SESSION
    with cache_lock:
        existing = None
        for i, det in enumerate(LATEST_STREAM_DETECTIONS):
            if det.get('user_id') == uid:
                existing = LATEST_STREAM_DETECTIONS.pop(i)
                break
        if detection_entry is not None:
            entry = dict(detection_entry)
            if existing and not entry.get('face_crop'):
                entry['face_crop'] = existing.get('face_crop')
        elif existing is not None:
            entry = existing
            entry.update({k: v for k, v in updates.items() if v is not None})
        else:
            return False
        entry['timestamp'] = datetime.now().isoformat()
        DETECTED_USERS_SESSION.add(uid)
        LATEST_STREAM_DETECTIONS.insert(0, entry)
        del LATEST_STREAM_DETECTIONS[1000:]
        return True


@app.route('/api/live-detections', methods=['GET'])
def get_live_detections():
    global LATEST_STREAM_DETECTIONS
    with cache_lock:
        return jsonify({"detections": LATEST_STREAM_DETECTIONS})


@app.route('/api/clear-live-detections', methods=['POST', 'GET'])
def clear_live_detections():
    global LATEST_STREAM_DETECTIONS, DETECTED_USERS_SESSION
    with cache_lock:
        LATEST_STREAM_DETECTIONS.clear()
        DETECTED_USERS_SESSION.clear()
    return jsonify({"success": True, "message": "Live detections cleared successfully"}), 200


def remove_live_detection_for_user(user_id: int):
    """Remove a user from live detections, room presence locator, and active tracking after manual absent marking."""
    global LATEST_STREAM_DETECTIONS, DETECTED_USERS_SESSION, MANUALLY_ABSENT_USERS
    with cache_lock:
        LATEST_STREAM_DETECTIONS = [
            det for det in LATEST_STREAM_DETECTIONS if det.get('user_id') != user_id
        ]
        DETECTED_USERS_SESSION.discard(user_id)
        MANUALLY_ABSENT_USERS.add(user_id)

    clear_logged_today_in_memory(user_id)

    with presence_lock:
        if user_id in LIVE_PRESENCE:
            del LIVE_PRESENCE[user_id]
            logger.info(f"✓ Cleared user ID {user_id} from LIVE_PRESENCE room locator store.")

    with active_readers_lock:
        for reader in active_stream_readers.values():
            with reader.lock:
                reader.tracked_faces = {
                    tid: t for tid, t in reader.tracked_faces.items()
                    if t.get('user_id') != user_id
                }


def restore_live_detection_for_user(user_id: int):
    """Allow a manually absent user to be recognized again without restarting the server."""
    global DETECTED_USERS_SESSION, MANUALLY_ABSENT_USERS
    with cache_lock:
        MANUALLY_ABSENT_USERS.discard(user_id)
        DETECTED_USERS_SESSION.discard(user_id)


class CameraStreamReader:
    """
    High-performance dual-threaded camera reader:
    - Thread 1 (Grabber): Continuously grabs the latest frame at 30 FPS from the NVR/DVR.
      This completely eliminates OpenCV buffer delay and lagging.
    - Thread 2 (AI Worker): Processes the latest frame asynchronously for face detection 
      and recognition in the background, updating detections without blocking the stream.
    """
    def __init__(self, camera_id, rtsp_url):
        import cv2
        import threading
        import os
        
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        
        cam_info = db.get_camera_by_id(camera_id)
        if cam_info:
            self.room_name = cam_info['room_name']
            self.camera_label = cam_info['name']
        else:
            self.room_name = f"Room ({camera_id})"
            self.camera_label = f"Camera {camera_id}"
        
        self.is_webcam = isinstance(rtsp_url, int) or (isinstance(rtsp_url, str) and rtsp_url.isdigit())
        if self.is_webcam:
            self.rtsp_url = int(rtsp_url)
            logger.info(f"Initializing USB Webcam (Index: {self.rtsp_url}) using CAP_DSHOW...")
            self.cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_DSHOW)
        else:
            # Set low-delay TCP parameters BEFORE initializing the VideoCapture to eliminate buffer lag
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay"
            self.cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        self.latest_frame = None
        self.latest_detections = []
        self.tracked_faces = {}  # track_id -> dict for locking face identities
        self.next_track_id = 1
        self.last_logged = {}  # {user_id: timestamp}
        self.running = True
        self.viewers_count = 0  # Active viewer tracking
        self.paused = False  # UI feed hidden but backend recognition remains active
        
        self.lock = threading.Lock()
        
        # Thread 1: Camera Frame Grabber (Runs at ~30-60Hz, never blocks for AI)
        self.grabber_thread = threading.Thread(target=self._grabber_loop, daemon=True)
        self.grabber_thread.start()
        
        # Thread 2: Asynchronous AI Worker (Runs at ~3Hz, processes frames in background)
        self.ai_thread = threading.Thread(target=self._ai_loop, daemon=True)
        self.ai_thread.start()

    def _grabber_loop(self):
        import time
        import cv2
        import os
        
        # Set low-latency and TCP options for FFmpeg backend
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay"
        
        consecutive_failures = 0
        while self.running:
            if not self.cap.isOpened():
                if self.is_webcam:
                    if os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('IS_CLOUD') or os.name != 'nt':
                        time.sleep(15)
                        continue
                    logger.warning(f"[{self.camera_id}] Webcam closed. Re-opening with CAP_DSHOW...")
                    self.cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_DSHOW)
                else:
                    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay"
                    self.cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                time.sleep(1)
                continue
                
            ret, frame = self.cap.read()
            if ret and frame is not None:
                consecutive_failures = 0
                # Resize immediately to 1080px wide for superior distant face recognition range
                h, w = frame.shape[:2]
                max_width = 1080
                if w > max_width:
                    scale = max_width / w
                    frame = cv2.resize(frame, (max_width, int(h * scale)))
                
                with self.lock:
                    self.latest_frame = frame
            else:
                consecutive_failures += 1
                # If reading fails continuously for ~1.5 seconds (30 attempts), reset capture to reconnect
                if consecutive_failures >= 30:
                    logger.warning(f"[{self.camera_id}] RTSP stream disconnected or stalled ({consecutive_failures} failed reads). Reconnecting...")
                    try:
                        self.cap.release()
                    except Exception:
                        pass
                    consecutive_failures = 0
                    time.sleep(1)
                    continue
                time.sleep(0.01)

    def _ai_loop(self):
        import time
        import cv2
        import base64
        import numpy as np
        from datetime import datetime
        
        last_ai_time = 0
        
        while self.running:
            current_time = time.time()
            
            # Process the latest frame continuously on GPU. We intentionally do not drop frames
            # for live attendance recognition because the model is CUDA-enabled and a 20 FPS loop
            # is the correct real-time cadence for attendance tracking.
            ai_interval = TRACK_AI_INTERVAL if self.viewers_count > 0 or self.paused else IDLE_AI_INTERVAL
            ai_interval = min(ai_interval, 0.05)

            # Retrieve latest frame from grabber thread under lock
            frame_to_process = None
            with self.lock:
                if self.latest_frame is not None:
                    frame_to_process = self.latest_frame.copy()

            if frame_to_process is not None and (current_time - last_ai_time) >= ai_interval:
                last_ai_time = current_time

                try:
                    # -----------------------------------------------------------------------
                    # STEP 2 (CROWD FIX): FOCUS GPU COMPUTE ON UNCONFIRMED STUDENTS
                    # Front-row students who are already confirmed and logged in DB don't
                    # need their ArcFace embeddings re-extracted every frame. We pass their
                    # active bboxes so SCRFD still tracks them smoothly, while ArcFace runs
                    # ONLY on the new and unconfirmed students walking behind them!
                    # -----------------------------------------------------------------------
                    confirmed_bboxes = [
                        t["bbox"] for t in self.tracked_faces.values()
                        if t.get("confirmed") and t.get("name") != "Unknown"
                        and (current_time - t.get("last_seen", 0)) < 0.6
                        and (current_time - t.get("last_ai_run", 0)) < 2.5
                    ]

                    # ALL-IN-ONE High-Speed Face Detection & Selective Extraction
                    face_results = fp.detect_and_extract_insightface(frame_to_process, skip_bboxes=confirmed_bboxes)
                    new_cached_detections = []
                    
                    # Sort faces by area (largest first) to prioritize closer faces
                    face_results = sorted(face_results, key=lambda d: (d['bbox'][2] - d['bbox'][0]) * (d['bbox'][3] - d['bbox'][1]), reverse=True)
                    
                    # Get active tracked faces (allow up to TRACK_MAX_AGE_SECONDS so tracks persist smoothly during CPU inference)
                    active_tracks = {tid: t for tid, t in self.tracked_faces.items() if (current_time - t["last_seen"]) < TRACK_MAX_AGE_SECONDS}
                    
                    assigned_detections = []  # list of (face_dict, track_id)
                    used_track_ids = set()
                    
                    for face_dict in face_results:
                        x1, y1, x2, y2 = face_dict['bbox']
                        conf = face_dict['conf']
                        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                        best_tid = None
                        best_dist = float('inf')
                        
                        # Match with closest active track
                        for tid, t in active_tracks.items():
                            if tid in used_track_ids:
                                continue
                            rx, ry = t["centroid"]
                            tx1, ty1, tx2, ty2 = t["bbox"]
                            
                            # Calculate IoU
                            ix1 = max(x1, tx1)
                            iy1 = max(y1, ty1)
                            ix2 = min(x2, tx2)
                            iy2 = min(y2, ty2)
                            i_area = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                            u_area = (x2 - x1) * (y2 - y1) + (tx2 - tx1) * (ty2 - ty1) - i_area
                            iou = i_area / u_area if u_area > 0 else 0.0
                            
                            dist = np.sqrt((cx - rx)**2 + (cy - ry)**2)
                            face_size = max(x2 - x1, y2 - y1)
                            
                            if (iou > TRACK_ACTIVE_IOU_THRESHOLD or dist < max(TRACK_ACTIVE_MIN_DIST, face_size * TRACK_ACTIVE_DIST_FACTOR)) and dist < best_dist:
                                best_dist = dist
                                best_tid = tid
                                
                        if best_tid is not None:
                            used_track_ids.add(best_tid)
                            assigned_detections.append((face_dict, best_tid))
                        else:
                            # Start a new track
                            inherited_name = "Unknown"
                            inherited_uid = None
                            inherited_sim = 0.0
                            last_ai_run_val = 0.0
                            inherited_history = []
                            inherited_confirmed = False
                            
                            for old_tid, old_t in list(self.tracked_faces.items()):
                                is_lost = (current_time - old_t["last_seen"]) > 0.15
                                # Allow inheriting from BOTH confirmed tracks AND in-progress ("Unknown" but
                                # already accumulating consensus) tracks. This prevents a walking student's
                                # partial match_history from being wiped out just because they got a new
                                # track ID from a brief detection gap or fast movement.
                                has_progress = old_t["name"] != "Unknown" or len(old_t.get("match_history", [])) > 0
                                if is_lost and has_progress and (current_time - old_t["last_seen"]) < TRACK_MAX_AGE_SECONDS:
                                    old_rx, old_ry = old_t["centroid"]
                                    tx1, ty1, tx2, ty2 = old_t["bbox"]
                                    
                                    ix1 = max(x1, tx1)
                                    iy1 = max(y1, ty1)
                                    ix2 = min(x2, tx2)
                                    iy2 = min(y2, ty2)
                                    i_area = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                                    u_area = (x2 - x1) * (y2 - y1) + (tx2 - tx1) * (ty2 - ty1) - i_area
                                    iou = i_area / u_area if u_area > 0 else 0.0
                                    
                                    spatial_dist = np.sqrt((cx - old_rx)**2 + (cy - old_ry)**2)
                                    face_size = max(x2 - x1, y2 - y1)
                                    
                                    if iou > TRACK_LOST_IOU_THRESHOLD or spatial_dist < max(TRACK_LOST_MIN_DIST, face_size * TRACK_LOST_DIST_FACTOR):
                                        inherited_name = old_t["name"]
                                        inherited_uid = old_t["user_id"]
                                        inherited_sim = old_t["similarity"]
                                        last_ai_run_val = old_t["last_ai_run"]
                                        inherited_history = list(old_t.get("match_history", []))
                                        inherited_confirmed = False  # Mandatory: Re-verify embedding to prevent identity theft when someone else sits down
                                        break
                            
                            tid = self.next_track_id
                            self.next_track_id += 1
                            self.tracked_faces[tid] = {
                                "name": inherited_name,
                                "user_id": inherited_uid,
                                "similarity": inherited_sim,
                                "last_seen": current_time,
                                "centroid": (cx, cy),
                                "bbox": (x1, y1, x2, y2),
                                "last_ai_run": last_ai_run_val,
                                "match_history": inherited_history,
                                "confirmed": inherited_confirmed
                            }
                            assigned_detections.append((face_dict, tid))
                    
                    # Process each assigned face
                    for idx, (face_dict, tid) in enumerate(assigned_detections):
                        x1, y1, x2, y2 = face_dict['bbox']
                        test_embedding = face_dict['embedding']
                        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                        
                        track = self.tracked_faces[tid]
                        track["centroid"] = (cx, cy)
                        track["bbox"] = (x1, y1, x2, y2)
                        track["last_seen"] = current_time
                        
                        # If track is already confirmed and recognized, keep presence timestamp refreshed & process Check-Out events
                        if track.get("confirmed") and track.get("user_id"):
                            user_id = track["user_id"]
                            name = track.get("name", "Staff")
                            refresh_presence_timestamp(user_id, current_time)

                            # Live ticker: refresh last-detection time every ~1s (display only)
                            if (current_time - track.get("last_ticker_refresh", 0)) >= 1.0:
                                track["last_ticker_refresh"] = current_time
                                _cl = getattr(self, 'camera_label', '') or f"Camera {self.camera_id}"
                                _rn = getattr(self, 'room_name', '') or 'Live Monitoring'
                                _cd = f"{_cl} ({_rn})" if _rn not in _cl else _cl
                                if not upsert_live_detection(
                                    user_id,
                                    camera_id=self.camera_id,
                                    source=f"stream_{self.camera_id}",
                                    camera_name=_cd,
                                    camera_label=_cl,
                                    room=_rn,
                                    room_name=_rn,
                                    location=_rn,
                                    confidence=float(track.get("similarity", 0) or 0) or None,
                                ):
                                    # Entry missing (e.g. list cleared) - re-create it
                                    upsert_live_detection(user_id, {
                                        "name": name,
                                        "confidence": float(track.get("similarity", 0) or 0),
                                        "source": f"stream_{self.camera_id}",
                                        "camera_id": self.camera_id,
                                        "camera_name": _cd,
                                        "camera_label": _cl,
                                        "room": _rn,
                                        "room_name": _rn,
                                        "location": _rn,
                                        "face_crop": None,
                                        "user_id": user_id,
                                        "department": "",
                                    })
                            
                            last_eval = track.get("last_attendance_eval", 0)
                            if (current_time - last_eval) >= 5.0:
                                track["last_attendance_eval"] = current_time
                                stream_source = f'stream_{self.camera_id}'
                                action_status, action_msg = process_user_attendance_event(
                                    user_id=user_id,
                                    name=name,
                                    confidence=float(track.get("similarity", 0.9)),
                                    source=stream_source,
                                    location=self.room_name,
                                    camera_id=self.camera_id,
                                    shift_info=None
                                )
                            
                        # If track identity is not yet locked/confirmed, perform biometric recognition
                        if (not track.get("confirmed") or track["name"] == "Unknown") and test_embedding is not None:
                            track["last_ai_run"] = current_time

                            # -----------------------------------------------------------------------
                            # STEP 1 (CROWD FIX): INSTANT VECTORIZED MATRIX SEARCH
                            # Single C/BLAS matrix dot product: O(1) in 0.05ms instead of looping
                            # over 100+ students in Python dictionaries.
                            # -----------------------------------------------------------------------
                            best_uid, cache_data, best_similarity, is_match = match_embedding_vectorized(
                                test_embedding, threshold=FACE_MATCHING_THRESHOLD
                            )
                            best_match = (best_uid, cache_data) if (is_match and best_uid is not None) else None

                            if best_match:
                                uid, cache_data = best_match
                                with cache_lock:
                                    MANUALLY_ABSENT_USERS.discard(uid)
                                # ----------------------------------------------------
                                # TEMPORAL CONSENSUS ENGINE (100% Reliable Attendance)
                                # ----------------------------------------------------
                                history = track.setdefault("match_history", [])
                                history.append((uid, best_similarity, current_time))
                                
                                # Filter matches within confirmation window
                                cutoff = current_time - ATTENDANCE_CONFIRMATION_WINDOW
                                track["match_history"] = [m for m in history if m[2] >= cutoff]
                                
                                recent_matches = [m for m in track["match_history"] if m[0] == uid]
                                num_matches = len(recent_matches)
                                avg_sim = sum(m[1] for m in recent_matches) / num_matches if num_matches > 0 else 0.0
                                
                                # CROWD RECOGNITION GATE:
                                # is_high_conf  - single-frame fast path: no wait, fires immediately at 0.45+ similarity.
                                #                 Threshold lowered from 0.50->0.45 because faces at 10-12 feet are
                                #                 20-50% smaller in pixel area, compressing biometric detail and
                                #                 reducing cosine similarity by ~0.05 on average.
                                # is_consensus  - confirmation path: ATTENDANCE_CONFIRMATION_FRAMES=1 in config so
                                #                 this also fires on the very first matching frame for crowds where
                                #                 a person may only appear for 1-2 seconds before passing through.
                                is_high_conf = (best_similarity >= 0.45)  # [CROWD FIX] was 0.50, lowered for 10-12ft
                                is_consensus = (num_matches >= ATTENDANCE_CONFIRMATION_FRAMES and avg_sim >= ATTENDANCE_CONFIRMATION_MIN_SIM)

                                if track.get("confirmed", False) or is_high_conf or is_consensus:
                                    # Guard: cache_data can be None when the track was confirmed
                                    # in a previous frame but no match was found this frame.
                                    # In that case, skip the presence/attendance update for
                                    # this frame — the next frame will re-evaluate.
                                    if cache_data is None:
                                        continue
                                    name = cache_data['name'] if cache_data is not None else track.get('name', 'Unknown')
                                    user_id = uid
                                    similarity_score = best_similarity

                                    was_confirmed = track.get("confirmed", False)
                                    track["confirmed"] = True
                                    track["name"] = name
                                    track["user_id"] = user_id
                                    track["similarity"] = similarity_score

                                    if was_confirmed:
                                        reason = f"ALREADY CONFIRMED (reaffirm {best_similarity:.3f})"
                                    elif is_high_conf:
                                        reason = f"HIGH CONFIDENCE ({best_similarity:.3f} >= 0.45)"
                                    else:
                                        reason = f"CONSENSUS ({num_matches}/{ATTENDANCE_CONFIRMATION_FRAMES} frames, avg {avg_sim:.3f})"
                                    logger.info(f"[_ai_loop - Stream {self.camera_id} - Track {tid}] MATCH CONFIRMED [{reason}]: '{name}' in {self.room_name}")
                                    
                                    # Face crop extraction for live snapshot
                                    face_crop_b64 = None
                                    try:
                                        fh, fw = frame_to_process.shape[:2]
                                        fx1, fy1, fx2, fy2 = max(0, x1), max(0, y1), min(fw, x2), min(fh, y2)
                                        crop = frame_to_process[fy1:fy2, fx1:fx2]
                                        if crop.size > 0:
                                            crop_resized = cv2.resize(crop, (80, 80))
                                            _, buf = cv2.imencode('.jpg', crop_resized, [cv2.IMWRITE_JPEG_QUALITY, 85])
                                            face_crop_b64 = "data:image/jpeg;base64," + base64.b64encode(buf).decode('utf-8')
                                    except Exception as ce:
                                        logger.warning(f"Face crop extraction failed: {ce}")
                                    
                                    # Update Global Multi-Camera Room Presence Registry
                                    update_live_presence(
                                        user_id=uid,
                                        name=name,
                                        department=cache_data.get('department', '') if cache_data is not None else '',
                                        camera_id=self.camera_id,
                                        camera_name=self.camera_label,
                                        room_name=self.room_name,
                                        confidence=float(best_similarity),
                                        face_crop=face_crop_b64
                                    )
                                    
                                    stream_source = f'stream_{self.camera_id}'
                                    action_status, action_msg = process_user_attendance_event(
                                        user_id=user_id,
                                        name=name,
                                        confidence=float(best_similarity),
                                        source=stream_source,
                                        location=self.room_name,
                                        camera_id=self.camera_id,
                                        shift_info=None
                                    )

                                    cam_label_val = getattr(self, 'camera_label', '') or f"Camera {self.camera_id}"
                                    room_name_val = getattr(self, 'room_name', '') or 'Live Monitoring'
                                    cam_display = f"{cam_label_val} ({room_name_val})" if cam_label_val and room_name_val and room_name_val not in cam_label_val else (cam_label_val or room_name_val)

                                    detection_entry = {
                                        "name": name,
                                        "timestamp": datetime.now().isoformat(),
                                        "confidence": float(best_similarity),
                                        "source": f"stream_{self.camera_id}",
                                        "camera_id": self.camera_id,
                                        "camera_name": cam_display,
                                        "camera_label": cam_label_val,
                                        "room": room_name_val,
                                        "room_name": room_name_val,
                                        "location": room_name_val,
                                        "face_crop": face_crop_b64,
                                        "user_id": uid,
                                        "department": cache_data.get('department', '') if cache_data is not None else ''
                                    }
                                    # One row per employee: update time & move to top on every re-appearance
                                    upsert_live_detection(uid, detection_entry)

                                    if action_status == "REJECTED_OUTSIDE_SHIFT":
                                        logger.info(f"[_ai_loop - Stream {self.camera_id}] Shift check blocked attendance for '{name}': {action_msg}")
                                else:
                                    logger.debug(f"[_ai_loop - Stream {self.camera_id} - Track {tid}] Accumulating consensus for '{cache_data['name'] if cache_data is not None else 'Unknown'}': {num_matches}/{ATTENDANCE_CONFIRMATION_FRAMES} frames (avg {avg_sim:.3f})")
                            else:
                                if track["name"] == "Unknown":
                                    logger.debug(f"[_ai_loop - Stream {self.camera_id} - Track {tid}] Biometric check: No match (Highest: {best_similarity:.4f})")
                                    
                    # Build new_cached_detections from all active tracks (smooth grace period based on TRACK_MAX_AGE_SECONDS)
                    for tid, track in list(self.tracked_faces.items()):
                        if (current_time - track["last_seen"]) < TRACK_MAX_AGE_SECONDS:
                            name = track["name"]
                            similarity_score = track["similarity"]
                            bbox = track["bbox"]
                            color = (0, 255, 0) if (name != "Unknown" and track.get("confirmed")) else (0, 0, 255)
                            
                            display_name = f"{name} ({int(similarity_score * 100)}%)" if track.get("confirmed") else "Verifying..."
                            new_cached_detections.append({
                                'bbox': bbox,
                                'name': display_name,
                                'color': color,
                                'similarity': similarity_score
                            })
                        
                    # Clean up very old inactive tracks (>6.0 seconds)
                    self.tracked_faces = {tid: t for tid, t in self.tracked_faces.items() if (current_time - t["last_seen"]) < 6.0}

                    # If a manually absent user has left the scene entirely, allow them to be recognized again later.
                    with cache_lock:
                        active_suppressed = {
                            t.get('user_id') for t in self.tracked_faces.values() if t.get('user_id') in MANUALLY_ABSENT_USERS
                        }
                        for uid in list(MANUALLY_ABSENT_USERS):
                            if uid not in active_suppressed:
                                MANUALLY_ABSENT_USERS.discard(uid)
                    
                    with self.lock:
                        self.latest_detections = new_cached_detections
                        
                except Exception as e:
                    logger.error(f"Background AI processing failed for {self.camera_id}: {e}")
            else:
                # Keep background recognition alive when the UI feed is paused; clear only when truly idle and not paused.
                has_viewers = self.viewers_count > 0 or self.paused
                if not has_viewers and not self.paused and len(self.latest_detections) > 0:
                    with self.lock:
                        self.latest_detections = []
            
            time.sleep(0.02)

    def get_frame(self):
        with self.lock:
            if self.latest_frame is None:
                return False, None, []
            return True, self.latest_frame.copy(), list(self.latest_detections)

    def stop(self):
        self.running = False
        self.grabber_thread.join(timeout=1.0)
        self.ai_thread.join(timeout=1.0)
        self.cap.release()


def get_or_create_reader(camera_id, rtsp_url):
    """Retrieve or spin up a background RTSP stream reader."""
    global active_stream_readers
    with active_readers_lock:
        if camera_id in active_stream_readers:
            existing = active_stream_readers[camera_id]
            if existing.running and existing.rtsp_url == rtsp_url:
                with monitoring_state_lock:
                    existing.paused = STREAM_PAUSED_STATE.get(camera_id, False)
                return existing
            else:
                logger.info(f"[*] Camera {camera_id} URL changed or stopped. Recreating stream reader...")
                try:
                    existing.stop()
                except Exception as e:
                    logger.warning(f"Error stopping old reader for {camera_id}: {e}")
                del active_stream_readers[camera_id]
        
        logger.info(f"[*] Starting background thread stream reader for: {camera_id} ({rtsp_url})")
        reader = CameraStreamReader(camera_id, rtsp_url)
        with monitoring_state_lock:
            reader.paused = STREAM_PAUSED_STATE.get(camera_id, False)
        active_stream_readers[camera_id] = reader
        return reader


@app.route('/api/camera-monitoring/<camera_id>', methods=['POST'])
def set_camera_monitoring_state(camera_id):
    """Pause the UI stream display without stopping backend recognition and attendance logging."""
    data = request.get_json(silent=True) or {}
    paused = bool(data.get('paused', False))

    with monitoring_state_lock:
        STREAM_PAUSED_STATE[camera_id] = paused

    with active_readers_lock:
        reader = active_stream_readers.get(camera_id)
        if reader is not None:
            reader.paused = paused

    return jsonify({
        'success': True,
        'camera_id': camera_id,
        'paused': paused,
        'message': 'Backend monitoring continues in background.' if paused else 'Monitoring resumed.'
    })


@app.route('/api/stream/<camera_id>')
def video_stream(camera_id):
    """Real-time video streaming with dynamic camera configuration from DB."""
    cam = db.get_camera_by_id(camera_id)
    if not cam or not cam.get('active', 1):
        return jsonify({'error': f'Camera "{camera_id}" is not configured or is disabled. Add it in System Settings > Camera Management.'}), 404

    rtsp_url = cam['rtsp_url']
    if cam['camera_type'] == 'webcam':
        try:
            rtsp_url = int(cam['rtsp_url'])
        except (TypeError, ValueError):
            rtsp_url = 0

    if rtsp_url is None or rtsp_url == '':
        return jsonify({'error': 'Camera has no connection URL configured'}), 400

    reader = get_or_create_reader(camera_id, rtsp_url)
    
    def generate_frames():
        import cv2
        import time
        
        with reader.lock:
            reader.viewers_count += 1
            
        try:
            while True:
                ret, frame, detections = reader.get_frame()
                if not ret or frame is None:
                    offline_frame = np.zeros((360, 640, 3), dtype=np.uint8)
                    cv2.putText(
                        offline_frame, "CAMERA CONNECTING...", (140, 180),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2, cv2.LINE_AA
                    )
                    ret_encode, jpeg = cv2.imencode('.jpg', offline_frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
                    if ret_encode:
                        yield (b'--frame\r\n'
                               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
                    time.sleep(0.5)
                    continue
                    
                # Encode with optimal 75% quality for lightweight high-performance multi-camera streaming
                ret_encode, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if not ret_encode:
                    time.sleep(0.02)
                    continue
                    
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
                time.sleep(0.04)
        except (GeneratorExit, Exception):
            pass
        finally:
            with reader.lock:
                reader.viewers_count = max(0, reader.viewers_count - 1)
            
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


# ============================================
# MULTI-CAMERA PERSON & ROOM LOCATOR ENDPOINTS
# ============================================

@app.route('/api/locate/person', methods=['GET'])
def api_locate_person():
    """Locate an employee across all cameras and return their current room, status, and live snapshot."""
    import time
    name_query = request.args.get('name', '').strip().lower()
    user_id = request.args.get('user_id', type=int)
    
    users = db.get_all_users()
    matched_users = []
    
    if user_id:
        matched_users = [u for u in users if u['id'] == user_id]
    elif name_query:
        matched_users = [u for u in users if name_query in u['name'].strip().lower()]
    else:
        matched_users = users
        
    current_time = time.time()
    results = []
    
    with presence_lock:
        presence_snapshot = dict(LIVE_PRESENCE)
        
    for u in matched_users:
        uid = u['id']
        p = presence_snapshot.get(uid)
        
        status = "not_seen"
        status_label = "🔴 Not Detected Today"
        status_text = "No room detection recorded today"
        room_name = "Unknown / Off-site"
        camera_id = None
        camera_name = "None"
        confidence = 0.0
        face_crop = None
        dwell_minutes = 0
        last_seen_str = "Never"
        
        if p:
            elapsed = current_time - p['last_seen']
            dwell_minutes = max(1, int((current_time - p.get('first_seen_room', p['last_seen'])) // 60))
            camera_id = p['camera_id']
            camera_name = p['camera_name']
            room_name = p['room_name']
            confidence = p['confidence']
            face_crop = p.get('face_crop')
            
            if elapsed <= PRESENCE_ACTIVE_TIMEOUT:
                status = "active"
                status_label = f"🟢 Sitting in {room_name}"
                status_text = f"Active right now ({int(elapsed)}s ago) • In room for {dwell_minutes} min"
                last_seen_str = "Just now"
            elif elapsed <= PRESENCE_RECENT_TIMEOUT:
                status = "recent"
                mins_ago = max(1, int(elapsed // 60))
                status_label = f"🟡 Recently in {room_name}"
                status_text = f"Left room ~{mins_ago}m ago"
                last_seen_str = f"{mins_ago}m ago"
            else:
                status = "away"
                mins_ago = int(elapsed // 60)
                status_label = "⚪ Away From Room"
                status_text = f"Last seen in {room_name} ({mins_ago}m ago)"
                last_seen_str = f"{mins_ago}m ago"
        else:
            # Fallback to database location logs
            latest_db = db.get_latest_user_location(uid)
            if latest_db:
                status = "offline"
                status_label = f"⚪ Previously in {latest_db['location']}"
                status_text = f"Logged at {latest_db['timestamp']}"
                room_name = latest_db['location']
                camera_id = latest_db['device_id']
                confidence = latest_db['confidence']
                last_seen_str = latest_db['timestamp']
                
        results.append({
            "user_id": uid,
            "name": u['name'],
            "department": u.get('department', ''),
            "photo_url": f"/api/users/{uid}/photo",
            "status": status,
            "status_label": status_label,
            "status_text": status_text,
            "room_name": room_name,
            "camera_id": camera_id,
            "camera_name": camera_name,
            "confidence": float(confidence),
            "face_crop": face_crop,
            "last_seen_str": last_seen_str,
            "dwell_minutes": dwell_minutes,
            "stream_url": f"/api/stream/{camera_id}" if camera_id else None
        })
        
    return jsonify({
        "success": True,
        "query": name_query,
        "count": len(results),
        "results": results
    }), 200


@app.route('/api/locate/scan-now', methods=['POST'])
def api_locate_scan_now():
    """Trigger an on-demand real-time parallel scan across ALL active cameras for a specific person."""
    import time
    import cv2
    import base64
    
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    user_id = data.get('user_id')
    
    target_user = None
    all_users = db.get_all_users()
    if user_id:
        target_user = next((u for u in all_users if u['id'] == int(user_id)), None)
    elif name:
        target_user = next((u for u in all_users if name.lower() in u['name'].strip().lower()), None)
        
    if not target_user:
        return jsonify({'success': False, 'message': 'Target employee not found'}), 404
        
    uid = target_user['id']
    user_name = target_user['name']
    
    # Retrieve user aggregate embedding from memory cache
    with cache_lock:
        cache_data = EMBEDDING_CACHE.get(uid)
        
    if not cache_data or cache_data.get('aggregate_embedding') is None:
        user_embs = db.get_embeddings_for_user(uid)
        if user_embs:
            refresh_embedding_cache()
            with cache_lock:
                cache_data = EMBEDDING_CACHE.get(uid)

    if not cache_data or cache_data.get('aggregate_embedding') is None:
        return jsonify({'success': False, 'message': f'No facial profile enrolled for {user_name}'}), 400
        
    target_emb = cache_data['aggregate_embedding']
    
    scan_results = []
    found_camera = None
    
    with active_readers_lock:
        readers_snapshot = list(active_stream_readers.items())
        
    camera_mappings = db.get_camera_room_mappings()
    for cam_id, reader in readers_snapshot:
        ret, frame, _ = reader.get_frame()
        mapping = camera_mappings.get(cam_id, {})
        room_name = mapping.get('room_name', f"Room {cam_id}")
        cam_label = mapping.get('camera_name', f"Camera {cam_id}")
        
        if not ret or frame is None:
            scan_results.append({
                "camera_id": cam_id,
                "room_name": room_name,
                "camera_name": cam_label,
                "status": "camera_offline",
                "matched": False
            })
            continue
            
        try:
            faces = fp.detect_and_extract_insightface(frame)
            best_sim = -1
            best_face = None
            
            for f in faces:
                emb = f.get('embedding')
                if emb is not None:
                    sim, is_match = fp.compare_embeddings(target_emb, emb, threshold=FACE_MATCHING_THRESHOLD)
                    if is_match and sim > best_sim:
                        best_sim = sim
                        best_face = f
                        
            if best_face is not None:
                # Extract live crop
                x1, y1, x2, y2 = best_face['bbox']
                fh, fw = frame.shape[:2]
                fx1, fy1, fx2, fy2 = max(0, x1), max(0, y1), min(fw, x2), min(fh, y2)
                crop = frame[fy1:fy2, fx1:fx2]
                crop_b64 = None
                if crop.size > 0:
                    crop_res = cv2.resize(crop, (80, 80))
                    _, buf = cv2.imencode('.jpg', crop_res, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    crop_b64 = "data:image/jpeg;base64," + base64.b64encode(buf).decode('utf-8')
                    
                update_live_presence(
                    user_id=uid,
                    name=user_name,
                    department=target_user.get('department', ''),
                    camera_id=cam_id,
                    camera_name=cam_label,
                    room_name=room_name,
                    confidence=float(best_sim),
                    face_crop=crop_b64
                )
                
                scan_res = {
                    "camera_id": cam_id,
                    "room_name": room_name,
                    "camera_name": cam_label,
                    "status": "matched",
                    "matched": True,
                    "confidence": float(best_sim),
                    "face_crop": crop_b64
                }
                scan_results.append(scan_res)
                if found_camera is None:
                    found_camera = scan_res
            else:
                scan_results.append({
                    "camera_id": cam_id,
                    "room_name": room_name,
                    "camera_name": cam_label,
                    "status": "scanned_no_match",
                    "matched": False
                })
        except Exception as ex:
            logger.error(f"Instant scan failed for camera {cam_id}: {ex}")
            scan_results.append({
                "camera_id": cam_id,
                "room_name": room_name,
                "camera_name": cam_label,
                "status": f"error: {str(ex)}",
                "matched": False
            })
            
    return jsonify({
        "success": True,
        "user_id": uid,
        "name": user_name,
        "found": found_camera is not None,
        "found_details": found_camera,
        "all_cameras_scanned": scan_results,
        "timestamp": datetime.now().isoformat()
    }), 200


@app.route('/api/locate/rooms', methods=['GET'])
def api_locate_rooms():
    """Return all rooms and who is currently sitting in each room."""
    import time
    current_time = time.time()
    
    rooms_data = {}
    for cam_id, info in db.get_camera_room_mappings().items():
        room_name = info['room_name']
        rooms_data[cam_id] = {
            "camera_id": cam_id,
            "room_name": room_name,
            "camera_name": info['camera_name'],
            "description": info.get('description', ''),
            "occupants": [],
            "occupant_count": 0,
            "stream_url": f"/api/stream/{cam_id}"
        }
        
    with presence_lock:
        presence_snapshot = dict(LIVE_PRESENCE)
        
    for uid, p in presence_snapshot.items():
        elapsed = current_time - p['last_seen']
        cam_id = p.get('camera_id')
        if elapsed <= PRESENCE_ACTIVE_TIMEOUT and cam_id in rooms_data:
            rooms_data[cam_id]["occupants"].append({
                "user_id": uid,
                "name": p['name'],
                "department": p.get('department', ''),
                "confidence": p['confidence'],
                "last_seen_sec": int(elapsed),
                "face_crop": p.get('face_crop'),
                "dwell_minutes": max(1, int((current_time - p.get('first_seen_room', p['last_seen'])) // 60))
            })
            
    for cam_id in rooms_data:
        rooms_data[cam_id]["occupant_count"] = len(rooms_data[cam_id]["occupants"])
        
    return jsonify({
        "success": True,
        "rooms": list(rooms_data.values())
    }), 200


@app.route('/api/cameras/registry', methods=['GET'])
def api_cameras_registry():
    """Return camera-to-room mappings (from the cameras table)."""
    return jsonify({"registry": db.get_camera_room_mappings()}), 200


@app.route('/api/locate/history', methods=['GET'])
def api_locate_history():
    """Return persistent five-minute room snapshots for a person or room."""
    user_id = request.args.get('user_id', type=int)
    room_name = request.args.get('room_name', '').strip() or None
    limit = request.args.get('limit', 500, type=int)
    return jsonify({
        'success': True,
        'history': db.get_locator_history(user_id=user_id, room_name=room_name, limit=limit),
        'interval_minutes': 5,
        'server_time': datetime.now().astimezone().isoformat(timespec='seconds')
    }), 200


# ============================================
# ADMIN/DASHBOARD ENDPOINTS
# ============================================

@app.route('/api/users', methods=['GET'])
def get_users():
    current_tenant = get_current_tenant_id()
    users = db.get_all_users(
        organization_id=current_tenant,
        branch_id=get_current_branch_id(),
    )
    return jsonify({'users': users, 'organization_id': current_tenant}), 200


# ============================================
# SHIFT MANAGEMENT ENDPOINTS
# ============================================

@app.route('/api/shifts', methods=['GET'])
def api_get_shifts():
    """Retrieve all shifts with employee count scoped to current tenant."""
    current_tenant = get_current_tenant_id()
    shifts = db.get_all_shifts(
        organization_id=current_tenant,
        branch_id=get_current_branch_id(),
    )
    return jsonify({'success': True, 'shifts': shifts}), 200


@app.route('/api/shifts', methods=['POST'])
def api_create_shift():
    """Create a new flexible custom shift scoped to tenant."""
    try:
        data = request.get_json() or {}
        name = data.get('name')
        start_time = data.get('start_time', '09:00')
        end_time = data.get('end_time', '17:00')
        raw_grace = data.get('grace_period_minutes')
        grace_period = int(raw_grace) if raw_grace is not None else 15
        raw_checkout_grace = data.get('checkout_grace_period_minutes')
        if raw_checkout_grace is not None:
            if isinstance(raw_checkout_grace, float):
                return jsonify({'error': 'checkout_grace_period_minutes must be a whole number'}), 400
            if isinstance(raw_checkout_grace, str) and ('.' in raw_checkout_grace or not raw_checkout_grace.lstrip('-').isdigit()):
                return jsonify({'error': 'checkout_grace_period_minutes must be a whole number'}), 400
            try:
                checkout_grace_period = int(raw_checkout_grace)
            except (ValueError, TypeError):
                return jsonify({'error': 'checkout_grace_period_minutes must be a whole number'}), 400
            if checkout_grace_period < 0 or checkout_grace_period > 120:
                return jsonify({'error': 'checkout_grace_period_minutes must be between 0 and 120'}), 400
        else:
            checkout_grace_period = 0
        working_days = data.get('working_days')
        
        if not name or not name.strip():
            return jsonify({'error': 'Shift name is required'}), 400
            
        current_tenant = get_current_tenant_id()
        current_branch = get_current_branch_id()
        shift_id = db.add_shift(
            name=name,
            start_time=start_time,
            end_time=end_time,
            grace_period_minutes=grace_period,
            checkout_grace_period_minutes=checkout_grace_period,
            working_days=working_days,
            organization_id=current_tenant,
            branch_id=current_branch
        )
        if shift_id:
            return jsonify({'success': True, 'shift_id': shift_id, 'message': f"Shift '{name}' created successfully"}), 201
        else:
            return jsonify({'error': f"Shift '{name}' already exists or creation failed"}), 400
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"API create shift error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shifts/<int:shift_id>', methods=['PUT'])
def api_update_shift(shift_id):
    """Update custom shift configuration with tenant ownership check."""
    try:
        data = request.get_json() or {}
        name = data.get('name')
        start_time = data.get('start_time', '09:00')
        end_time = data.get('end_time', '17:00')
        raw_grace = data.get('grace_period_minutes')
        grace_period = int(raw_grace) if raw_grace is not None else 15
        raw_checkout_grace = data.get('checkout_grace_period_minutes')
        if raw_checkout_grace is not None:
            if isinstance(raw_checkout_grace, float):
                return jsonify({'error': 'checkout_grace_period_minutes must be a whole number'}), 400
            if isinstance(raw_checkout_grace, str) and ('.' in raw_checkout_grace or not raw_checkout_grace.lstrip('-').isdigit()):
                return jsonify({'error': 'checkout_grace_period_minutes must be a whole number'}), 400
            try:
                checkout_grace_period = int(raw_checkout_grace)
            except (ValueError, TypeError):
                return jsonify({'error': 'checkout_grace_period_minutes must be a whole number'}), 400
            if checkout_grace_period < 0 or checkout_grace_period > 120:
                return jsonify({'error': 'checkout_grace_period_minutes must be between 0 and 120'}), 400
        else:
            checkout_grace_period = 0
        working_days = data.get('working_days')
        
        if not name or not name.strip():
            return jsonify({'error': 'Shift name is required'}), 400
            
        current_tenant = get_current_tenant_id()
        success = db.update_shift(
            shift_id,
            name=name,
            start_time=start_time,
            end_time=end_time,
            grace_period_minutes=grace_period,
            checkout_grace_period_minutes=checkout_grace_period,
            working_days=working_days,
            organization_id=current_tenant
        )
        if success:
            return jsonify({'success': True, 'message': f"Shift '{name}' updated successfully"}), 200
        else:
            return jsonify({'error': 'Shift not found or update failed'}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"API update shift error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shifts/<int:shift_id>', methods=['DELETE'])
def api_delete_shift(shift_id):
    """Delete a custom shift (default shift ID 1 cannot be deleted) with tenant check."""
    try:
        if shift_id == 1:
            return jsonify({'error': 'Cannot delete default Morning Shift'}), 400
            
        current_tenant = get_current_tenant_id()
        success = db.delete_shift(shift_id, organization_id=current_tenant)
        if success:
            return jsonify({'success': True, 'message': 'Shift deleted successfully'}), 200
        else:
            return jsonify({'error': 'Shift not found or deletion failed'}), 404
    except Exception as e:
        logger.error(f"API delete shift error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/shifts/<int:shift_id>/set-default', methods=['POST'])
def api_set_default_shift(shift_id):
    """Set a specific shift as the default shift for all unassigned staff."""
    try:
        success = db.set_default_shift(shift_id, organization_id=get_current_tenant_id())
        if success:
            return jsonify({'success': True, 'message': f'Shift #{shift_id} set as default shift successfully'}), 200
        else:
            return jsonify({'error': 'Failed to set default shift'}), 400
    except Exception as e:
        logger.error(f"API set default shift error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/<int:user_id>/assign-shift', methods=['POST', 'PUT'])
@app.route('/api/staff/<int:user_id>/assign-shift', methods=['POST', 'PUT'])
def api_assign_user_shift(user_id):
    """Assign a shift to an individual user immediately with tenant verification."""
    try:
        if not verify_user_access(user_id):
            return jsonify({'error': 'User not found or unauthorized'}), 404
        data = request.get_json(silent=True) or {}
        shift_id = data.get('shift_id')
        if not shift_id:
            return jsonify({'error': 'Shift ID is required'}), 400
        effective_from = data.get('effective_from')
        confirm_shift_reclassify = bool(data.get('confirm_shift_reclassify'))
        success = db.assign_user_shift(
            user_id,
            int(shift_id),
            effective_from,
            confirm_shift_reclassify=confirm_shift_reclassify,
            organization_id=get_current_tenant_id(),
        )
        if success:
            return jsonify({'success': True, 'message': f'Shift assigned to User #{user_id}', 'effective_from': effective_from}), 200
        return jsonify({'error': 'User not found or shift assignment failed'}), 400
    except ValueError as exc:
        status = 409 if str(exc) == db.PAYROLL_LOCK_MESSAGE or 'reclassifies' in str(exc) else 400
        return jsonify({'error': str(exc), 'field': 'effective_from'}), status
    except Exception as e:
        logger.error(f"API assign user shift error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/bulk-assign-shifts', methods=['POST'])
@app.route('/api/staff/bulk-assign-shifts', methods=['POST'])
@app.route('/api/shifts/bulk-assign', methods=['POST'])
def api_bulk_assign_shifts():
    """Bulk assign shift to multiple or all employees."""
    try:
        data = request.get_json(silent=True) or {}
        user_ids = data.get('user_ids', 'all')
        shift_id = data.get('shift_id')
        effective_from = data.get('effective_from')
        if not shift_id:
            return jsonify({'error': 'Shift ID is required'}), 400
        success, count = db.bulk_assign_shifts(
            user_ids=user_ids,
            shift_id=int(shift_id),
            effective_from=effective_from,
            organization_id=get_current_tenant_id(),
        )
        if success:
            return jsonify({'success': True, 'count': count, 'message': f'Shift successfully assigned to {count} staff member(s)'}), 200
        return jsonify({'error': 'Failed to bulk assign shifts'}), 500
    except Exception as e:
        logger.error(f"API bulk assign shifts error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/<int:user_id>/delete', methods=['POST', 'DELETE'])
@app.route('/api/users/<int:user_id>', methods=['DELETE', 'POST'])
def api_delete_user(user_id):
    try:
        user = verify_user_access(user_id)
        if not user:
            return jsonify({'error': 'User not found or unauthorized'}), 404
        success = db.delete_user(user_id)
        if success:
            remove_live_detection_for_user(user_id)
            refresh_embedding_cache()
            return jsonify({'success': True, 'message': f'User ID {user_id} deleted successfully'}), 200
        else:
            return jsonify({'error': 'User not found or deletion failed'}), 404
    except Exception as e:
        logger.error(f"API delete user error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/<int:user_id>/deactivate', methods=['POST'])
def api_deactivate_user(user_id):
    user = verify_user_access(user_id)
    if not user:
        return jsonify({'error': 'User not found or unauthorized'}), 404
    data = request.get_json(silent=True) or {}
    exit_date = str(data.get('exit_date') or date.today().isoformat())[:10]
    try:
        if not db.deactivate_user(user_id, exit_date=exit_date):
            return jsonify({'error': 'Deactivation failed', 'field': 'exit_date'}), 400
        return jsonify({'success': True, 'exit_date': exit_date, 'active': 0})
    except ValueError as exc:
        return jsonify({'error': str(exc), 'field': 'exit_date'}), 400


@app.route('/api/users/<int:user_id>/reactivate', methods=['POST'])
def api_reactivate_user(user_id):
    user = verify_user_access(user_id)
    if not user:
        return jsonify({'error': 'User not found or unauthorized'}), 404
    if not db.reactivate_user(user_id):
        return jsonify({'error': 'Reactivation failed'}), 400
    return jsonify({'success': True, 'active': 1})


@app.route('/api/users/<int:user_id>/mark-absent', methods=['POST'])
def api_mark_user_absent(user_id):
    try:
        user = verify_user_access(user_id)
        if not user:
            return jsonify({'error': 'User not found or unauthorized'}), 404
        data = request.get_json(silent=True) or {}
        target_date = data.get('date')
        success = db.mark_user_absent_today(
            user_id,
            target_date=target_date,
            organization_id=get_current_tenant_id(),
        )
        if success:
            remove_live_detection_for_user(user_id)
            return jsonify({'success': True, 'message': f'User ID {user_id} marked absent'}), 200
        else:
            return jsonify({'error': 'Failed to mark user absent'}), 400
    except ValueError as e:
        if str(e) == db.PAYROLL_LOCK_MESSAGE:
            return jsonify({'error': str(e), 'reopen': '/api/payroll/reopen'}), 409
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"API mark absent error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/<int:user_id>/mark-present', methods=['POST'])
def api_mark_user_present(user_id):
    try:
        user = verify_user_access(user_id)
        if not user:
            return jsonify({'error': 'User not found or unauthorized'}), 404
        data = request.get_json(silent=True) or {}
        target_date = data.get('date')
        check_in_time = data.get('check_in_time')
        if check_in_time is None:
            check_in_time = data.get('check_in')
        check_out_time = data.get('check_out_time')
        if check_out_time is None:
            check_out_time = data.get('check_out')
        shift_id_raw = data.get('shift_id')
        notes = data.get('notes') or ''

        if check_in_time is not None and str(check_in_time).strip() == '':
            return jsonify({'error': 'Check-in time is required'}), 400
        if check_out_time is not None and str(check_out_time).strip() == '':
            return jsonify({'error': 'Check-out time is required'}), 400

        parsed_shift_id = None
        if shift_id_raw is not None and str(shift_id_raw).strip():
            try:
                parsed_shift_id = int(shift_id_raw)
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid shift_id'}), 400

        acting_admin = session.get('admin_name') or 'Admin'

        success = db.mark_user_present_today(
            user_id, 
            target_date=target_date, 
            check_in_time=check_in_time, 
            check_out_time=check_out_time, 
            shift_id=parsed_shift_id,
            notes=notes,
            marked_by=acting_admin
        )
        if success:
            restore_live_detection_for_user(user_id)
            mark_logged_today_in_memory(user_id, 'manual')
            return jsonify({'success': True, 'message': f'User ID {user_id} attendance saved successfully'}), 200
        else:
            return jsonify({'error': 'Failed to save attendance'}), 400
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        logger.error(f"API mark present error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/attendance/edit', methods=['POST'])
def api_edit_attendance():
    """ATT-02: Edit attendance record for an employee with input validation and tenant scoping."""
    try:
        data = request.get_json(silent=True) or {}
        user_id_raw = data.get('user_id')
        if not user_id_raw:
            return jsonify({'error': 'user_id is required'}), 400
        try:
            user_id = int(user_id_raw)
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid user_id'}), 400

        user = verify_user_access(user_id)
        if not user:
            return jsonify({'error': 'User not found or unauthorized'}), 404

        target_date = data.get('date') or data.get('target_date') or datetime.now().date().isoformat()
        status = str(data.get('status') or '').strip().lower()
        check_in_time = data.get('check_in_time') or data.get('check_in')
        check_out_time = data.get('check_out_time') or data.get('check_out')
        shift_id_raw = data.get('shift_id')
        notes = data.get('notes') or ''

        parsed_shift_id = None
        if shift_id_raw is not None and str(shift_id_raw).strip():
            try:
                parsed_shift_id = int(shift_id_raw)
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid shift_id'}), 400

        acting_admin = session.get('admin_name') or 'Admin'
        current_tenant = get_current_tenant_id()

        if status == 'absent':
            success = db.mark_user_absent_today(user_id, target_date=target_date, organization_id=current_tenant)
            if success:
                remove_live_detection_for_user(user_id)
                return jsonify({'success': True, 'message': f'Attendance for user {user_id} updated to absent'}), 200
            else:
                return jsonify({'error': 'Failed to update attendance to absent'}), 400

        success = db.mark_user_present_today(
            user_id=user_id,
            target_date=target_date,
            check_in_time=check_in_time,
            check_out_time=check_out_time,
            shift_id=parsed_shift_id,
            notes=notes,
            marked_by=acting_admin,
            organization_id=current_tenant
        )
        if success:
            restore_live_detection_for_user(user_id)
            mark_logged_today_in_memory(user_id, 'manual')
            return jsonify({'success': True, 'message': f'Attendance for user {user_id} saved successfully'}), 200
        else:
            return jsonify({'error': 'Failed to save attendance'}), 400
    except ValueError as ve:
        if str(ve) == db.PAYROLL_LOCK_MESSAGE:
            return jsonify({'error': str(ve), 'reopen': '/api/payroll/reopen'}), 409
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        logger.error(f"API edit attendance error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/attendance/bulk-mark', methods=['POST'])
def api_bulk_mark_attendance():
    """Mark all or specific selected users present or absent for today or target date."""
    try:
        data = request.get_json(silent=True) or {}
        action = data.get('action')
        target_date = data.get('date')
        user_ids_list = data.get('user_ids')

        if action not in ('present', 'absent'):
            return jsonify({'error': 'Action must be present or absent'}), 400

        if user_ids_list and isinstance(user_ids_list, list):
            target_users = [{'id': int(uid)} for uid in user_ids_list if str(uid).isdigit()]
        else:
            target_users = db.get_all_users()

        succeeded = 0
        failed = []
        for user in target_users:
            user_id = user['id']
            success = (
                db.mark_user_present_today(user_id, target_date=target_date)
                if action == 'present'
                else db.mark_user_absent_today(user_id, target_date=target_date)
            )
            if success:
                succeeded += 1
                if action == 'present':
                    restore_live_detection_for_user(user_id)
                    mark_logged_today_in_memory(user_id, 'manual')
                else:
                    remove_live_detection_for_user(user_id)
            else:
                failed.append(user_id)

        if not user_ids_list:
            if action == 'absent':
                clear_logged_today_in_memory()
                with cache_lock:
                    LATEST_STREAM_DETECTIONS.clear()
                    DETECTED_USERS_SESSION.clear()
            else:
                resync_logged_today_in_memory()
        else:
            resync_logged_today_in_memory()

        return jsonify({
            'success': not failed,
            'action': action,
            'processed': len(target_users),
            'succeeded': succeeded,
            'failed_user_ids': failed,
            'message': f'{succeeded} users marked {action}'
        }), 200 if not failed else 207
    except Exception as e:
        logger.error(f"Bulk attendance error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/attendance/logs', methods=['GET'])
def get_attendance_logs():
    limit = request.args.get('limit', 100, type=int)
    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    logs = db.get_attendance_logs(limit, organization_id=current_tenant, branch_id=current_branch)
    response = jsonify({
        'logs': logs,
        'server_time': datetime.now().astimezone().isoformat(timespec='seconds')
    })
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response, 200


@app.route('/api/attendance/user/<int:user_id>', methods=['GET'])
def get_user_attendance(user_id):
    days = request.args.get('days', 7, type=int)
    logs = db.get_attendance_by_user(user_id, days)
    return jsonify({'user_id': user_id, 'logs': logs, 'days': days}), 200


_stats_cache = {}
_stats_cache_lock = threading.Lock()
_STATS_CACHE_TTL = 30.0  # In-memory cache protects DB from rapid polling (shields PostgreSQL)


@app.route('/api/stats', methods=['GET'])
def get_stats():
    try:
        current_tenant = get_current_tenant_id()
        current_branch = get_current_branch_id()
        cache_key = (current_tenant, current_branch)
        now_ts = time.time()

        with _stats_cache_lock:
            cached = _stats_cache.get(cache_key)
            if cached and (now_ts - cached['time']) < _STATS_CACHE_TTL:
                return jsonify(cached['data']), 200

        stats = db.get_attendance_statistics(organization_id=current_tenant, branch_id=current_branch)
        users = db.get_all_users(organization_id=current_tenant)
        total_users = len(users)
        present_users = stats.get('unique_users_today', 0)
        data = {
            'total_users': total_users,
            'present_users': present_users,
            'absent_users': max(total_users - present_users, 0),
            'today_attendance': stats.get('today_count', 0),
            'unique_users_today': present_users,
            'total_logs': stats.get('total_records', 0),
            'avg_confidence': stats.get('avg_confidence', 0),
            'recent_entries': stats.get('recent_entries', []),
            'timestamp': datetime.now().isoformat()
        }

        with _stats_cache_lock:
            _stats_cache[cache_key] = {'time': now_ts, 'data': data}

        return jsonify(data), 200
    except Exception as e:
        logger.error(f"Stats endpoint error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/system/health', methods=['GET'])
def system_health():
    try:
        db_ok = True
        models_ok = True
        try:
            db_ok = db.init_db()
        except:
            db_ok = False
        try:
            fp.get_insightface_model()
        except:
            models_ok = False
        return jsonify({
            'status': 'healthy' if (db_ok and models_ok) else 'degraded',
            'database': 'ok' if db_ok else 'error',
            'models': 'ok' if models_ok else 'error',
            'timestamp': datetime.now().isoformat()
        }), 200
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()}), 200


# ==============================================================================
# STEP 4: QINTELLECT LOCAL INTEGRATION CONTRACT (Endpoints)
# ==============================================================================


@app.route('/api/integration/status', methods=['GET'])
def integration_status():
    """
    Return safe integration status to Main Dashboard.
    DO NOT return passwords, RTSP URLs, face embeddings, or private employee records.
    """
    try:
        cfg = load_integration_config()
        return jsonify({
            'connected': True,
            'linked': bool(cfg.get('linked')),
            'organization_id': cfg.get('organization_id'),
            'organization_name': cfg.get('organization_name'),
            'branch_id': cfg.get('branch_id'),
            'branch_name': cfg.get('branch_name'),
            'branch_status': cfg.get('branch_status') or ('active' if cfg.get('linked') else None),
            'local_status': 'healthy',
            'licensed_modules': cfg.get('licensed_modules', list(CANONICAL_MODULES)),
            'license_status': cfg.get('license_status') or ('active' if cfg.get('linked') else None),
            'license_id': cfg.get('license_id'),
            'license_start': cfg.get('license_start'),
            'license_expiry': cfg.get('license_expiry'),
            'last_sync_timestamp': cfg.get('last_sync_timestamp'),
            'local_instance_id': cfg.get('local_instance_id') or 'LOC-INST-001',
            'timestamp': datetime.now().isoformat()
        }), 200
    except Exception as e:
        logger.error(f"Error in /api/integration/status: {e}")
        return jsonify({'connected': False, 'local_status': 'error', 'message': str(e)}), 500


@app.route('/api/integration/sync', methods=['POST'])
def integration_sync():
    """
    Receive organization, branch, license, and module entitlement from Main Dashboard.
    Persist ONLY in config_integration.json. NEVER write into attendance.db.
    """
    integration_key = os.environ.get('LOCAL_DASHBOARD_API_KEY', '').strip()
    if len(integration_key) < 32:
        return jsonify({'success': False, 'message': 'Integration authentication is not securely configured.'}), 503
    supplied_key = request.headers.get('X-Integration-Key', '')
    if not secrets.compare_digest(supplied_key, integration_key):
        return jsonify({'success': False, 'message': 'Integration authentication required.'}), 401

    try:
        payload = request.get_json(silent=True) or {}
        if not payload:
            return jsonify({'success': False, 'message': 'Invalid JSON payload'}), 400

        # Validate canonical modules only
        if 'licensed_modules' in payload:
            valid_modules = [m for m in (payload.get('licensed_modules') or []) if m in CANONICAL_MODULES or m in {'attendance', 'payroll', 'leave', 'overtime', 'shifts'}]
        else:
            valid_modules = list(CANONICAL_MODULES)

        cfg = load_integration_config()
        cfg['linked'] = True
        cfg['organization_id'] = payload.get('organization_id') or cfg.get('organization_id')
        cfg['organization_name'] = payload.get('organization_name') or cfg.get('organization_name')
        cfg['branch_id'] = payload.get('branch_id') or cfg.get('branch_id')
        cfg['branch_name'] = payload.get('branch_name') or cfg.get('branch_name')
        cfg['branch_status'] = payload.get('branch_status', 'active')
        cfg['license_id'] = payload.get('license_id') or cfg.get('license_id')
        cfg['license_status'] = payload.get('license_status', 'active')
        cfg['license_start'] = payload.get('license_start') or cfg.get('license_start')
        cfg['license_expiry'] = payload.get('license_expiry') or cfg.get('license_expiry')
        cfg['licensed_modules'] = valid_modules
        cfg['last_sync_timestamp'] = datetime.now().isoformat()
        if payload.get('local_instance_id'):
            cfg['local_instance_id'] = payload.get('local_instance_id')

        if save_integration_config(cfg):
            logger.info(f"Integration config synced successfully for Branch: {cfg['branch_id']} ({cfg['branch_name']})")
            return jsonify({
                'success': True,
                'message': 'Integration configuration synchronized',
                'config': {
                    'linked': cfg['linked'],
                    'organization_id': cfg['organization_id'],
                    'organization_name': cfg['organization_name'],
                    'branch_id': cfg['branch_id'],
                    'branch_name': cfg['branch_name'],
                    'licensed_modules': cfg['licensed_modules'],
                    'license_status': cfg['license_status'],
                    'last_sync_timestamp': cfg['last_sync_timestamp'],
                    'local_instance_id': cfg['local_instance_id']
                }
            }), 200
        else:
            return jsonify({'success': False, 'message': 'Failed to persist integration configuration'}), 500
    except Exception as e:
        logger.error(f"Error in /api/integration/sync: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/integration/cameras', methods=['GET'])
def integration_get_cameras():
    """Return one branch's active camera configuration to the authenticated control plane."""
    integration_key = os.environ.get('LOCAL_DASHBOARD_API_KEY', '').strip()
    if len(integration_key) < 32:
        return jsonify({'success': False, 'message': 'Integration authentication is not securely configured.'}), 503
    supplied_key = request.headers.get('X-Integration-Key', '')
    if not secrets.compare_digest(supplied_key, integration_key):
        return jsonify({'success': False, 'message': 'Integration authentication required.'}), 401

    organization_id = request.args.get('organization_id', '').strip()
    branch_id = request.args.get('branch_id', '').strip()
    if not organization_id or not branch_id:
        return jsonify({'success': False, 'message': 'Organization and branch are required.'}), 400

    cameras = db.get_all_cameras(
        active_only=True,
        organization_id=organization_id,
        branch_id=branch_id,
    )
    return jsonify({
        'success': True,
        'organization_id': organization_id,
        'branch_id': branch_id,
        'cameras': cameras,
        'count': len(cameras),
    }), 200


@app.route('/api/integration/employees', methods=['GET'])
def integration_get_employees():
    """Return branch-scoped employee profiles to the authenticated control plane."""
    integration_key = os.environ.get('LOCAL_DASHBOARD_API_KEY', '').strip()
    if len(integration_key) < 32:
        return jsonify({'success': False, 'message': 'Integration authentication is not securely configured.'}), 503
    supplied_key = request.headers.get('X-Integration-Key', '')
    if not secrets.compare_digest(supplied_key, integration_key):
        return jsonify({'success': False, 'message': 'Integration authentication required.'}), 401

    organization_id = request.args.get('organization_id', '').strip()
    branch_id = request.args.get('branch_id', '').strip()
    if not organization_id or not branch_id:
        return jsonify({'success': False, 'message': 'Organization and branch are required.'}), 400

    employees = db.get_all_users(
        organization_id=organization_id,
        branch_id=branch_id,
    )
    return jsonify({
        'success': True,
        'organization_id': organization_id,
        'branch_id': branch_id,
        'employees': [{
            **employee,
            'employee_code': employee.get('notes') or f"EMP-{employee['id']}",
        } for employee in employees],
        'count': len(employees),
    }), 200


@app.route('/api/integration/employees/<int:user_id>/embeddings', methods=['POST'])
def integration_sync_employee_embeddings(user_id):
    """Store a branch employee's face vectors from an authenticated Main Dashboard relay."""
    integration_key = os.environ.get('LOCAL_DASHBOARD_API_KEY', '').strip()
    if len(integration_key) < 32:
        return jsonify({'success': False, 'message': 'Integration authentication is not securely configured.'}), 503
    supplied_key = request.headers.get('X-Integration-Key', '')
    if not secrets.compare_digest(supplied_key, integration_key):
        return jsonify({'success': False, 'message': 'Integration authentication required.'}), 401

    organization_id = request.args.get('organization_id', '').strip()
    branch_id = request.args.get('branch_id', '').strip()
    if not organization_id or not branch_id:
        return jsonify({'success': False, 'message': 'Organization and branch are required.'}), 400

    payload = request.get_json(silent=True) or {}
    vectors = payload.get('embeddings')
    if not isinstance(vectors, list) or not vectors or len(vectors) > 20:
        return jsonify({'success': False, 'message': 'Between 1 and 20 face embeddings are required.'}), 400

    validated_vectors = []
    for vector in vectors:
        try:
            validated = np.asarray(vector, dtype=np.float32)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'Face embeddings must be numeric vectors.'}), 400
        if validated.shape != (512,) or not np.isfinite(validated).all():
            return jsonify({'success': False, 'message': 'Each face embedding must contain 512 finite values.'}), 400
        validated_vectors.append(validated.tolist())

    try:
        with sqlite3.connect(db.DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM users WHERE id = ? AND organization_id = ? AND branch_id = ? AND active = 1",
                (user_id, organization_id, branch_id),
            )
            if not cur.fetchone():
                return jsonify({'success': False, 'message': 'Employee was not found in this active branch.'}), 404

            for statement in (
                "ALTER TABLE embeddings ADD COLUMN organization_id TEXT",
                "ALTER TABLE embeddings ADD COLUMN branch_id TEXT",
                "ALTER TABLE users ADD COLUMN face_trained INTEGER DEFAULT 0",
            ):
                try:
                    cur.execute(statement)
                except sqlite3.OperationalError as error:
                    if "duplicate column name" not in str(error).lower():
                        raise

            cur.execute(
                "DELETE FROM embeddings WHERE user_id = ? AND organization_id = ? AND branch_id = ?",
                (user_id, organization_id, branch_id),
            )
            source = str(payload.get('source') or 'local_ai_edge_node')[:200]
            cur.executemany(
                "INSERT INTO embeddings (user_id, embedding, source_video, organization_id, branch_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (user_id, json.dumps(vector).encode('utf-8'), source, organization_id, branch_id)
                    for vector in validated_vectors
                ],
            )
            cur.execute(
                "UPDATE users SET face_trained = 1 WHERE id = ? AND organization_id = ? AND branch_id = ?",
                (user_id, organization_id, branch_id),
            )
            conn.commit()

        refresh_embedding_cache()
        return jsonify({
            'success': True,
            'synced_count': len(validated_vectors),
            'is_trained': True,
        }), 200
    except Exception:
        logger.exception("Could not sync biometric embeddings for employee %s.", user_id)
        return jsonify({'success': False, 'error': 'Could not save employee face embeddings.'}), 500


@app.route('/api/integration/employees/<int:user_id>/embeddings', methods=['GET'])
def integration_get_employee_embeddings(user_id):
    """Return face templates for one active employee in the requested branch."""
    integration_key = os.environ.get('LOCAL_DASHBOARD_API_KEY', '').strip()
    if len(integration_key) < 32:
        return jsonify({'success': False, 'message': 'Integration authentication is not securely configured.'}), 503
    supplied_key = request.headers.get('X-Integration-Key', '')
    if not secrets.compare_digest(supplied_key, integration_key):
        return jsonify({'success': False, 'message': 'Integration authentication required.'}), 401

    organization_id = request.args.get('organization_id', '').strip()
    branch_id = request.args.get('branch_id', '').strip()
    if not organization_id or not branch_id:
        return jsonify({'success': False, 'message': 'Organization and branch are required.'}), 400

    try:
        with sqlite3.connect(db.DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id FROM users
                WHERE id = ? AND organization_id = ? AND branch_id = ? AND active = 1
                """,
                (user_id, organization_id, branch_id),
            )
            if not cur.fetchone():
                return jsonify({'success': False, 'message': 'Employee was not found in this active branch.'}), 404

            rows = cur.execute(
                """
                SELECT embedding FROM embeddings
                WHERE user_id = ? AND organization_id = ? AND branch_id = ?
                ORDER BY id
                LIMIT 20
                """,
                (user_id, organization_id, branch_id),
            ).fetchall()

        vectors = []
        for (embedding_blob,) in rows:
            try:
                vector = np.asarray(json.loads(embedding_blob.decode('utf-8')), dtype=np.float32)
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                logger.exception("Stored biometric template is invalid for employee %s.", user_id)
                return jsonify({'success': False, 'message': 'Stored biometric templates are invalid.'}), 500
            if vector.shape != (512,) or not np.isfinite(vector).all():
                logger.error("Stored biometric template has an invalid shape for employee %s.", user_id)
                return jsonify({'success': False, 'message': 'Stored biometric templates are invalid.'}), 500
            vectors.append(vector.tolist())

        return jsonify({
            'success': True,
            'organization_id': organization_id,
            'branch_id': branch_id,
            'user_id': user_id,
            'embeddings': vectors,
            'is_trained': bool(vectors),
        }), 200
    except Exception:
        logger.exception("Could not retrieve biometric templates for employee %s.", user_id)
        return jsonify({'success': False, 'error': 'Could not retrieve employee face templates.'}), 500


@app.route('/api/integration/edge-sync-state', methods=['GET'])
def integration_edge_sync_state():
    """Expose the branch selected by the authenticated Main Dashboard relay."""
    integration_key = os.environ.get('LOCAL_DASHBOARD_API_KEY', '').strip()
    if len(integration_key) < 32:
        return jsonify({'success': False, 'error': 'Integration authentication is not securely configured.'}), 503
    supplied_key = request.headers.get('X-Integration-Key', '')
    if not secrets.compare_digest(supplied_key, integration_key):
        return jsonify({'success': False, 'error': 'Integration authentication required.'}), 401

    organization_id = request.args.get('organization_id', '').strip()
    branch_id = request.args.get('branch_id', '').strip()
    if not organization_id or not branch_id:
        return jsonify({'success': False, 'error': 'Organization and branch are required.'}), 400

    try:
        absent_user_ids = db.get_manual_absent_user_ids_today(organization_id, branch_id)
    except Exception:
        logger.exception('Could not load current-branch manual absence overrides.')
        return jsonify({'success': False, 'error': 'Attendance state is temporarily unavailable.'}), 500

    return jsonify({
        'success': True,
        'organization_id': organization_id,
        'branch_id': branch_id,
        'date': datetime.now().strftime('%Y-%m-%d'),
        'manual_absent_user_ids': absent_user_ids,
    }), 200


@app.route('/api/integration/shifts', methods=['GET'])
def integration_get_shifts():
    """Return branch-scoped shifts to the authenticated control plane for the EXE relay."""
    integration_key = os.environ.get('LOCAL_DASHBOARD_API_KEY', '').strip()
    if len(integration_key) < 32:
        return jsonify({'success': False, 'message': 'Integration authentication is not securely configured.'}), 503
    supplied_key = request.headers.get('X-Integration-Key', '')
    if not secrets.compare_digest(supplied_key, integration_key):
        return jsonify({'success': False, 'message': 'Integration authentication required.'}), 401

    organization_id = request.args.get('organization_id', '').strip()
    branch_id = request.args.get('branch_id', '').strip()
    if not organization_id or not branch_id:
        return jsonify({'success': False, 'message': 'Organization and branch are required.'}), 400

    shifts = db.get_all_shifts(
        active_only=True,
        organization_id=organization_id,
        branch_id=branch_id,
    )
    employees = db.get_all_users(
        organization_id=organization_id,
        branch_id=branch_id,
    )
    assignments = []
    for employee in employees:
        try:
            shift_id = int(employee.get('shift_id') or 1)
        except (TypeError, ValueError):
            shift_id = 1
        if shift_id < 1:
            shift_id = 1
        assignments.append({
            'user_id': int(employee['id']),
            'shift_id': shift_id,
            'checkin_cameras': str(employee.get('checkin_cameras') or 'all').strip() or 'all',
            'checkout_cameras': str(employee.get('checkout_cameras') or 'all').strip() or 'all',
        })
    return jsonify({
        'success': True,
        'organization_id': organization_id,
        'branch_id': branch_id,
        'shifts': shifts,
        'assignments': assignments,
        'count': len(shifts),
    }), 200


@app.route('/api/attendance/edge-mark', methods=['POST'])
def api_edge_mark_attendance():
    """
    Accept an authenticated Main Dashboard relay and process it within the bound tenant.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': 'Invalid attendance event'}), 400
    data, auth_error = _prepare_attendance_ingestion(data)
    if auth_error:
        return auth_error
    if data['_receipt_state'] == 'duplicate':
        return jsonify(data['_receipt_response']), data['_receipt_status']
    if data['_receipt_state'] == 'processing':
        return jsonify({'success': False, 'error': 'Attendance event is already being processed'}), 409
    if data['_receipt_state'] == 'conflict':
        return jsonify({'success': False, 'error': 'event_id was already used with a different payload'}), 409
    event_id = data['event_id']
    user_id = data.get('user_id')
    detected_name = data.get('name') or data.get('detected_name') or 'Unknown'
    try:
        confidence = float(data.get('confidence', 0.85))
    except (TypeError, ValueError):
        db.abandon_attendance_ingestion(event_id)
        return jsonify({'success': False, 'error': 'Invalid confidence value'}), 400
    camera_id = data.get('camera_id') or 'edge_node'
    camera_name = data.get('camera_name') or 'Live AI Camera'
    source = data.get('source') or f"camera_{camera_id}"
    location = data.get('room_name') or camera_name
    from datetime import timedelta
    timestamp_str = data.get('timestamp')
    att_time = None
    if timestamp_str:
        try:
            att_time = datetime.fromisoformat(str(timestamp_str).replace('Z', ''))
        except Exception:
            att_time = None
    if att_time is None:
        att_time = datetime.utcnow() + timedelta(hours=5)
    event_type = str(data.get('event_type') or 'CHECKIN').upper()
    checkout_cam_id = camera_id if event_type == 'CHECKOUT' else data.get('checkout_camera_id')
    checkout_loc = camera_name if event_type == 'CHECKOUT' else data.get('checkout_location')

    try:
        success, msg = db.log_attendance(
            user_id=int(user_id),
            detected_name=detected_name,
            confidence=confidence,
            source=source,
            location=location,
            device_id=camera_id,
            checkout_camera_id=checkout_cam_id if event_type == 'CHECKOUT' else None,
            checkout_location=checkout_loc if event_type == 'CHECKOUT' else None,
            bypass_shift_check=True,
            attendance_time=att_time,
        )
    except Exception:
        db.abandon_attendance_ingestion(event_id)
        logger.exception('Error processing authenticated edge attendance event')
        return jsonify({'success': False, 'error': 'Attendance event processing failed'}), 500

    if not success:
        db.abandon_attendance_ingestion(event_id)
        return jsonify({
            'success': False,
            'status': 'REJECTED',
            'message': msg,
            'event_id': event_id,
        }), 400

    status = 'CHECKOUT' if event_type == 'CHECKOUT' else 'CHECKIN'
    with cache_lock:
        LATEST_STREAM_DETECTIONS.insert(0, {
            'user_id': user_id,
            'name': detected_name,
            'confidence': int(confidence * 100),
            'camera_name': camera_name,
            'timestamp': att_time.strftime('%H:%M:%S'),
            'time': att_time.strftime('%H:%M:%S'),
            'source': camera_name,
        })
        if len(LATEST_STREAM_DETECTIONS) > 50:
            LATEST_STREAM_DETECTIONS.pop()

    return _finish_attendance_ingestion(event_id, {
        'success': True,
        'status': status,
        'message': msg,
        'user_id': user_id,
        'name': detected_name,
        'timestamp': att_time.isoformat(),
        'event_id': event_id,
    }, 201, event={
        "organization_id": data.get("organization_id"),
        "branch_id": data.get("branch_id"),
        "user_id": user_id,
        "name": detected_name,
        "camera_id": camera_id,
        "timestamp": att_time.isoformat(),
        "event_type": status,
        "confidence": confidence,
    })


@app.route('/api/init', methods=['POST'])
def initialize_system():
    try:
        if not db.init_db():
            return jsonify({'status': 'failed', 'error': 'Database initialization failed; check attendance.log'}), 500
        models_ok = verify_models()
        if models_ok:
            refresh_embedding_cache()
            return jsonify({'status': 'initialized', 'database': 'ready', 'models': 'ready', 'message': 'System ready'}), 200
        else:
            refresh_embedding_cache()
            return jsonify({'status': 'partial', 'database': 'ready', 'models': 'incomplete', 'message': 'Database ready but some models failed'}), 202
    except Exception as e:
        return jsonify({'status': 'failed', 'error': str(e)}), 500


@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': 'File too large. Max size: 500MB'}), 413

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500


import base64 as _b64

@app.route('/api/users/<int:user_id>/photo', methods=['POST'])
def upload_user_photo(user_id):
    """Upload professional profile photo for a user with tenant isolation."""
    if not verify_user_access(user_id):
        return jsonify({'error': 'User not found or unauthorized'}), 404
    if 'photo' not in request.files:
        return jsonify({'error': 'No photo provided'}), 400
    photo = request.files['photo']
    if not photo.filename or photo.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Validate extension
    ext = photo.filename.rsplit('.', 1)[-1].lower()
    if ext not in ['jpg', 'jpeg', 'png', 'webp']:
        return jsonify({'error': 'Only jpg/png/webp allowed'}), 400
    
    # Save to static/profile_photos/
    photos_dir = Path('static/profile_photos')
    photos_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        import cv2
        import numpy as np
        
        file_bytes = np.frombuffer(photo.read(), np.uint8)
        img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        
        if img is None:
            return jsonify({'error': 'Invalid image file'}), 400
        
        filename = f"user_{user_id}.jpg"
        filepath = photos_dir / filename
        
        cv2.imwrite(str(filepath), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        
        db.save_user_photo(user_id, str(filepath), organization_id=get_current_tenant_id())
        return jsonify({
            'success': True,
            'photo_url': f'/profile_photos/{filename}'
        }), 200
        
    except Exception as e:
        logger.error(f"Photo upload error: {e}")
        return jsonify({'error': f'Photo processing failed: {str(e)}'}), 500

@app.route('/api/users/<int:user_id>/photo', methods=['GET'])
def get_user_photo(user_id):
    """Serve profile photo for a user with tenant verification."""
    if not verify_user_access(user_id):
        return jsonify({'error': 'User not found or unauthorized'}), 404
    photo_path = db.get_user_photo(user_id, organization_id=get_current_tenant_id())
    if not photo_path or not os.path.exists(photo_path):
        return jsonify({'error': 'No photo found'}), 404
    
    directory = os.path.dirname(os.path.abspath(photo_path))
    filename = os.path.basename(photo_path)
    return send_from_directory(directory, filename)

@app.route('/profile_photos/<path:filename>')
def serve_profile_photo_direct(filename):
    photos_dir = os.path.join(app.root_path, 'static', 'profile_photos')
    return send_from_directory(photos_dir, filename)


# ============================================
# AUTH ENDPOINTS
# ============================================

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    email = str(data.get('email', data.get('username', ''))).strip()
    password = str(data.get('password', ''))
    if not email or not password:
        return jsonify({'success': False, 'message': 'Email and password required'}), 400
    user = db.authenticate_user(email, password)
    if not user:
        return jsonify({'success': False, 'message': 'Invalid credentials'}), 401
    session.clear()
    session['admin_unlocked'] = True
    session['user_id'] = user['id']
    session['organization_id'] = user.get('organization_id') or 'DEFAULT_ORG'
    session['branch_id'] = user.get('branch_id') or 'DEFAULT_BRANCH'
    safe = {k: v for k, v in user.items() if k not in {'password', 'password_hash'}}
    return jsonify({'success': True, 'user': safe})


@app.route('/api/change-password', methods=['POST'])
def api_change_password():
    data = request.get_json() or {}
    user_id = data.get('user_id')
    new_pass = str(data.get('new_password', ''))
    if not user_id or not new_pass:
        return jsonify({'success': False}), 400
    if len(new_pass) < 14:
        return jsonify({'success': False, 'message': 'Password must be at least 14 characters.'}), 400
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid user ID.'}), 400
    session_user_id = session.get('user_id')
    if session_user_id is not None and int(session_user_id) != user_id:
        return jsonify({'success': False, 'message': 'You may only change your own password.'}), 403
    if not verify_user_access(user_id):
        return jsonify({'success': False, 'message': 'Employee not found or access denied.'}), 404
    if not db.change_password(user_id, new_pass):
        return jsonify({'success': False, 'message': 'Password could not be updated.'}), 500
    return jsonify({'success': True})


@app.route('/api/client/login', methods=['POST'])
def api_client_login():
    """
    Authenticate a provisioned client against Main Dashboard.
    Tenant binding lives in this browser session only. Do not rewrite
    config_integration.json (that file is one-instance operator state).
    """
    data = request.get_json() or {}
    username = str(data.get('username') or data.get('email') or '').strip()
    password = str(data.get('password') or '')
    branch_key = str(data.get('branch_key') or '').strip()
    portal_slug = str(data.get('portal_slug') or data.get('company_slug') or '').strip().lower()

    if not username and not branch_key:
        return jsonify({'success': False, 'message': 'Username/Email or Branch Key required'}), 400

    main_url = os.environ.get("MAIN_DASHBOARD_URL", "https://quanteye-main-dashboard-production.up.railway.app").rstrip('/')
    
    auth_data = None
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            f"{main_url}/api/client/authenticate",
            data=json.dumps({
                "username": username,
                "password": password,
                "branch_key": branch_key,
                "portal_slug": portal_slug,
            }).encode('utf-8'),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        auth_timeout = float(os.environ.get("CLIENT_AUTH_TIMEOUT", "12.0"))
        with urllib.request.urlopen(req, timeout=auth_timeout) as resp:
            auth_data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        logger.info("Main Dashboard rejected client login with HTTP %s", e.code)
        if e.code == 403:
            return jsonify({'success': False, 'message': 'These credentials do not belong to this company portal.'}), 403
        return jsonify({'success': False, 'message': 'Invalid client credentials or activation key.'}), 401
    except Exception:
        logger.exception("Main dashboard auth service is unavailable")
        return jsonify({'success': False, 'message': 'Client authentication service is unavailable.'}), 502

    if auth_data and auth_data.get('success'):
        org_name = auth_data.get('organization_name', 'Client Organization')
        branch_name = auth_data.get('branch_name', 'Main Branch')
        modules = auth_data.get('modules') or ['attendance']
        if not isinstance(modules, list):
            modules = ['attendance']
        organization_id = str(auth_data.get('organization_id') or '').strip()
        branch_id = str(auth_data.get('branch_id') or '').strip()
        if not organization_id or not branch_id:
            return jsonify({'success': False, 'message': 'Authentication service returned an invalid tenant binding.'}), 502

        session.clear()
        session['client_user'] = username or org_name
        session['client_org'] = org_name
        session['client_org_id'] = organization_id
        session['organization_id'] = organization_id
        session['client_branch'] = branch_name
        session['client_branch_id'] = branch_id
        session['branch_id'] = branch_id
        session['licensed_modules'] = modules
        session['portal_slug'] = str(auth_data.get('portal_slug') or portal_slug or '').strip().lower()

        return jsonify({
            'success': True,
            'organization_id': organization_id,
            'organization_name': org_name,
            'portal_slug': session['portal_slug'],
            'branch_name': branch_name,
            'modules': modules,
            'user': username or org_name,
            'is_client_session': True,
            'admin_unlocked': False,
        }), 200

    return jsonify({'success': False, 'message': 'Invalid credentials. Please verify your email and password.'}), 401


def _mobile_company(organization_id: str):
    """Ask Main whether this company selected Mobile App, and return its name."""
    org_id = str(organization_id or '').strip()
    if not org_id:
        return False, ''
    main_url = os.environ.get(
        "MAIN_DASHBOARD_URL",
        "https://quanteye-main-dashboard-production.up.railway.app",
    ).rstrip('/')
    try:
        import urllib.parse
        import urllib.request
        with urllib.request.urlopen(
            f"{main_url}/api/public/mobile-module/{urllib.parse.quote(org_id)}",
            timeout=4.0,
        ) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        return bool(payload.get('mobile')), str(payload.get('name') or '').strip()
    except Exception:
        logger.exception("Could not confirm the Mobile App module for %s", org_id)
        return False, ''


def _organization_has_mobile_module(organization_id: str) -> bool:
    """Ask Main whether this company selected the Mobile App module."""
    allowed, _company_name = _mobile_company(organization_id)
    return allowed


@app.route('/m')
@app.route('/mobile')
def mobile_attendance_page():
    """One phone page: login, then that employee's attendance."""
    return render_template('mobile.html')


@app.route('/m/manifest.webmanifest')
def mobile_manifest():
    return jsonify({
        'name': 'QuantEye Attendance',
        'short_name': 'QuantEye',
        'start_url': '/m',
        'scope': '/m',
        'display': 'standalone',
        'background_color': '#eef8ff',
        'theme_color': '#0d9488',
        'icons': [
            {'src': '/m/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/m/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
        ],
    })


@app.route('/.well-known/assetlinks.json')
def mobile_assetlinks():
    """Lets Android open the phone page from the app icon."""
    return send_from_directory(
        os.path.join(app.root_path, 'static', 'mobile'),
        'assetlinks.json',
        mimetype='application/json',
    )


@app.route('/m/QuantEye.apk')
def mobile_apk_download():
    """The same install file for every compatible Android phone."""
    return send_file(
        os.path.join(app.root_path, 'static', 'mobile', 'QuantEye.apk'),
        mimetype='application/vnd.android.package-archive',
        as_attachment=True,
        download_name='QuantEye.apk',
    )


@app.route('/m/icon-<int:size>.png')
def mobile_icon(size):
    """Home-screen icon for the phone app. It contains no attendance logic."""
    if size not in (192, 512):
        return ('', 404)
    return send_from_directory(
        os.path.join(app.root_path, 'static', 'mobile'),
        f'icon-{size}.png',
    )


@app.route('/m/sw.js')
def mobile_service_worker():
    body = (
        "self.addEventListener('install', function(event){ self.skipWaiting(); });\n"
        "self.addEventListener('activate', function(event){ event.waitUntil(self.clients.claim()); });\n"
        "self.addEventListener('fetch', function(event){ event.respondWith(fetch(event.request)); });\n"
    )
    return app.response_class(body, mimetype='application/javascript')


@app.route('/api/mobile/login', methods=['POST'])
def api_mobile_login():
    data = request.get_json(silent=True) or {}
    email = str(data.get('email') or data.get('username') or '').strip()
    password = str(data.get('password') or data.get('pin') or '')
    user = db.authenticate_employee_email(email, password)
    if not user:
        return jsonify({'success': False, 'message': 'Email or password is incorrect.'}), 401
    if not user.get('active', 1) or (user.get('exit_date') and str(user['exit_date'])[:10] < date.today().isoformat()):
        return jsonify({'success': False, 'message': 'Account is inactive. Employee has left the organization.'}), 403
    organization_id = str(user.get('organization_id') or '').strip()
    _allowed, company_name = _mobile_company(organization_id)
    if not company_name:
        company_name = str(user.get('organization_name') or '').strip()
    session['mobile_employee_id'] = int(user['id'])
    session['mobile_organization_id'] = organization_id
    session['mobile_branch_id'] = str(user.get('branch_id') or 'DEFAULT_BRANCH')
    session['mobile_employee_name'] = user.get('name') or ''
    session['mobile_company_name'] = company_name
    return jsonify({'success': True, 'name': user.get('name') or '', 'company': company_name})


@app.route('/api/mobile/logout', methods=['POST'])
def api_mobile_logout():
    for key in (
        'mobile_employee_id',
        'mobile_organization_id',
        'mobile_branch_id',
        'mobile_employee_name',
        'mobile_company_name',
    ):
        session.pop(key, None)
    return jsonify({'success': True})


@app.route('/api/mobile/today', methods=['GET'])
def api_mobile_today():
    user_id = session.get('mobile_employee_id')
    organization_id = session.get('mobile_organization_id')
    if not user_id or not organization_id:
        return jsonify({'success': False, 'message': 'Please log in.'}), 401
    _allowed, company_name = _mobile_company(str(organization_id))
    if not company_name:
        company_name = str(session.get('mobile_company_name') or session.get('mobile_employee_name') or '')
    card = db.employee_mobile_attendance(
        int(user_id),
        str(organization_id),
        str(session.get('mobile_branch_id') or 'DEFAULT_BRANCH'),
    )
    card['company'] = company_name or str(session.get('mobile_company_name') or '')
    if card['company']:
        session['mobile_company_name'] = card['company']
    return jsonify({'success': True, 'attendance': card})


@app.route('/api/mobile/latest-punch', methods=['GET'])
def api_mobile_latest_punch():
    user_id = session.get('mobile_employee_id')
    organization_id = session.get('mobile_organization_id')
    if not user_id or not organization_id:
        return jsonify({'success': False, 'message': 'Please log in.'}), 401
    punch = db.get_latest_employee_punch(
        int(user_id),
        str(organization_id),
        str(session.get('mobile_branch_id') or 'DEFAULT_BRANCH'),
    )
    return jsonify({'success': True, 'punch': punch})


@app.route('/api/mobile/month', methods=['GET'])
def api_mobile_month():
    user_id = session.get('mobile_employee_id')
    organization_id = session.get('mobile_organization_id')
    if not user_id or not organization_id:
        return jsonify({'success': False, 'message': 'Please log in.'}), 401
    target_month = request.args.get('month', '').strip() or None
    month_data = db.employee_mobile_month_attendance(
        int(user_id),
        str(organization_id),
        str(session.get('mobile_branch_id') or 'DEFAULT_BRANCH'),
        target_month=target_month,
    )
    return jsonify({'success': True, 'data': month_data})


@app.route('/api/mobile/profile', methods=['GET'])
def api_mobile_profile():
    user_id = session.get('mobile_employee_id')
    organization_id = session.get('mobile_organization_id')
    if not user_id or not organization_id:
        return jsonify({'success': False, 'message': 'Please log in.'}), 401
    _allowed, company_name = _mobile_company(str(organization_id))
    if not company_name:
        company_name = str(session.get('mobile_company_name') or '')
    profile_data = db.employee_mobile_profile(
        int(user_id),
        str(organization_id),
        str(session.get('mobile_branch_id') or 'DEFAULT_BRANCH'),
        company_name=company_name,
    )
    return jsonify({'success': True, 'profile': profile_data})


@app.route('/api/mobile/credentials', methods=['GET'])
def api_mobile_list_credentials():
    if not (session.get('client_user') or session.get('admin_unlocked')):
        return jsonify({'success': False, 'message': 'Authentication required.'}), 401
    if not licensed_module_flags(browser_licensed_modules() or []).get('mobile'):
        return _forbidden_module_response('mobile')
    result = db.list_mobile_credentials(
        get_current_tenant_id(),
        get_current_branch_id(),
    )
    return jsonify({'success': True, 'credentials': result['credentials']})


@app.route('/api/mobile/credentials', methods=['POST'])
def api_mobile_generate_credentials():
    if not (session.get('client_user') or session.get('admin_unlocked')):
        return jsonify({'success': False, 'message': 'Authentication required.'}), 401
    if not licensed_module_flags(browser_licensed_modules() or []).get('mobile'):
        return _forbidden_module_response('mobile')
    data = request.get_json(silent=True) or {}
    result = db.generate_mobile_credentials(
        get_current_tenant_id(),
        get_current_branch_id(),
        remaining_only=bool(data.get('remaining_only')),
    )
    return jsonify({
        'success': True,
        'phone_page': '/m',
        'credentials': result['credentials'],
        'skipped': result['skipped'],
    })


@app.route('/api/logout', methods=['POST'])
def api_logout():
    """Sign out active user session and re-lock dashboard."""
    session.clear()
    return jsonify({'success': True, 'message': 'Logged out successfully'}), 200


def get_current_tenant_id() -> str:
    """
    Browser tenant comes from the Flask session only.
    config_integration.json is never used for a logged-in client.
    """
    if is_client_browser_session():
        return str(session.get('organization_id') or session.get('client_org_id') or '').strip()
    if session.get('organization_id'):
        return str(session['organization_id']).strip()
    if session.get('client_org_id'):
        return str(session['client_org_id']).strip()
    return 'DEFAULT_ORG'


def get_current_branch_id() -> str:
    """Return the active branch from the browser session only."""
    if is_client_browser_session():
        return str(session.get('branch_id') or session.get('client_branch_id') or '').strip()
    if session.get('branch_id'):
        return str(session['branch_id']).strip()
    if session.get('client_branch_id'):
        return str(session['client_branch_id']).strip()
    return 'DEFAULT_BRANCH'


def payroll_tenant() -> Dict[str, str]:
    return {
        'organization_id': get_current_tenant_id(),
        'branch_id': get_current_branch_id(),
    }


def verify_user_access(user_id: int, organization_id: Optional[str] = None) -> Optional[Dict]:
    """
    Tenant Isolation Security Verification Helper.
    Verify that user exists and strictly belongs to the authoritative tenant.
    Returns user dict if valid and authorized, None otherwise.
    """
    tenant = organization_id or get_current_tenant_id()
    user = db.get_user_by_id(user_id)
    if not user:
        return None
    user_org = user.get('organization_id') or 'DEFAULT_ORG'
    if user_org != tenant:
        logger.warning(f"Tenant isolation violation prevented: User #{user_id} belongs to '{user_org}', caller is '{tenant}'")
        return None
    branch = get_current_branch_id()
    user_branch = user.get('branch_id') or 'DEFAULT_BRANCH'
    if user_branch != branch:
        logger.warning(f"Branch isolation violation prevented: User #{user_id} belongs to '{user_branch}', caller is '{branch}'")
        return None
    return user


# ============================================
# ADMIN CONFIGURATION PROTECTION
# ============================================

# Set ADMIN_CONFIG_PASSWORD in the local environment; never commit the value.
_ADMIN_CONFIG_PASSWORD = os.environ.get('ADMIN_CONFIG_PASSWORD')

@app.route('/api/admin/verify-password', methods=['POST'])
def api_admin_verify_password():
    """Verify admin config password server-side. Sets session flag on success."""
    data = request.get_json() or {}
    password = str(data.get('password', '')).strip()
    if not _ADMIN_CONFIG_PASSWORD or len(_ADMIN_CONFIG_PASSWORD) < 16:
        logger.error("ADMIN_CONFIG_PASSWORD is missing or shorter than 16 characters.")
        return jsonify({'success': False, 'message': 'Admin password is not securely configured.'}), 503
    if secrets.compare_digest(password, _ADMIN_CONFIG_PASSWORD):
        session.clear()
        session['admin_unlocked'] = True
        return jsonify({'success': True, 'message': 'Access granted'})
    return jsonify({'success': False, 'message': 'Incorrect password'}), 401


def _can_manage_enrollment() -> bool:
    if session.get('admin_unlocked') and not is_client_browser_session():
        return True
    return bool(is_client_browser_session() and session.get('enrollment_unlocked'))


@app.route('/api/admin/lock', methods=['POST'])
def api_admin_lock():
    """Lock admin configuration and revoke enrollment access. Client login stays."""
    if is_client_browser_session():
        session.pop('enrollment_enabled', None)
        session.pop('enrollment_unlocked', None)
        return jsonify({'success': True, 'message': 'Staff Enrollment locked'})
    session.pop('admin_unlocked', None)
    session.pop('enrollment_enabled', None)
    session.pop('enrollment_unlocked', None)
    return jsonify({'success': True, 'message': 'Configuration locked'})


@app.route('/api/enrollment/unlock', methods=['POST'])
def api_enrollment_unlock():
    """Client Settings: enable Staff Enrollment with the handover enrollment key."""
    if not is_client_browser_session():
        return jsonify({'success': False, 'message': 'Client login required'}), 401
    flags = licensed_module_flags(session_licensed_modules())
    if not flags.get('attendance'):
        return jsonify({'success': False, 'message': 'Attendance is not licensed for this organization.'}), 403
    data = request.get_json(silent=True) or {}
    enrollment_key = str(data.get('enrollment_key') or data.get('enrollmentKey') or '').strip()
    if not enrollment_key:
        return jsonify({'success': False, 'message': 'Staff enrollment key is required.'}), 400
    org_id = str(session.get('organization_id') or '').strip()
    main_url = os.environ.get("MAIN_DASHBOARD_URL", "https://quanteye-main-dashboard-production.up.railway.app").rstrip('/')
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            f"{main_url}/api/client/verify-enrollment-key",
            data=json.dumps({"organization_id": org_id, "enrollment_key": enrollment_key}).encode('utf-8'),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        if not payload.get('success'):
            return jsonify({'success': False, 'message': 'Invalid staff enrollment key.'}), 401
    except urllib.error.HTTPError:
        return jsonify({'success': False, 'message': 'Invalid staff enrollment key.'}), 401
    except Exception:
        logger.exception("Enrollment key verification failed")
        return jsonify({'success': False, 'message': 'Could not verify the staff enrollment key.'}), 502
    session['enrollment_unlocked'] = True
    session['enrollment_enabled'] = True
    return jsonify({'success': True, 'message': 'Staff Enrollment enabled. Add Employee is now available.', 'enrollment_enabled': True})


@app.route('/api/enrollment/lock', methods=['POST'])
def api_enrollment_lock():
    """Hide Staff Enrollment again. Does not log the client out."""
    if not is_client_browser_session() and not session.get('admin_unlocked'):
        return jsonify({'success': False, 'message': 'Authentication required'}), 401
    session.pop('enrollment_enabled', None)
    session.pop('enrollment_unlocked', None)
    return jsonify({'success': True, 'message': 'Staff Enrollment locked'})


@app.route('/api/admin/enable-enrollment', methods=['POST'])
def api_admin_enable_enrollment():
    """Enable staff enrollment (platform admin or client after enrollment-key unlock)."""
    if not _can_manage_enrollment():
        return jsonify({'success': False, 'message': 'Enter the staff enrollment key in Settings first.'}), 403
    session['enrollment_enabled'] = True
    return jsonify({'success': True, 'message': 'Staff Enrollment enabled successfully'})


@app.route('/api/admin/disable-enrollment', methods=['POST'])
def api_admin_disable_enrollment():
    """Disable staff enrollment (platform admin or client after enrollment-key unlock)."""
    if not _can_manage_enrollment():
        return jsonify({'success': False, 'message': 'Enter the staff enrollment key in Settings first.'}), 403
    session.pop('enrollment_enabled', None)
    return jsonify({'success': True, 'message': 'Staff Enrollment disabled'})


@app.route('/api/admin/status', methods=['GET'])
def api_admin_status():
    """Return current browser auth state. Client sessions are not platform admin."""
    client_session = is_client_browser_session()
    platform_admin = bool(session.get('admin_unlocked')) and not client_session
    return jsonify({
        'authenticated': bool(platform_admin or client_session),
        'admin_unlocked': platform_admin,
        'client_authenticated': client_session,
        'is_client_session': client_session,
        'enrollment_enabled': bool(session.get('enrollment_enabled')),
        'enrollment_unlocked': bool(session.get('enrollment_unlocked') or session.get('enrollment_enabled')),
        'organization_id': session.get('organization_id') if (platform_admin or client_session) else None,
        'branch_id': session.get('branch_id') if (platform_admin or client_session) else None,
        'licensed_modules': session_licensed_modules() if client_session else [],
    })


def _require_enrollment_access():
    """Staff create/train APIs require enrollment to be explicitly enabled."""
    if session.get('enrollment_enabled'):
        return None
    if session.get('admin_unlocked') and not is_client_browser_session() and session.get('enrollment_enabled'):
        return None
    return jsonify({'success': False, 'message': 'Staff Enrollment is disabled. Enable it from Settings with the staff enrollment key.'}), 403


def _require_admin_configuration():
    """Require an authenticated browser session for protected payroll changes."""
    if session.get('client_user') or session.get('admin_unlocked'):
        return None
    return jsonify({'success': False, 'error': 'Admin authentication required'}), 403


# ============================================
# STAFF / USER ENDPOINTS
# ============================================

def _safe_user(u):
    return {k: v for k, v in u.items() if k not in {'password', 'password_hash'}} if u else {}


@app.route('/api/staff', methods=['GET'])
def api_get_staff():
    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    role = request.args.get('role')
    users = db.get_all_users(
        role=role,
        organization_id=current_tenant,
        branch_id=current_branch,
    )
    return jsonify([_safe_user(u) for u in users])


@app.route('/api/staff', methods=['POST'])
def api_add_staff():
    data = request.get_json() or {}
    try:
        shift_id = int(data.get('shift_id', 1) or 1)
    except (ValueError, TypeError):
        shift_id = 1
    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    try:
        salary = db.parse_salary_amount(data.get('salary', 0))
    except ValueError as salary_err:
        return jsonify({'success': False, 'message': str(salary_err)}), 400
    uid = db.add_user(
        name=data.get('name', ''),
        email=data.get('email', ''),
        role=data.get('role', 'staff'),
        department=data.get('department', ''),
        phone=data.get('phone', ''),
        notes=data.get('notes', ''),
        cnic=data.get('cnic', ''),
        position=data.get('position', ''),
        salary=salary,
        join_date=data.get('join_date', ''),
        shift_id=shift_id,
        checkin_cameras=data.get('checkin_cameras', 'all') or 'all',
        checkout_cameras=data.get('checkout_cameras', 'all') or 'all',
        organization_id=current_tenant,
        branch_id=current_branch
    )
    if uid is None:
        return jsonify({'success': False, 'message': 'Email or name already exists'}), 409
    return jsonify({'success': True, 'user': _safe_user(db.get_user_by_id(uid))}), 201


@app.route('/api/users/<int:user_id>', methods=['GET'])
def api_get_user(user_id):
    user = verify_user_access(user_id)
    if not user:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(_safe_user(user))


@app.route('/api/users/<int:user_id>/update', methods=['POST', 'PUT'])
@app.route('/api/users/<int:user_id>', methods=['PUT', 'POST'])
def api_update_user(user_id):
    """Update employee HR profile, shift assignment, and camera assignments with tenant verification."""
    user = verify_user_access(user_id)
    if not user:
        return jsonify({'error': 'Staff member not found'}), 404

    data = request.get_json() or {}
    name = data.get('name')
    if not name or not str(name).strip():
        return jsonify({'error': 'Name is required'}), 400

    shift_val = data.get('shift_id')
    shift_id = None
    if shift_val is not None and str(shift_val).strip() != '':
        try:
            shift_id = int(shift_val)
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid shift ID'}), 400

        if not db.get_shift_by_id(shift_id):
            return jsonify({'error': 'Invalid shift ID'}), 400

    salary_val = data.get('salary')
    try:
        salary = db.parse_salary_amount(salary_val, default=None) if salary_val is not None and str(salary_val).strip() != '' else None
    except ValueError as salary_err:
        return jsonify({'error': str(salary_err)}), 400

    checkin_cameras = data.get('checkin_cameras')
    checkout_cameras = data.get('checkout_cameras')

    try:
        success = db.update_user(
        user_id=user_id,
        name=name.strip(),
        email=data.get('email'),
        phone=data.get('phone'),
        department=data.get('department'),
        notes=data.get('notes'),
        position=data.get('position'),
        cnic=data.get('cnic'),
        salary=salary,
        join_date=data.get('join_date'),
        shift_id=shift_id,
        checkin_cameras=checkin_cameras,
        checkout_cameras=checkout_cameras,
        shift_effective_from=data.get('shift_effective_from'),
        exit_date=data.get('exit_date'),
        confirm_shift_reclassify=bool(data.get('confirm_shift_reclassify')),
        )
    except ValueError as exc:
        status = 409 if str(exc) == db.PAYROLL_LOCK_MESSAGE or 'reclassifies' in str(exc) else 400
        return jsonify({'error': str(exc), 'field': 'shift_effective_from' if 'shift' in str(exc).lower() else 'exit_date'}), status
    if success:
        refresh_embedding_cache()
        return jsonify({'success': True, 'message': f'Staff profile for {name.strip()} updated successfully'}), 200
    return jsonify({'error': f'Failed to update staff ID {user_id}'}), 500





@app.route('/api/users/bulk-assign-cameras', methods=['POST'])
@app.route('/api/staff/bulk-assign-cameras', methods=['POST'])
@app.route('/api/users/bulk-cameras', methods=['POST'])
@app.route('/api/staff/bulk-cameras', methods=['POST'])
def api_bulk_assign_cameras():
    """Bulk assign Check-In and Check-Out camera permissions to staff members."""
    data = request.get_json() or {}
    user_ids = data.get('user_ids', 'all')
    checkin_cams = data.get('checkin_cameras', 'all')
    checkout_cams = data.get('checkout_cameras', 'all')

    success, count = db.bulk_assign_cameras(
        user_ids=user_ids,
        checkin_cameras=checkin_cams,
        checkout_cameras=checkout_cams
    )
    if success:
        return jsonify({
            'success': True,
            'count': count,
            'updated_count': count,
            'message': f'Cameras successfully assigned to {count} employee(s)'
        }), 200
    return jsonify({'error': 'Failed to perform bulk camera assignment'}), 500


# ============================================
# ATTENDANCE ENDPOINTS
# ============================================

@app.route('/api/attendance', methods=['GET'])
def api_get_attendance():
    user_id = request.args.get('user_id', type=int)
    limit = request.args.get('limit', 200, type=int)
    start = request.args.get('start')
    end = request.args.get('end')
    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    if user_id and start and end:
        logs = db.get_attendance_by_user(user_id, start_date=start, end_date=end)
    elif user_id:
        logs = db.get_attendance_by_user(user_id)
    else:
        logs = db.get_attendance_logs(limit=limit, organization_id=current_tenant, branch_id=current_branch)
    return jsonify(logs)


@app.route('/attendance-report')
def attendance_report_page():
    """Serve the complete attendance records and department reports page."""
    return render_template('attendance_report.html')


@app.route('/staff-management')
@app.route('/staff-directory')
@app.route('/staff')
@app.route('/users-management')
@app.route('/staff-list-page')
def staff_management_page():
    """Redirect to main dashboard index page with staff-view tab active."""
    return redirect('/?tab=staff-view')


def format_report_date(date_str: str) -> str:
    """Format YYYY-MM-DD to DD Mon YYYY (e.g. 06 Oct 2026)."""
    if not date_str or str(date_str).strip() in ('', 'None', '—', '-', '--'):
        return '-'
    try:
        s = str(date_str).strip().split('T')[0].split(' ')[0]
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.strftime("%d %b %Y")
    except Exception:
        return str(date_str)


def format_report_time(time_val: Any) -> str:
    """Format timestamp/time to 12-hour AM/PM string (e.g. 08:34 AM)."""
    if not time_val or str(time_val).strip() in ('', 'None', '—', '-', '--:--', '--'):
        return '-'
    try:
        s = str(time_val).strip()
        if ' ' in s:
            s = s.split(' ')[1]
        elif 'T' in s:
            s = s.split('T')[1]
        
        s = s.split('.')[0] # strip microseconds
        parts = s.split(':')
        if len(parts) >= 2:
            hour = int(parts[0])
            minute = int(parts[1])
            second = int(parts[2]) if len(parts) > 2 else 0
            t = datetime.strptime(f"{hour:02d}:{minute:02d}:{second:02d}", "%H:%M:%S")
            return t.strftime("%I:%M %p")
    except Exception:
        pass
    return str(time_val)


def generate_excel_attendance_report(report_data: dict, filter_info: dict = None) -> Any:
    """
    Generate a clean, client-facing professional Excel attendance report (.xlsx).
    Contains NO technical/confidence scores and strictly respects all active filters.
    """
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Attendance Report"
    ws.views.sheetView[0].showGridLines = True

    # Color Palette & Styling Tokens
    brand_dark = "0F2942"      # Deep Navy
    brand_blue = "1A56DB"      # Professional Accent
    text_dark = "1E293B"       # Slate 800
    text_muted = "64748B"      # Slate 500
    bg_subtle = "F8FAFC"       # Light gray zebra
    border_color = "CBD5E1"    # Slate 300
    
    font_family = "Segoe UI"

    title_font = Font(name=font_family, size=16, bold=True, color=brand_dark)
    subtitle_font = Font(name=font_family, size=11, bold=True, color=brand_blue)
    meta_label_font = Font(name=font_family, size=9, bold=True, color=text_muted)
    meta_val_font = Font(name=font_family, size=9, bold=False, color=text_dark)
    
    header_font = Font(name=font_family, size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color=brand_dark, end_color=brand_dark, fill_type="solid")
    
    thin_border_side = Side(style='thin', color=border_color)
    cell_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
    
    alt_row_fill = PatternFill(start_color=bg_subtle, end_color=bg_subtle, fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

    # Status Pill Styling Tokens
    status_styles = {
        'PRESENT': {
            'font': Font(name=font_family, size=9, bold=True, color="065F46"),
            'fill': PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")
        },
        'CHECKED_IN': {
            'font': Font(name=font_family, size=9, bold=True, color="1E40AF"),
            'fill': PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
        },
        'CHECKED_OUT': {
            'font': Font(name=font_family, size=9, bold=True, color="4338CA"),
            'fill': PatternFill(start_color="E0E7FF", end_color="E0E7FF", fill_type="solid")
        },
        'LATE': {
            'font': Font(name=font_family, size=9, bold=True, color="92400E"),
            'fill': PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
        },
        'ABSENT': {
            'font': Font(name=font_family, size=9, bold=True, color="991B1B"),
            'fill': PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
        },
        'ON_LEAVE': {
            'font': Font(name=font_family, size=9, bold=True, color="0F766E"),
            'fill': PatternFill(start_color="CCFBF1", end_color="CCFBF1", fill_type="solid")
        }
    }

    # 1. Sheet Header: Company Name & Report Title
    ws['A1'] = "QIntellect Technologies"
    ws['A1'].font = title_font
    ws['A1'].alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 24

    ws['A2'] = "Attendance Report"
    ws['A2'].font = subtitle_font
    ws['A2'].alignment = Alignment(vertical="center")
    ws.row_dimensions[2].height = 18

    # 2. Active Report Information & Filter Badges
    filter_info = filter_info or {}
    date_label = report_data.get('date_label', 'Today')
    dept_label = filter_info.get('department')
    shift_label = filter_info.get('shift')
    status_label = filter_info.get('status')
    staff_label = filter_info.get('search')
    
    meta_items = [("Report Period", date_label)]
    if dept_label and dept_label.lower() not in ('all', 'all departments', 'none'):
        meta_items.append(("Department", dept_label))
    if shift_label and str(shift_label).lower() not in ('all', 'all shifts', 'none'):
        meta_items.append(("Shift", str(shift_label)))
    if status_label and status_label.lower() not in ('all', 'all statuses', 'none'):
        meta_items.append(("Status Filter", status_label.upper()))
    if staff_label and staff_label.strip():
        meta_items.append(("Employee / Search", staff_label.strip()))
    meta_items.append(("Generated At", datetime.now().strftime("%d %b %Y, %I:%M %p")))

    meta_text = "   •   ".join([f"{k}: {v}" for k, v in meta_items])
    ws.merge_cells('A4:J4')
    meta_cell = ws['A4']
    meta_cell.value = meta_text
    meta_cell.font = Font(name=font_family, size=9.5, italic=False, color="334155")
    meta_cell.fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    meta_cell.alignment = Alignment(vertical="center", indent=1)
    meta_cell.border = cell_border
    ws.row_dimensions[4].height = 22

    # 3. Summary Statistics Cards
    records = report_data.get('records', [])
    tot = len(records)
    pres_cnt = sum(1 for r in records if (r.get('status') or '').lower() in ('present', 'checked_in', 'checked_out'))
    checked_in_cnt = sum(1 for r in records if (r.get('status') or '').lower() == 'checked_in')
    checked_out_cnt = sum(1 for r in records if (r.get('status') or '').lower() == 'checked_out')
    late_cnt = sum(1 for r in records if (r.get('status') or '').lower() == 'late')
    absent_cnt = sum(1 for r in records if (r.get('status') or '').lower() == 'absent')

    kpi_cards = [
        ("Total Records", tot, "A", "B"),
        ("Present", pres_cnt, "C", "D"),
        ("Checked In", checked_in_cnt, "E", "E"),
        ("Checked Out", checked_out_cnt, "F", "F"),
        ("Late", late_cnt, "G", "H"),
        ("Absent", absent_cnt, "I", "J")
    ]

    card_header_fill = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")
    card_value_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    for title, val, start_col, end_col in kpi_cards:
        if start_col != end_col:
            ws.merge_cells(f"{start_col}6:{end_col}6")
            ws.merge_cells(f"{start_col}7:{end_col}7")
        
        lbl_cell = ws[f"{start_col}6"]
        lbl_cell.value = title
        lbl_cell.font = meta_label_font
        lbl_cell.alignment = Alignment(horizontal="center", vertical="center")
        lbl_cell.fill = card_header_fill
        lbl_cell.border = cell_border

        val_cell = ws[f"{start_col}7"]
        val_cell.value = val
        val_cell.font = Font(name=font_family, size=12, bold=True, color=brand_dark)
        val_cell.alignment = Alignment(horizontal="center", vertical="center")
        val_cell.fill = card_value_fill
        val_cell.border = cell_border

        # Ensure borders are set on all merged cells
        for c in range(ord(start_col), ord(end_col) + 1):
            col_ch = chr(c)
            ws[f"{col_ch}6"].border = cell_border
            ws[f"{col_ch}7"].border = cell_border

    ws.row_dimensions[6].height = 16
    ws.row_dimensions[7].height = 20

    # 4. Clean Attendance Table Header (Row 9)
    # Exactly 11 columns - NO Confidence column anywhere
    headers = [
        ("Employee ID", 14, "center"),
        ("Employee Name", 24, "left"),
        ("Department", 20, "left"),
        ("Designation", 20, "left"),
        ("Shift", 20, "left"),
        ("Date", 15, "center"),
        ("Check-In", 14, "center"),
        ("Check-In Camera", 18, "left"),
        ("Check-Out", 14, "center"),
        ("Check-Out Camera", 18, "left"),
        ("Status", 16, "center")
    ]

    header_row = 9
    ws.row_dimensions[header_row].height = 26

    for col_idx, (header_text, default_width, h_align) in enumerate(headers, 1):
        cell = ws.cell(row=header_row, column=col_idx, value=header_text)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal=h_align, vertical="center", wrap_text=True)
        cell.border = cell_border

    # 5. Populate Data Rows (Row 10+)
    start_data_row = header_row + 1
    for i, r in enumerate(records):
        curr_row = start_data_row + i
        ws.row_dimensions[curr_row].height = 20
        is_alt = (i % 2 == 1)
        base_fill = alt_row_fill if is_alt else white_fill

        # Status normalization (ATT-05: preserve day type label)
        raw_status = (r.get('status') or 'absent').strip().upper()
        day_type_lbl = r.get('day_type_label')
        if day_type_lbl and '—' in str(day_type_lbl):
            status_display = str(day_type_lbl)
            style_key = 'PRESENT' if 'present' in status_display.lower() else ('LATE' if 'late' in status_display.lower() else 'ABSENT')
            style_cfg = status_styles.get(style_key, status_styles['PRESENT'])
        else:
            if raw_status == 'PRESENT':
                status_display = 'PRESENT'
            elif raw_status == 'CHECKED_IN':
                status_display = 'CHECKED_IN'
            elif raw_status == 'CHECKED_OUT':
                status_display = 'CHECKED_OUT'
            elif raw_status == 'LATE':
                status_display = 'LATE'
            elif raw_status in ('LEAVE', 'ON_LEAVE', 'ON LEAVE'):
                status_display = 'ON_LEAVE'
            else:
                status_display = 'ABSENT'
            style_cfg = status_styles.get(status_display, status_styles['ABSENT'])
        
        # Formatted fields
        emp_id = str(r.get('user_id', ''))
        emp_id_disp = f"EMP-{int(emp_id):03d}" if emp_id.isdigit() else str(emp_id)
        emp_name = str(r.get('name', '—'))
        dept = str(r.get('department') or '—')
        designation = str(r.get('position') or 'Staff Member')
        
        shift_name = r.get('shift_name') or 'General Shift'
        shift_start = r.get('shift_start')
        shift_end = r.get('shift_end')
        if shift_start and shift_end:
            shift_disp = f"{shift_name} ({shift_start}-{shift_end})"
        else:
            shift_disp = shift_name

        rec_date = format_report_date(r.get('date', ''))
        check_in = format_report_time(r.get('first_seen'))
        check_out = format_report_time(r.get('last_seen'))
        
        # Camera names
        cin_cam = r.get('checkin_camera_name') or r.get('checkin_camera') or r.get('location') or '—'
        cout_cam = r.get('checkout_camera_name') or r.get('checkout_camera') or r.get('checkout_location') or '—'
        if check_in == '—':
            cin_cam = '—'
        if check_out == '—':
            cout_cam = '—'

        row_values = [
            (emp_id_disp, Alignment(horizontal="center", vertical="center"), Font(name=font_family, size=9.5, bold=True, color="1E40AF"), base_fill),
            (emp_name, Alignment(horizontal="left", vertical="center"), Font(name=font_family, size=9.5, bold=True, color=text_dark), base_fill),
            (dept, Alignment(horizontal="left", vertical="center"), Font(name=font_family, size=9.5, color="334155"), base_fill),
            (designation, Alignment(horizontal="left", vertical="center"), Font(name=font_family, size=9.5, color=text_muted), base_fill),
            (shift_disp, Alignment(horizontal="left", vertical="center"), Font(name=font_family, size=9, color="475569"), base_fill),
            (rec_date, Alignment(horizontal="center", vertical="center"), Font(name=font_family, size=9.5, color=text_dark), base_fill),
            (check_in, Alignment(horizontal="center", vertical="center"), Font(name=font_family, size=9.5, color=text_dark if check_in != '—' else text_muted), base_fill),
            (cin_cam, Alignment(horizontal="left", vertical="center"), Font(name=font_family, size=9, color=text_dark if cin_cam != '—' else text_muted), base_fill),
            (check_out, Alignment(horizontal="center", vertical="center"), Font(name=font_family, size=9.5, color=text_dark if check_out != '—' else text_muted), base_fill),
            (cout_cam, Alignment(horizontal="left", vertical="center"), Font(name=font_family, size=9, color=text_dark if cout_cam != '—' else text_muted), base_fill),
            (status_display, Alignment(horizontal="center", vertical="center"), style_cfg['font'], style_cfg['fill'])
        ]

        for col_idx, (val, align, font, fill) in enumerate(row_values, 1):
            cell = ws.cell(row=curr_row, column=col_idx, value=val)
            cell.alignment = align
            cell.font = font
            cell.fill = fill
            cell.border = cell_border

    last_row = start_data_row + len(records) - 1 if records else header_row

    # 6. Freeze Header Row & Enable Excel Filters
    ws.freeze_panes = ws['A10']
    ws.auto_filter.ref = f"A{header_row}:K{max(last_row, header_row)}"

    # 7. Auto-fit column widths with sensible min/max constraints
    for col_idx, (header_text, default_width, _) in enumerate(headers, 1):
        col_letter = get_column_letter(col_idx)
        max_len = len(str(header_text))
        for row in range(start_data_row, last_row + 1):
            val = ws.cell(row=row, column=col_idx).value
            if val:
                max_len = max(max_len, len(str(val)))
        
        computed_width = max(default_width, min(max_len + 3, 35))
        ws.column_dimensions[col_letter].width = computed_width

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def generate_report_filename(report_data: dict, department: str = None, shift: str = None, status: str = None) -> str:
    """Generate professional, clean filename based on active report filters."""
    target_date = report_data.get('date_label') or 'Report'
    # Format date segment
    clean_date = str(target_date).replace(' ', '_').replace('/', '-').replace(':', '-')
    
    parts = ["Attendance_Report"]
    
    if department and department.strip().lower() not in ('all', 'all departments', 'none'):
        clean_dept = department.strip().replace(' ', '_').replace('&', 'and')
        parts.append(clean_dept)
        
    if shift and str(shift).strip().lower() not in ('all', 'all shifts', 'none'):
        clean_shift = str(shift).strip().replace(' ', '_')
        parts.append(clean_shift)
        
    if status and status.strip().lower() not in ('all', 'all statuses', 'none'):
        parts.append(status.strip().capitalize())
        
    parts.append(clean_date)
    return "_".join(parts) + ".xlsx"


@app.route('/api/reports/attendance', methods=['GET'])
def api_reports_attendance():
    """Return comprehensive attendance report with combinable filters."""
    target_date = request.args.get('date', '').strip() or None
    start_date = request.args.get('start_date', '').strip() or None
    end_date = request.args.get('end_date', '').strip() or None
    department = request.args.get('department', '').strip() or None
    shift_id = request.args.get('shift_id', '').strip() or request.args.get('shift', '').strip() or None
    status = request.args.get('status', '').strip() or None
    search = request.args.get('search', '').strip() or None
    checkin_camera = request.args.get('checkin_camera', '').strip() or None
    checkout_camera = request.args.get('checkout_camera', '').strip() or None
    camera_id = request.args.get('camera_id', '').strip() or request.args.get('camera', '').strip() or None
    user_id = request.args.get('user_id', type=int)

    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    report = db.get_detailed_attendance_report(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        department=department,
        shift_id=shift_id,
        status_filter=status,
        search_query=search,
        checkin_camera=checkin_camera,
        checkout_camera=checkout_camera,
        camera_id=camera_id,
        user_id=user_id,
        organization_id=current_tenant,
        branch_id=current_branch,
    )
    return jsonify(report), 200


@app.route('/api/reports/departments', methods=['GET'])
def api_reports_departments():
    """Return list of distinct departments."""
    depts = db.get_all_departments(organization_id=get_current_tenant_id())
    return jsonify({'departments': depts}), 200


@app.route('/api/reports/shifts', methods=['GET'])
def api_reports_shifts():
    """Return list of active shifts for filtering."""
    shifts = db.get_all_shifts(active_only=True, organization_id=get_current_tenant_id())
    return jsonify({'shifts': shifts}), 200


@app.route('/api/reports/export/excel', methods=['GET'])
@app.route('/api/reports/attendance/export', methods=['GET'])
def api_reports_export_excel():
    """Export attendance report to professionally formatted Excel workbook (.xlsx)."""
    target_date = request.args.get('date', '').strip() or None
    start_date = request.args.get('start_date', '').strip() or None
    end_date = request.args.get('end_date', '').strip() or None
    department = request.args.get('department', '').strip() or None
    shift_id = request.args.get('shift_id', '').strip() or request.args.get('shift', '').strip() or None
    status = request.args.get('status', '').strip() or None
    search = request.args.get('search', '').strip() or None
    checkin_camera = request.args.get('checkin_camera', '').strip() or None
    checkout_camera = request.args.get('checkout_camera', '').strip() or None
    camera_id = request.args.get('camera_id', '').strip() or request.args.get('camera', '').strip() or None
    user_id = request.args.get('user_id', type=int)

    report = db.get_detailed_attendance_report(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        department=department,
        shift_id=shift_id,
        status_filter=status,
        search_query=search,
        checkin_camera=checkin_camera,
        checkout_camera=checkout_camera,
        camera_id=camera_id,
        user_id=user_id,
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    )

    filter_info = {
        'department': department if department and department.lower() != 'all' else None,
        'shift': shift_id if shift_id and str(shift_id).lower() != 'all' else None,
        'status': status.upper() if status and status.lower() != 'all' else None,
        'search': search if search and search.strip() else None
    }

    excel_buf = generate_excel_attendance_report(report, filter_info)
    filename = generate_report_filename(report, department=department, shift=shift_id, status=status)

    return send_file(
        excel_buf,
        mimetype="打击vnd.openxmlformats-officedocument.spreadsheetml.sheet".replace("打击", "application/"),
        as_attachment=True,
        download_name=filename
    )


@app.route('/api/reports/export/pdf', methods=['GET'])
def api_reports_export_pdf():
    """Export the filtered attendance report as a downloadable professional PDF."""
    from payroll_extensions import attendance_report_pdf_bytes

    target_date = request.args.get('date', '').strip() or None
    start_date = request.args.get('start_date', '').strip() or None
    end_date = request.args.get('end_date', '').strip() or None
    department = request.args.get('department', '').strip() or None
    shift_id = request.args.get('shift_id', '').strip() or request.args.get('shift', '').strip() or None
    status = request.args.get('status', '').strip() or None
    search = request.args.get('search', '').strip() or None
    checkin_camera = request.args.get('checkin_camera', '').strip() or None
    checkout_camera = request.args.get('checkout_camera', '').strip() or None
    camera_id = request.args.get('camera_id', '').strip() or request.args.get('camera', '').strip() or None
    user_id = request.args.get('user_id', type=int)

    report = db.get_detailed_attendance_report(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        department=department,
        shift_id=shift_id,
        status_filter=status,
        search_query=search,
        checkin_camera=checkin_camera,
        checkout_camera=checkout_camera,
        camera_id=camera_id,
        user_id=user_id,
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    )
    date_label = report.get('date_label', 'Today')
    branch_cfg = db.get_branch_settings()
    org_name = (branch_cfg.get('branch_name') or branch_cfg.get('company_name') or 'Organization').strip()

    dept_disp = department if department and department.lower() != 'all' else 'All Departments'
    status_disp = status.upper() if status and status.lower() != 'all' else 'All Statuses'
    shift_disp = str(shift_id) if shift_id and str(shift_id).lower() != 'all' else 'All Shifts'

    filter_info = {
        'date_range': date_label,
        'department': dept_disp,
        'status': status_disp,
        'shift': shift_disp,
        'search': search or 'None',
        'generated_at': datetime.now().strftime('%d %b %Y, %I:%M %p')
    }

    pdf_data = attendance_report_pdf_bytes(report, filter_info, org_name)
    buffer = io.BytesIO(pdf_data)
    buffer.seek(0)
    filename = 'attendance-report-{}.pdf'.format((date_label or 'period').replace(' ', '_'))
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/pdf')


@app.route('/api/reports/export/csv', methods=['GET'])
def api_reports_export_csv():
    """Export attendance report records to downloadable professional CSV without technical confidence metrics."""
    import csv
    import io

    target_date = request.args.get('date', '').strip() or None
    start_date = request.args.get('start_date', '').strip() or None
    end_date = request.args.get('end_date', '').strip() or None
    department = request.args.get('department', '').strip() or None
    shift_id = request.args.get('shift_id', '').strip() or request.args.get('shift', '').strip() or None
    status = request.args.get('status', '').strip() or None
    search = request.args.get('search', '').strip() or None
    checkin_camera = request.args.get('checkin_camera', '').strip() or None
    checkout_camera = request.args.get('checkout_camera', '').strip() or None
    camera_id = request.args.get('camera_id', '').strip() or request.args.get('camera', '').strip() or None
    user_id = request.args.get('user_id', type=int)

    report = db.get_detailed_attendance_report(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        department=department,
        shift_id=shift_id,
        status_filter=status,
        search_query=search,
        checkin_camera=checkin_camera,
        checkout_camera=checkout_camera,
        camera_id=camera_id,
        user_id=user_id,
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    )

    records = report.get('records', [])
    date_label = report.get('date_label', 'Today')
    
    # Active filters metadata
    dept_disp = department if department and department.lower() != 'all' else 'All Departments'
    shift_disp_filter = str(shift_id) if shift_id and str(shift_id).lower() != 'all' else 'All Shifts'
    status_disp_filter = status.upper() if status and status.lower() != 'all' else 'All Statuses'
    gen_time = datetime.now().strftime("%d %b %Y, %I:%M %p")

    # KPI counts strictly from matching records
    tot = len(records)
    pres_cnt = sum(1 for r in records if (r.get('status') or '').lower() in ('present', 'checked_in', 'checked_out'))
    checked_in_cnt = sum(1 for r in records if (r.get('status') or '').lower() == 'checked_in')
    checked_out_cnt = sum(1 for r in records if (r.get('status') or '').lower() == 'checked_out')
    late_cnt = sum(1 for r in records if (r.get('status') or '').lower() == 'late')
    absent_cnt = sum(1 for r in records if (r.get('status') or '').lower() == 'absent')

    is_multi_day = report.get('is_multi_day', False)

    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    
    # Top Report Banner & Information
    writer.writerow(['QIntellect Technologies - Attendance Report'])
    writer.writerow([f'Report Period: {date_label} | Department: {dept_disp} | Shift: {shift_disp_filter} | Status: {status_disp_filter} | Generated At: {gen_time}'])
    
    summary = report.get('summary', {})
    if is_multi_day:
        tot_enrolled = summary.get('total_enrolled', tot)
        att_cnt = summary.get('attended_count', 0)
        zero_cnt = summary.get('zero_attendance_count', 0)
        tot_pres_d = summary.get('total_present_days', 0)
        tot_late_d = summary.get('total_late_days', 0)
        tot_abs_d = summary.get('total_absent_days', 0)
        rate = summary.get('attendance_rate', 0.0)
        writer.writerow([f'Total Enrolled: {tot_enrolled} | Attended In Period: {att_cnt} | Zero Attendance: {zero_cnt} | Total Present Days: {tot_pres_d} | Total Late Days: {tot_late_d} | Overall Rate: {rate}%'])
    else:
        pres_cnt = summary.get('present', 0)
        late_cnt = summary.get('late', 0)
        absent_cnt = summary.get('absent', 0)
        writer.writerow([f'Total Matching: {tot} | Present: {pres_cnt} | Late: {late_cnt} | Absent: {absent_cnt}'])
    
    writer.writerow([]) # Blank separator row

    if is_multi_day:
        # Multi-Day Summary Columns (ONE ROW PER EMPLOYEE)
        writer.writerow([
            'Employee ID', 'Employee Name', 'Department', 'Designation', 'Shift', 'Reporting Period',
            'Present Days', 'Absent Days', 'Late Days', 'Attendance %', 'Marking Method'
        ])

        for r in records:
            emp_id = str(r.get('user_id', ''))
            emp_id_disp = f"EMP-{int(emp_id):03d}" if emp_id.isdigit() else str(emp_id)
            shift_name = r.get('shift_name') or 'General Shift'
            shift_start = r.get('shift_start')
            shift_end = r.get('shift_end')
            shift_disp = f"{shift_name} ({shift_start}-{shift_end})" if shift_start and shift_end else shift_name
            methods = {
                'Manually' if str(day.get('source', '')).lower() == 'manual' else 'Automatically'
                for day in r.get('daily_records', [])
            }
            marking_method = next(iter(methods)) if len(methods) == 1 else ('Mixed' if methods else '-')

            writer.writerow([
                emp_id_disp,
                r.get('name') or '-',
                r.get('department') or '-',
                r.get('position') or 'Staff Member',
                shift_disp,
                date_label,
                r.get('present_display', f"{r.get('present_days', 0)} / {r.get('working_days', 0)}"),
                r.get('absent_display', f"{r.get('absent_days', 0)} / {r.get('working_days', 0)}"),
                r.get('late_days', 0),
                f"{r.get('attendance_rate', 0.0)}%",
                marking_method
            ])
    else:
        # Single-Day Attendance Columns
        writer.writerow([
            'Employee ID', 'Employee Name', 'Department', 'Designation', 'Shift', 'Date',
            'Check-In', 'Check-In Camera', 'Check-Out', 'Check-Out Camera', 'Marking Method', 'Status'
        ])

        for r in records:
            emp_id = str(r.get('user_id', ''))
            emp_id_disp = f"EMP-{int(emp_id):03d}" if emp_id.isdigit() else str(emp_id)
            shift_name = r.get('shift_name') or 'General Shift'
            shift_start = r.get('shift_start')
            shift_end = r.get('shift_end')
            shift_disp = f"{shift_name} ({shift_start}-{shift_end})" if shift_start and shift_end else shift_name

            raw_status = (r.get('status') or 'absent').strip().upper()
            day_type_lbl = r.get('day_type_label')
            if day_type_lbl and '—' in str(day_type_lbl):
                status_display = str(day_type_lbl)
            elif raw_status == 'PRESENT':
                status_display = 'PRESENT'
            elif raw_status == 'CHECKED_IN':
                status_display = 'CHECKED_IN'
            elif raw_status == 'CHECKED_OUT':
                status_display = 'CHECKED_OUT'
            elif raw_status == 'LATE':
                status_display = 'LATE'
            elif raw_status in ('LEAVE', 'ON_LEAVE', 'ON LEAVE'):
                status_display = 'ON_LEAVE'
            else:
                status_display = 'ABSENT'

            cin_time = format_report_time(r.get('first_seen'))
            cout_time = format_report_time(r.get('last_seen'))
            cin_cam = r.get('checkin_camera_name') or r.get('checkin_camera') or r.get('location') or '-'
            cout_cam = r.get('checkout_camera_name') or r.get('checkout_camera') or r.get('checkout_location') or '-'
            if cin_time == '—' or cin_time == '-':
                cin_cam = '-'
            if cout_time == '—' or cout_time == '-':
                cout_cam = '-'

            writer.writerow([
                emp_id_disp,
                r.get('name') or '-',
                r.get('department') or '-',
                r.get('position') or 'Staff Member',
                shift_disp,
                format_report_date(r.get('date', '')),
                cin_time,
                cin_cam,
                cout_time,
                cout_cam,
                'Manually' if str(r.get('source', '')).lower() == 'manual' else 'Automatically',
                status_display
            ])

    csv_data = '\ufeff' + output.getvalue()
    filename = generate_report_filename(report, department=department, shift=shift_id, status=status).replace('.xlsx', '.csv')
    
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )


@app.route('/api/reports/export/employee-csv', methods=['GET'])
def api_reports_export_employee_csv():
    """Export an individual employee's complete daily timeline for a reporting period."""
    import csv
    import io

    user_id = request.args.get('user_id', '').strip()
    target_date = request.args.get('date', '').strip() or None
    start_date = request.args.get('start_date', '').strip() or None
    end_date = request.args.get('end_date', '').strip() or None

    if not user_id:
        return jsonify({'error': 'user_id is required'}), 400

    try:
        uid_int = int(user_id)
        u_target = verify_user_access(uid_int)
        if not u_target:
            return jsonify({'error': 'Employee record not found or access denied'}), 404
    except ValueError:
        return jsonify({'error': 'Invalid user_id'}), 400

    report = db.get_detailed_attendance_report(
        target_date=target_date,
        start_date=start_date,
        end_date=end_date,
        organization_id=get_current_tenant_id(),
    )

    records = report.get('records', [])
    emp_record = next((r for r in records if str(r.get('user_id')) == str(user_id)), None)

    if not emp_record:
        return jsonify({'error': 'Employee record not found in range'}), 404

    date_label = report.get('date_label', 'Period')
    emp_name = emp_record.get('name', f"User #{user_id}")
    emp_id_disp = f"EMP-{int(user_id):03d}" if user_id.isdigit() else str(user_id)
    dept = emp_record.get('department', '-')
    designation = emp_record.get('position', 'Staff Member')
    shift = emp_record.get('shift_name', 'General Shift')

    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')

    writer.writerow(['QIntellect Technologies - Employee Attendance History'])
    writer.writerow([f'Employee: {emp_name} ({emp_id_disp}) | Department: {dept} | Designation: {designation} | Shift: {shift}'])
    writer.writerow([f'Reporting Period: {date_label} | Generated At: {datetime.now().strftime("%d %b %Y, %I:%M %p")}'])
    
    pres_disp = emp_record.get('present_display', f"{emp_record.get('present_days', 0)} / {emp_record.get('working_days', 0)}")
    rate_disp = f"{emp_record.get('attendance_rate', 0.0)}%"
    writer.writerow([f'Present Days: {pres_disp} | Late Days: {emp_record.get("late_days", 0)} | Attendance Rate: {rate_disp}'])
    writer.writerow([]) # Blank separator row

    writer.writerow(['Date', 'Day', 'Status', 'Marking Method', 'Check-In', 'Check-In Camera', 'Check-Out', 'Check-Out Camera', 'Shift'])

    for d in emp_record.get('daily_records', []):
        writer.writerow([
            format_report_date(d.get('date', '')),
            d.get('day', '-'),
            d.get('status_display', d.get('status', '-')).upper(),
            'Manually' if str(d.get('source', '')).lower() == 'manual' else 'Automatically',
            format_report_time(d.get('first_seen')),
            d.get('checkin_camera_name') or d.get('checkin_camera') or d.get('location') or '-',
            format_report_time(d.get('last_seen')),
            d.get('checkout_camera_name') or d.get('checkout_camera') or d.get('checkout_location') or '-',
            d.get('shift_name', shift)
        ])

    clean_name = emp_name.replace(' ', '_')
    clean_period = str(date_label).replace(' ', '_').replace('/', '-').replace(':', '-')
    filename = f"Attendance_{clean_name}_{clean_period}.csv"

    return Response(
        '\ufeff' + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )


@app.route('/api/attendance/today', methods=['GET'])
def api_attendance_today():
    return jsonify(db.get_attendance_today())


@app.route('/api/attendance/mark-absent', methods=['POST'])
def api_mark_absent():
    data = request.get_json() or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'success': False}), 400
    user_id = int(user_id)
    target_date = data.get('date') or data.get('target_date')
    success = db.mark_user_absent_today(
        user_id,
        target_date=target_date,
        organization_id=get_current_tenant_id(),
    )
    if success:
        remove_live_detection_for_user(user_id)
    return jsonify({'success': success})


# ============================================
# LEAVE ENDPOINTS
# ============================================

@app.route('/api/leaves', methods=['GET'])
def api_get_leaves():
    current_tenant = get_current_tenant_id()
    user_id = request.args.get('user_id', type=int)
    if user_id and not verify_user_access(user_id):
        return jsonify([]), 200
    status = request.args.get('status')
    return jsonify(db.get_leave_requests(user_id=user_id, status=status, organization_id=current_tenant))


@app.route('/api/leaves', methods=['POST'])
def api_add_leave():
    data = request.get_json() or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'message': 'user_id required'}), 400
    if not verify_user_access(int(user_id)):
        return jsonify({'success': False, 'message': 'User not found or unauthorized'}), 404

    leave_type = data.get('leave_type', 'casual')
    leave_duration = str(data.get('leave_duration', 'full')).strip().lower()
    start_date = (data.get('start_date') or '').strip()
    end_date = (data.get('end_date') or '').strip()

    if leave_duration not in ('full', 'half'):
        return jsonify({'success': False, 'message': 'Leave duration must be full or half.'}), 400
    if not start_date or not end_date:
        return jsonify({'success': False, 'message': 'Start date and end date are required.'}), 400
    if leave_duration == 'half' and start_date != end_date:
        return jsonify({'success': False, 'message': 'Half-day leave must be requested for a single date.'}), 400

    try:
        d1 = datetime.strptime(start_date, '%Y-%m-%d')
        d2 = datetime.strptime(end_date, '%Y-%m-%d')
        if d2 < d1:
            return jsonify({
                'success': False,
                'error': 'End date cannot be earlier than start date.',
                'message': 'End date cannot be earlier than start date.'
            }), 400
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    try:
        with sqlite3.connect(db.DB_PATH) as conn:
            db.validate_employment_dates(conn, int(user_id), start_date, end_date, activity_name='Leave')
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e), 'error': str(e)}), 400

    holiday = db.get_company_holiday_in_range(
        start_date,
        end_date,
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    )
    if holiday:
        holiday_name = holiday.get('name') or 'A company holiday'
        holiday_date = holiday.get('matched_date') or start_date
        message = f"{holiday_name} is a public holiday on {holiday_date}. Leave cannot be requested on a holiday. Please choose another date."
        return jsonify({
            'success': False,
            'reason_code': 'public_holiday',
            'error': message,
            'message': message,
        }), 400

    excess_treatment = data.get('excess_treatment')
    quota = db.check_leave_quota(
        int(user_id), leave_type, start_date, end_date,
        leave_duration=leave_duration,
    )

    requested_days = int(quota.get('requested_days') or 0)
    quota_days_requested = int(quota.get('quota_days_requested', requested_days) or 0)
    quota_days = 0
    paid_days = 0
    unpaid_days = 0
    paid_through_date = None
    quota_through_date = None

    if not quota.get('allowed', True):
        if quota.get('reason_code') != 'quota_exceeded':
            return jsonify({
                'success': False,
                'error': quota.get('message'),
                'message': quota.get('message')
            }), 400
        if not excess_treatment:
            return jsonify({
                'success': False,
                'error': quota.get('message'),
                'message': quota.get('message')
            }), 400
        if excess_treatment not in ('paid', 'unpaid'):
            return jsonify({'success': False, 'message': 'Choose whether excess leave should be paid or unpaid.'}), 400

        quota_days = min(quota_days_requested, max(0, int(quota.get('remaining_days', 0))))
        quota_range, _ = db.split_leave_request_dates(int(user_id), start_date, end_date, quota_days)
        if quota_range:
            quota_through_date = quota_range[1]
        if leave_duration == 'half':
            if excess_treatment == 'paid':
                quota_days = quota_days_requested
        elif excess_treatment == 'paid':
            paid_days = requested_days
        else:
            paid_days = quota_days
            unpaid_days = max(0, requested_days - quota_days)
            if paid_days > 0:
                paid_through_date = quota_through_date
    elif leave_duration == 'half':
        quota_days = quota_days_requested
    elif int(quota.get('is_paid', 1)) == 0:
        unpaid_days = requested_days
    else:
        paid_days = requested_days
        quota_days = requested_days
        quota_range, _ = db.split_leave_request_dates(int(user_id), start_date, end_date, quota_days)
        if quota_range:
            quota_through_date = quota_range[1]

    try:
        with sqlite3.connect(db.DB_PATH) as conn:
            db.validate_employment_dates(conn, int(user_id), start_date, end_date, activity_name='Leave')
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e), 'error': str(e)}), 400

    start_time = (data.get('start_time') or '').strip() or None
    end_time = (data.get('end_time') or '').strip() or None
    if leave_duration == 'half':
        leave_types = db.get_leave_types(organization_id=get_current_tenant_id())
        matched_type = next((lt for lt in leave_types if str(lt.get('code')) == str(leave_type) or str(lt.get('id')) == str(leave_type)), None)
        quota_mode = db.get_payroll_settings(organization_id=get_current_tenant_id()).get('half_leave_quota_mode') or 'category'
        company_half_allowed = quota_mode in ('monthly_cap', 'monthly_convert')
        type_half_allowed = bool(matched_type and matched_type.get('half_leave_enabled'))
        scoped_rule = db.get_scoped_payroll_rule('half_leave_quota', int(user_id)) if hasattr(db, 'get_scoped_payroll_rule') else None
        if matched_type and not (type_half_allowed or company_half_allowed or scoped_rule):
            msg = f'Half-day leave is not permitted for leave type {matched_type.get("name", leave_type)}.'
            return jsonify({'success': False, 'message': msg, 'error': msg}), 400

        if not start_time:
            return jsonify({'success': False, 'message': 'Half-day start time is required.', 'error': 'Half-day start time is required.'}), 400
        if not end_time:
            return jsonify({'success': False, 'message': 'Half-day end time is required.', 'error': 'Half-day end time is required.'}), 400
        if end_time <= start_time:
            return jsonify({'success': False, 'message': 'Leave end time must be later than start time.', 'error': 'Leave end time must be later than start time.'}), 400

        # QA-52: A half-day leave can cover only up to half of the scheduled shift duration
        try:
            with sqlite3.connect(db.DB_PATH) as conn_shift:
                cur_s = conn_shift.cursor()
                cur_s.execute("""
                    SELECT s.start_time, s.end_time 
                    FROM shifts s
                    JOIN users u ON u.shift_id = s.id
                    WHERE u.id = ?
                """, (int(user_id),))
                s_row = cur_s.fetchone()
                if not s_row:
                    cur_s.execute("SELECT start_time, end_time FROM shifts WHERE id = 1")
                    s_row = cur_s.fetchone()
                if s_row:
                    def _t_to_m(t_val):
                        parts = str(t_val).strip().split(':')
                        return int(parts[0]) * 60 + int(parts[1])
                    req_duration = _t_to_m(end_time) - _t_to_m(start_time)
                    if req_duration < 0:
                        req_duration += 24 * 60
                    shift_duration = _t_to_m(s_row[1]) - _t_to_m(s_row[0])
                    if shift_duration <= 0:
                        shift_duration += 24 * 60
                    max_allowed_mins = shift_duration / 2.0
                    if req_duration > max_allowed_mins:
                        max_hrs = max_allowed_mins / 60.0
                        hrs_str = f"{int(max_hrs)}h" if max_hrs.is_integer() else f"{max_hrs:.1f}h"
                        msg = f"A half-day leave can only cover up to half of the scheduled shift duration ({hrs_str} max). For longer absences, please file a full-day leave."
                        return jsonify({'success': False, 'message': msg, 'error': msg}), 400
        except Exception as shift_val_err:
            logger.warning("Could not validate half-day leave against shift: %s", shift_val_err)

    user = db.get_user_by_id(int(user_id))
    try:
        lid = db.add_leave_request(
            user_id=int(user_id),
            user_name=user['name'] if user else data.get('user_name', ''),
            leave_type=leave_type,
            leave_duration=leave_duration,
            start_date=start_date,
            end_date=end_date,
            reason=data.get('reason', ''),
            paid_days=paid_days,
            unpaid_days=unpaid_days,
            quota_days=quota_days,
            paid_through_date=paid_through_date,
            quota_through_date=quota_through_date,
            start_time=start_time,
            end_time=end_time,
        )
    except ValueError as exc:
        if str(exc) == db.PAYROLL_LOCK_MESSAGE:
            return jsonify({'error': str(exc), 'reopen': '/api/payroll/reopen'}), 409
        return jsonify({'error': str(exc)}), 400
    if not lid:
        return jsonify({'success': False, 'message': 'This leave request overlaps an existing active leave or could not be saved. Please review the employee\'s leave dates.'}), 409
    return jsonify({
        'success': True,
        'id': lid,
        'requested_days': requested_days,
        'paid_days': paid_days,
        'unpaid_days': unpaid_days,
    }), 201


@app.route('/api/leaves/check-quota', methods=['GET'])
def api_check_leave_quota():
    user_id = request.args.get('user_id', type=int)
    leave_type = request.args.get('leave_type', 'casual')
    start_date = (request.args.get('start_date') or '').strip() or None
    end_date = (request.args.get('end_date') or '').strip() or None
    leave_duration = request.args.get('leave_duration', 'full')
    res = db.check_leave_quota(
        user_id=user_id,
        leave_type_code=leave_type,
        start_date=start_date,
        end_date=end_date,
        leave_duration=leave_duration,
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    )
    return jsonify(res), 200


@app.route('/api/leaves/<int:leave_id>', methods=['PUT'])
def api_update_leave(leave_id):
    current_tenant = get_current_tenant_id()
    leave = db.get_leave_by_id(leave_id, organization_id=current_tenant)
    if not leave:
        return jsonify({'success': False, 'message': 'Leave request not found or unauthorized'}), 404
    data = request.get_json() or {}
    try:
        updated = db.update_leave_status(
            leave_id=leave_id,
            status=data.get('status', 'approved'),
            approved_by=data.get('approved_by', 'Admin'),
            organization_id=current_tenant,
            portion=data.get('portion'),
            rejection_reason=data.get('rejection_reason'),
        )
    except ValueError as exc:
        if str(exc) == db.PAYROLL_LOCK_MESSAGE:
            return jsonify({'error': str(exc), 'reopen': '/api/payroll/reopen'}), 409
        return jsonify({'error': str(exc)}), 400
    if not updated:
        return jsonify({'success': False, 'reason_code': 'overlapping_leave', 'message': 'This leave request overlaps another active leave or no longer exists. Please review the employee\'s leave dates.'}), 409
    return jsonify({'success': True})


@app.route('/api/leaves/<int:leave_id>', methods=['DELETE'])
def api_delete_leave(leave_id):
    current_tenant = get_current_tenant_id()
    leave = db.get_leave_by_id(leave_id, organization_id=current_tenant)
    if not leave:
        return jsonify({'success': False, 'message': 'Leave request not found or unauthorized'}), 404
    deleted = db.delete_leave_request(leave_id, organization_id=current_tenant)
    if not deleted:
        return jsonify({'success': False, 'message': 'Failed to delete leave request'}), 404
    return jsonify({'success': True})


@app.route('/api/leave-types', methods=['GET'])
def api_get_leave_types():
    return jsonify(db.get_leave_types(
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    ))


@app.route('/api/leave-types', methods=['POST'])
def api_add_leave_type():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    code = data.get('code', '').strip().lower().replace(' ', '_')
    if not name or not code:
        return jsonify({'success': False, 'message': 'name and code are required'}), 400

    existing_types = db.get_leave_types(
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    )
    if any(str(item.get('name', '')).strip().casefold() == name.casefold() for item in existing_types):
        return jsonify({
            'success': False,
            'message': f'A leave type named "{name}" already exists. Choose a different name.'
        }), 409
    if any(str(item.get('code', '')).strip().casefold() == code.casefold() for item in existing_types):
        return jsonify({
            'success': False,
            'message': f'The leave type code "{code}" is already in use.'
        }), 409
    allowance_period = data.get('allowance_period', 'yearly')
    default_months = {'monthly': 1, 'half_yearly': 6, 'yearly': 12}.get(allowance_period, 12)
    try:
        allowance_months = int(data.get('allowance_months', default_months))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Policy duration must be a whole number from 1 to 12 months.'}), 400
    if not 1 <= allowance_months <= 12:
        return jsonify({'success': False, 'message': 'Policy duration must be from 1 to 12 months.'}), 400
    
    quota_raw = data.get('half_leave_quota_type_id')
    try:
        quota_id = int(quota_raw) if quota_raw not in (None, '', 'null') else None
    except (TypeError, ValueError):
        quota_id = None
    tid = db.add_leave_type(
        name=name,
        code=code,
        yearly_allowance=float(data.get('yearly_allowance', 12.0)),
        is_paid=int(data.get('is_paid', 1)),
        description=data.get('description', ''),
        allowance_period=allowance_period,
        allowance_months=allowance_months,
        half_leave_enabled=bool(data.get('half_leave_enabled')),
        half_leave_mode=str(data.get('half_leave_mode') or 'separate'),
        half_leave_allowance_count=int(data.get('half_leave_allowance_count') or 0),
        half_leave_allowance_months=int(data.get('half_leave_allowance_months') or 12),
        half_leaves_per_quota_day=int(data.get('half_leaves_per_quota_day') or 2),
        half_leave_quota_type_id=quota_id,
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    )
    if tid:
        return jsonify({'success': True, 'id': tid}), 201
    return jsonify({'success': False, 'message': 'Failed to add leave type'}), 400


@app.route('/api/leave-types/<int:type_id>', methods=['PUT'])
def api_update_leave_type(type_id):
    data = request.get_json() or {}
    allowance_period = data.get('allowance_period', 'yearly')
    default_months = {'monthly': 1, 'half_yearly': 6, 'yearly': 12}.get(allowance_period, 12)
    try:
        allowance_months = int(data.get('allowance_months', default_months))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Policy duration must be a whole number from 1 to 12 months.'}), 400
    if not 1 <= allowance_months <= 12:
        return jsonify({'success': False, 'message': 'Policy duration must be from 1 to 12 months.'}), 400
    quota_raw = data.get('half_leave_quota_type_id')
    try:
        quota_id = int(quota_raw) if quota_raw not in (None, '', 'null') else None
    except (TypeError, ValueError):
        quota_id = None
    ok = db.update_leave_type(
        type_id=type_id,
        name=data.get('name', ''),
        yearly_allowance=float(data.get('yearly_allowance', 12.0)),
        is_paid=int(data.get('is_paid', 1)),
        description=data.get('description', ''),
        active=int(data.get('active', 1)),
        allowance_period=allowance_period,
        allowance_months=allowance_months,
        half_leave_enabled=bool(data.get('half_leave_enabled')),
        half_leave_mode=str(data.get('half_leave_mode') or 'separate'),
        half_leave_allowance_count=int(data.get('half_leave_allowance_count') or 0),
        half_leave_allowance_months=int(data.get('half_leave_allowance_months') or 12),
        half_leaves_per_quota_day=int(data.get('half_leaves_per_quota_day') or 2),
        half_leave_quota_type_id=quota_id,
        organization_id=get_current_tenant_id(),
    )
    return jsonify({'success': ok})


@app.route('/api/leave-types/<int:type_id>', methods=['DELETE'])
def api_delete_leave_type(type_id):
    ok = db.delete_leave_type(type_id, organization_id=get_current_tenant_id())
    return jsonify({'success': ok})


@app.route('/api/leaves/balances', methods=['GET'])
def api_get_leave_balances():
    year = request.args.get('year', 2026, type=int)
    user_id = request.args.get('user_id', type=int)
    current_tenant = get_current_tenant_id()
    if user_id is not None:
        u_target = verify_user_access(user_id, organization_id=current_tenant)
        if not u_target:
            return jsonify({'error': 'Employee not found or access denied'}), 404
    return jsonify(db.get_leave_balances(year=year, user_id=user_id, organization_id=current_tenant))


@app.route('/api/holidays', methods=['GET'])
def api_get_holidays():
    year = request.args.get('year', type=int)
    current_tenant = get_current_tenant_id()
    return jsonify(db.get_company_holidays(
        year=year,
        organization_id=current_tenant,
        branch_id=get_current_branch_id(),
    ))


@app.route('/api/holidays', methods=['POST'])
def api_add_holiday():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    hdate = data.get('holiday_date', '').strip()
    edate = data.get('end_date', '').strip() or hdate
    if not name or not hdate:
        return jsonify({'success': False, 'message': 'name and holiday_date are required'}), 400
    try:
        holiday_start = datetime.strptime(hdate, '%Y-%m-%d')
        holiday_end = datetime.strptime(edate, '%Y-%m-%d')
    except ValueError:
        return jsonify({'success': False, 'message': 'Holiday dates must use YYYY-MM-DD format.'}), 400
    if holiday_end < holiday_start:
        return jsonify({'success': False, 'message': 'Holiday end date cannot be earlier than its start date.'}), 400
    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    if any(
        holiday.get('holiday_date') == hdate
        for holiday in db.get_company_holidays(
            organization_id=current_tenant,
            branch_id=current_branch,
        )
    ):
        return jsonify({'success': False, 'message': 'A holiday already starts on this date.'}), 409
    hid = db.add_company_holiday(
        name=name,
        holiday_date=hdate,
        end_date=edate,
        description=data.get('description', ''),
        is_recurring=int(data.get('is_recurring', 0)),
        organization_id=current_tenant,
        branch_id=current_branch,
    )
    if hid:
        return jsonify({'success': True, 'id': hid}), 201
    return jsonify({'success': False, 'message': 'Failed to add holiday'}), 400


@app.route('/api/holidays/<int:holiday_id>', methods=['PUT', 'POST'])
def api_update_holiday(holiday_id):
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    hdate = data.get('holiday_date', '').strip()
    edate = data.get('end_date', '').strip() or hdate
    if not name or not hdate:
        return jsonify({'success': False, 'message': 'name and holiday_date are required'}), 400
    try:
        holiday_start = datetime.strptime(hdate, '%Y-%m-%d')
        holiday_end = datetime.strptime(edate, '%Y-%m-%d')
    except ValueError:
        return jsonify({'success': False, 'message': 'Holiday dates must use YYYY-MM-DD format.'}), 400
    if holiday_end < holiday_start:
        return jsonify({'success': False, 'message': 'Holiday end date cannot be earlier than its start date.'}), 400
    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    ok = db.update_company_holiday(
        holiday_id=holiday_id,
        name=name,
        holiday_date=hdate,
        end_date=edate,
        description=data.get('description', ''),
        is_recurring=int(data.get('is_recurring', 0)),
        organization_id=current_tenant,
        branch_id=current_branch,
    )
    if ok:
        return jsonify({'success': True}), 200
    return jsonify({'success': False, 'message': 'Failed to update holiday'}), 400


@app.route('/api/holidays/<int:holiday_id>', methods=['DELETE'])
def api_delete_holiday(holiday_id):
    ok = db.delete_company_holiday(
        holiday_id,
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    )
    return jsonify({'success': ok})


# ============================================
# OVERTIME ENDPOINTS
# ============================================

@app.route('/api/overtime', methods=['GET'])
def api_get_overtime():
    user_id = request.args.get('user_id', type=int)
    status = request.args.get('status')
    department = request.args.get('department')
    ot_date = request.args.get('ot_date')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    current_tenant = get_current_tenant_id()
    return jsonify(db.get_overtime(
        user_id=user_id, status=status, department=department,
        ot_date=ot_date, start_date=start_date, end_date=end_date,
        organization_id=current_tenant,
        branch_id=get_current_branch_id(),
    ))


@app.route('/api/overtime/rate-preview', methods=['GET'])
def api_overtime_rate_preview():
    user_id = request.args.get('user_id', type=int)
    overtime_date = (request.args.get('ot_date') or '').strip()
    if not user_id or not overtime_date:
        return jsonify({'error': 'user_id and ot_date are required'}), 400
    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    if not verify_user_access(user_id, organization_id=current_tenant):
        return jsonify({'error': 'Employee not found or access denied'}), 404
    preview = db.get_overtime_rate_preview(
        user_id,
        overtime_date,
        organization_id=current_tenant,
        branch_id=current_branch,
    )
    if not preview:
        return jsonify({'error': 'Employee or overtime date is invalid'}), 400
    return jsonify(preview)


@app.route('/api/overtime', methods=['POST'])
def api_add_overtime():
    data = request.get_json() or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'user_id is required'}), 400
    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'user_id must be an integer'}), 400
    user = verify_user_access(user_id, organization_id=current_tenant)
    if not user or (user.get('branch_id') or 'DEFAULT_BRANCH') != current_branch:
        return jsonify({'success': False, 'error': 'Employee not found or access denied'}), 404
    hours = float(data.get('hours', 0) or 0)
    if hours <= 0:
        return jsonify({'success': False, 'error': 'hours must be greater than 0'}), 400
    ot_date = str(data.get('ot_date', '')).strip()
    if not ot_date:
        return jsonify({'success': False, 'error': 'ot_date is required in YYYY-MM-DD format'}), 400
    try:
        parsed_ot = datetime.strptime(ot_date, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'success': False, 'error': 'ot_date must use YYYY-MM-DD format'}), 400

    limit_status = db.get_overtime_limit_status(user_id, ot_date, hours)
    if limit_status.get('exceeded'):
        return jsonify({'success': False, 'error': f"Overtime limit exceeded: Maximum allowed is {limit_status['maximum_hours']:g} hours {limit_status['period_label']}."}), 400
    
    reason = str(data.get('reason') or '')
    try:
        oid = db.add_overtime(
            user_id=user_id,
            user_name=user['name'],
            ot_date=ot_date,
            hours=hours,
            reason=reason,
            organization_id=current_tenant,
            branch_id=current_branch,
        )
    except ValueError as exc:
        if str(exc) == db.PAYROLL_LOCK_MESSAGE:
            return jsonify({'error': str(exc), 'reopen': '/api/payroll/reopen'}), 409
        return jsonify({'error': str(exc)}), 400
    if not oid:
        return jsonify({'success': False, 'error': 'Failed to save overtime record'}), 500
    return jsonify({
        'success': True,
        'id': oid,
        'limit_exceeded': bool(limit_status.get('exceeded')),
        'limit_message': (
            f"This OT exceeds the configured maximum of {limit_status['maximum_hours']:g} hours {limit_status['period_label']}. "
            "It was sent for admin review."
            if limit_status.get('exceeded') else ''
        ),
    }), 201


@app.route('/api/overtime/<int:ot_id>', methods=['PUT'])
def api_update_overtime(ot_id):
    data = request.get_json() or {}
    new_status = data.get('status', 'approved')
    approved_by = data.get('approved_by') or session.get('admin_name') or session.get('client_user') or session.get('username') or 'Admin'

    if new_status not in ('approved', 'rejected'):
        return jsonify({'success': False, 'error': 'status must be approved or rejected'}), 400

    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    ot_record = db.get_overtime_by_id(
        ot_id,
        organization_id=current_tenant,
        branch_id=current_branch,
    )
    if not ot_record:
        return jsonify({'success': False, 'error': 'Overtime record not found'}), 404
    current_status = (ot_record.get('status') or 'pending').lower()
    if current_status == new_status.lower():
        return jsonify({'success': True, 'id': ot_id, 'status': new_status, 'approved_by': approved_by})
    if current_status != 'pending':
        if not (current_status == 'approved' and new_status.lower() == 'rejected'):
            return jsonify({'success': False, 'error': f'Overtime is already finalized as {current_status}.'}), 409
    if new_status == 'approved':
        day_type = db.get_overtime_day_type(int(ot_record['user_id']), str(ot_record['ot_date']))
        if day_type == 'regular' and db.get_user_attendance_status(
            int(ot_record['user_id']), str(ot_record['ot_date'])
        ) == 'absent':
            return jsonify({
                'success': False,
                'reason_code': 'absent_on_overtime_date',
                'error': 'This overtime was not approved because the employee was absent on the overtime date.'
            }), 409
        with sqlite3.connect(db.DB_PATH) as _conn:
            following_att = db._get_following_workday_attendance_status(_conn.cursor(), int(ot_record['user_id']), str(ot_record['ot_date']))
        if following_att == 'absent':
            return jsonify({
                'success': False,
                'reason_code': 'absent_on_following_workday',
                'error': 'This overtime was not approved because the employee was absent on the following workday.'
            }), 409

    rejection_reason = data.get('rejection_reason')
    try:
        success = db.update_overtime_status(
            ot_id,
            status=new_status,
            approved_by=approved_by,
            organization_id=current_tenant,
            branch_id=current_branch,
            rejection_reason=rejection_reason,
        )
    except ValueError as exc:
        if str(exc) == db.PAYROLL_LOCK_MESSAGE:
            return jsonify({'error': str(exc), 'reopen': '/api/payroll/reopen'}), 409
        return jsonify({'error': str(exc)}), 400
    if not success:
        latest_record = db.get_overtime_by_id(
            ot_id, organization_id=current_tenant, branch_id=current_branch
        )
        if latest_record and (latest_record.get('status') or 'pending').lower() not in ('pending', 'approved'):
            return jsonify({'success': False, 'error': 'Overtime was finalized by another request.'}), 409
        return jsonify({'success': False, 'error': 'Overtime record not found or could not be updated'}), 404
    return jsonify({'success': True, 'id': ot_id, 'status': new_status, 'approved_by': approved_by})


@app.route('/api/overtime/bulk-approve', methods=['POST'])
def api_bulk_approve_overtime():
    """Bulk approve/reject multiple pending OT records. Skips already-final records."""
    data = request.get_json() or {}
    ot_ids = data.get('ids', [])
    new_status = data.get('status', 'approved')
    approved_by = data.get('approved_by') or session.get('admin_name') or session.get('client_user') or session.get('username') or 'Admin'

    if new_status not in ('approved', 'rejected'):
        return jsonify({'success': False, 'error': 'status must be approved or rejected'}), 400
    if not ot_ids or not isinstance(ot_ids, list):
        return jsonify({'success': False, 'error': 'ids must be a non-empty list'}), 400

    updated = []
    skipped = []
    errors = []
    blocked_absent = []
    for ot_id in ot_ids:
        try:
            current_tenant = get_current_tenant_id()
            current_branch = get_current_branch_id()
            rec = db.get_overtime_by_id(
                int(ot_id),
                organization_id=current_tenant,
                branch_id=current_branch,
            )
            if not rec:
                errors.append({'id': ot_id, 'reason': 'not found'})
                continue
            cur_status = (rec.get('status') or 'pending').lower()
            if cur_status != 'pending' and not (cur_status == 'approved' and new_status.lower() == 'rejected'):
                skipped.append({'id': ot_id, 'reason': f'already finalized as {cur_status}'})
                continue
            if new_status == 'approved':
                day_type = db.get_overtime_day_type(int(rec['user_id']), str(rec['ot_date']))
                if day_type == 'regular' and db.get_user_attendance_status(
                    int(rec['user_id']), str(rec['ot_date'])
                ) == 'absent':
                    blocked_absent.append({'id': ot_id, 'reason': 'employee was absent on the overtime date'})
                    continue
                with sqlite3.connect(db.DB_PATH) as _conn:
                    following_att = db._get_following_workday_attendance_status(_conn.cursor(), int(rec['user_id']), str(rec['ot_date']))
                if following_att == 'absent':
                    blocked_absent.append({'id': ot_id, 'reason': 'employee was absent on the following workday'})
                    continue
            ok = db.update_overtime_status(
                int(ot_id),
                status=new_status,
                approved_by=approved_by,
                organization_id=current_tenant,
                branch_id=current_branch,
                rejection_reason=data.get('rejection_reason'),
            )
            if ok:
                updated.append(ot_id)
            else:
                latest_rec = db.get_overtime_by_id(
                    int(ot_id),
                    organization_id=current_tenant,
                    branch_id=current_branch,
                )
                if latest_rec and (latest_rec.get('status') or 'pending').lower() not in ('pending', 'approved'):
                    skipped.append({'id': ot_id, 'reason': 'already finalized'})
                else:
                    errors.append({'id': ot_id, 'reason': 'update failed'})
        except ValueError as e:
            if str(e) == db.PAYROLL_LOCK_MESSAGE:
                return jsonify({'error': str(e), 'reopen': '/api/payroll/reopen'}), 409
            errors.append({'id': ot_id, 'reason': str(e)})
        except Exception as e:
            errors.append({'id': ot_id, 'reason': str(e)})

    updated_count = len(updated)
    blocked_absent_count = len(blocked_absent)
    message_parts = []
    if updated_count:
        message_parts.append(f'{updated_count} record(s) {new_status}.')
    if blocked_absent_count:
        message_parts.append(
            f'{blocked_absent_count} not approved because the employee was absent on the overtime date.'
        )
    if skipped:
        message_parts.append(f'{len(skipped)} already finalized record(s) skipped.')
    if errors:
        message_parts.append(f'{len(errors)} record(s) could not be processed.')
    if not message_parts:
        message_parts.append('No overtime records were changed.')

    return jsonify({
        'success': True,
        'updated': updated,
        'updated_count': updated_count,
        'skipped': skipped,
        'skipped_count': len(skipped),
        'blocked_absent': blocked_absent,
        'blocked_absent_count': blocked_absent_count,
        'errors': errors,
        'message': ' '.join(message_parts)
    })


@app.route('/api/overtime/<int:ot_id>', methods=['DELETE'])
def api_delete_overtime(ot_id):
    try:
        current_tenant = get_current_tenant_id()
        current_branch = get_current_branch_id()
        ot_record = db.get_overtime_by_id(
            ot_id,
            organization_id=current_tenant,
            branch_id=current_branch,
        )
        if not ot_record:
            return jsonify({'success': False, 'error': 'Overtime record not found'}), 404
        if (ot_record.get('status') or '').lower() == 'approved':
            return jsonify({'success': False, 'error': 'Cannot delete an approved overtime record.'}), 403
            
        success = db.delete_overtime(
            ot_id,
            organization_id=current_tenant,
            branch_id=current_branch,
        )
        if not success:
            return jsonify({'success': False, 'error': 'Overtime record could not be deleted'}), 400
        return jsonify({'success': True, 'message': 'Overtime record deleted successfully'})
    except Exception as e:
        logger.exception("Failed to delete overtime record %s", ot_id)
        return jsonify({'success': False, 'error': f'Failed to delete overtime record: {e}'}), 500


# ============================================
# SALARY ENDPOINTS
# ============================================

@app.route('/api/salary', methods=['GET'])
def api_get_all_salary():
    try:
        active_only = request.args.get('active_only', 'true').lower() == 'true'
        department = request.args.get('department')
        search = request.args.get('search')
        current_tenant = get_current_tenant_id()
        return jsonify(db.get_all_salary_configs(active_only=active_only, department=department, search=search, organization_id=current_tenant))
    except Exception as e:
        logger.exception("Failed to load salary configs")
        return jsonify({'error': f'Failed to load salary configs: {e}'}), 500


@app.route('/api/salary/summary', methods=['GET'])
def api_get_salary_summary():
    try:
        current_tenant = get_current_tenant_id()
        return jsonify(db.get_salary_summary(organization_id=current_tenant))
    except Exception as e:
        logger.exception("Failed to load salary summary")
        return jsonify({'error': f'Failed to load salary summary: {e}'}), 500


@app.route('/api/salary/<int:user_id>', methods=['GET'])
def api_get_salary(user_id):
    try:
        current_tenant = get_current_tenant_id()
        u_target = verify_user_access(user_id, organization_id=current_tenant)
        if not u_target:
            return jsonify({'error': f'Employee #{user_id} not found or access denied'}), 404
        cfg = db.get_salary_config(user_id)
        if not cfg:
            return jsonify({'error': f'Employee #{user_id} not found'}), 404
        return jsonify(cfg)
    except Exception as e:
        logger.exception("Failed to load salary config for user %s", user_id)
        return jsonify({'error': f'Failed to load salary config: {e}'}), 500


@app.route('/api/salary', methods=['POST'])
@app.route('/api/salary/<int:user_id>', methods=['PUT'])
def api_set_salary(user_id=None):
    try:
        data = request.get_json(silent=True) or {}
        uid = user_id or data.get('user_id')
        if not uid:
            return jsonify({'success': False, 'message': 'user_id is required'}), 400

        try:
            uid = int(uid)
            basic_salary = float(data.get('basic_salary', 0) or 0)
            allowances = float(data.get('allowances', 0) or 0)
            deductions = float(data.get('deductions', 0) or 0)
            ot_rate = float(data.get('ot_rate', 0) or 0)
        except (ValueError, TypeError) as e:
            return jsonify({'success': False, 'message': f'Invalid numeric values provided: {e}'}), 400

        current_tenant = get_current_tenant_id()
        u_target = verify_user_access(uid, organization_id=current_tenant)
        if not u_target:
            return jsonify({'success': False, 'message': f'Employee #{uid} not found or access denied'}), 404

        if basic_salary < 0 or allowances < 0 or deductions < 0 or ot_rate < 0:
            return jsonify({'success': False, 'message': 'Salary, allowances, deductions, and OT rate must be non-negative'}), 400
        if basic_salary > db.SALARY_MAX or allowances > db.SALARY_MAX or deductions > db.SALARY_MAX:
            return jsonify({'success': False, 'message': f'Salary amounts cannot exceed {db.SALARY_MAX:,.0f}'}), 400

        ok = db.set_salary_config(
            user_id=uid,
            basic_salary=basic_salary,
            allowances=allowances,
            deductions=deductions,
            ot_rate=ot_rate,
            effective_from=data.get('effective_from'),
        )
        if ok:
            updated_cfg = db.get_salary_config(uid)
            return jsonify({
                'success': True,
                'message': 'Salary configuration saved successfully',
                'config': updated_cfg
            }), 200
        return jsonify({'success': False, 'message': f'Employee #{uid} not found or database error'}), 404
    except ValueError as e:
        if str(e) == db.PAYROLL_LOCK_MESSAGE:
            return jsonify({'success': False, 'error': str(e), 'reopen': '/api/payroll/reopen'}), 409
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.exception("Failed to save salary configuration")
        return jsonify({'success': False, 'message': f'Failed to save salary configuration: {e}'}), 500


@app.route('/api/salary/<int:user_id>', methods=['DELETE'])
def api_delete_salary(user_id):
    try:
        current_tenant = get_current_tenant_id()
        u_target = verify_user_access(user_id, organization_id=current_tenant)
        if not u_target:
            return jsonify({'success': False, 'message': f'Employee #{user_id} not found or access denied'}), 404
        ok = db.reset_salary_config(user_id)
        if ok:
            return jsonify({'success': True, 'message': 'Salary package reset to default'}), 200
        return jsonify({'success': False, 'message': 'Failed to reset salary package'}), 500
    except Exception as e:
        logger.exception("Failed to reset salary config for user %s", user_id)
        return jsonify({'success': False, 'message': f'Failed to reset salary: {e}'}), 500


@app.route('/api/salary/auto-ot-rates', methods=['POST'])
def api_auto_ot_rates():
    try:
        data = request.get_json(silent=True) or {}
        divisor = float(data.get('divisor', 208.0) or 208.0)
        settings = db.get_payroll_settings(
            organization_id=get_current_tenant_id(),
            branch_id=get_current_branch_id(),
        )
        if 'divisor' not in data:
            divisor = float(settings.get('ot_hour_divisor') or 208.0)
        count = db.auto_calculate_ot_rates(
            rate_divisor=divisor,
            organization_id=get_current_tenant_id(),
            branch_id=get_current_branch_id(),
        )
        return jsonify({
            'success': True,
            'message': f'Successfully updated standard overtime rates for {count} active staff members',
            'updated_count': count
        }), 200
    except Exception as e:
        logger.exception("Failed to auto-calculate OT rates")
        return jsonify({'success': False, 'message': f'Failed to calculate OT rates: {e}'}), 500


@app.route('/api/payroll/history', methods=['GET'])
def api_get_payroll_history():
    try:
        current_tenant = get_current_tenant_id()
        return jsonify(db.get_payroll_history(
            organization_id=current_tenant,
            branch_id=get_current_branch_id(),
        ))
    except Exception as e:
        logger.exception("Failed to load payroll history")
        return jsonify({'error': f'Failed to load payroll history: {e}'}), 500


@app.route('/api/payroll', methods=['GET'])
def api_get_monthly_payroll():
    payroll_month = request.args.get('month', '').strip()
    recalculate = request.args.get('recalculate', '').lower() in ('1', 'true', 'yes') or request.args.get('force', '').lower() in ('1', 'true', 'yes')
    if not payroll_month:
        return jsonify({'error': 'Payroll month is required in YYYY-MM format'}), 400
    try:
        current_tenant = get_current_tenant_id()
        current_branch = get_current_branch_id()
        saved_run = None if recalculate else db.get_monthly_payroll(
            payroll_month,
            organization_id=current_tenant,
            branch_id=current_branch,
        )
        return jsonify(saved_run or db.calculate_monthly_payroll(
            payroll_month,
            organization_id=current_tenant,
            branch_id=current_branch,
        ))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("Failed to calculate payroll for %s", payroll_month)
        return jsonify({'error': f'Failed to calculate payroll: {e}'}), 500


def _parse_payroll_month_arg(value: Optional[str] = None) -> Tuple[str, int, int]:
    raw = str(value or request.args.get('month') or '').strip()
    try:
        parsed = datetime.strptime(raw, '%Y-%m')
    except (TypeError, ValueError) as exc:
        raise ValueError('Payroll month must use YYYY-MM format') from exc
    return raw, parsed.year, parsed.month


def _bonus_rows_from_workbook(file_storage) -> List[Dict[str, Any]]:
    import openpyxl
    import re
    if isinstance(file_storage, (bytes, bytearray)):
        file_storage = io.BytesIO(file_storage)
    workbook = openpyxl.load_workbook(file_storage, data_only=True, read_only=True)
    try:
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()
    if not rows:
        return []
    aliases = {
        'employee_id': 'employee_id', 'user_id': 'employee_id', 'id': 'employee_id',
        'emp_id': 'employee_id', 'staff_id': 'employee_id', 'emp': 'employee_id',
        'employee_name': 'name', 'name': 'name', 'employee': 'name', 'full_name': 'name',
        'email': 'email', 'email_address': 'email',
        'amount': 'amount', 'bonus': 'amount', 'bonus_amount': 'amount',
        'note': 'note', 'notes': 'note', 'remarks': 'note', 'comment': 'note',
    }
    def _clean_header(val):
        h = str(val or '').strip().lower()
        h = re.sub(r'\(.*?\)', '', h)
        h = h.replace(' ', '_').replace('-', '_').strip('_')
        return aliases.get(h, aliases.get(h.replace('_', '')))

    headers = [_clean_header(cell) for cell in rows[0]]
    parsed: List[Dict[str, Any]] = []
    instruction_keywords = (
        'instruction', 'instructions', 'match employees', 'amount is the bonus',
        'off-days follow', 'note:', 'notes:', 'total'
    )
    for row in rows[1:]:
        if not row or all(cell is None or str(cell).strip() == '' for cell in row):
            continue
        first_cell_str = str(row[0] or '').strip().lower()
        if any(first_cell_str.startswith(kw) for kw in instruction_keywords):
            continue
        # Also check if any cell in the row starts with instructions
        if any(str(c or '').strip().lower().startswith(('instructions', 'match employees by')) for c in row):
            continue
        item: Dict[str, Any] = {}
        for key, cell in zip(headers, row):
            if not key:
                continue
            item[key] = cell
        # If the row has neither an employee identifier nor an amount, skip
        has_id = bool(item.get('employee_id') or item.get('email') or item.get('name'))
        has_amount = item.get('amount') is not None and str(item.get('amount')).strip() != ''
        if not has_id and not has_amount:
            continue
        parsed.append(item)
    return parsed


@app.route('/api/payroll/bonuses', methods=['GET'])
def api_list_payroll_bonuses():
    try:
        _month, year, month = _parse_payroll_month_arg()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    bonuses = db.list_payroll_bonuses(year, month, **payroll_tenant())
    return jsonify({'month': _month, 'bonuses': bonuses, 'count': len(bonuses)})


@app.route('/api/payroll/bonuses/template', methods=['GET'])
def api_payroll_bonus_template():
    try:
        payroll_month, year, month = _parse_payroll_month_arg()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        logger.exception('openpyxl is not installed')
        return jsonify({'error': 'Excel export is unavailable because openpyxl is not installed on this server.'}), 500
    try:
        tenant = payroll_tenant()
        bonuses = {
            int(row['user_id']): row
            for row in db.list_payroll_bonuses(year, month, **tenant)
        }
        users = db.get_all_users(organization_id=tenant['organization_id'], branch_id=tenant['branch_id'])
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = 'Bonuses'
        headers = ['Employee ID', 'Employee Name', 'Email', 'Amount', 'Note']
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill(start_color='0F2942', end_color='0F2942', fill_type='solid')
            cell.alignment = Alignment(horizontal='center')
        for user in users:
            if not user.get('active', 1):
                continue
            existing = bonuses.get(int(user['id']), {})
            sheet.append([
                user['id'],
                user.get('name') or '',
                user.get('email') or '',
                float(existing.get('amount') or 0),
                existing.get('note') or '',
            ])
        sheet.append([])
        sheet.append(['Instructions'])
        sheet.append(['Match employees by Employee ID, Email, or exact Name.'])
        sheet.append(['Amount is the bonus for this payroll month. Leave 0 if none.'])
        sheet.append(['Off-days follow each employee Shift schedule, not a fixed Sunday.'])
        buffer = io.BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        filename = f'payroll-bonuses-{payroll_month}.xlsx'
        return send_file(
            buffer,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
    except Exception as e:
        logger.exception('Failed to build payroll bonus template')
        return jsonify({'error': f'Failed to build bonus template: {e}'}), 500


@app.route('/api/payroll/bonuses', methods=['POST'])
def api_save_payroll_bonus_row():
    try:
        payroll_month, year, month = _parse_payroll_month_arg()
    except ValueError as e:
        return jsonify({'error': str(e), 'field': 'month'}), 400
    data = request.get_json(silent=True) or {}
    try:
        result = db.save_payroll_bonus_row(
            year, month, int(data.get('user_id')), float(data.get('amount')),
            str(data.get('note') or ''), **payroll_tenant(),
        )
    except (TypeError, ValueError) as exc:
        return jsonify({'error': str(exc), 'field': 'amount'}), 400
    if result.get('errors'):
        err = result['errors'][0]
        return jsonify({'error': err['error'], 'field': err.get('field'), 'errors': result['errors']}), 400
    return jsonify({'success': True, 'bonuses': result.get('bonuses')})


@app.route('/api/payroll/bonuses/upload', methods=['POST'])
def api_upload_payroll_bonuses():
    try:
        payroll_month, year, month = _parse_payroll_month_arg()
    except ValueError as e:
        return jsonify({'error': str(e), 'field': 'month'}), 400
    uploaded = request.files.get('file') or request.files.get('bonus_file')
    if uploaded is None or not uploaded.filename:
        return jsonify({'error': 'Upload an Excel file (.xlsx) with bonus rows.', 'field': 'file'}), 400
    filename = uploaded.filename.lower()
    if not filename.endswith('.xlsx'):
        return jsonify({'error': 'Wrong file type. Upload an .xlsx Excel workbook.', 'field': 'file'}), 400
    uploaded.stream.seek(0, 2)
    size = uploaded.stream.tell()
    uploaded.stream.seek(0)
    if size == 0:
        return jsonify({'error': 'The uploaded file is empty.', 'field': 'file'}), 400
    if size > 2 * 1024 * 1024:
        return jsonify({'error': 'File is over 2 MB.', 'field': 'file'}), 400
    file_month = str(request.form.get('file_month') or request.args.get('file_month') or '').strip()
    if file_month and file_month != payroll_month:
        return jsonify({'error': f'File month {file_month} does not match payroll month {payroll_month}.', 'field': 'month'}), 400
    preview_mode = str(request.form.get('preview') or request.args.get('preview') or '').lower() in ('1', 'true', 'yes')
    confirm_param = request.form.get('confirm') or request.args.get('confirm')
    if confirm_param is not None:
        commit = str(confirm_param).lower() in ('1', 'true', 'yes')
    else:
        commit = not preview_mode
    try:
        rows = _bonus_rows_from_workbook(uploaded)
        if not rows:
            return jsonify({'error': 'The workbook has no bonus rows.', 'field': 'file'}), 400
        result = db.replace_payroll_bonuses(year, month, rows, commit=commit, **payroll_tenant())
    except Exception as e:
        logger.exception('Failed to import payroll bonuses')
        return jsonify({'error': str(e), 'field': 'file'}), 400
    if result.get('errors') and commit:
        return jsonify({'error': result['errors'][0]['error'], 'field': result['errors'][0].get('field'), **result}), 400
    return jsonify({
        'success': bool(result.get('committed') or not result.get('errors')),
        'month': payroll_month,
        'imported': result['imported'],
        'preview': result.get('preview') or [],
        'errors': result.get('errors') or [],
        'unmatched': result['unmatched'],
        'committed': result.get('committed'),
        'duplicate_policy': 'reject',
        'bonuses': result['bonuses'],
        'message': (
            f"Imported {result['imported']} bonus row(s) for {payroll_month}."
            if result.get('committed') else
            'Preview only. Confirm to commit. Duplicates are rejected, not summed.'
        ),
    })


@app.route('/api/payroll/export.xlsx', methods=['GET'])
def api_export_payroll_excel():
    try:
        payroll_month, _year, _month = _parse_payroll_month_arg()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    try:
        payload = db.export_monthly_payroll_excel(payroll_month, **payroll_tenant())
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception('Failed to export payroll Excel')
        return jsonify({'error': f'Failed to export payroll: {e}'}), 500
    buffer = io.BytesIO(payload)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'payroll-{payroll_month}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@app.route('/api/payroll/payslip/<int:employee_id>/pdf', methods=['GET'])
def api_download_payslip_pdf(employee_id):
    payroll_month = request.args.get('month', '').strip()
    if not payroll_month:
        return jsonify({'error': 'Payroll month is required in YYYY-MM format'}), 400
    if not verify_user_access(employee_id):
        return jsonify({'error': 'Employee not found or access denied'}), 404
    try:
        payload = db.export_employee_payslip_pdf(payroll_month, employee_id, **payroll_tenant())
        if not payload:
            return jsonify({'error': 'No payroll record found for this employee'}), 404
        buffer = io.BytesIO(payload)
        buffer.seek(0)
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f'payslip-{payroll_month}-{employee_id}.pdf',
            mimetype='application/pdf',
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception('Failed to export payslip PDF')
        return jsonify({'error': f'Failed to export payslip: {e}'}), 500


@app.route('/api/payroll/component-types', methods=['GET'])
def api_list_payroll_component_types():
    return jsonify({'types': db.list_payroll_component_types(**payroll_tenant())})


@app.route('/api/payroll/component-types', methods=['POST'])
def api_save_payroll_component_type():
    data = request.get_json(silent=True) or {}
    try:
        saved_id = db.save_payroll_component_type(
            name=str(data.get('name') or '').strip(),
            code=str(data.get('code') or '').strip(),
            kind=str(data.get('kind') or 'earning'),
            category=str(data.get('category') or 'allowance'),
            calc_mode=str(data.get('calc_mode') or 'fixed'),
            default_amount=float(data.get('default_amount') or 0),
            active=bool(data.get('active', True)),
            component_id=data.get('id'),
            **payroll_tenant(),
        )
    except ValueError as exc:
        msg = str(exc)
        field = 'name' if 'name' in msg.lower() else ('code' if 'code' in msg.lower() else 'form')
        return jsonify({'error': msg, 'message': msg, 'field': field}), 400
    if not saved_id:
        return jsonify({'error': 'Enter a valid pay-line name, code, earning/deduction kind, and category.'}), 400
    current = db.get_payroll_settings(**payroll_tenant())
    if not current.get('enable_named_components'):
        try:
            db.update_payroll_settings(
                bool(current.get('deduct_unpaid_leave')),
                current.get('half_leave_quota_mode', 'category'),
                current.get('half_leaves_per_quota_day', 2),
                current.get('holiday_ot_premium_percent', 0),
                current.get('non_working_day_ot_premium_percent', 0),
                organization_id=current.get('organization_id') or get_current_tenant_id(),
                branch_id=current.get('branch_id') or get_current_branch_id(),
                feature_flags={'enable_named_components': True},
            )
        except ValueError:
            logger.exception('Pay line saved but could not turn on named pay lines')
    return jsonify({'success': True, 'id': saved_id, 'types': db.list_payroll_component_types(**payroll_tenant())})


@app.route('/api/payroll/component-types/<int:component_id>', methods=['DELETE'])
def api_delete_payroll_component_type(component_id):
    if not db.delete_payroll_component_type(component_id, **payroll_tenant()):
        return jsonify({'error': 'Pay line not found'}), 404
    return jsonify({'success': True, 'types': db.list_payroll_component_types(**payroll_tenant())})


@app.route('/api/payroll/employee-components/<int:user_id>', methods=['GET'])
def api_list_employee_components(user_id):
    if not verify_user_access(user_id):
        return jsonify({'error': 'Employee not found or access denied'}), 404
    return jsonify({'components': db.list_employee_components(user_id, **payroll_tenant())})


@app.route('/api/payroll/component-duplicates', methods=['GET'])
def api_list_payroll_component_duplicates():
    return jsonify({'duplicates': db.list_payroll_component_duplicates(**payroll_tenant())})


@app.route('/api/payroll/employee-components', methods=['POST'])
def api_save_employee_component():
    data = request.get_json(silent=True) or {}
    scope = str(data.get('scope') or data.get('scope_type') or 'employee').strip().lower()
    user_id = data.get('user_id')
    join_date = None
    if user_id not in (None, ''):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError, OverflowError):
            return jsonify({'error': 'Employee must be a number', 'field': 'user_id'}), 400
        user = verify_user_access(user_id)
        if not user:
            return jsonify({'error': 'Employee not found or access denied', 'field': 'user_id'}), 404
        join_date = (user.get('join_date') or user.get('enrollment_date') or '')[:10] if isinstance(user, dict) else None
    try:
        result = db.save_employee_component(
            user_id=user_id,
            component_type_id=data.get('component_type_id'),
            amount=float(data.get('amount') or 0),
            percent=float(data.get('percent') or 0),
            effective_from=data.get('effective_from'),
            effective_to=data.get('effective_to'),
            scope_type=scope,
            scope_id=data.get('scope_id') or data.get('department') or data.get('designation') or data.get('shift_id'),
            excluded=bool(data.get('excluded')),
            min_amount=data.get('min_amount'),
            max_amount=data.get('max_amount'),
            rounding=data.get('rounding') or 'cent',
            calc_mode=data.get('calc_mode'),
            confirm_affected_months=bool(data.get('confirm_affected_months')),
            join_date=join_date,
            **payroll_tenant(),
        )
    except ValueError as exc:
        message = str(exc)
        field = 'effective_from' if 'effective_from' in message else (
            'user_id' if 'Employee' in message else (
                'component_type_id' if 'Pay line' in message else (
                    'amount' if any(w in message.lower() for w in ('amount', 'lowest', 'highest', 'limit')) else 'scope_type'
                )
            )
        )
        return jsonify({'error': message, 'field': field, 'success': False}), 400
    except Exception as exc:
        logger.exception("Unexpected error saving employee component")
        return jsonify({'error': f"Could not assign pay line: {exc}", 'field': 'form', 'success': False}), 400
    if not result.get('ok'):
        return jsonify({
            'error': result.get('error'),
            'field': result.get('field'),
            'affected_months': result.get('affected_months') or [],
            'needs_confirmation': True,
        }), 409
    listing_user = int(user_id or 0)
    try:
        components = db.list_employee_components(listing_user, **payroll_tenant()) if listing_user else []
    except Exception:
        logger.exception("Could not list employee components after save")
        components = []
    return jsonify({'success': True, 'id': result.get('id'), 'components': components})


@app.route('/api/payroll/employee-components/<int:assignment_id>', methods=['DELETE'])
def api_delete_employee_component(assignment_id):
    if not db.delete_employee_component(assignment_id, **payroll_tenant()):
        return jsonify({'error': 'Assignment not found'}), 404
    return jsonify({'success': True})


@app.route('/api/payroll/adjustments', methods=['GET'])
def api_list_payroll_adjustments():
    try:
        payroll_month, _year, _month = _parse_payroll_month_arg()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'month': payroll_month, 'adjustments': db.list_payroll_adjustments(payroll_month, **payroll_tenant())})


@app.route('/api/payroll/adjustments', methods=['POST'])
def api_save_payroll_adjustment():
    try:
        payroll_month, _year, _month = _parse_payroll_month_arg()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    data = request.get_json(silent=True) or {}
    try:
        user_id = int(data.get('user_id'))
    except (TypeError, ValueError, OverflowError):
        return jsonify({'error': 'Employee is required'}), 400
    if not verify_user_access(user_id):
        return jsonify({'error': 'Employee not found or access denied'}), 404
    saved_id = db.save_payroll_adjustment(
        user_id=user_id,
        payroll_month=payroll_month,
        adj_type=str(data.get('adj_type') or ''),
        pay_mode=str(data.get('pay_mode') or 'fixed'),
        amount=float(data.get('amount') or 0),
        multiplier=float(data.get('multiplier') or 0),
        work_date=data.get('work_date'),
        note=str(data.get('note') or ''),
        source_month=data.get('source_month'),
        created_by=session.get('admin_name') or session.get('username') or 'Admin',
        **payroll_tenant(),
    )
    if not saved_id:
        return jsonify({'error': 'Choose a valid exception type and amount or multiplier.'}), 400
    return jsonify({
        'success': True,
        'id': saved_id,
        'adjustments': db.list_payroll_adjustments(payroll_month, **payroll_tenant()),
    })


@app.route('/api/payroll/adjustments/<int:adjustment_id>', methods=['DELETE'])
def api_delete_payroll_adjustment(adjustment_id):
    if not db.delete_payroll_adjustment(adjustment_id, **payroll_tenant()):
        return jsonify({'error': 'Exception not found'}), 404
    return jsonify({'success': True})


@app.route('/api/payroll/audit', methods=['GET'])
def api_list_payroll_audit():
    month = (request.args.get('month') or '').strip() or None
    return jsonify({'audit': db.list_payroll_audit(month, **payroll_tenant())})


@app.route('/api/payroll/payslip/<int:employee_id>', methods=['GET'])
def api_get_employee_payslip(employee_id):
    payroll_month = request.args.get('month', '').strip()
    if not payroll_month:
        return jsonify({'error': 'Payroll month is required in YYYY-MM format'}), 400
    if not verify_user_access(employee_id):
        return jsonify({'error': 'Employee not found or access denied'}), 404
    try:
        payslip = db.get_employee_payslip(
            payroll_month,
            employee_id,
            organization_id=get_current_tenant_id(),
            branch_id=get_current_branch_id(),
        )
        if not payslip:
            return jsonify({'error': 'No payroll record found for this employee'}), 404
        from payroll_extensions import payslip_invoice_html
        payslip['html'] = payslip_invoice_html(payslip)
        return jsonify(payslip)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("Failed to load payslip")
        return jsonify({'error': f'Failed to load payslip: {e}'}), 500


@app.route('/api/payroll/scope-options', methods=['GET'])
def api_get_payroll_scope_options():
    try:
        return jsonify(db.get_payroll_scope_options(**payroll_tenant()))
    except Exception as exc:
        logger.exception("Failed to load payroll scope options")
        return jsonify({
            'departments': [], 'designations': [], 'shifts': [],
            'employees': [], 'branches': [], 'error': str(exc),
        }), 500


@app.route('/api/payroll-settings/scoped-rules/half-leave', methods=['GET'])
def api_get_scoped_half_leave_rules():
    try:
        all_rules = db.get_payroll_scoped_rules(**payroll_tenant())
        half_leave_rules = [r for r in all_rules if r.get('policy_type') == 'half_leave_quota']
        return jsonify(half_leave_rules)
    except Exception as e:
        logger.exception("Failed to get scoped half-leave rules")
        return jsonify({'error': str(e)}), 500

@app.route('/api/payroll-settings/scoped-rules/half-leave', methods=['POST'])
def api_save_scoped_half_leave_rule():
    data = request.json or {}
    scope_type = data.get('scope_type')
    scope_id = data.get('scope_id')
    quota_mode = data.get('half_leave_quota_mode')
    monthly_count = data.get('half_leaves_per_quota_day')
    
    if not scope_type or not scope_id or not quota_mode or not monthly_count:
        return jsonify({'error': 'Missing required fields'}), 400
        
    try:
        res = db.save_scoped_half_leave_rule(scope_type, scope_id, quota_mode, monthly_count, **payroll_tenant())
        if isinstance(res, dict) or hasattr(res, 'get'):
            if res.get('duplicate'):
                return jsonify({'error': res.get('error'), 'duplicate': True, 'target_name': res.get('target_name')}), 409
            if res.get('success'):
                return jsonify({'success': True, 'message': res.get('message'), 'target_name': res.get('target_name')})
            return jsonify({'error': res.get('error') or 'Failed to save rule. Ensure target exists and values are valid.'}), 400
        if res:
            return jsonify({'success': True, 'message': 'Half-day rule added.'})
        return jsonify({'error': 'Failed to save rule. Ensure target exists and values are valid.'}), 400
    except Exception as e:
        logger.exception("Failed to save scoped half-leave rule")
        return jsonify({'error': str(e)}), 500

@app.route('/api/payroll-settings/scoped-rules/half-leave/<int:rule_id>', methods=['DELETE'])
def api_delete_scoped_half_leave_rule(rule_id):
    try:
        all_rules = db.get_payroll_scoped_rules(**payroll_tenant())
        rule = next((r for r in all_rules if r['id'] == rule_id), None)
        if not rule or rule.get('policy_type') != 'half_leave_quota':
            return jsonify({'error': 'Rule not found or is not a half-leave rule'}), 404
            
        success = db.delete_payroll_scoped_rule(rule_id, **payroll_tenant())
        if success:
            return jsonify({'success': True})
        return jsonify({'error': 'Rule could not be deleted'}), 400
    except Exception as e:
        logger.exception("Failed to delete scoped rule")
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings/payroll/scoped-rules/overtime-limit', methods=['GET'])
@app.route('/api/payroll-settings/scoped-rules/overtime-limit', methods=['GET'])
def api_get_scoped_overtime_limit_rules():
    try:
        all_rules = db.get_payroll_scoped_rules(**payroll_tenant())
        ot_limit_rules = [r for r in all_rules if r.get('policy_type') == 'overtime_limit']
        return jsonify(ot_limit_rules)
    except Exception as e:
        logger.exception("Failed to get scoped overtime limit rules")
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings/payroll/scoped-rules/overtime-limit', methods=['POST'])
@app.route('/api/payroll-settings/scoped-rules/overtime-limit', methods=['POST'])
def api_save_scoped_overtime_limit_rule():
    data = request.json or {}
    scope_type = data.get('scope_type')
    scope_id = data.get('scope_id')
    max_hours = data.get('overtime_max_hours')
    period = data.get('overtime_max_period')
    
    if not scope_type or scope_id is None or str(scope_id).strip() == '' or max_hours is None or not period:
        return jsonify({'error': 'Missing required fields'}), 400
    try:
        max_h = float(max_hours)
    except (TypeError, ValueError, OverflowError):
        return jsonify({'error': 'Invalid max hours'}), 400
    if not 0 <= max_h <= 744:
        return jsonify({'error': 'Maximum overtime hours must be from 0 to 744.'}), 400
        
    try:
        success = db.save_scoped_overtime_limit_rule(scope_type, scope_id, max_h, period, **payroll_tenant())
        if success:
            return jsonify({'success': True})
        return jsonify({'error': 'Failed to save rule. Ensure target exists and values are valid.'}), 400
    except Exception as e:
        logger.exception("Failed to save scoped overtime limit rule")
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings/payroll/scoped-rules/overtime-limit/<int:rule_id>', methods=['DELETE'])
@app.route('/api/payroll-settings/scoped-rules/overtime-limit/<int:rule_id>', methods=['DELETE'])
def api_delete_scoped_overtime_limit_rule(rule_id):
    try:
        all_rules = db.get_payroll_scoped_rules(**payroll_tenant())
        rule = next((r for r in all_rules if r['id'] == rule_id), None)
        if not rule or rule.get('policy_type') != 'overtime_limit':
            return jsonify({'error': 'Rule not found or is not an overtime-limit rule'}), 404
            
        success = db.delete_payroll_scoped_rule(rule_id, **payroll_tenant())
        if success:
            return jsonify({'success': True})
        return jsonify({'error': 'Rule could not be deleted'}), 400
    except Exception as e:
        logger.exception("Failed to delete scoped rule")
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings/payroll/scoped-rules/overtime-premium', methods=['GET'])
@app.route('/api/payroll-settings/scoped-rules/overtime-premium', methods=['GET'])
def api_get_scoped_overtime_premium_rules():
    try:
        all_rules = db.get_payroll_scoped_rules(**payroll_tenant())
        premium_rules = [r for r in all_rules if r.get('policy_type') == 'overtime_premium']
        return jsonify(premium_rules)
    except Exception as e:
        logger.exception("Failed to get scoped overtime premium rules")
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings/payroll/scoped-rules/overtime-premium', methods=['POST'])
@app.route('/api/payroll-settings/scoped-rules/overtime-premium', methods=['POST'])
def api_save_scoped_overtime_premium_rule():
    data = request.json or {}
    scope_type = data.get('scope_type')
    scope_id = data.get('scope_id')
    day_type = data.get('day_type')
    premium_percent = data.get('premium_percent')
    if not scope_type or scope_id in (None, '') or not day_type or premium_percent is None:
        return jsonify({'error': 'Choose a group, a day type, and a premium percent.'}), 400
    try:
        premium = float(premium_percent)
    except (TypeError, ValueError, OverflowError):
        return jsonify({'error': 'Premium must be a number between 0 and 1000.'}), 400
    if not 0 <= premium <= 1000:
        return jsonify({'error': 'Premium must be between 0 and 1000 percent.'}), 400
    try:
        success = db.save_scoped_overtime_premium_rule(
            scope_type, scope_id, day_type, premium, **payroll_tenant()
        )
        if success:
            return jsonify({'success': True})
        return jsonify({'error': 'Failed to save rule. Choose a valid department or person, and Holiday or Rest day.'}), 400
    except Exception as e:
        logger.exception("Failed to save scoped overtime premium rule")
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings/payroll/scoped-rules/overtime-premium/<int:rule_id>', methods=['DELETE'])
@app.route('/api/payroll-settings/scoped-rules/overtime-premium/<int:rule_id>', methods=['DELETE'])
def api_delete_scoped_overtime_premium_rule(rule_id):
    try:
        all_rules = db.get_payroll_scoped_rules(**payroll_tenant())
        rule = next((r for r in all_rules if r['id'] == rule_id), None)
        if not rule or rule.get('policy_type') != 'overtime_premium':
            return jsonify({'error': 'Rule not found or is not an overtime extra rule'}), 404
        success = db.delete_payroll_scoped_rule(rule_id, **payroll_tenant())
        if success:
            return jsonify({'success': True})
        return jsonify({'error': 'Rule could not be deleted'}), 400
    except Exception as e:
        logger.exception("Failed to delete scoped overtime premium rule")
        return jsonify({'error': str(e)}), 500

@app.route('/api/payroll/rules', methods=['GET'])
def api_get_payroll_rules():
    try:
        return jsonify(db.get_payroll_rules(active_only=False, **payroll_tenant()))
    except Exception as e:
        logger.exception("Failed to load payroll rules")
        return jsonify({'error': f'Failed to load payroll rules: {e}'}), 500


@app.route('/api/payroll/rules', methods=['POST'])
def api_create_payroll_rule():
    access_error = _require_admin_configuration()
    if access_error:
        return access_error
    data = request.get_json(silent=True) or {}
    try:
        rule = db.save_payroll_rule(
            min_absent_days=data.get('min_absent_days'),
            max_absent_days=data.get('max_absent_days'),
            deduction_type=data.get('deduction_type', 'percentage'),
            deduction_percent=data.get('deduction_percent'),
            fixed_amount=data.get('fixed_amount', 0),
            active=data.get('active', True),
            rule_basis=data.get('rule_basis', 'absence'),
            **payroll_tenant(),
        )
        return jsonify({'success': True, 'rule': rule}), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("Failed to create payroll rule")
        return jsonify({'error': f'Failed to create payroll rule: {e}'}), 500


@app.route('/api/payroll/rules/<int:rule_id>', methods=['PUT'])
def api_update_payroll_rule(rule_id):
    access_error = _require_admin_configuration()
    if access_error:
        return access_error
    data = request.get_json(silent=True) or {}
    try:
        rule = db.save_payroll_rule(
            min_absent_days=data.get('min_absent_days'),
            max_absent_days=data.get('max_absent_days'),
            deduction_type=data.get('deduction_type', 'percentage'),
            deduction_percent=data.get('deduction_percent'),
            fixed_amount=data.get('fixed_amount', 0),
            active=data.get('active', True),
            rule_id=rule_id,
            rule_basis=data.get('rule_basis', 'absence'),
            **payroll_tenant(),
        )
        return jsonify({'success': True, 'rule': rule})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("Failed to update payroll rule %s", rule_id)
        return jsonify({'error': f'Failed to update payroll rule: {e}'}), 500


@app.route('/api/payroll/rules/<int:rule_id>', methods=['DELETE'])
def api_deactivate_payroll_rule(rule_id):
    access_error = _require_admin_configuration()
    if access_error:
        return access_error
    try:
        rule = db.set_payroll_rule_active(rule_id, False, **payroll_tenant())
        if not rule:
            return jsonify({'error': 'Payroll rule not found'}), 404
        return jsonify({'success': True, 'rule': rule})
    except Exception as e:
        logger.exception("Failed to deactivate payroll rule %s", rule_id)
        return jsonify({'error': f'Failed to deactivate payroll rule: {e}'}), 500


@app.route('/api/payroll/rules/<int:rule_id>/activate', methods=['POST'])
def api_activate_payroll_rule(rule_id):
    access_error = _require_admin_configuration()
    if access_error:
        return access_error
    try:
        rule = db.set_payroll_rule_active(rule_id, True, **payroll_tenant())
        if not rule:
            return jsonify({'error': 'Payroll rule not found'}), 404
        return jsonify({'success': True, 'rule': rule})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("Failed to activate payroll rule %s", rule_id)
        return jsonify({'error': f'Failed to activate payroll rule: {e}'}), 500


@app.route('/api/payroll/calendar', methods=['GET'])
def api_get_payroll_calendar():
    try:
        return jsonify(db.get_special_date_overrides(**payroll_tenant()))
    except Exception as e:
        logger.exception("Failed to load payroll calendar overrides")
        return jsonify({'error': f'Failed to load payroll calendar: {e}'}), 500


@app.route('/api/payroll/calendar', methods=['POST'])
def api_create_payroll_calendar_override():
    access_error = _require_admin_configuration()
    if access_error:
        return access_error
    data = request.get_json(silent=True) or {}
    try:
        override = db.save_special_date_override(
            calendar_date=data.get('calendar_date'),
            override_type=data.get('override_type'),
            note=data.get('note', ''),
            scope_type=data.get('scope_type', 'all'),
            scope_id=data.get('scope_id'),
            **payroll_tenant(),
        )
        return jsonify({'success': True, 'override': override}), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("Failed to create payroll calendar override")
        return jsonify({'error': f'Failed to create calendar override: {e}'}), 500


@app.route('/api/payroll/calendar/<int:override_id>', methods=['PUT'])
def api_update_payroll_calendar_override(override_id):
    access_error = _require_admin_configuration()
    if access_error:
        return access_error
    data = request.get_json(silent=True) or {}
    try:
        override = db.save_special_date_override(
            calendar_date=data.get('calendar_date'),
            override_type=data.get('override_type'),
            note=data.get('note', ''),
            override_id=override_id,
            scope_type=data.get('scope_type', 'all'),
            scope_id=data.get('scope_id'),
            **payroll_tenant(),
        )
        return jsonify({'success': True, 'override': override})
    except ValueError as e:
        status = 404 if str(e) == 'Special date not found' else 400
        return jsonify({'error': str(e)}), status
    except Exception as e:
        logger.exception("Failed to update payroll calendar override %s", override_id)
        return jsonify({'error': f'Failed to update calendar override: {e}'}), 500


@app.route('/api/payroll/calendar/<int:override_id>', methods=['DELETE'])
def api_delete_payroll_calendar_override(override_id):
    access_error = _require_admin_configuration()
    if access_error:
        return access_error
    try:
        if not db.delete_special_date_override(override_id, **payroll_tenant()):
            return jsonify({'error': 'Special date not found'}), 404
        return jsonify({'success': True})
    except Exception as e:
        logger.exception("Failed to delete payroll calendar override %s", override_id)
        return jsonify({'error': f'Failed to delete calendar override: {e}'}), 500


@app.route('/api/payroll/finalize', methods=['POST'])
def api_finalize_monthly_payroll():
    access_error = _require_admin_configuration()
    if access_error:
        return access_error
    data = request.get_json(silent=True) or {}
    payroll_month = str(data.get('month', '')).strip()
    if not payroll_month:
        return jsonify({'error': 'Payroll month is required in YYYY-MM format'}), 400
    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    if db.get_monthly_payroll(
        payroll_month,
        organization_id=current_tenant,
        branch_id=current_branch,
    ):
        return jsonify({'error': f'Payroll for {payroll_month} is already finalized'}), 409
    try:
        preview = db.calculate_monthly_payroll(
            payroll_month,
            organization_id=current_tenant,
            branch_id=current_branch,
        )
        excluded = {int(item.get('user_id') or item.get('employee_id')): str(item.get('reason') or '').strip()
                    for item in (data.get('excluded') or []) if item}
        if any(not reason for reason in excluded.values()):
            return jsonify({'error': 'Each excluded employee needs a mandatory reason', 'field': 'excluded'}), 400
        if excluded:
            preview['records'] = [row for row in preview['records'] if int(row.get('employee_id') or 0) not in excluded]
            preview['excluded'] = [{'user_id': uid, 'reason': reason} for uid, reason in excluded.items()]
        issues = []
        for row in preview['records']:
            if row.get('shift_history_missing'):
                issues.append({'employee_id': row['employee_id'], 'employee_name': row['employee_name'], 'issue': 'missing_shift_history'})
            if row['basic_salary'] <= 0:
                issues.append({'employee_id': row['employee_id'], 'employee_name': row['employee_name'], 'issue': 'missing_salary'})
            if row.get('rule_missing'):
                issues.append({'employee_id': row['employee_id'], 'employee_name': row['employee_name'], 'issue': 'missing_rule'})
        if not preview['records']:
            return jsonify({'error': 'There are no active employees to include in payroll', 'issues': issues}), 400
        if issues:
            return jsonify({'error': 'Resolve or exclude blocking employees before finalize', 'issues': issues}), 400
        preview['finalize_early'] = bool(data.get('finalize_early'))
        saved_run = db.finalize_monthly_payroll(
            preview,
            organization_id=current_tenant,
            branch_id=current_branch,
            finalized_by=session.get('admin_name') or session.get('username') or session.get('organization_name') or 'Admin',
        )
        if not saved_run:
            return jsonify({'error': f'Payroll for {payroll_month} was finalized by another request'}), 409
        return jsonify({'success': True, 'payroll': saved_run}), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception("Failed to finalize payroll for %s", payroll_month)
        return jsonify({'error': f'Failed to finalize payroll: {e}'}), 500


@app.route('/api/payroll/reopen', methods=['POST'])
def api_reopen_monthly_payroll():
    access_error = _require_admin_configuration()
    if access_error:
        return access_error
    data = request.get_json(silent=True) or {}
    payroll_month = str(data.get('month') or '').strip()
    reason = str(data.get('reason') or '').strip()
    if not payroll_month:
        return jsonify({'error': 'Payroll month is required in YYYY-MM format', 'field': 'month'}), 400
    try:
        result = db.reopen_monthly_payroll(
            payroll_month, reason, **payroll_tenant(),
            actor=session.get('admin_name') or session.get('username') or 'Admin',
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({'error': str(exc), 'field': 'reason' if 'reason' in str(exc).lower() else 'month'}), 400
    except Exception as exc:
        logger.exception('Failed to reopen payroll')
        return jsonify({'error': str(exc)}), 500


# ============================================
# SYSTEM & BRANCH CONFIGURATION ENDPOINTS
# ============================================

@app.route('/api/settings/payroll', methods=['GET'])
def api_get_payroll_settings():
    return jsonify(db.get_payroll_settings(**payroll_tenant()))


@app.route('/api/settings/payroll', methods=['POST', 'PUT'])
def api_update_payroll_settings():
    data = request.get_json(silent=True) or {}
    current_settings = db.get_payroll_settings(**payroll_tenant())
    value = data.get('deduct_unpaid_leave')
    if value not in (True, False, 0, 1):
        return jsonify({'error': 'deduct_unpaid_leave must be true or false'}), 400
    quota_mode = str(data.get('half_leave_quota_mode', current_settings.get('half_leave_quota_mode', 'category'))).strip().lower()
    quota_mode = {'separate': 'category', 'convert': 'monthly_convert'}.get(quota_mode, quota_mode)
    if quota_mode not in ('category', 'monthly_cap', 'monthly_convert'):
        return jsonify({'error': 'Choose the category policy, a monthly cap, or monthly conversion.'}), 400
    try:
        conversion_count = int(data.get('half_leaves_per_quota_day', current_settings.get('half_leaves_per_quota_day', 2)))
    except (TypeError, ValueError, OverflowError):
        return jsonify({'error': 'half_leaves_per_quota_day must be a positive integer'}), 400
    if conversion_count < 1:
        return jsonify({'error': 'half_leaves_per_quota_day must be a positive integer'}), 400
    try:
        holiday_premium = float(data.get('holiday_ot_premium_percent', current_settings.get('holiday_ot_premium_percent', 0)))
        non_working_premium = float(data.get('non_working_day_ot_premium_percent', current_settings.get('non_working_day_ot_premium_percent', 0)))
    except (TypeError, ValueError, OverflowError):
        return jsonify({'error': 'Overtime premiums must be valid percentages between 0 and 1000'}), 400
    if not (0 <= holiday_premium <= 1000 and 0 <= non_working_premium <= 1000):
        return jsonify({'error': 'Overtime premiums must be between 0 and 1000 percent'}), 400
    try:
        db.update_payroll_settings(
            bool(value), quota_mode, conversion_count, holiday_premium, non_working_premium,
            organization_id=current_settings.get('organization_id') or get_current_tenant_id(),
            branch_id=current_settings.get('branch_id') or get_current_branch_id(),
            ot_hour_divisor=data.get('ot_hour_divisor', current_settings.get('ot_hour_divisor')),
            proration_basis=data.get('proration_basis', current_settings.get('proration_basis')),
            ot_rounding=data.get('ot_rounding', current_settings.get('ot_rounding')),
            attendance_pay_mode=data.get('attendance_pay_mode', current_settings.get('attendance_pay_mode')),
            currency_code=data.get('currency_code', current_settings.get('currency_code')),
            decimal_places=data.get('decimal_places', current_settings.get('decimal_places')),
            holiday_ot_pay_mode=data.get('holiday_ot_pay_mode', current_settings.get('holiday_ot_pay_mode')),
            non_working_day_ot_pay_mode=data.get('non_working_day_ot_pay_mode', current_settings.get('non_working_day_ot_pay_mode')),
            holiday_ot_multiplier=data.get('holiday_ot_multiplier', current_settings.get('holiday_ot_multiplier')),
            non_working_day_ot_multiplier=data.get('non_working_day_ot_multiplier', current_settings.get('non_working_day_ot_multiplier')),
            holiday_ot_fixed_amount=data.get('holiday_ot_fixed_amount', current_settings.get('holiday_ot_fixed_amount')),
            non_working_day_ot_fixed_amount=data.get('non_working_day_ot_fixed_amount', current_settings.get('non_working_day_ot_fixed_amount')),
            off_day_attendance_pay_active=data.get(
                'off_day_attendance_pay_active',
                current_settings.get('off_day_attendance_pay_active'),
            ),
            off_day_attendance_pay_mode=data.get('off_day_attendance_pay_mode', current_settings.get('off_day_attendance_pay_mode')),
            off_day_attendance_pay_multiplier=data.get(
                'off_day_attendance_pay_multiplier',
                current_settings.get('off_day_attendance_pay_multiplier'),
            ),
            off_day_attendance_pay_fixed_amount=data.get(
                'off_day_attendance_pay_fixed_amount',
                current_settings.get('off_day_attendance_pay_fixed_amount'),
            ),
            feature_flags={key: data.get(key, current_settings.get(key)) for key in db.FEATURE_FLAGS},
            letterhead={key: data.get(key, current_settings.get(key)) for key in db.PAYSLIP_LETTERHEAD_FIELDS},
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        logger.exception('Failed to save payroll settings')
        return jsonify({'error': f'Failed to save payroll settings: {exc}'}), 500
    return jsonify({
        'success': True,
        'settings': db.get_payroll_settings(**payroll_tenant()),
        'message': 'Payroll settings saved.'
    })


@app.route('/api/settings/payroll/preview', methods=['GET', 'POST'])
def api_preview_payroll_settings():
    data = request.get_json(silent=True) or {}
    args = request.args
    try:
        return jsonify(db.preview_payroll_settings_impact(
            **payroll_tenant(),
            proration_basis=data.get('proration_basis') or args.get('proration_basis'),
            ot_rounding=data.get('ot_rounding') or args.get('ot_rounding'),
            payroll_month=data.get('month') or args.get('month'),
        ))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/salary/tenant-report', methods=['GET'])
def api_salary_config_tenant_report():
    access_error = _require_admin_configuration()
    if access_error:
        return access_error
    return jsonify({'mismatches': db.list_salary_config_tenant_report(), 'auto_fixed': False})


@app.route('/api/settings/payroll/overtime-rules', methods=['GET'])
def api_get_overtime_premium_rules():
    return jsonify(db.get_overtime_premium_rules(**payroll_tenant()))


@app.route('/api/settings/payroll/overtime-limit', methods=['PUT'])
def api_update_overtime_limit_settings():
    data = request.get_json(silent=True) or {}
    try:
        max_hours = float(data.get('overtime_max_hours'))
    except (TypeError, ValueError, OverflowError):
        return jsonify({'success': False, 'error': 'Maximum overtime hours must be a number from 0 to 744.'}), 400
    period = str(data.get('overtime_max_period') or '').strip().lower()
    if not 0 <= max_hours <= 744:
        return jsonify({'success': False, 'error': 'Maximum overtime hours must be from 0 to 744.'}), 400
    if period not in ('per_entry', 'day', 'week', 'month'):
        return jsonify({'success': False, 'error': 'Choose an OT entry, day, week, or calendar month limit.'}), 400
    if not db.update_overtime_limit_settings(max_hours, period, **payroll_tenant()):
        return jsonify({'success': False, 'error': 'Failed to save overtime limit settings.'}), 500
    return jsonify({
        'success': True,
        'settings': db.get_payroll_settings(**payroll_tenant()),
        'message': 'Overtime limit saved.',
    })


@app.route('/api/settings/payroll/overtime-rules', methods=['POST'])
def api_save_overtime_premium_rule():
    data = request.get_json(silent=True) or {}
    day_type = str(data.get('day_type') or '').strip()
    try:
        premium_percent = float(data.get('premium_percent'))
    except (TypeError, ValueError, OverflowError):
        return jsonify({'success': False, 'error': 'Premium must be a number between 0 and 1000.'}), 400
    if not db.save_overtime_premium_rule(day_type, premium_percent, **payroll_tenant()):
        return jsonify({'success': False, 'error': 'Choose a valid overtime rule and a premium between 0 and 1000 percent.'}), 400
    return jsonify({'success': True, 'rules': db.get_overtime_premium_rules(**payroll_tenant()), 'message': 'Overtime premium rule saved and activated.'})


@app.route('/api/settings/payroll/overtime-rules/<day_type>', methods=['PUT', 'DELETE'])
def api_manage_overtime_premium_rule(day_type):
    if request.method == 'DELETE':
        success = db.remove_overtime_premium_rule(day_type, **payroll_tenant())
        message = 'Overtime premium rule removed.'
    else:
        data = request.get_json(silent=True) or {}
        active = data.get('active')
        if active not in (True, False, 0, 1):
            return jsonify({'success': False, 'error': 'active must be true or false.'}), 400
        success = db.set_overtime_premium_rule_active(day_type, bool(active), **payroll_tenant())
        message = 'Overtime premium rule activated.' if active else 'Overtime premium rule deactivated.'
    if not success:
        return jsonify({'success': False, 'error': 'Overtime premium rule not found.'}), 404
    return jsonify({'success': True, 'rules': db.get_overtime_premium_rules(**payroll_tenant()), 'message': message})


@app.route('/api/settings/payroll/overtime-holiday-rules', methods=['GET'])
def api_get_overtime_holiday_premium_rules():
    return jsonify(db.get_overtime_holiday_premium_rules(**payroll_tenant()))


@app.route('/api/settings/payroll/overtime-holiday-rules/<int:holiday_id>', methods=['PUT', 'DELETE'])
def api_manage_overtime_holiday_premium_rule(holiday_id):
    if request.method == 'DELETE':
        success = db.set_company_holiday_overtime_premium(holiday_id, None, **payroll_tenant())
        message = 'Holiday now uses the default public holiday premium.'
    else:
        data = request.get_json(silent=True) or {}
        try:
            premium_percent = float(data.get('premium_percent'))
        except (TypeError, ValueError, OverflowError):
            return jsonify({'success': False, 'error': 'Premium must be a number between 0 and 1000.'}), 400
        if not 0 <= premium_percent <= 1000:
            return jsonify({'success': False, 'error': 'Premium must be between 0 and 1000 percent.'}), 400
        success = db.set_company_holiday_overtime_premium(holiday_id, premium_percent, **payroll_tenant())
        message = 'Holiday overtime premium saved.'
    if not success:
        return jsonify({'success': False, 'error': 'Holiday not found or premium could not be saved.'}), 404
    return jsonify({
        'success': True,
        'rules': db.get_overtime_holiday_premium_rules(**payroll_tenant()),
        'message': message,
    })


@app.route('/api/settings/branch', methods=['GET'])
def api_get_branch_settings():
    return jsonify(db.get_branch_settings())

@app.route('/api/settings/branch', methods=['POST', 'PUT'])
def api_update_branch_settings():
    data = request.get_json() or {}
    current = db.get_branch_settings()
    branch_name = (data.get('branch_name') or '').strip()
    city = (data.get('city') or '').strip()
    if not branch_name or not city:
        return jsonify({'error': 'Branch name and city are required'}), 400
    company_name = data.get('company_name', current.get('company_name', '')) or ''
    admin_name = data.get('admin_name', current.get('admin_name', 'Mr. Junaid')) or 'Mr. Junaid'
    address = data.get('address', current.get('address', '')) or ''
    phone = data.get('phone', current.get('phone', '')) or ''
    email = data.get('email', current.get('email', '')) or ''
    
    success = db.update_branch_settings(
        branch_name=branch_name,
        city=city,
        company_name=company_name,
        address=address,
        phone=phone,
        email=email,
        admin_name=admin_name
    )
    if success:
        return jsonify({'success': True, 'message': 'Branch settings updated successfully'})
    return jsonify({'error': 'Failed to update branch settings'}), 500


# ============================================
# DEPARTMENT MANAGEMENT ENDPOINTS
# ============================================

@app.route('/api/departments', methods=['GET'])
def api_get_departments():
    active_only = request.args.get('active_only', 'false').lower() == 'true'
    try:
        return jsonify(db.get_all_departments_list(active_only=active_only, organization_id=get_current_tenant_id()))
    except Exception as e:
        logger.exception("Failed to load departments")
        return jsonify({'error': f'Failed to load departments: {e}'}), 500

@app.route('/api/departments', methods=['POST'])
def api_add_department():
    data = request.get_json() or {}
    name = data.get('name')
    if not name:
        return jsonify({'error': 'Department name is required'}), 400
    try:
        dept_id = db.add_department(
            name=name,
            code=data.get('code', ''),
            description=data.get('description', ''),
            organization_id=get_current_tenant_id(),
            branch_id=get_current_branch_id()
        )
    except Exception as e:
        logger.exception("Failed to create department '%s'", name)
        return jsonify({'error': f'Failed to create department: {e}'}), 500
    if dept_id:
        return jsonify({'success': True, 'id': dept_id, 'message': f'Department {name} created successfully'}), 201
    return jsonify({'error': f'Department {name} already exists'}), 409

@app.route('/api/departments/<int:dept_id>', methods=['PUT'])
def api_update_department(dept_id):
    data = request.get_json() or {}
    name = data.get('name')
    if not name:
        return jsonify({'error': 'Department name is required'}), 400
    success = db.update_department(
        dept_id=dept_id,
        name=name,
        code=data.get('code', ''),
        description=data.get('description', ''),
        active=int(data.get('active', 1)),
        organization_id=get_current_tenant_id()
    )
    if success:
        return jsonify({'success': True, 'message': f'Department {name} updated successfully'})
    return jsonify({'error': 'Failed to update department or access denied'}), 404

@app.route('/api/departments/<int:dept_id>', methods=['DELETE'])
def api_delete_department(dept_id):
    success = db.delete_department(dept_id, organization_id=get_current_tenant_id())
    if success:
        return jsonify({'success': True, 'message': 'Department disabled successfully'})
    return jsonify({'error': 'Failed to disable department or access denied'}), 404


# ============================================
# DESIGNATION MANAGEMENT ENDPOINTS
# ============================================

@app.route('/api/designations', methods=['GET'])
def api_get_designations():
    dept_id = request.args.get('department_id', type=int)
    dept_name = request.args.get('department_name')
    active_only = request.args.get('active_only', 'false').lower() == 'true'
    return jsonify(db.get_designations(
        department_id=dept_id,
        department_name=dept_name,
        active_only=active_only,
        organization_id=get_current_tenant_id()
    ))

@app.route('/api/designations', methods=['POST'])
def api_add_designation():
    data = request.get_json() or {}
    dept_id = data.get('department_id')
    title = data.get('title')
    if dept_id is None or title is None or not str(title).strip():
        return jsonify({'error': 'Department and designation title are required'}), 400
    try:
        dept_id_int = int(dept_id)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid department ID: must be an integer'}), 400

    current_tenant = get_current_tenant_id()
    dept = db.get_department(dept_id_int, organization_id=current_tenant)
    if not dept:
        return jsonify({'error': f'Department ID {dept_id_int} does not exist or access denied'}), 404
    if not dept.get('active'):
        return jsonify({'error': f"Department '{dept.get('name')}' is inactive and cannot accept new designations"}), 400

    desig_id = db.add_designation(
        department_id=dept_id_int,
        title=str(title).strip(),
        description=data.get('description', ''),
        organization_id=current_tenant,
        branch_id=get_current_branch_id()
    )
    if desig_id:
        return jsonify({'success': True, 'id': desig_id, 'message': f'Designation {title} created successfully'}), 201
    return jsonify({'error': 'Failed to create designation'}), 500

@app.route('/api/designations/<int:desig_id>', methods=['PUT'])
def api_update_designation(desig_id):
    data = request.get_json() or {}
    title = data.get('title')
    if not title:
        return jsonify({'error': 'Designation title is required'}), 400
    success = db.update_designation(
        desig_id=desig_id,
        title=title,
        description=data.get('description', ''),
        active=int(data.get('active', 1)),
        organization_id=get_current_tenant_id()
    )
    if success:
        return jsonify({'success': True, 'message': 'Designation updated successfully'})
    return jsonify({'error': 'Failed to update designation or access denied'}), 404

@app.route('/api/designations/<int:desig_id>', methods=['DELETE'])
def api_delete_designation(desig_id):
    success = db.delete_designation(desig_id, organization_id=get_current_tenant_id())
    if success:
        return jsonify({'success': True, 'message': 'Designation disabled successfully'})
    return jsonify({'error': 'Failed to disable designation or access denied'}), 404


# ============================================
# DYNAMIC CAMERA MANAGEMENT ENDPOINTS
# ============================================

def _camera_source(cam):
    """Resolve the OpenCV source for a camera row (int index for webcams, URL string otherwise)."""
    if cam['camera_type'] == 'webcam':
        try:
            return int(cam['rtsp_url'])
        except (TypeError, ValueError):
            return 0
    return cam['rtsp_url']


VALID_CAMERA_TYPES = {'dvr', 'nvr', 'ip_camera', 'webcam'}


def _validate_camera_connection(camera_type, rtsp_url):
    """Return an error message if the camera connection info is invalid, else None."""
    if camera_type not in VALID_CAMERA_TYPES:
        return f'Invalid camera type: {camera_type}'
    url = str(rtsp_url or '').strip()
    if camera_type == 'webcam':
        if not url.isdigit():
            return 'Select a detected local web camera (device index).'
    elif not url.lower().startswith(('rtsp://', 'rtsps://', 'http://', 'https://')):
        return 'DVR / NVR / IP cameras require a valid RTSP URL (e.g. rtsp://user:pass@ip:554/...)'
    return None


def reload_all_camera_streams():
    """Sync background stream readers with the cameras configured in the DB.

    - Starts readers for every active camera (new or URL-changed).
    - Stops readers for cameras that were deleted or disabled.
    """
    if os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('DISABLE_CAMERA_STREAMS', '').lower() in {'true', '1'}:
        logger.info("[*] Cloud environment detected: Camera RTSP streaming is handled by Local Client Node. Skipping background readers.")
        return

    try:
        cameras = db.get_all_cameras(active_only=True)
        if MAX_ACTIVE_CAMERAS > 0 and len(cameras) > MAX_ACTIVE_CAMERAS:
            logger.warning(f"[!] {len(cameras)} active cameras configured but MAX_ACTIVE_CAMERAS={MAX_ACTIVE_CAMERAS}; extra cameras will not be processed.")
            cameras = cameras[:MAX_ACTIVE_CAMERAS]
        logger.info(f"[*] Reloading camera streams from DB ({len(cameras)} active)...")

        wanted_ids = {cam['camera_id'] for cam in cameras}
        with active_readers_lock:
            stale = [cid for cid in active_stream_readers if cid not in wanted_ids]
            stale_readers = [(cid, active_stream_readers.pop(cid)) for cid in stale]
        for cid, reader in stale_readers:
            try:
                reader.stop()
                logger.info(f"[*] Stopped stream reader for removed/disabled camera: {cid}")
            except Exception as e:
                logger.warning(f"Error stopping reader {cid}: {e}")

        for cam in cameras:
            try:
                get_or_create_reader(cam['camera_id'], _camera_source(cam))
            except Exception as e:
                logger.error(f"Failed to start reader for {cam['camera_id']}: {e}")
    except Exception as e:
        logger.error(f"Error reloading camera streams: {e}")

@app.route('/api/cameras', methods=['GET'])
def api_get_cameras():
    active_only = request.args.get('active_only', 'false').lower() == 'true'
    current_tenant = request.args.get('organization_id') or get_current_tenant_id()
    current_branch = request.args.get('branch_id') or get_current_branch_id()
    return jsonify(db.get_all_cameras(active_only=active_only, organization_id=current_tenant, branch_id=current_branch))

@app.route('/api/cameras', methods=['POST'])
def api_add_camera():
    data = request.get_json() or {}
    name = data.get('name')
    room_name = data.get('room_name')
    camera_type = data.get('camera_type', 'ip_camera')
    rtsp_url = data.get('rtsp_url', '')
    camera_id = data.get('camera_id') or f"{camera_type}_{int(time.time())}"
    
    if not name or not room_name:
        return jsonify({'error': 'Camera Name and Room/Location Name are required'}), 400
    conn_error = _validate_camera_connection(camera_type, rtsp_url)
    if conn_error:
        return jsonify({'error': conn_error}), 400
        
    current_tenant = get_current_tenant_id()
    current_branch = get_current_branch_id()
    cam_db_id = db.add_camera(
        camera_id=camera_id,
        name=name,
        room_name=room_name,
        camera_type=camera_type,
        rtsp_url=rtsp_url,
        organization_id=current_tenant,
        branch_id=current_branch
    )
    if cam_db_id:
        threading.Thread(target=reload_all_camera_streams, daemon=True).start()
        return jsonify({'success': True, 'id': cam_db_id, 'camera_id': camera_id, 'message': f'Camera {name} configured successfully'}), 201
    return jsonify({'error': f'Camera ID {camera_id} already exists or configuration failed'}), 409

@app.route('/api/cameras/<int:cam_id>', methods=['PUT'])
def api_update_camera(cam_id):
    data = request.get_json() or {}
    name = data.get('name')
    room_name = data.get('room_name')
    camera_type = data.get('camera_type', 'ip_camera')
    rtsp_url = data.get('rtsp_url', '')
    camera_id = data.get('camera_id', f'cam_{cam_id}')
    active = int(data.get('active', 1))

    if not name or not room_name:
        return jsonify({'error': 'Camera Name and Room/Location Name are required'}), 400
    conn_error = _validate_camera_connection(camera_type, rtsp_url)
    if conn_error:
        return jsonify({'error': conn_error}), 400
    
    current_tenant = get_current_tenant_id()
    success = db.update_camera(
        cam_db_id=cam_id,
        camera_id=camera_id,
        name=name,
        room_name=room_name,
        camera_type=camera_type,
        rtsp_url=rtsp_url,
        active=active,
        organization_id=current_tenant
    )
    if success:
        threading.Thread(target=reload_all_camera_streams, daemon=True).start()
        return jsonify({'success': True, 'message': f'Camera {name} updated successfully'})
    return jsonify({'error': 'Failed to update camera or access denied'}), 404

@app.route('/api/cameras/<int:cam_id>', methods=['DELETE'])
def api_delete_camera(cam_id):
    current_tenant = get_current_tenant_id()
    success = db.delete_camera(cam_id, organization_id=current_tenant)
    if success:
        threading.Thread(target=reload_all_camera_streams, daemon=True).start()
        return jsonify({'success': True, 'message': 'Camera deleted successfully'})
    return jsonify({'error': 'Failed to delete camera or access denied'}), 404

@app.route('/api/cameras/reload', methods=['POST'])
def api_reload_cameras():
    threading.Thread(target=reload_all_camera_streams, daemon=True).start()
    return jsonify({'success': True, 'message': 'All camera stream readers reloading in background'})

@app.route('/api/cameras/detect-webcams', methods=['GET'])
def api_detect_local_webcams():
    """Probe local USB/built-in camera devices connected to this machine.

    Devices already opened by an active stream reader cannot be re-opened on Windows
    (DirectShow holds them exclusively), so those are reported as 'in_use' instead.
    """
    host = (request.host or '').lower()
    if 'railway.app' in host or os.environ.get('RAILWAY_ENVIRONMENT'):
        return jsonify({
            'webcams': [],
            'probed_up_to': 0,
            'scope': 'local-machine-only',
            'message': 'Detect USB Webcams lists cameras attached to this Local Dashboard / EXE machine. It does not run on the cloud host and does not delete camera configs or attendance records.',
        })
    max_index = max(1, min(request.args.get('max_index', getattr(config, 'WEBCAM_PROBE_MAX_INDEX', 6), type=int), 20))

    in_use = {}
    with active_readers_lock:
        for cid, reader in active_stream_readers.items():
            if getattr(reader, 'is_webcam', False):
                in_use[int(reader.rtsp_url)] = cid
    configured = {}
    for cam in db.get_all_cameras():
        if cam['camera_type'] == 'webcam' and str(cam['rtsp_url']).isdigit():
            configured[int(cam['rtsp_url'])] = cam['name']

    available = []
    misses = 0
    for index in range(max_index):
        if index in in_use:
            available.append({
                'index': index,
                'name': f'Local Camera #{index}',
                'in_use': True,
                'configured_as': configured.get(index),
            })
            misses = 0
            continue
        try:
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            ok = cap.isOpened() and cap.read()[0]
            cap.release()
        except Exception:
            ok = False
        if ok:
            available.append({
                'index': index,
                'name': f'Local Camera #{index}',
                'in_use': False,
                'configured_as': configured.get(index),
            })
            misses = 0
        else:
            misses += 1
            if misses >= 2 and index >= 1:
                break  # stop after two consecutive empty slots

    return jsonify({'webcams': available, 'probed_up_to': max_index})


# ============================================
# LEGACY ROUTES (for backward compatibility)
# ============================================

@app.route('/get_staff_list')
def legacy_staff_list():
    return jsonify([_safe_user(u) for u in db.get_all_users(
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    )])


@app.route('/add_staff', methods=['POST', 'OPTIONS'])
def legacy_add_staff():
    data = request.get_json() or {}
    uid = db.add_user(
        name=data.get('name', ''), email=data.get('email', ''),
        role=data.get('role', 'staff'),
        department=data.get('department', ''), phone=data.get('phone', ''),
        notes=data.get('notes', ''), cnic=data.get('cnic', ''),
        position=data.get('position', ''), salary=float(data.get('salary', 0) or 0),
        join_date=data.get('join_date', ''),
        organization_id=get_current_tenant_id(),
        branch_id=get_current_branch_id(),
    )
    if uid is None:
        return jsonify({'success': False, 'message': 'Email already exists'}), 409
    return jsonify({'success': True, 'id': uid})


@app.route('/get_attendance_today')
def legacy_get_attendance_today():
    stats = db.get_attendance_statistics()
    users = db.get_all_users()
    present = [u for u in users if db.is_user_present_today(u['id'])]
    absent = [u for u in users if not db.is_user_present_today(u['id'])]
    return jsonify({
        'present': [_safe_user(u) for u in present],
        'absent': [_safe_user(u) for u in absent],
        'total': stats['total_users'],
        'present_count': stats['present_today'],
        'absent_count': stats['absent_today'],
    })


@app.route('/get_attendance_today_array')
def legacy_attendance_today_array():
    return jsonify(db.get_attendance_today())


@app.route('/get_pending_leaves')
def legacy_pending_leaves():
    return jsonify(db.get_leave_requests(status='pending'))


@app.route('/update_leave_status', methods=['POST'])
def legacy_update_leave():
    data = request.get_json() or {}
    leave_id = data.get('leave_id')
    if leave_id is None:
        return jsonify({'success': False, 'error': 'leave_id is required'}), 400
    try:
        updated = db.update_leave_status(leave_id, data.get('status', 'approved'), 'Admin')
    except ValueError as exc:
        if str(exc) == db.PAYROLL_LOCK_MESSAGE:
            return jsonify({'error': str(exc), 'reopen': '/api/payroll/reopen'}), 409
        return jsonify({'error': str(exc)}), 400
    if not updated:
        return jsonify({'success': False, 'message': 'This leave request overlaps another active leave or no longer exists. Please review the employee\'s leave dates.'}), 409
    return jsonify({'success': True})


@app.route('/get_detected_name/all')
def legacy_detected_name_all():
    with cache_lock:
        dets = list(LATEST_STREAM_DETECTIONS)
    return jsonify({
        'nvr': dets, 'dvr': dets, 'detections': dets,
        'name': dets[0]['name'] if dets else 'No Detection',
    })


@app.route('/get_detected_name/nvr')
def legacy_detected_name_nvr():
    return jsonify({'camera': 'nvr', 'detected_names': [], 'names': []})


@app.route('/get_detected_name/dvr')
def legacy_detected_name_dvr():
    return jsonify({'camera': 'dvr', 'detected_names': [], 'names': []})


@app.route('/get_staff_by_name')
def legacy_get_staff_by_name():
    name = request.args.get('name', '').strip().lower()
    users = db.get_all_users()
    match = next((u for u in users if u['name'].strip().lower() == name), None)
    if match:
        return jsonify({k: v for k, v in match.items() if k != 'password'})
    return jsonify({'error': 'Not found'}), 404


@app.route('/get_attendance_by_name')
def legacy_get_attendance_by_name():
    name = request.args.get('name', '').strip().lower()
    logs = db.get_attendance_logs(limit=500)
    matched = [l for l in logs if l.get('user_name', '').strip().lower() == name]
    return jsonify(matched)


@app.route('/video_feed/nvr_raw')
def legacy_nvr_raw():
    return video_stream('nvr_office')


@app.route('/video_feed/dvr_raw')
def legacy_dvr_raw():
    return video_stream('dvr_office')


if __name__ == '__main__':
    logger.info("\n" + "="*60)
    logger.info("Flask AI Attendance System - Starting")
    logger.info("="*60)
    
    try:
        if not db.init_db():
            raise SystemExit("Database initialization failed; check attendance.log")
        logger.info("✓ Database initialized")
        refresh_embedding_cache()
        
        is_cloud_env = bool(
            os.environ.get('RAILWAY_ENVIRONMENT')
            or os.environ.get('DISABLE_CAMERA_STREAMS', '').lower() in {'true', '1'}
        )
        
        if not is_cloud_env:
            logger.info("[*] Warming up AI models (InsightFace)...")
            fp.get_insightface_model()
            logger.info("✓ AI models loaded and warmed up successfully")
        else:
            logger.info("[*] Running on Railway Cloud: AI Face Recognition is handled by Local Client Node. Skipping Buffalo_l startup download.")
        
        import os
        if not is_cloud_env and (os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug):
            logger.info("[*] Proactively initializing background camera stream readers from DB (non-blocking)...")
            threading.Thread(target=reload_all_camera_streams, daemon=True).start()
        elif is_cloud_env:
            logger.info("[*] Running on Railway Cloud: CCTV streaming is handled by Local Client Node. Background RTSP readers disabled.")
    except Exception as e:
        logger.error(f"✗ Startup initialization failed: {e}")
    
    port = int(os.environ.get('PORT', '5000'))
    logger.info(f"Starting server on http://localhost:{port}")
    logger.info("="*60 + "\n")

    if getattr(sys, 'frozen', False):
        def _open_client_browser():
            time.sleep(1.2)
            try:
                webbrowser.open("http://localhost:5000")
            except Exception:
                pass
        threading.Thread(target=_open_client_browser, daemon=True).start()

    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug and not getattr(sys, 'frozen', False), host='0.0.0.0', port=port, use_reloader=False)