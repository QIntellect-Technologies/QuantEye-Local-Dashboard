import os
import sqlite3
import tempfile
import unittest
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
        with patch.object(
            app_module,
            "process_user_attendance_event",
            return_value=("CHECKIN", "Attendance recorded"),
        ) as attendance:
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

        missing_key = self.client.get("/api/integration/edge-sync-state")
        self.assertEqual(missing_key.status_code, 401)

        response = self.client.get(
            "/api/integration/edge-sync-state",
            headers={"X-Integration-Key": "unit-test-integration-key-0123456789"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["organization_id"], "ORG-ALPHA")
        self.assertEqual(data["branch_id"], "BR-ALPHA-1")
        self.assertEqual(data["manual_absent_user_ids"], [101])


if __name__ == "__main__":
    unittest.main()
