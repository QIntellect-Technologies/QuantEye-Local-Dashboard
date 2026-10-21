"""Tests for mobile leave requests and organization-scoped leave types."""
import os
import sys
import tempfile
import unittest

_LOCAL_DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_LOCAL_DASHBOARD_DIR)
sys.path.insert(0, _LOCAL_DASHBOARD_DIR)

for _module_name, _module in list(sys.modules.items()):
    _module_file = getattr(_module, "__file__", None)
    if not _module_file:
        continue
    _module_path = os.path.normcase(os.path.abspath(_module_file))
    if not _module_path.startswith(os.path.normcase(_PROJECT_ROOT + os.sep)):
        continue
    if not _module_path.startswith(os.path.normcase(_LOCAL_DASHBOARD_DIR + os.sep)):
        del sys.modules[_module_name]

_ORIGINAL_DB_PATH_ENV = os.environ.get("QINTELLECT_DB_PATH")
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="quanteye-mobile-leaves-")
_TEST_DB_PATH = os.path.join(_TEST_DB_DIR.name, "attendance.db")
os.environ["QINTELLECT_DB_PATH"] = _TEST_DB_PATH

import config as db_config
import app as app_module
import database as db

db_config.DB_PATH = _TEST_DB_PATH
db.DB_PATH = _TEST_DB_PATH

ORG_A = "ORG-MOB-A"
ORG_B = "ORG-MOB-B"
BR_A = "BR-MOB-A"
BR_B = "BR-MOB-B"


class MobileLeaveRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not db.init_db():
            raise RuntimeError("Could not initialize test database")
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()
        cls._seed()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = os.environ.get("QINTELLECT_DB_PATH", _TEST_DB_PATH)
        if _ORIGINAL_DB_PATH_ENV is None:
            os.environ.pop("QINTELLECT_DB_PATH", None)
        else:
            os.environ["QINTELLECT_DB_PATH"] = _ORIGINAL_DB_PATH_ENV
        _TEST_DB_DIR.cleanup()

    @classmethod
    def _seed(cls):
        # Create users
        cls.user_a_id = db.add_user(
            name="Alice Mobile", email="alice@mob-a.test",
            organization_id=ORG_A, branch_id=BR_A, role="Employee",
        )
        cls.user_b_id = db.add_user(
            name="Bob Mobile", email="bob@mob-b.test",
            organization_id=ORG_B, branch_id=BR_B, role="Employee",
        )

        # Create org-specific custom leave types
        db.add_leave_type("Alice Special Leave", "alice_special", 5, 1, "Alice only", organization_id=ORG_A, branch_id=BR_A)
        db.add_leave_type("Bob Study Leave", "bob_study", 10, 1, "Bob only", organization_id=ORG_B, branch_id=BR_B)

    def _login_mobile(self, user_id, org_id, branch_id, name="User"):
        with self.client.session_transaction() as sess:
            sess["mobile_employee_id"] = user_id
            sess["mobile_organization_id"] = org_id
            sess["mobile_branch_id"] = branch_id
            sess["mobile_employee_name"] = name

    def _logout_mobile(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def test_leave_types_requires_login(self):
        self._logout_mobile()
        res = self.client.get("/api/mobile/leave-types")
        self.assertEqual(res.status_code, 401)

    def test_leave_types_scoped_to_organization(self):
        # Alice logs in (Org A)
        self._login_mobile(self.user_a_id, ORG_A, BR_A, "Alice Mobile")
        res_a = self.client.get("/api/mobile/leave-types")
        self.assertEqual(res_a.status_code, 200)
        data_a = res_a.get_json()
        self.assertTrue(data_a["success"])
        codes_a = {lt["code"] for lt in data_a["leave_types"]}
        self.assertIn("alice_special", codes_a)
        self.assertNotIn("bob_study", codes_a)

        # Bob logs in (Org B)
        self._login_mobile(self.user_b_id, ORG_B, BR_B, "Bob Mobile")
        res_b = self.client.get("/api/mobile/leave-types")
        self.assertEqual(res_b.status_code, 200)
        data_b = res_b.get_json()
        self.assertTrue(data_b["success"])
        codes_b = {lt["code"] for lt in data_b["leave_types"]}
        self.assertIn("bob_study", codes_b)
        self.assertNotIn("alice_special", codes_b)

    def test_submit_leave_request_and_history(self):
        self._login_mobile(self.user_a_id, ORG_A, BR_A, "Alice Mobile")

        # Validation: end_date < start_date
        bad_res = self.client.post("/api/mobile/leave-requests", json={
            "leave_type": "alice_special",
            "start_date": "2026-11-10",
            "end_date": "2026-11-05",
            "reason": "Test",
        })
        self.assertEqual(bad_res.status_code, 400)

        # Valid submission
        good_res = self.client.post("/api/mobile/leave-requests", json={
            "leave_type": "alice_special",
            "start_date": "2026-11-10",
            "end_date": "2026-11-12",
            "leave_duration": "full",
            "reason": "Family vacation",
        })
        self.assertEqual(good_res.status_code, 200)
        res_data = good_res.get_json()
        self.assertTrue(res_data["success"])
        self.assertIn("request_id", res_data)

        # Fetch Alice's history
        history_res = self.client.get("/api/mobile/leave-requests")
        self.assertEqual(history_res.status_code, 200)
        history_data = history_res.get_json()
        self.assertTrue(history_data["success"])
        requests = history_data["requests"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["leave_type"], "alice_special")
        self.assertEqual(requests[0]["type_name"], "Alice Special Leave")
        self.assertEqual(requests[0]["status"], "pending")
        self.assertEqual(requests[0]["reason"], "Family vacation")

        # Bob's history should be empty (strict isolation!)
        self._login_mobile(self.user_b_id, ORG_B, BR_B, "Bob Mobile")
        bob_res = self.client.get("/api/mobile/leave-requests")
        self.assertEqual(bob_res.status_code, 200)
        bob_history = bob_res.get_json()["requests"]
        self.assertEqual(len(bob_history), 0)

    def test_module_entitlement_gating(self):
        # Configure ORG_A cache with attendance & mobile only (NO leave, NO overtime)
        import time
        app_module._MOBILE_COMPANY_CACHE[ORG_A] = (True, "Org A", ["attendance", "mobile"], time.time())

        self._login_mobile(self.user_a_id, ORG_A, BR_A, "Alice Mobile")

        # Today endpoint returns module entitlement flags
        today_res = self.client.get("/api/mobile/today")
        self.assertEqual(today_res.status_code, 200)
        mods = today_res.get_json().get("modules", {})
        self.assertTrue(mods.get("attendance"))
        self.assertTrue(mods.get("mobile"))
        self.assertFalse(mods.get("leave"))
        self.assertFalse(mods.get("overtime"))

        # Gated leave endpoints return 403 Forbidden
        lt_res = self.client.get("/api/mobile/leave-types")
        self.assertEqual(lt_res.status_code, 403)
        self.assertIn("not licensed", lt_res.get_json().get("message", ""))

        lr_res = self.client.get("/api/mobile/leave-requests")
        self.assertEqual(lr_res.status_code, 403)

        post_res = self.client.post("/api/mobile/leave-requests", json={
            "leave_type": "casual",
            "start_date": "2026-11-10",
            "end_date": "2026-11-12",
        })
        self.assertEqual(post_res.status_code, 403)

        # Clear cache for other tests
        app_module._MOBILE_COMPANY_CACHE.pop(ORG_A, None)


if __name__ == "__main__":
    unittest.main()
