"""
Self-contained cloud seeding module for Mercury (ORG-708405).
Seeds all leave types, diverse leave requests, overtime, and all 9 optional payroll features.
"""

import os
import sys
import sqlite3
import random
from datetime import date, datetime, timedelta
from werkzeug.security import generate_password_hash

ORG_ID = 'ORG-708405'
ORG_NAME = 'Mercury'
BRANCH_ID = 'BR-2174CB75459E453B'
BRANCH_NAME = 'Lahore'

def ensure_cloud_mercury_seeded(db_path=None, force=False):
    # Only run in production runtime (e.g. Railway) or when SEED_CLOUD is set or force=True
    is_prod = (
        force
        or bool(os.environ.get('RAILWAY_ENVIRONMENT'))
        or os.environ.get('APP_ENV', '').lower() == 'production'
        or os.environ.get('FLASK_ENV', '').lower() == 'production'
        or bool(os.environ.get('SEED_CLOUD'))
    )
    if not is_prod:
        return

    # Never run during unit tests unless forced
    if not force and ('unittest' in sys.modules or 'pytest' in sys.modules or os.environ.get('TESTING')):
        return

    if not db_path:
        db_path = os.environ.get('QINTELLECT_DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'attendance.db'))

    if not os.path.exists(db_path):
        return

    try:
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            try:
                # Check if already seeded with all leave types
                c.execute("SELECT COUNT(DISTINCT leave_type) FROM leave_requests WHERE organization_id = ?", (ORG_ID,))
                row = c.fetchone()
                distinct_leaves = row[0] if row else 0

                c.execute("SELECT COUNT(*) FROM users WHERE organization_id = ?", (ORG_ID,))
                row_u = c.fetchone()
                existing_users = row_u[0] if row_u else 0
            except Exception:
                return

            if not force and existing_users >= 200 and distinct_leaves >= 4:
                return  # Already has 200+ users and diverse leave types!

        print(f"[CloudSeed] Seeding comprehensive dataset for {ORG_NAME} ({ORG_ID})...")
        _execute_seed(db_path)
        print(f"[CloudSeed] Comprehensive seeding completed successfully for {ORG_ID}!")
    except Exception as e:
        print(f"[CloudSeed] Warning: Cloud seeding failed: {e}")

def _execute_seed(db_path):
    random.seed(42)
    import database
    import payroll_extensions

    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        payroll_extensions.ensure_payroll_extension_tables(c)

        # -------------------------------------------------------------
        # 1. PAYROLL SETTINGS (Enable ALL 9 Optional Features)
        # -------------------------------------------------------------
        c.execute("DELETE FROM payroll_settings WHERE organization_id = ?", (ORG_ID,))
        c.execute('''
            INSERT INTO payroll_settings (
                organization_id, branch_id, deduct_unpaid_leave, half_leave_quota_mode, half_leaves_per_quota_day,
                holiday_ot_premium_percent, non_working_day_ot_premium_percent,
                holiday_ot_premium_active, non_working_day_ot_premium_active,
                holiday_ot_pay_mode, non_working_day_ot_pay_mode,
                holiday_ot_multiplier, non_working_day_ot_multiplier,
                overtime_max_hours, overtime_max_period, ot_hour_divisor,
                proration_basis, currency_code, decimal_places,
                off_day_attendance_pay_active, off_day_attendance_pay_mode, off_day_attendance_pay_multiplier,
                enable_named_components, enable_tax, enable_loans, enable_advances,
                enable_provident, enable_insurance, enable_bonuses, enable_individual_adjustments,
                enable_salary_revisions, attendance_pay_mode, ot_rounding,
                payslip_company_name, payslip_tagline, payslip_phone, payslip_email,
                payslip_website, payslip_address, payslip_ntn, payslip_footer, payslip_powered,
                payslip_bank_title, payslip_bank_name, payslip_bank_iban
            ) VALUES (
                ?, ?, 1, 'category', 2,
                100.0, 100.0,
                1, 1,
                'multiplier', 'multiplier',
                2.0, 2.0,
                60.0, 'month', 208.0,
                'calendar_days', 'PKR', 2,
                1, 'multiplier', 1.5,
                1, 1, 1, 1,
                1, 1, 1, 1,
                1, 'full_salary', 'round_rate_first',
                'Mercury', 'DRIVING THE FUTURE TECHNOLOGY POWERED BY QUANTUM & AI', '03006782867', 'mercury@gmail.com',
                'mercury.com', 'Lahore, Pakistan', 'MS-708405', '© 2026 Mercury. All rights reserved.', 'Powered by QuantEye',
                'Mercury Pvt Ltd', 'Habib Bank Limited (HBL)', 'PK36HABB0001234567890123'
            )
        ''', (ORG_ID, BRANCH_ID))

        # Rules
        c.execute("DELETE FROM payroll_rules WHERE organization_id = ?", (ORG_ID,))
        c.execute('''
            INSERT INTO payroll_rules (min_absent_days, max_absent_days, deduction_percent, active, deduction_type, rule_basis, organization_id, branch_id)
            VALUES (1, 2, 5.0, 1, 'percentage', 'absence', ?, ?)
        ''', (ORG_ID, BRANCH_ID))
        c.execute('''
            INSERT INTO payroll_rules (min_absent_days, max_absent_days, deduction_percent, active, deduction_type, rule_basis, organization_id, branch_id)
            VALUES (3, 5, 15.0, 1, 'percentage', 'absence', ?, ?)
        ''', (ORG_ID, BRANCH_ID))
        c.execute('''
            INSERT INTO payroll_rules (min_absent_days, max_absent_days, deduction_percent, active, deduction_type, rule_basis, organization_id, branch_id)
            VALUES (6, 31, 30.0, 1, 'percentage', 'absence', ?, ?)
        ''', (ORG_ID, BRANCH_ID))

        # Shifts
        c.execute("SELECT id FROM shifts WHERE organization_id = ? AND name LIKE '%Morning%'", (ORG_ID,))
        row_m = c.fetchone()
        if row_m:
            shift_morning_id = row_m[0]
        else:
            c.execute('''
                INSERT INTO shifts (name, start_time, end_time, grace_period_minutes, active, is_default, working_days, checkout_grace_period_minutes, organization_id, branch_id)
                VALUES ('Morning Shift', '09:00', '17:00', 15, 1, 1, '[0, 1, 2, 3, 4, 5]', 15, ?, ?)
            ''', (ORG_ID, BRANCH_ID))
            shift_morning_id = c.lastrowid

        c.execute("SELECT id FROM shifts WHERE organization_id = ? AND name LIKE '%General%'", (ORG_ID,))
        row_g = c.fetchone()
        if row_g:
            shift_general_id = row_g[0]
        else:
            c.execute('''
                INSERT INTO shifts (name, start_time, end_time, grace_period_minutes, active, is_default, working_days, checkout_grace_period_minutes, organization_id, branch_id)
                VALUES ('General Shift', '09:00', '18:00', 15, 1, 0, '[0, 1, 2, 3, 4, 5]', 15, ?, ?)
            ''', (ORG_ID, BRANCH_ID))
            shift_general_id = c.lastrowid

        # -------------------------------------------------------------
        # 2. ALL LEAVE TYPES (Full and Half-day enabled)
        # -------------------------------------------------------------
        leave_types_spec = [
            ('Casual Leave', 'casual', 10.0, 1, 1, 'Short-term personal and casual leave'),
            ('Sick Leave', 'sick', 8.0, 1, 1, 'Medical or health related illness'),
            ('Annual / Paid Leave', 'annual', 14.0, 1, 1, 'Earned annual recreational paid leave'),
            ('Unpaid Leave (LWP)', 'unpaid', 0.0, 0, 1, 'Leave without pay / salary deduction applies'),
            ('Emergency Leave', 'emergency', 5.0, 1, 1, 'Immediate urgent household emergency'),
            ('Bereavement Leave', 'bereavement', 3.0, 1, 1, 'Compassionate leave for family condolences'),
            ('Maternity / Paternity Leave', 'maternity', 60.0, 1, 0, 'Parental childcare leave')
        ]
        for lt_name, lt_code, lt_allowance, lt_paid, lt_half, lt_desc in leave_types_spec:
            c.execute("SELECT id FROM leave_types WHERE organization_id = ? AND code = ?", (ORG_ID, lt_code))
            row_lt = c.fetchone()
            if row_lt:
                c.execute('''
                    UPDATE leave_types SET name = ?, yearly_allowance = ?, is_paid = ?, half_leave_enabled = 1, description = ?
                    WHERE id = ?
                ''', (lt_name, lt_allowance, lt_paid, lt_desc, row_lt[0]))
            else:
                c.execute('''
                    INSERT INTO leave_types (name, code, yearly_allowance, is_paid, description, active, allowance_period, half_leave_enabled, half_leaves_per_quota_day, organization_id, branch_id)
                    VALUES (?, ?, ?, ?, ?, 1, 'yearly', 1, 2, ?, ?)
                ''', (lt_name, lt_code, lt_allowance, lt_paid, lt_desc, ORG_ID, BRANCH_ID))

        # Holidays
        c.execute("SELECT id FROM company_holidays WHERE organization_id = ? AND holiday_date = '2026-08-14'", (ORG_ID,))
        if not c.fetchone():
            c.execute('''
                INSERT INTO company_holidays (name, holiday_date, end_date, description, is_recurring, overtime_premium_percent, organization_id, branch_id)
                VALUES ('Independence Day', '2026-08-14', '2026-08-14', 'National Independence Day', 1, 100.0, ?, ?)
            ''', (ORG_ID, BRANCH_ID))

        # -------------------------------------------------------------
        # 3. PAYROLL EXTENSION CATALOG (All 9 Optional Component Types)
        # -------------------------------------------------------------
        component_types_spec = [
            ("House Rent Allowance", "HRA", "earning", "allowance", "percent_basic", 40.0),
            ("Medical Allowance", "MED", "earning", "allowance", "percent_basic", 10.0),
            ("Utility Allowance", "UTIL", "earning", "allowance", "percent_basic", 10.0),
            ("Conveyance Allowance", "CONV", "earning", "allowance", "fixed", 5000.0),
            ("Quarterly Performance Bonus", "PERF_BONUS", "earning", "bonus", "fixed", 15000.0),
            ("Income Tax Withholding", "TAX", "deduction", "tax", "fixed", 4500.0),
            ("Provident Fund Contribution", "PF", "deduction", "provident", "percent_basic", 5.0),
            ("Health & Life Insurance", "INS", "deduction", "insurance", "fixed", 2500.0),
            ("Company Loan Repayment", "LOAN", "deduction", "loan", "fixed", 8000.0),
            ("Salary Advance Recovery", "ADV", "deduction", "advance", "fixed", 10000.0),
        ]
        comp_type_ids = {}
        for c_name, c_code, c_kind, c_cat, c_calc, c_def in component_types_spec:
            c.execute("SELECT id FROM payroll_component_types WHERE organization_id = ? AND code = ?", (ORG_ID, c_code))
            row_c = c.fetchone()
            if row_c:
                comp_type_ids[c_code] = row_c[0]
            else:
                c.execute('''
                    INSERT INTO payroll_component_types (
                        organization_id, branch_id, name, code, kind, category, calc_mode, default_amount, active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ''', (ORG_ID, BRANCH_ID, c_name, c_code, c_kind, c_cat, c_calc, c_def))
                comp_type_ids[c_code] = c.lastrowid

        conn.commit()

    # -------------------------------------------------------------
    # 4. LOAD USERS & GUARANTEE SALARY CONFIGS
    # -------------------------------------------------------------
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, salary, join_date, exit_date FROM users WHERE organization_id = ?", (ORG_ID,))
        users_in_org = c.fetchall()

        for uid, uname, usal, ujdate, uedate in users_in_org:
            sal_val = float(usal or 50000.0)
            if sal_val <= 0:
                sal_val = 50000.0
                c.execute("UPDATE users SET salary = 50000.0 WHERE id = ?", (uid,))

            basic = round(sal_val * 0.7, 2)
            allowances = round(sal_val * 0.3, 2)

            c.execute("SELECT user_id FROM salary_configs WHERE user_id = ?", (uid,))
            if not c.fetchone():
                c.execute('''
                    INSERT INTO salary_configs (user_id, basic_salary, allowances, deductions, ot_rate, organization_id, branch_id)
                    VALUES (?, ?, ?, 0, 0, ?, ?)
                ''', (uid, basic, allowances, ORG_ID, BRANCH_ID))
            else:
                c.execute('''
                    UPDATE salary_configs SET basic_salary = ?, allowances = ?, organization_id = ?, branch_id = ?
                    WHERE user_id = ?
                ''', (basic, allowances, ORG_ID, BRANCH_ID, uid))

        conn.commit()

    # -------------------------------------------------------------
    # 5. ASSIGN PAYROLL COMPONENTS, ADJUSTMENTS, ADVANCES, LOANS
    # -------------------------------------------------------------
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM payroll_employee_components WHERE organization_id = ?", (ORG_ID,))
        c.execute("DELETE FROM payroll_adjustments WHERE organization_id = ?", (ORG_ID,))
        c.execute("DELETE FROM payroll_bonuses WHERE user_id IN (SELECT id FROM users WHERE organization_id = ?)", (ORG_ID,))
        c.execute("DELETE FROM salary_revisions WHERE organization_id = ?", (ORG_ID,))

        comp_assignments = []
        now_date_str = "2026-01-01"

        for idx, (uid, uname, usal, ujdate, uedate) in enumerate(users_in_org):
            sal_val = float(usal or 50000.0)

            # 1. HRA (40%)
            if "HRA" in comp_type_ids:
                comp_assignments.append((ORG_ID, BRANCH_ID, uid, comp_type_ids["HRA"], 0, 40.0, now_date_str))
            # 2. Medical (10%)
            if "MED" in comp_type_ids:
                comp_assignments.append((ORG_ID, BRANCH_ID, uid, comp_type_ids["MED"], 0, 10.0, now_date_str))
            # 3. Utility (10%)
            if "UTIL" in comp_type_ids:
                comp_assignments.append((ORG_ID, BRANCH_ID, uid, comp_type_ids["UTIL"], 0, 10.0, now_date_str))
            # 4. Conveyance (Fixed 5,000)
            if "CONV" in comp_type_ids:
                comp_assignments.append((ORG_ID, BRANCH_ID, uid, comp_type_ids["CONV"], 5000.0, 0, now_date_str))
            # 5. Provident Fund (5%)
            if "PF" in comp_type_ids:
                comp_assignments.append((ORG_ID, BRANCH_ID, uid, comp_type_ids["PF"], 0, 5.0, now_date_str))
            # 6. Health Insurance (Fixed 2,500)
            if "INS" in comp_type_ids:
                comp_assignments.append((ORG_ID, BRANCH_ID, uid, comp_type_ids["INS"], 2500.0, 0, now_date_str))

            # 7. Tax (for higher salaries > 85k)
            if sal_val > 85000 and "TAX" in comp_type_ids:
                tax_amount = round((sal_val - 85000) * 0.10, 2)
                comp_assignments.append((ORG_ID, BRANCH_ID, uid, comp_type_ids["TAX"], tax_amount, 0, now_date_str))

            # 8. Loan Repayment (for first 20 employees)
            if idx < 20 and "LOAN" in comp_type_ids:
                comp_assignments.append((ORG_ID, BRANCH_ID, uid, comp_type_ids["LOAN"], 8000.0, 0, now_date_str))

            # 9. Advance Recovery (for employees 21 to 45)
            if 20 <= idx < 45 and "ADV" in comp_type_ids:
                comp_assignments.append((ORG_ID, BRANCH_ID, uid, comp_type_ids["ADV"], 10000.0, 0, now_date_str))

            # Bonuses (Excel uploaded)
            if idx % 5 == 0:
                c.execute('''
                    INSERT INTO payroll_bonuses (organization_id, branch_id, year, month, user_id, amount, note, source)
                    VALUES (?, ?, 2026, 7, ?, 15000.0, 'Performance Milestone Bonus', 'excel')
                ''', (ORG_ID, BRANCH_ID, uid))
                c.execute('''
                    INSERT INTO payroll_bonuses (organization_id, branch_id, year, month, user_id, amount, note, source)
                    VALUES (?, ?, 2026, 8, ?, 20000.0, 'Independence Day Project Bonus', 'excel')
                ''', (ORG_ID, BRANCH_ID, uid))

            # Individual Adjustments (Additions & Deductions)
            if idx % 4 == 0:
                c.execute('''
                    INSERT INTO payroll_adjustments (
                        organization_id, branch_id, user_id, payroll_month, adj_type, pay_mode, amount, note, created_by
                    ) VALUES (?, ?, ?, '2026-08', 'earning', 'fixed', 7500.0, 'Travel Reimbursement', 'usman@gmail.com')
                ''', (ORG_ID, BRANCH_ID, uid))
                c.execute('''
                    INSERT INTO payroll_adjustments (
                        organization_id, branch_id, user_id, payroll_month, adj_type, pay_mode, amount, note, created_by
                    ) VALUES (?, ?, ?, '2026-09', 'deduction', 'fixed', 1500.0, 'Cafeteria Charge Recovery', 'usman@gmail.com')
                ''', (ORG_ID, BRANCH_ID, uid))

            # Salary Revisions (for 25 senior employees)
            if idx < 25:
                revised_basic = round(sal_val * 0.75, 2)
                revised_allowances = round(sal_val * 0.25, 2)
                c.execute('''
                    INSERT INTO salary_revisions (
                        organization_id, branch_id, user_id, effective_from, basic_salary, allowances, deductions, ot_rate
                    ) VALUES (?, ?, ?, '2026-07-01', ?, ?, 0, 0)
                ''', (ORG_ID, BRANCH_ID, uid, revised_basic, revised_allowances))

        c.executemany('''
            INSERT INTO payroll_employee_components (
                organization_id, branch_id, user_id, component_type_id, amount, percent, effective_from, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        ''', comp_assignments)

        conn.commit()

    # -------------------------------------------------------------
    # 6. DIVERSE LEAVE REQUESTS (ALL 7 LEAVE TYPES + MULTIPLE STATUSES)
    # -------------------------------------------------------------
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM leave_requests WHERE organization_id = ?", (ORG_ID,))

        diverse_leave_types = [
            # (name, is_paid, reasons)
            ('Casual Leave', 1, [
                'Attending family wedding ceremony', 'Renewing passport and national CNIC',
                'Home repair and urgent plumbing maintenance', 'Child school admission interview'
            ]),
            ('Sick Leave', 1, [
                'Seasonal viral flu and high temperature', 'Severe migraine and bed rest recommended',
                'Dental root canal procedure and recovery', 'Food poisoning and medical observation'
            ]),
            ('Annual / Paid Leave', 1, [
                'Annual family vacation trip to Northern Pakistan', 'Summer recreational holiday leave',
                'Personal travel and family gathering'
            ]),
            ('Unpaid Leave (LWP)', 0, [
                'Extended family commitment beyond quota', 'Personal business matter leave without pay',
                'Unforeseen domestic travel beyond paid quota'
            ]),
            ('Emergency Leave', 1, [
                'Urgent hospital emergency for family member', 'Unexpected domestic accident and repair',
                'Sudden family crisis requiring immediate presence'
            ]),
            ('Bereavement Leave', 1, [
                'Funeral and condolence gathering for close relative', 'Family bereavement rituals'
            ])
        ]

        leave_inserts = []
        statuses = ['Approved', 'Approved', 'Approved', 'Pending', 'Rejected']

        # Dates to scatter leaves across July, August, September, and current October
        base_dates = [
            (date(2026, 7, 5), date(2026, 7, 6)),
            (date(2026, 7, 12), date(2026, 7, 12)),
            (date(2026, 7, 18), date(2026, 7, 22)), # Multi-day annual leave
            (date(2026, 7, 28), date(2026, 7, 28)),
            (date(2026, 8, 3), date(2026, 8, 4)),
            (date(2026, 8, 10), date(2026, 8, 10)),
            (date(2026, 8, 15), date(2026, 8, 19)), # Multi-day vacation
            (date(2026, 8, 24), date(2026, 8, 25)),
            (date(2026, 9, 2), date(2026, 9, 2)),
            (date(2026, 9, 8), date(2026, 9, 9)),
            (date(2026, 9, 15), date(2026, 9, 18)), # Multi-day
            (date(2026, 9, 22), date(2026, 9, 22)),
            (date(2026, 9, 28), date(2026, 9, 29)),
            (date(2026, 10, 5), date(2026, 10, 5)),
            (date(2026, 10, 12), date(2026, 10, 13)),
        ]

        # Generate ~150 diverse leave requests across employees
        req_count = 0
        for uid, uname, usal, ujdate, uedate in users_in_org:
            if req_count >= 140:
                break

            # Pick 1 or 2 leaves per selected employee
            chosen_lt_spec = diverse_leave_types[req_count % len(diverse_leave_types)]
            lt_name, lt_is_paid, reasons = chosen_lt_spec
            s_date, e_date = base_dates[req_count % len(base_dates)]

            # Decide full or half day
            is_half = (lt_name in ['Casual Leave', 'Sick Leave', 'Emergency Leave'] and req_count % 3 == 0)
            if is_half:
                e_date = s_date
                duration_str = 'half_day'
                quota_charge = 0.5
                paid_d = 0.5 if lt_is_paid else 0.0
                unpaid_d = 0.0 if lt_is_paid else 0.5
            else:
                duration_str = 'full_day'
                diff_days = (e_date - s_date).days + 1
                quota_charge = float(diff_days)
                paid_d = float(diff_days) if lt_is_paid else 0.0
                unpaid_d = 0.0 if lt_is_paid else float(diff_days)

            status = statuses[req_count % len(statuses)]
            reason = random.choice(reasons)
            created_at = f"{s_date} 08:00:00"

            leave_inserts.append((
                uid, uname, lt_name, s_date.strftime("%Y-%m-%d"), e_date.strftime("%Y-%m-%d"),
                reason, status, 'usman@gmail.com' if status == 'Approved' else None,
                created_at, paid_d, unpaid_d, quota_charge,
                s_date.strftime("%Y-%m-%d") if lt_is_paid else None,
                e_date.strftime("%Y-%m-%d") if lt_is_paid else None,
                duration_str, None, quota_charge, ORG_ID, BRANCH_ID,
                'paid' if lt_is_paid else 'unpaid',
                'unpaid' if not lt_is_paid else 'paid'
            ))
            req_count += 1

        c.executemany('''
            INSERT INTO leave_requests (
                user_id, user_name, leave_type, start_date, end_date, reason, status, approved_by,
                created_at, paid_days, unpaid_days, quota_days, paid_through_date, quota_through_date,
                leave_duration, half_leave_quota_type_id, half_leave_quota_charge, organization_id, branch_id,
                paid_status, unpaid_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', leave_inserts)

        conn.commit()

    # -------------------------------------------------------------
    # 7. RE-CALCULATE AND FINALIZE MONTHLY PAYROLL WITH ALL 9 FEATURES
    # -------------------------------------------------------------
    for pmonth in ['2026-07', '2026-08', '2026-09']:
        try:
            prev = database.calculate_monthly_payroll(
                payroll_month=pmonth,
                organization_id=ORG_ID,
                branch_id=BRANCH_ID,
                force_recalculate=True
            )
            database.finalize_monthly_payroll(
                preview=prev,
                organization_id=ORG_ID,
                branch_id=BRANCH_ID,
                finalized_by='usman@gmail.com'
            )
            print(f"[CloudSeed] Successfully finalized {pmonth} payroll with all 9 optional features!")
        except Exception as p_err:
            print(f"[CloudSeed] Payroll calculation note for {pmonth}: {p_err}")

if __name__ == '__main__':
    ensure_cloud_mercury_seeded(force=True)
