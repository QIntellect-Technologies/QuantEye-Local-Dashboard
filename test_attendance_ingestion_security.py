import os
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import uuid
from unittest.mock import patch

_ORIGINAL_DB_PATH_ENV = os.environ.get("QINTELLECT_DB_PATH")
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="quanteye-ingestion-tests-")
_TEST_DB_PATH = os.path.join(_TEST_DB_DIR.name, "attendance.db")
os.environ["QINTELLECT_DB_PATH"] = _TEST_DB_PATH

import config as db_config
import app as app_module
import database as db

_ORIGINAL_CONFIG_DB_PATH = db_config.DB_PATH
_ORIGINAL_DATABASE_DB_PATH = db.DB_PATH
db_config.DB_PATH = _TEST_DB_PATH
db.DB_PATH = _TEST_DB_PATH


class AttendanceIngestionSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db_path = _ORIGINAL_DATABASE_DB_PATH
        cls.original_config_db_path = _ORIGINAL_CONFIG_DB_PATH
        if not db.init_db():
            raise RuntimeError("Could not initialize isolated ingestion test database")
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.executemany(
                "INSERT INTO users (id, name, organization_id, branch_id, active) "
                "VALUES (?, ?, ?, ?, 1)",
                [
                    (101, "Alice Alpha", "ORG-ALPHA", "BR-ALPHA-1"),
                    (102, "Aaron Alpha", "ORG-ALPHA", "BR-ALPHA-2"),
                    (201, "Bob Beta", "ORG-BETA", "BR-BETA-1"),
                ],
            )
        app_module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls.original_db_path
        db_config.DB_PATH = cls.original_config_db_path
        if _ORIGINAL_DB_PATH_ENV is None:
            os.environ.pop("QINTELLECT_DB_PATH", None)
        else:
            os.environ["QINTELLECT_DB_PATH"] = _ORIGINAL_DB_PATH_ENV
        _TEST_DB_DIR.cleanup()

    def setUp(self):
        self.client = app_module.app.test_client()
        self.config_patch = patch.object(
            app_module,
            "load_integration_config",
            return_value={
                "linked": True,
                "organization_id": "ORG-ALPHA",
                "branch_id": "BR-ALPHA-1",
                "branch_status": "active",
                "licensed_modules": ["attendance"],
            },
        )
        self.config_patch.start()
        self.env_patch = patch.dict(
            os.environ, {"LOCAL_DASHBOARD_API_KEY": "unit-test-integration-key-0123456789"}
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.config_patch.stop()

    def test_dashboard_requires_a_server_session_and_logout_removes_it(self):
        with self.client.session_transaction() as session:
            session.clear()
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'<body class="auth-required">', page.data)
        self.assertIn(b'background:#0b1220 !important', page.data)

        with self.client.session_transaction() as session:
            session['admin_unlocked'] = True
            session['client_user'] = 'first-org@example.test'
            session['client_org'] = 'First Organization'
            session['client_branch'] = 'First Branch'
            session['licensed_modules'] = ['attendance', 'payroll']
        authenticated_page = self.client.get('/')
        self.assertIn(b'First Organization', authenticated_page.data)
        self.assertNotIn(b'<body class="auth-required">', authenticated_page.data)
        self.assertIn(
            b'.sidebar-menu .menu-item[data-module="leave"] { display: none !important; }',
            authenticated_page.data,
        )
        self.assertNotIn(
            b'.sidebar-menu .menu-item[data-module="payroll"] { display: none !important; }',
            authenticated_page.data,
        )
        self.assertIn(
            b'window.clientSessionModules = ["attendance", "payroll"]',
            authenticated_page.data,
        )

        logout = self.client.post('/api/logout')
        self.assertEqual(logout.status_code, 200)
        logged_out_page = self.client.get('/')
        self.assertIn(b'<body class="auth-required">', logged_out_page.data)
        self.assertNotIn(b'First Organization', logged_out_page.data)

    @staticmethod
    def _payload(**overrides):
        payload = {
            "event_id": str(uuid.uuid4()),
            "organization_id": "ORG-ALPHA",
            "branch_id": "BR-ALPHA-1",
            "user_id": 101,
            "name": "Alice Alpha",
            "confidence": 0.98,
            "camera_id": "cam-alpha",
            "camera_name": "Alpha Entrance",
            "timestamp": "2026-10-10T12:00:00",
        }
        payload.update(overrides)
        return payload

    def _post(self, path, payload, authenticated=True, forged_client_header=False):
        headers = {}
        if authenticated:
            headers["X-Integration-Key"] = "unit-test-integration-key-0123456789"
        if forged_client_header:
            headers["X-Client-Auth"] = "true"
        return self.client.post(path, json=payload, headers=headers)

    def test_integration_camera_endpoint_requires_service_key(self):
        missing_key = self.client.get(
            "/api/integration/cameras?organization_id=ORG-ALPHA&branch_id=BR-ALPHA-1"
        )
        invalid_key = self.client.get(
            "/api/integration/cameras?organization_id=ORG-ALPHA&branch_id=BR-ALPHA-1",
            headers={"X-Integration-Key": "incorrect"},
        )
        self.assertEqual(missing_key.status_code, 401)
        self.assertEqual(invalid_key.status_code, 401)

    @patch.object(db, "get_all_cameras")
    def test_integration_camera_endpoint_scopes_to_requested_tenant(self, get_all_cameras):
        get_all_cameras.return_value = [{"camera_id": "camera-a", "rtsp_url": "rtsp://example.test/a"}]
        response = self.client.get(
            "/api/integration/cameras?organization_id=ORG-ALPHA&branch_id=BR-ALPHA-2",
            headers={"X-Integration-Key": "unit-test-integration-key-0123456789"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["cameras"], get_all_cameras.return_value)
        get_all_cameras.assert_called_once_with(
            active_only=True,
            organization_id="ORG-ALPHA",
            branch_id="BR-ALPHA-2",
        )

    def test_integration_employee_endpoint_requires_service_key_and_scopes_branch(self):
        endpoint = "/api/integration/employees?organization_id=ORG-ALPHA&branch_id=BR-ALPHA-2"
        with patch.dict(os.environ, {"LOCAL_DASHBOARD_API_KEY": "unit-test-integration-key-0123456789"}):
            unauthorized = self.client.get(endpoint)
            with patch.object(db, "get_all_users", return_value=[{
                "id": 102,
                "name": "Aaron Alpha",
                "notes": "EMP-102",
            }]) as get_all_users:
                response = self.client.get(
                    endpoint,
                    headers={"X-Integration-Key": "unit-test-integration-key-0123456789"},
                )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["employees"][0]["employee_code"], "EMP-102")
        get_all_users.assert_called_once_with(
            organization_id="ORG-ALPHA",
            branch_id="BR-ALPHA-2",
        )

    def test_integration_shift_endpoint_uses_api_key_without_browser_session(self):
        endpoint = "/api/integration/shifts?organization_id=ORG-ALPHA&branch_id=BR-ALPHA-2"
        with self.client.session_transaction() as session:
            session.clear()
        missing_key = self.client.get(endpoint)
        with patch.object(db, "get_all_shifts", return_value=[{
            "id": 1,
            "name": "Morning Shift",
            "start_time": "23:04",
            "end_time": "23:07",
            "grace_period_minutes": 2,
        }]) as get_all_shifts, patch.object(db, "get_all_users", return_value=[{
            "id": 102,
            "shift_id": 1,
            "checkin_cameras": "Room",
            "checkout_cameras": "AI TEAM ROOM",
        }]):
            response = self.client.get(
                endpoint,
                headers={"X-Integration-Key": "unit-test-integration-key-0123456789"},
            )
        self.assertEqual(missing_key.status_code, 401)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["organization_id"], "ORG-ALPHA")
        self.assertEqual(payload["branch_id"], "BR-ALPHA-2")
        self.assertEqual(payload["shifts"][0]["start_time"], "23:04")
        self.assertEqual(payload["assignments"][0]["user_id"], 102)
        self.assertEqual(payload["assignments"][0]["shift_id"], 1)
        self.assertEqual(payload["assignments"][0]["checkin_cameras"], "Room")
        self.assertEqual(payload["assignments"][0]["checkout_cameras"], "AI TEAM ROOM")
        get_all_shifts.assert_called_once_with(
            active_only=True,
            organization_id="ORG-ALPHA",
            branch_id="BR-ALPHA-2",
        )

    def test_integration_embedding_sync_requires_service_key_and_exact_branch(self):
        endpoint = "/api/integration/employees/102/embeddings?organization_id=ORG-ALPHA&branch_id=BR-ALPHA-2"
        payload = {"embeddings": [[0.25] * 512], "source": "node-test"}
        with patch.dict(os.environ, {"LOCAL_DASHBOARD_API_KEY": "unit-test-integration-key-0123456789"}):
            unauthorized = self.client.post(endpoint, json=payload)
            wrong_branch = self.client.post(
                endpoint.replace("BR-ALPHA-2", "BR-ALPHA-1"),
                json=payload,
                headers={"X-Integration-Key": "unit-test-integration-key-0123456789"},
            )
            with patch.object(app_module, "refresh_embedding_cache"):
                response = self.client.post(
                    endpoint,
                    json=payload,
                    headers={"X-Integration-Key": "unit-test-integration-key-0123456789"},
                )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(wrong_branch.status_code, 404)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["is_trained"])
        with sqlite3.connect(db.DB_PATH) as conn:
            count, trained = conn.execute(
                "SELECT (SELECT COUNT(*) FROM embeddings WHERE user_id = 102 AND organization_id = ? AND branch_id = ?), "
                "face_trained FROM users WHERE id = 102",
                ("ORG-ALPHA", "BR-ALPHA-2"),
            ).fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(trained, 1)

    def test_integration_embedding_download_requires_service_key_and_exact_branch(self):
        endpoint = "/api/integration/employees/102/embeddings?organization_id=ORG-ALPHA&branch_id=BR-ALPHA-2"
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "DELETE FROM embeddings WHERE user_id = 102 AND organization_id = ? AND branch_id = ?",
                ("ORG-ALPHA", "BR-ALPHA-2"),
            )
            conn.execute(
                "INSERT INTO embeddings (user_id, embedding, organization_id, branch_id) "
                "VALUES (?, ?, ?, ?)",
                (102, json.dumps([0.25] * 512).encode("utf-8"), "ORG-ALPHA", "BR-ALPHA-2"),
            )

        with patch.dict(os.environ, {"LOCAL_DASHBOARD_API_KEY": "unit-test-integration-key-0123456789"}):
            unauthorized = self.client.get(endpoint)
            wrong_branch = self.client.get(
                endpoint.replace("BR-ALPHA-2", "BR-ALPHA-1"),
                headers={"X-Integration-Key": "unit-test-integration-key-0123456789"},
            )
            response = self.client.get(
                endpoint,
                headers={"X-Integration-Key": "unit-test-integration-key-0123456789"},
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(wrong_branch.status_code, 404)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["is_trained"])
        self.assertEqual(payload["user_id"], 102)
        self.assertEqual(payload["organization_id"], "ORG-ALPHA")
        self.assertEqual(payload["branch_id"], "BR-ALPHA-2")
        self.assertEqual(payload["embeddings"], [[0.25] * 512])

    def test_shift_staff_counts_are_scoped_to_current_organization_and_branch(self):
        with self.client.session_transaction() as session:
            session["admin_unlocked"] = True
            session["organization_id"] = "ORG-ALPHA"
            session["branch_id"] = "BR-ALPHA-1"

        with patch.object(
            app_module,
            "load_integration_config",
            return_value={
                "linked": True,
                "organization_id": "ORG-ALPHA",
                "branch_id": "BR-ALPHA-1",
                "branch_status": "active",
                "licensed_modules": ["attendance", "shifts"],
            },
        ):
            response = self.client.get("/api/shifts")

        self.assertEqual(response.status_code, 200)
        shifts = response.get_json()["shifts"]
        default_shift = next(shift for shift in shifts if shift["id"] == 1)
        self.assertEqual(default_shift["user_count"], 1)

    def test_unauthenticated_and_forged_client_headers_are_rejected(self):
        payload = self._payload()
        self.assertEqual(
            self._post("/api/sync/attendance-event", payload, authenticated=False).status_code,
            401,
        )
        self.assertEqual(
            self._post(
                "/api/attendance/edge-mark",
                payload,
                authenticated=False,
                forged_client_header=True,
            ).status_code,
            401,
        )

    def test_edge_mark_uses_payload_tenant_not_integration_file(self):
        with patch.object(
            app_module,
            "load_integration_config",
            return_value={
                "linked": True,
                "organization_id": "ORG-311376",
                "branch_id": "BR-ORG-311376",
                "branch_status": "active",
                "licensed_modules": ["payroll"],
            },
        ):
            payload = self._payload()
            accepted = self._post("/api/attendance/edge-mark", payload)
        self.assertEqual(accepted.status_code, 201)
        self.assertTrue(accepted.get_json()["success"])

    def test_payload_tenant_branch_and_employee_ownership_are_enforced(self):
        self.assertEqual(
            self._post(
                "/api/attendance/edge-mark",
                self._payload(organization_id="ORG-BETA", branch_id="BR-BETA-1"),
            ).status_code,
            403,
        )
        self.assertEqual(
            self._post(
                "/api/attendance/edge-mark",
                self._payload(branch_id="BR-ALPHA-2"),
            ).status_code,
            403,
        )
        self.assertEqual(
            self._post(
                "/api/attendance/edge-mark",
                self._payload(user_id=102, name="Aaron Alpha"),
            ).status_code,
            403,
        )
        self.assertEqual(
            self._post(
                "/api/attendance/edge-mark",
                self._payload(user_id=201, name="Bob Beta"),
            ).status_code,
            403,
        )

    def test_edge_mark_valid_event_is_processed_once_and_replay_conflicts_are_rejected(self):
        payload = self._payload()
        with patch.object(db, "log_attendance", return_value=(True, "Attendance recorded")) as attendance:
            accepted = self._post("/api/attendance/edge-mark", payload)
            self.assertEqual(accepted.status_code, 201)
            self.assertTrue(accepted.get_json()["success"])

            replay = self._post("/api/attendance/edge-mark", payload)
            self.assertEqual(replay.status_code, 201)
            self.assertEqual(attendance.call_count, 1)

            changed = dict(payload, camera_id="another-camera")
            conflict = self._post("/api/attendance/edge-mark", changed)
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(attendance.call_count, 1)

    def test_edge_mark_uses_edge_timestamp_and_skips_local_shift_clock(self):
        payload = self._payload(timestamp="2026-10-14T23:26:07")
        with patch.object(db, "check_user_shift_allowed", return_value=(False, "outside", None)):
            with patch.object(db, "log_attendance", return_value=(True, "Attendance recorded")) as log:
                accepted = self._post("/api/attendance/edge-mark", payload)
        self.assertEqual(accepted.status_code, 201)
        kwargs = log.call_args.kwargs
        self.assertTrue(kwargs["bypass_shift_check"])
        self.assertEqual(kwargs["attendance_time"].strftime("%Y-%m-%d %H:%M:%S"), "2026-10-14 23:26:07")

    def test_edge_mark_failed_receipt_can_be_retried_after_local_accepts(self):
        payload = self._payload()
        with patch.object(db, "log_attendance", return_value=(False, "outside shift")):
            rejected = self._post("/api/attendance/edge-mark", payload)
        self.assertEqual(rejected.status_code, 400)
        with patch.object(db, "log_attendance", return_value=(True, "Attendance recorded")):
            retried = self._post("/api/attendance/edge-mark", payload)
        self.assertEqual(retried.status_code, 201)
        self.assertTrue(retried.get_json()["success"])

    def test_failed_ingestion_receipt_is_reclaimed_for_the_same_event(self):
        event_id = str(uuid.uuid4())
        payload_hash = "same-payload-hash"
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO attendance_ingestion_receipts "
                "(event_id, payload_hash, response_json, response_status) "
                "VALUES (?, ?, ?, ?)",
                (event_id, payload_hash, json.dumps({"success": False}), 400),
            )
        receipt = db.begin_attendance_ingestion(event_id, payload_hash)
        self.assertEqual(receipt["state"], "new")

    def test_sync_valid_event_is_authenticated_and_idempotent(self):
        payload = self._payload(event_type="CHECKIN")
        with patch.object(db, "log_attendance", return_value=(True, "Attendance recorded")) as log:
            accepted = self._post("/api/sync/attendance-event", payload)
            self.assertEqual(accepted.status_code, 200)
            self.assertTrue(accepted.get_json()["attendance_logged"])

            replay = self._post("/api/sync/attendance-event", payload)
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(log.call_count, 1)

    def test_edge_sync_state_is_authenticated_and_branch_scoped(self):
        for user_id in (101, 102, 201):
            self.assertTrue(db.mark_user_absent_today(user_id))

        endpoint = "/api/integration/edge-sync-state?organization_id=ORG-ALPHA&branch_id=BR-ALPHA-1"
        missing_key = self.client.get(endpoint)
        self.assertEqual(missing_key.status_code, 401)

        response = self.client.get(
            endpoint,
            headers={"X-Integration-Key": "unit-test-integration-key-0123456789"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["organization_id"], "ORG-ALPHA")
        self.assertEqual(data["branch_id"], "BR-ALPHA-1")
        self.assertEqual(data["manual_absent_user_ids"], [101])

        missing_branch = self.client.get(
            "/api/integration/edge-sync-state?organization_id=ORG-ALPHA",
            headers={"X-Integration-Key": "unit-test-integration-key-0123456789"},
        )
        self.assertEqual(missing_branch.status_code, 400)


class Layer1IdentityTests(unittest.TestCase):
    """Browser tenant is the Flask session. The integration file is ignored."""

    HELLO_WORLD_FILE = {
        "linked": True,
        "organization_id": "ORG-311376",
        "organization_name": "Hello World",
        "branch_id": "BR-ORG-311376",
        "branch_status": "active",
        "licensed_modules": ["attendance", "shifts", "payroll"],
    }

    def setUp(self):
        self.client_a = app_module.app.test_client()
        self.client_b = app_module.app.test_client()
        self.config_patch = patch.object(
            app_module, "load_integration_config", return_value=dict(self.HELLO_WORLD_FILE)
        )
        self.save_patch = patch.object(
            app_module, "save_integration_config", side_effect=AssertionError("client login must not write config_integration.json")
        )
        self.config_patch.start()
        self.save_patch.start()

    def tearDown(self):
        self.save_patch.stop()
        self.config_patch.stop()

    def _authenticate_payload(self, org_id, branch_id, modules, org_name, branch_name):
        return {
            "success": True,
            "organization_id": org_id,
            "organization_name": org_name,
            "branch_id": branch_id,
            "branch_name": branch_name,
            "modules": modules,
        }

    def test_client_login_binds_session_only_and_ignores_hello_world_file(self):
        auth = self._authenticate_payload(
            "ORG-A", "BR-A", ["attendance", "shifts"], "Org A", "Branch A"
        )
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(auth).encode("utf-8")
            login = self.client_a.post(
                "/api/client/login",
                json={"username": "a@example.test", "password": "ClientPortalPass-2026"},
            )
        self.assertEqual(login.status_code, 200)
        body = login.get_json()
        self.assertEqual(body["organization_id"], "ORG-A")
        self.assertFalse(body["admin_unlocked"])
        self.assertTrue(body["is_client_session"])

        status = self.client_a.get("/api/admin/status").get_json()
        self.assertTrue(status["authenticated"])
        self.assertTrue(status["client_authenticated"])
        self.assertFalse(status["admin_unlocked"])
        self.assertEqual(status["organization_id"], "ORG-A")
        self.assertEqual(status["branch_id"], "BR-A")
        self.assertEqual(status["licensed_modules"], ["attendance", "shifts"])

        page = self.client_a.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Org A", page.data)
        company_home = self.client_a.get("/c/org-a-orga/")
        self.assertEqual(company_home.status_code, 200)
        self.assertIn(b"window.companyPortalSlug = \"org-a-orga\"", company_home.data)
        self.assertNotIn(b"Hello World", page.data)
        self.assertIn(b'window.clientSessionModules = ["attendance", "shifts"]', page.data)

        staff = self.client_a.get("/api/staff")
        self.assertEqual(staff.status_code, 200)
        for row in staff.get_json():
            self.assertEqual(row.get("organization_id"), "ORG-A")

    def test_unique_company_url_renders_login_html_when_logged_out(self):
        guest = app_module.app.test_client()
        for path in (
            "/c/qintellect-technologies-org415278/",
            "/c/qintellect-technologies-org415278",
        ):
            page = guest.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn("text/html", page.content_type)
            self.assertNotIn(b"Unlock the dashboard first", page.data)
            self.assertIn(b"window.companyPortalSlug", page.data)

    def test_two_client_sessions_stay_isolated_after_refresh(self):
        auth_a = self._authenticate_payload(
            "ORG-A", "BR-A", ["attendance", "shifts"], "Org A", "Branch A"
        )
        auth_b = self._authenticate_payload(
            "ORG-B", "BR-B", ["attendance", "shifts", "payroll"], "Org B", "Branch B"
        )

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(auth_a).encode("utf-8")
            self.assertEqual(
                self.client_a.post(
                    "/api/client/login",
                    json={"username": "a@example.test", "password": "ClientPortalPass-2026"},
                ).status_code,
                200,
            )
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(auth_b).encode("utf-8")
            self.assertEqual(
                self.client_b.post(
                    "/api/client/login",
                    json={"username": "b@example.test", "password": "ClientPortalPass-2026"},
                ).status_code,
                200,
            )

        for _ in range(3):
            status_a = self.client_a.get("/api/admin/status").get_json()
            status_b = self.client_b.get("/api/admin/status").get_json()
            self.assertEqual(status_a["organization_id"], "ORG-A")
            self.assertEqual(status_b["organization_id"], "ORG-B")
            self.assertEqual(status_a["licensed_modules"], ["attendance", "shifts"])
            self.assertEqual(status_b["licensed_modules"], ["attendance", "shifts", "payroll"])
            page_a = self.client_a.get("/")
            page_b = self.client_b.get("/")
            self.assertIn(b"Org A", page_a.data)
            self.assertIn(b"Org B", page_b.data)
            self.assertNotIn(b"Org B", page_a.data)
            self.assertNotIn(b"Org A", page_b.data)

    def test_logout_clears_only_the_browser_session(self):
        auth = self._authenticate_payload(
            "ORG-A", "BR-A", ["attendance"], "Org A", "Branch A"
        )
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(auth).encode("utf-8")
            self.client_a.post(
                "/api/client/login",
                json={"username": "a@example.test", "password": "ClientPortalPass-2026"},
            )
        logout = self.client_a.post("/api/logout")
        self.assertEqual(logout.status_code, 200)
        status = self.client_a.get("/api/admin/status").get_json()
        self.assertFalse(status["authenticated"])
        self.assertIsNone(status["organization_id"])
        page = self.client_a.get("/")
        self.assertIn(b'<body class="auth-required">', page.data)
        self.assertNotIn(b"Org A", page.data)

    def test_company_portal_slug_mismatch_is_forbidden(self):
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = urllib.error.HTTPError(
                "https://main.example/api/client/authenticate",
                403,
                "Forbidden",
                hdrs=None,
                fp=None,
            )
            denied = self.client_a.post(
                "/api/client/login",
                json={
                    "username": "a@example.test",
                    "password": "ClientPortalPass-2026",
                    "portal_slug": "other-company",
                },
            )
        self.assertEqual(denied.status_code, 403)


class Layer2ModuleFirstPaintTests(unittest.TestCase):
    """First HTML matches the session modules. The integration file cannot grant all modules."""

    def setUp(self):
        self.client = app_module.app.test_client()
        self.config_patch = patch.object(
            app_module,
            "load_integration_config",
            return_value={
                "linked": False,
                "organization_id": "ORG-311376",
                "licensed_modules": ["attendance", "shifts", "leave", "overtime", "payroll"],
            },
        )
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()

    def _login_session(self, modules):
        with self.client.session_transaction() as session:
            session.clear()
            session["client_user"] = "layer2@example.test"
            session["client_org"] = "Paint Org"
            session["organization_id"] = "ORG-PAINT"
            session["branch_id"] = "BR-PAINT"
            session["licensed_modules"] = modules

    def test_two_module_session_omits_unlicensed_sidebar_on_first_paint(self):
        self._login_session(["attendance", "shifts"])
        for _ in range(10):
            page = self.client.get("/")
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn('id="menuItemShifts"', html)
            self.assertIn('id="menuItemStaff"', html)
            self.assertNotIn('id="menuItemPayroll"', html)
            self.assertNotIn('id="menuItemLeaves"', html)
            self.assertNotIn('id="menuItemOvertime"', html)
            self.assertNotIn('id="payroll-rules-tab"', html)
            self.assertNotIn('id="payroll-calendar-tab"', html)
            self.assertNotIn('id="salary-config-tab"', html)
            self.assertNotIn("Attendance-Based Deduction Rules", html)
            self.assertNotIn("Employee Salary Structure &amp; Compensation Packages", html)
            self.assertIn("payroll: false", html)
            self.assertIn("leave: false", html)
            self.assertIn("overtime: false", html)
            self.assertNotIn("fetch('/api/integration/status')", html)

    def test_three_module_session_keeps_licensed_sidebar_items(self):
        self._login_session(["attendance", "shifts", "payroll"])
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="menuItemPayroll"', html)
        self.assertIn('id="menuItemShifts"', html)
        self.assertNotIn('id="menuItemLeaves"', html)
        self.assertNotIn('id="menuItemOvertime"', html)
        self.assertIn("payroll: true", html)
        self.assertIn('id="payroll-rules-tab"', html)
        self.assertIn("Payroll Rules", html)
        self.assertNotIn("Half Leave Setup", html)
        self.assertNotIn("Maximum Overtime", html)
        self.assertNotIn("Overtime Day Premiums", html)

    def test_payroll_plus_leave_and_overtime_shows_connected_settings(self):
        self._login_session(["attendance", "shifts", "payroll", "leave", "overtime"])
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Half Leave Setup", html)
        self.assertIn("Maximum Overtime", html)
        self.assertIn("Overtime Day Premiums", html)
        self.assertIn('id="menuItemLeaves"', html)
        self.assertIn('id="menuItemOvertime"', html)

    def test_unlicensed_payroll_url_and_api_are_forbidden(self):
        self._login_session(["attendance", "shifts"])
        payroll_page = self.client.get("/payroll")
        self.assertEqual(payroll_page.status_code, 403)
        self.assertEqual(payroll_page.get_json()["module"], "payroll")
        tab = self.client.get("/?tab=payroll-view")
        self.assertEqual(tab.status_code, 403)
        api = self.client.get("/api/payroll")
        self.assertEqual(api.status_code, 403)
        leaves = self.client.get("/api/leaves")
        self.assertEqual(leaves.status_code, 403)
        overtime = self.client.get("/api/overtime")
        self.assertEqual(overtime.status_code, 403)
        self.assertEqual(self.client.get("/api/settings/payroll").status_code, 403)
        self.assertEqual(self.client.get("/api/salary").status_code, 403)
        self.assertEqual(self.client.get("/api/payroll-settings/scoped-rules/half-leave").status_code, 403)
        self.assertEqual(self.client.get("/api/settings/payroll/overtime-rules").status_code, 403)
        branch = self.client.get("/api/settings/branch")
        self.assertEqual(branch.status_code, 200)

    def test_payroll_settings_api_allows_payroll_but_not_unlicensed_leave_or_overtime(self):
        self._login_session(["attendance", "shifts", "payroll"])
        self.assertEqual(self.client.get("/api/settings/payroll").status_code, 200)
        self.assertEqual(self.client.get("/api/settings/payroll/overtime-rules").status_code, 403)
        self.assertEqual(self.client.get("/api/payroll-settings/scoped-rules/half-leave").status_code, 403)
        self.assertEqual(self.client.get("/api/payroll-settings/scoped-rules/overtime-premium").status_code, 403)

    def test_cloud_staff_enrollment_page_omits_video_training_console(self):
        self._login_session(["attendance", "shifts"])
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Register Staff", html)
        self.assertNotIn("Upload Enrollment Video", html)
        self.assertNotIn("AI Biometric Pipeline Monitor", html)
        self.assertNotIn("Extract & Train Biometrics", html)
        self.assertNotIn("Clear Console", html)
        self.assertNotIn("Associated Profile User ID", html)
        self.assertNotIn("Click to choose a video file", html)

    def test_staff_enrollment_requires_handover_key_then_lock_hides_it(self):
        self._login_session(["attendance", "shifts"])
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Staff Enrollment Lock", html)
        self.assertIn('id="enrollmentKeyInput"', html)
        denied = self.client.post("/api/users", json={"name": "New Hire"})
        self.assertEqual(denied.status_code, 403)
        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps({"success": True}).encode("utf-8")
            unlocked = self.client.post("/api/enrollment/unlock", json={"enrollment_key": "ENR-TESTKEY"})
        self.assertEqual(unlocked.status_code, 200)
        self.assertTrue(unlocked.get_json()["enrollment_enabled"])
        locked = self.client.post("/api/enrollment/lock")
        self.assertEqual(locked.status_code, 200)
        denied_again = self.client.post("/api/users", json={"name": "New Hire"})
        self.assertEqual(denied_again.status_code, 403)

    def test_unlinked_file_does_not_grant_all_modules_to_client_session(self):
        self._login_session(["attendance"])
        self.assertEqual(self.client.get("/api/payroll").status_code, 403)
        self.assertEqual(self.client.get("/api/shifts").status_code, 403)
        staff = self.client.get("/api/staff")
        self.assertEqual(staff.status_code, 200)


class ShiftTenantIsolationTests(unittest.TestCase):
    def test_other_org_does_not_see_evening_or_shared_morning(self):
        evening_id = db.add_shift(
            "Evening Shift",
            "23:07",
            "23:10",
            2,
            organization_id="ORG-ALPHA",
            branch_id="BR-ALPHA-1",
        )
        self.assertIsNotNone(evening_id)
        db.assign_user_shift(101, evening_id)

        alpha = db.get_all_shifts(organization_id="ORG-ALPHA", branch_id="BR-ALPHA-1")
        beta = db.get_all_shifts(organization_id="ORG-BETA", branch_id="BR-BETA-1")
        gs = db.get_all_shifts(organization_id="ORG-984600", branch_id="BR-GS")

        alpha_names = {row["name"] for row in alpha}
        beta_names = {row["name"] for row in beta}
        gs_names = {row["name"] for row in gs}
        self.assertIn("Evening Shift", alpha_names)
        self.assertNotIn("Evening Shift", beta_names)
        self.assertNotIn("Evening Shift", gs_names)
        self.assertTrue(any(row["start_time"] == "09:00" for row in gs))
        self.assertFalse(any(row["id"] == 1 for row in beta))
        self.assertFalse(any(row["id"] == 1 for row in gs))
        self.assertFalse(any(row["id"] == evening_id for row in beta))
        self.assertFalse(any(row["id"] == evening_id for row in gs))

    def test_deleted_camera_id_is_removed_from_staff_assignments(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, name, organization_id, branch_id, active, checkin_cameras, checkout_cameras
                ) VALUES (901, 'Salman FnK', 'ORG-FORK', 'BR-FORK', 1, 'cam_1669', 'cam_1669')
                """
            )
            conn.commit()
        db.add_camera(
            "cam_1669", "Old NVR", "AI Room", "nvr", "rtsp://old",
            organization_id="ORG-FORK", branch_id="BR-FORK",
        )
        db.add_camera(
            "cam_6858", "Local WebCam", "QI-Room", "webcam", "0",
            organization_id="ORG-FORK", branch_id="BR-FORK",
        )
        with sqlite3.connect(db.DB_PATH) as conn:
            old_row = conn.execute(
                "SELECT id FROM cameras WHERE camera_id = 'cam_1669' AND organization_id = 'ORG-FORK'"
            ).fetchone()
        self.assertTrue(db.delete_camera(old_row[0], organization_id="ORG-FORK"))
        users = db.get_all_users(organization_id="ORG-FORK", branch_id="BR-FORK")
        salman = next(user for user in users if user["id"] == 901)
        self.assertEqual(salman["checkin_cameras"], "all")
        self.assertEqual(salman["checkout_cameras"], "all")
        db.add_camera(
            "cam_5052", "NVR", "AI Room", "nvr", "rtsp://new",
            organization_id="ORG-FORK", branch_id="BR-FORK",
        )
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "UPDATE users SET checkin_cameras = 'cam_6858', checkout_cameras = 'cam_5052' WHERE id = 901"
            )
            conn.commit()
        users = db.get_all_users(organization_id="ORG-FORK", branch_id="BR-FORK")
        salman = next(user for user in users if user["id"] == 901)
        self.assertEqual(salman["checkin_cameras"], "cam_6858")
        self.assertEqual(salman["checkout_cameras"], "cam_5052")


if __name__ == "__main__":
    unittest.main()
