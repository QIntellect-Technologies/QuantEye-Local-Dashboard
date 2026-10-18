import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, date

_TEST_DIR = tempfile.TemporaryDirectory(prefix="quanteye-punch-test-")
os.environ["QINTELLECT_DB_PATH"] = os.path.join(_TEST_DIR.name, "attendance.db")

import database as db


class MobileLivePunchAndCountdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not db.init_db():
            raise RuntimeError("Could not initialize test database")

        with sqlite3.connect(db.DB_PATH) as conn:
            # Create shift
            conn.execute(
                """
                INSERT OR REPLACE INTO shifts (id, name, start_time, end_time, organization_id, branch_id)
                VALUES (999, 'General Shift', '09:00', '17:00', 'ORG-TEST', 'BR-HQ')
                """
            )
            # Create employee with shift_id
            conn.execute(
                """
                INSERT INTO users (id, name, email, organization_id, branch_id, active, position, department, shift_id)
                VALUES (501, 'Imran Khalid', 'imran@quanteye.ai', 'ORG-TEST', 'BR-HQ', 1, 'AI Engineer', 'Engineering', 999)
                """
            )
            conn.commit()

    def test_01_no_punch_returns_none(self):
        punch = db.get_latest_employee_punch(501, 'ORG-TEST', 'BR-HQ')
        self.assertIsNone(punch)

    def test_02_first_punch_checkin_notification(self):
        today = date.today().isoformat()
        punch_time = f"{today} 09:02:15"
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO attendance (id, user_id, timestamp, source, location, device_id, organization_id, branch_id)
                VALUES (1001, 501, ?, 'camera', 'Office Entrance Gate', 'Entrance AI Node', 'ORG-TEST', 'BR-HQ')
                """,
                (punch_time,)
            )
            conn.commit()

        punch = db.get_latest_employee_punch(501, 'ORG-TEST', 'BR-HQ')
        self.assertIsNotNone(punch)
        self.assertEqual(punch['id'], 1001)
        self.assertEqual(punch['first_name'], 'Imran')
        self.assertEqual(punch['punch_type'], 'in')
        self.assertEqual(punch['title'], 'Check-in Confirmed')
        self.assertEqual(punch['location'], 'Office Entrance Gate')
        self.assertIn('Imran', punch['message'])
        self.assertIn('Checked in at', punch['message'])
        self.assertIn('09:02 AM', punch['message'])

    def test_03_second_punch_checkout_notification(self):
        today = date.today().isoformat()
        punch_time = f"{today} 18:30:00"
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO attendance (id, user_id, timestamp, source, location, device_id, organization_id, branch_id)
                VALUES (1002, 501, ?, 'camera', 'Office Exit Turnstile', 'Exit AI Node', 'ORG-TEST', 'BR-HQ')
                """,
                (punch_time,)
            )
            conn.commit()

        punch = db.get_latest_employee_punch(501, 'ORG-TEST', 'BR-HQ')
        self.assertIsNotNone(punch)
        self.assertEqual(punch['id'], 1002)
        self.assertEqual(punch['punch_type'], 'out')
        self.assertEqual(punch['title'], 'Check-out Confirmed')
        self.assertIn('Checked out at 06:30 PM', punch['message'])
        self.assertIn('Have a great day, Imran!', punch['message'])

    def test_04_employee_mobile_attendance_payload_includes_meter_data(self):
        card = db.employee_mobile_attendance(501, 'ORG-TEST', 'BR-HQ')
        self.assertEqual(card['name'], 'Imran Khalid')
        self.assertIn('shift_start', card)
        self.assertIn('shift_end', card)
        self.assertIn('raw_check_in', card)
        self.assertIn('raw_check_out', card)
        self.assertIn('latest_punch', card)
        self.assertIsNotNone(card['latest_punch'])
        self.assertEqual(card['latest_punch']['id'], 1002)

    def test_05_mobile_html_contains_toast_and_meter_elements(self):
        html_path = os.path.join(os.path.dirname(__file__), 'templates', 'mobile.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Instant Punch Push Notification Toast elements
        self.assertIn('id="punchToast"', content)
        self.assertIn('id="toastMessage"', content)
        self.assertIn('id="toastTime"', content)
        self.assertIn('id="toastSub"', content)
        self.assertIn('checkLivePunch', content)
        self.assertIn('/api/mobile/latest-punch', content)
        self.assertIn('triggerPunchNotification', content)

        # Shift Countdown and Overtime Meter elements
        self.assertIn('id="shiftMeterCard"', content)
        self.assertIn('id="meterBadge"', content)
        self.assertIn('id="meterCountdown"', content)
        self.assertIn('id="meterFill"', content)
        self.assertIn('meter-overtime', content)
        self.assertIn('OVERTIME ACTIVE', content)
        self.assertIn('updateShiftMeter', content)


if __name__ == '__main__':
    unittest.main()
