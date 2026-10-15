import unittest

import database as db


class SalaryValidationTests(unittest.TestCase):
    def test_rejects_negative_salary(self):
        with self.assertRaises(ValueError):
            db.parse_salary_amount(-1)

    def test_rejects_above_max(self):
        with self.assertRaises(ValueError):
            db.parse_salary_amount(db.SALARY_MAX + 1)

    def test_accepts_zero_and_positive(self):
        self.assertEqual(db.parse_salary_amount(0), 0.0)
        self.assertEqual(db.parse_salary_amount('75000'), 75000.0)

    def test_empty_uses_default(self):
        self.assertEqual(db.parse_salary_amount('', default=0), 0.0)


class BonusTemplateImportTests(unittest.TestCase):
    def test_openpyxl_is_available_for_railway_template(self):
        import openpyxl
        self.assertTrue(hasattr(openpyxl, 'Workbook'))


if __name__ == '__main__':
    unittest.main()
