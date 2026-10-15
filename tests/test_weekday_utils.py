"""Unit tests for the canonical weekday helper. Live scenarios still required for payroll."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from weekday_utils import (
    DEFAULT_WORKING_DAYS,
    FRIDAY,
    MONDAY,
    SATURDAY,
    SUNDAY,
    WorkingDaysError,
    js_sunday0_to_python,
    parse_working_days,
    python_to_sqlite_sunday0,
    scheduled_days_in_month,
    sqlite_sunday0_to_python,
)


class WeekdayUtilsTests(unittest.TestCase):
    def test_default_is_monday_through_saturday(self):
        self.assertEqual(list(DEFAULT_WORKING_DAYS), [MONDAY, 1, 2, 3, 4, SATURDAY])
        self.assertNotIn(SUNDAY, DEFAULT_WORKING_DAYS)

    def test_rejects_empty_duplicates_and_out_of_range(self):
        with self.assertRaises(WorkingDaysError):
            parse_working_days([])
        with self.assertRaises(WorkingDaysError):
            parse_working_days([1, 2, 3, 4, 5, 6, 1])
        with self.assertRaises(WorkingDaysError):
            parse_working_days([7])
        with self.assertRaises(WorkingDaysError):
            parse_working_days([-1])
        tue_sun = parse_working_days([1, 2, 3, 4, 5, 6])
        self.assertEqual(tue_sun, [1, 2, 3, 4, 5, 6])
        parsed = parse_working_days(list(DEFAULT_WORKING_DAYS))
        self.assertEqual(parsed, [0, 1, 2, 3, 4, 5])

    def test_sqlite_and_js_sunday_zero_convert_to_python(self):
        self.assertEqual(sqlite_sunday0_to_python(0), SUNDAY)
        self.assertEqual(sqlite_sunday0_to_python(1), MONDAY)
        self.assertEqual(python_to_sqlite_sunday0(MONDAY), 1)
        self.assertEqual(js_sunday0_to_python(0), SUNDAY)
        self.assertEqual(js_sunday0_to_python(5), FRIDAY)

    def test_september_2026_has_26_mon_sat_days(self):
        days = scheduled_days_in_month(2026, 9, working_days=DEFAULT_WORKING_DAYS)
        self.assertEqual(len(days), 26)
        self.assertTrue(all(d.weekday() <= SATURDAY for d in days))
        self.assertNotIn(date(2026, 9, 6), days)


if __name__ == "__main__":
    unittest.main()
