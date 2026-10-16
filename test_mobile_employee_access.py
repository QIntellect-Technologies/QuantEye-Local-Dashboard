import os
import sqlite3
import tempfile
import unittest

_TEST_DIR = tempfile.TemporaryDirectory(prefix="quanteye-mobile-")
os.environ["QINTELLECT_DB_PATH"] = os.path.join(_TEST_DIR.name, "attendance.db")

import database as db


class MobileEmployeeAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not db.init_db():
            raise RuntimeError("Could not initialize the mobile test database")

    def test_password_uses_the_employee_first_name(self):
        self.assertEqual(db.mobile_password_for_name("Salman"), "salman@123")
        self.assertEqual(db.mobile_password_for_name("Sikandar Ali"), "sikandar@123")

    def test_generate_credentials_uses_email_and_name_password(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO users (id, name, email, organization_id, branch_id, active) VALUES (801, 'Salman', 'salman@gmail.com', 'ORG-MOBILE', 'BR-MOBILE', 1)"
            )
            conn.execute(
                "INSERT INTO users (id, name, email, organization_id, branch_id, active) VALUES (802, 'Sikandar', 'sikandar@gmail.com', 'ORG-MOBILE', 'BR-MOBILE', 1)"
            )
            conn.execute(
                "INSERT INTO users (id, name, email, organization_id, branch_id, active) VALUES (803, 'No Mail', '', 'ORG-MOBILE', 'BR-MOBILE', 1)"
            )
            conn.commit()

        result = db.generate_mobile_credentials("ORG-MOBILE", "BR-MOBILE")
        by_name = {item["name"]: item for item in result["credentials"]}
        self.assertEqual(by_name["Salman"]["username"], "salman@gmail.com")
        self.assertEqual(by_name["Salman"]["password"], "salman@123")
        self.assertEqual(by_name["Sikandar"]["username"], "sikandar@gmail.com")
        self.assertEqual(by_name["Sikandar"]["password"], "sikandar@123")
        self.assertTrue(any(item["name"] == "No Mail" for item in result["skipped"]))

        salman = db.authenticate_employee_email("salman@gmail.com", "salman@123")
        self.assertEqual(salman["id"], 801)
        self.assertIsNone(db.authenticate_employee_email("salman@gmail.com", "wrong@123"))

        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO users (id, name, email, organization_id, branch_id, active) VALUES (804, 'Shahid', 'shahid@gmail.com', 'ORG-MOBILE', 'BR-MOBILE', 1)"
            )
            conn.commit()
        remaining = db.generate_mobile_credentials("ORG-MOBILE", "BR-MOBILE", remaining_only=True)
        remaining_names = [item["name"] for item in remaining["credentials"]]
        self.assertEqual(remaining_names, ["Shahid"])
        self.assertEqual(remaining["credentials"][0]["password"], "shahid@123")

        saved = db.list_mobile_credentials("ORG-MOBILE", "BR-MOBILE")
        saved_names = [item["name"] for item in saved["credentials"]]
        self.assertEqual(saved_names, ["Salman", "Shahid", "Sikandar"])

        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO users (id, name, email, organization_id, branch_id, active) VALUES (901, 'Salman Other', 'salman@gmail.com', 'ORG-OTHER', 'BR-OTHER', 1)"
            )
            conn.commit()
        signed_in = db.authenticate_employee_email("salman@gmail.com", "salman@123")
        self.assertEqual(signed_in["id"], 801)
        self.assertEqual(signed_in["organization_id"], "ORG-MOBILE")
        self.assertTrue(db.assign_mobile_employee_password(901, "ORG-OTHER", "salman@123"))
        still_signed_in = db.authenticate_employee_email("salman@gmail.com", "salman@123")
        self.assertIsNotNone(still_signed_in)
        self.assertEqual(still_signed_in["email"].strip().lower(), "salman@gmail.com")


if __name__ == "__main__":
    unittest.main()
