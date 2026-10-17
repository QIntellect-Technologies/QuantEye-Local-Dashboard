"""
Self-contained cloud seeding module for organizations:
- Mercury (ORG-708405 / BR-2174CB75459E453B)
- Hello World (ORG-873824 / BR-891820B1BF4A4030)
Seeds all leave types, 200 employees, 3 months attendance history, diverse leaves, and all 9 payroll features.
"""

import os
import sys
import sqlite3
import random
from datetime import date, datetime, timedelta
from werkzeug.security import generate_password_hash

TARGET_ORGS = [
    {
        'org_id': 'ORG-708405',
        'branch_id': 'BR-2174CB75459E453B',
        'org_name': 'Mercury',
        'domain': 'mercury.com',
        'admin_email': 'mercury@gmail.com'
    },
    {
        'org_id': 'ORG-873824',
        'branch_id': 'BR-891820B1BF4A4030',
        'org_name': 'Hello World',
        'domain': 'helloworld.com',
        'admin_email': 'helloworld@gmail.com'
    }
]

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

    for target in TARGET_ORGS:
        org_id = target['org_id']
        org_name = target['org_name']
        try:
            with sqlite3.connect(db_path) as conn:
                c = conn.cursor()
                try:
                    c.execute("SELECT COUNT(DISTINCT leave_type) FROM leave_requests WHERE organization_id = ?", (org_id,))
                    row = c.fetchone()
                    distinct_leaves = row[0] if row else 0

                    c.execute("SELECT COUNT(*) FROM users WHERE organization_id = ?", (org_id,))
                    row_u = c.fetchone()
                    existing_users = row_u[0] if row_u else 0
                except Exception:
                    continue

                if not force and existing_users >= 200 and distinct_leaves >= 4:
                    continue  # Already seeded for this org

            print(f"[CloudSeed] Seeding comprehensive dataset for {org_name} ({org_id})...")
            _execute_seed_for_org(db_path, target)
            print(f"[CloudSeed] Comprehensive seeding completed successfully for {org_id}!")
        except Exception as e:
            print(f"[CloudSeed] Warning: Cloud seeding failed for {org_id}: {e}")

def _execute_seed_for_org(db_path, target):
    org_id = target['org_id']
    branch_id = target['branch_id']
    org_name = target['org_name']
    domain = target['domain']
    admin_email = target['admin_email']

    random.seed(42)
    import database
    import payroll_extensions

    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        payroll_extensions.ensure_payroll_extension_tables(c)

        # -------------------------------------------------------------
        # 1. PAYROLL SETTINGS (Enable ALL 9 Optional Features)
        # -------------------------------------------------------------
        c.execute("DELETE FROM payroll_settings WHERE organization_id = ?", (org_id,))
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
                ?, 'DRIVING THE FUTURE TECHNOLOGY POWERED BY QUANTUM & AI', '+92 311 7210168', ?,
                ?, 'Lahore, Pakistan', 'HW-873824', '© 2026. All rights reserved.', 'Powered by QuantEye',
                ?, 'Habib Bank Limited (HBL)', 'PK36HABB0001234567890123'
            )
        ''', (org_id, branch_id, org_name, admin_email, domain, org_name))

        # Rules
        c.execute("DELETE FROM payroll_rules WHERE organization_id = ?", (org_id,))
        c.execute('''
            INSERT INTO payroll_rules (min_absent_days, max_absent_days, deduction_percent, active, deduction_type, rule_basis, organization_id, branch_id)
            VALUES (1, 2, 5.0, 1, 'percentage', 'absence', ?, ?)
        ''', (org_id, branch_id))
        c.execute('''
            INSERT INTO payroll_rules (min_absent_days, max_absent_days, deduction_percent, active, deduction_type, rule_basis, organization_id, branch_id)
            VALUES (3, 5, 15.0, 1, 'percentage', 'absence', ?, ?)
        ''', (org_id, branch_id))
        c.execute('''
            INSERT INTO payroll_rules (min_absent_days, max_absent_days, deduction_percent, active, deduction_type, rule_basis, organization_id, branch_id)
            VALUES (6, 31, 30.0, 1, 'percentage', 'absence', ?, ?)
        ''', (org_id, branch_id))

        # Shifts
        c.execute("SELECT id FROM shifts WHERE organization_id = ? AND name LIKE '%Morning%'", (org_id,))
        row_m = c.fetchone()
        if row_m:
            shift_morning_id = row_m[0]
        else:
            c.execute("SELECT id FROM shifts WHERE organization_id = ? LIMIT 1", (org_id,))
            row_any = c.fetchone()
            if row_any:
                shift_morning_id = row_any[0]
            else:
                shift_morning_name = f"{org_name} Morning Shift"
                c.execute('''
                    INSERT INTO shifts (name, start_time, end_time, grace_period_minutes, active, is_default, working_days, checkout_grace_period_minutes, organization_id, branch_id)
                    VALUES (?, '09:00', '17:00', 15, 1, 1, '[0, 1, 2, 3, 4, 5]', 15, ?, ?)
                ''', (shift_morning_name, org_id, branch_id))
                shift_morning_id = c.lastrowid

        c.execute("SELECT id FROM shifts WHERE organization_id = ? AND name LIKE '%General%'", (org_id,))
        row_g = c.fetchone()
        if row_g:
            shift_general_id = row_g[0]
        else:
            shift_general_name = f"{org_name} General Shift"
            c.execute('''
                INSERT INTO shifts (name, start_time, end_time, grace_period_minutes, active, is_default, working_days, checkout_grace_period_minutes, organization_id, branch_id)
                VALUES (?, '09:00', '18:00', 15, 1, 0, '[0, 1, 2, 3, 4, 5]', 15, ?, ?)
            ''', (shift_general_name, org_id, branch_id))
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
            c.execute("SELECT id FROM leave_types WHERE organization_id = ? AND code = ?", (org_id, lt_code))
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
                ''', (lt_name, lt_code, lt_allowance, lt_paid, lt_desc, org_id, branch_id))

        # Holidays
        c.execute("SELECT id FROM company_holidays WHERE organization_id = ? AND holiday_date = '2026-08-14'", (org_id,))
        if not c.fetchone():
            c.execute('''
                INSERT INTO company_holidays (name, holiday_date, end_date, description, is_recurring, overtime_premium_percent, organization_id, branch_id)
                VALUES ('Independence Day', '2026-08-14', '2026-08-14', 'National Independence Day', 1, 100.0, ?, ?)
            ''', (org_id, branch_id))

        # Cameras
        c.execute("SELECT id FROM cameras WHERE organization_id = ? AND camera_id = ?", (org_id, f"CAM-{org_id}-01"))
        if not c.fetchone():
            c.execute('''
                INSERT INTO cameras (camera_id, name, room_name, camera_type, rtsp_url, active, organization_id, branch_id)
                VALUES (?, 'Entrance Turnstile', 'Reception', 'nvr', 'rtsp://192.168.10.10:554/ch1', 1, ?, ?)
            ''', (f"CAM-{org_id}-01", org_id, branch_id))
        c.execute("SELECT id FROM cameras WHERE organization_id = ? AND camera_id = ?", (org_id, f"CAM-{org_id}-02"))
        if not c.fetchone():
            c.execute('''
                INSERT INTO cameras (camera_id, name, room_name, camera_type, rtsp_url, active, organization_id, branch_id)
                VALUES (?, 'Lobby Exit Gate', 'Exit Corridor', 'nvr', 'rtsp://192.168.10.11:554/ch2', 1, ?, ?)
            ''', (f"CAM-{org_id}-02", org_id, branch_id))

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
            c.execute("SELECT id FROM payroll_component_types WHERE organization_id = ? AND code = ?", (org_id, c_code))
            row_c = c.fetchone()
            if row_c:
                comp_type_ids[c_code] = row_c[0]
            else:
                c.execute('''
                    INSERT INTO payroll_component_types (
                        organization_id, branch_id, name, code, kind, category, calc_mode, default_amount, active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ''', (org_id, branch_id, c_name, c_code, c_kind, c_cat, c_calc, c_def))
                comp_type_ids[c_code] = c.lastrowid

        conn.commit()

    # -------------------------------------------------------------
    # 4. BUILD 200 REALISTIC EMPLOYEES IF NOT ALREADY AT 200
    # -------------------------------------------------------------
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE organization_id = ?", (org_id,))
        current_emp_count = c.fetchone()[0]

    if current_emp_count < 200:
        first_names = [
            "Muhammad", "Ahmed", "Ali", "Usman", "Bilal", "Hamza", "Zubair", "Omer", "Hassan", "Hussein",
            "Tariq", "Sufyan", "Kamran", "Faisal", "Noman", "Kashif", "Waqas", "Asad", "Haris", "Imran",
            "Fatima", "Ayesha", "Zainab", "Mariam", "Sana", "Hira", "Anum", "Sadia", "Khadija", "Nimra",
            "Farhan", "Rizwan", "Babar", "Saad", "Adeel", "Danish", "Salman", "Rehan", "Shahid", "Waseem"
        ]
        last_names = [
            "Khan", "Malik", "Chaudhry", "Bhatti", "Sheikh", "Qureshi", "Butt", "Raza", "Ansari", "Mirza",
            "Siddiqui", "Abbasi", "Dar", "Hashmi", "Gill", "Mughal", "Shah", "Gondal", "Javed", "Akram"
        ]
        depts = [
            ("Core Engineering", ["Software Engineer", "Senior Backend Dev", "DevOps Engineer", "QA Engineer", "Frontend Specialist"], 45, 90000, 220000),
            ("Operations & Logistics", ["Operations Lead", "Fleet Coordinator", "Dispatcher", "Inventory Specialist", "Ops Assistant"], 55, 45000, 110000),
            ("Sales & Commercial", ["Sales Manager", "Account Executive", "Marketing Specialist", "Business Development Lead"], 35, 60000, 160000),
            ("Customer Experience", ["Customer Support Rep", "Senior CS Specialist", "Support Team Lead", "Technical Helpdesk"], 30, 40000, 85000),
            ("Finance & Accounts", ["Senior Accountant", "Payroll Analyst", "Billing Officer", "Internal Auditor"], 18, 65000, 175000),
            ("Human Resources", ["HR Manager", "Talent Acquisition Specialist", "HR Officer", "People Ops Lead"], 17, 55000, 150000)
        ]

        used_emails = set()
        used_cnics = set()

        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            for r in c.execute("SELECT email, cnic FROM users WHERE organization_id = ?", (org_id,)).fetchall():
                if r[0]: used_emails.add(r[0].lower())
                if r[1]: used_cnics.add(r[1])

        emp_idx = 1
        employees = []
        for dept_name, roles, target_count, min_sal, max_sal in depts:
            for _ in range(target_count):
                fn = random.choice(first_names)
                ln = random.choice(last_names)
                name = f"{fn} {ln}"
                pos = random.choice(roles)
                sal = float(round(random.randint(min_sal, max_sal) / 1000) * 1000)

                clean_org_prefix = org_name.lower().replace(" ", "")
                email_base = f"{fn.lower()}.{ln.lower()}{emp_idx}@{clean_org_prefix}.com"
                while email_base in used_emails:
                    emp_idx += 1
                    email_base = f"{fn.lower()}.{ln.lower()}{emp_idx}@{clean_org_prefix}.com"
                used_emails.add(email_base)

                phone = f"03{random.randint(10, 49)}{random.randint(1000000, 9999999)}"
                cnic = f"{random.randint(31101, 38405)}-{random.randint(1000000, 9999999)}-{random.randint(1, 9)}"
                while cnic in used_cnics:
                    cnic = f"{random.randint(31101, 38405)}-{random.randint(1000000, 9999999)}-{random.randint(1, 9)}"
                used_cnics.add(cnic)

                if emp_idx <= 170:
                    join_d = date(2024, 1, 1) + timedelta(days=random.randint(0, 800))
                    join_date_str = join_d.strftime("%Y-%m-%d")
                    exit_date_str = None
                elif emp_idx <= 180:
                    join_date_str = (date(2026, 7, 1) + timedelta(days=random.randint(2, 25))).strftime("%Y-%m-%d")
                    exit_date_str = None
                elif emp_idx <= 190:
                    join_date_str = (date(2026, 8, 1) + timedelta(days=random.randint(2, 25))).strftime("%Y-%m-%d")
                    exit_date_str = None
                elif emp_idx <= 195:
                    join_date_str = (date(2026, 9, 1) + timedelta(days=random.randint(2, 20))).strftime("%Y-%m-%d")
                    exit_date_str = None
                else:
                    join_date_str = "2024-03-01"
                    exit_date_str = (date(2026, 8, 1) + timedelta(days=random.randint(5, 50))).strftime("%Y-%m-%d")

                assigned_shift = shift_morning_id if emp_idx % 4 != 0 else shift_general_id

                if emp_idx <= 150: persona = 'punctual'
                elif emp_idx <= 170: persona = 'latecomer'
                elif emp_idx <= 185: persona = 'overtimer'
                elif emp_idx <= 195: persona = 'leave_taker'
                else: persona = 'absentee'

                employees.append({
                    'name': name,
                    'email': email_base,
                    'phone': phone,
                    'department': dept_name,
                    'position': pos,
                    'salary': sal,
                    'role': 'user' if emp_idx > 5 else 'admin',
                    'cnic': cnic,
                    'join_date': join_date_str,
                    'exit_date': exit_date_str,
                    'shift_id': assigned_shift,
                    'persona': persona
                })
                emp_idx += 1

        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            pwd = generate_password_hash(f"{clean_org_prefix.capitalize()}Emp2026!")
            now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for emp in employees:
                c.execute('''
                    INSERT INTO users (
                        name, email, phone, department, position, salary, role,
                        cnic, join_date, exit_date, shift_id, active, checkin_cameras, checkout_cameras,
                        organization_id, branch_id, face_trained, password_hash, mobile_login,
                        enrollment_date, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'all', 'all', ?, ?, 0, ?, 1, ?, ?)
                ''', (
                    emp['name'], emp['email'], emp['phone'], emp['department'], emp['position'],
                    emp['salary'], emp['role'], emp['cnic'], emp['join_date'], emp['exit_date'],
                    emp['shift_id'], org_id, branch_id, pwd, emp['join_date'], now_ts
                ))
                uid = c.lastrowid
                emp['id'] = uid

                basic = round(emp['salary'] * 0.7, 2)
                allowances = round(emp['salary'] * 0.3, 2)
                c.execute('''
                    INSERT INTO salary_configs (user_id, basic_salary, allowances, deductions, ot_rate, organization_id, branch_id)
                    VALUES (?, ?, ?, 0, 0, ?, ?)
                ''', (uid, basic, allowances, org_id, branch_id))

                c.execute('''
                    INSERT INTO employee_shift_history (
                        user_id, shift_id, effective_from, shift_name, start_time, end_time,
                        grace_period_minutes, working_days, history_source, organization_id, branch_id
                    ) VALUES (?, ?, ?, 'Morning Shift', '09:00', '17:00', 15, '[0, 1, 2, 3, 4, 5]', 'system_seed', ?, ?)
                ''', (uid, emp['shift_id'], emp['join_date'], org_id, branch_id))

            conn.commit()

    # -------------------------------------------------------------
    # 5. LOAD ALL USERS IN ORG & GUARANTEE SALARY CONFIGS
    # -------------------------------------------------------------
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, salary, join_date, exit_date FROM users WHERE organization_id = ?", (org_id,))
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
                ''', (uid, basic, allowances, org_id, branch_id))
            else:
                c.execute('''
                    UPDATE salary_configs SET basic_salary = ?, allowances = ?, organization_id = ?, branch_id = ?
                    WHERE user_id = ?
                ''', (basic, allowances, org_id, branch_id, uid))

        conn.commit()

    # -------------------------------------------------------------
    # 6. ATTENDANCE HISTORY (July 1 to September 30, 2026)
    # -------------------------------------------------------------
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM attendance WHERE organization_id = ?", (org_id,))
        att_cnt = c.fetchone()[0]

    if att_cnt < 5000:
        start_dt = date(2026, 7, 1)
        end_dt = date(2026, 9, 30)
        total_days = (end_dt - start_dt).days + 1

        attendance_inserts = []
        ot_inserts = []

        for day_offset in range(total_days):
            cur_date = start_dt + timedelta(days=day_offset)
            is_sunday = cur_date.weekday() == 6
            is_holiday = cur_date.isoformat() == '2026-08-14'

            for uid, uname, usal, ujdate, uedate in users_in_org:
                j_date = datetime.strptime(ujdate or '2024-01-01', "%Y-%m-%d").date()
                if cur_date < j_date:
                    continue
                if uedate:
                    e_date = datetime.strptime(uedate, "%Y-%m-%d").date()
                    if cur_date > e_date:
                        continue

                # Sunday / Holiday handling
                if is_sunday or is_holiday:
                    if (uid % 5 == 0) and random.random() < 0.25:
                        in_time = f"{cur_date} 09:{random.randint(10, 30):02d}:00"
                        out_time = f"{cur_date} 15:{random.randint(10, 45):02d}:00"
                        attendance_inserts.append((
                            uid, uname, 0.94, in_time, 'camera_entrance', 0.92,
                            'Reception', f"CAM-{org_id}-01", f"CAM-{org_id}-02", 'Lobby Exit Gate',
                            shift_morning_id, org_id, branch_id
                        ))
                        ot_inserts.append((
                            uid, uname, cur_date.strftime("%Y-%m-%d"), 6.0,
                            'Weekend Special Operations', 'Approved', admin_email,
                            f"{cur_date} 16:00:00", org_id, branch_id
                        ))
                    continue

                rand_val = random.random()
                if (uid % 15 == 0) and rand_val < 0.20:
                    continue  # Absent

                if (uid % 4 == 0) and rand_val < 0.30:
                    in_time = f"{cur_date} 09:{random.randint(18, 42):02d}:{random.randint(10, 55):02d}"
                else:
                    in_time = f"{cur_date} 08:{random.randint(45, 59):02d}:{random.randint(10, 55):02d}"

                if (uid % 7 == 0) and rand_val < 0.30:
                    ot_hrs = random.choice([2.0, 3.0, 4.0])
                    out_hour = 17 + int(ot_hrs)
                    out_time = f"{cur_date} {out_hour}:{random.randint(10, 45):02d}:00"
                    ot_inserts.append((
                        uid, uname, cur_date.strftime("%Y-%m-%d"), ot_hrs,
                        'Sprint Deployment Overtime', 'Approved', admin_email,
                        f"{cur_date} 17:30:00", org_id, branch_id
                    ))
                else:
                    out_time = f"{cur_date} 17:{random.randint(5, 30):02d}:{random.randint(10, 55):02d}"

                attendance_inserts.append((
                    uid, uname, round(random.uniform(0.88, 0.98), 2), in_time, 'camera_entrance',
                    round(random.uniform(0.85, 0.95), 2), 'Reception', f"CAM-{org_id}-01", f"CAM-{org_id}-02",
                    'Lobby Exit Gate', shift_morning_id, org_id, branch_id
                ))

        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.executemany('''
                INSERT INTO attendance (
                    user_id, detected_name, confidence, timestamp, source, frame_quality,
                    location, device_id, checkout_camera_id, checkout_location, shift_id,
                    organization_id, branch_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', attendance_inserts)

            c.executemany('''
                INSERT INTO overtime (
                    user_id, user_name, ot_date, hours, reason, status, approved_by,
                    created_at, organization_id, branch_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', ot_inserts)

            conn.commit()

    # -------------------------------------------------------------
    # 7. ASSIGN PAYROLL COMPONENTS, ADJUSTMENTS, ADVANCES, LOANS
    # -------------------------------------------------------------
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM payroll_employee_components WHERE organization_id = ?", (org_id,))
        c.execute("DELETE FROM payroll_adjustments WHERE organization_id = ?", (org_id,))
        c.execute("DELETE FROM payroll_bonuses WHERE organization_id = ?", (org_id,))
        c.execute("DELETE FROM salary_revisions WHERE organization_id = ?", (org_id,))

        comp_assignments = []
        now_date_str = "2026-01-01"

        for idx, (uid, uname, usal, ujdate, uedate) in enumerate(users_in_org):
            sal_val = float(usal or 50000.0)

            if "HRA" in comp_type_ids:
                comp_assignments.append((org_id, branch_id, uid, comp_type_ids["HRA"], 0, 40.0, now_date_str))
            if "MED" in comp_type_ids:
                comp_assignments.append((org_id, branch_id, uid, comp_type_ids["MED"], 0, 10.0, now_date_str))
            if "UTIL" in comp_type_ids:
                comp_assignments.append((org_id, branch_id, uid, comp_type_ids["UTIL"], 0, 10.0, now_date_str))
            if "CONV" in comp_type_ids:
                comp_assignments.append((org_id, branch_id, uid, comp_type_ids["CONV"], 5000.0, 0, now_date_str))
            if "PF" in comp_type_ids:
                comp_assignments.append((org_id, branch_id, uid, comp_type_ids["PF"], 0, 5.0, now_date_str))
            if "INS" in comp_type_ids:
                comp_assignments.append((org_id, branch_id, uid, comp_type_ids["INS"], 2500.0, 0, now_date_str))

            if sal_val > 85000 and "TAX" in comp_type_ids:
                tax_amount = round((sal_val - 85000) * 0.10, 2)
                comp_assignments.append((org_id, branch_id, uid, comp_type_ids["TAX"], tax_amount, 0, now_date_str))

            if idx < 20 and "LOAN" in comp_type_ids:
                comp_assignments.append((org_id, branch_id, uid, comp_type_ids["LOAN"], 8000.0, 0, now_date_str))

            if 20 <= idx < 45 and "ADV" in comp_type_ids:
                comp_assignments.append((org_id, branch_id, uid, comp_type_ids["ADV"], 10000.0, 0, now_date_str))

            if idx % 5 == 0:
                c.execute('''
                    INSERT INTO payroll_bonuses (organization_id, branch_id, year, month, user_id, amount, note, source)
                    VALUES (?, ?, 2026, 7, ?, 15000.0, 'Performance Milestone Bonus', 'excel')
                ''', (org_id, branch_id, uid))
                c.execute('''
                    INSERT INTO payroll_bonuses (organization_id, branch_id, year, month, user_id, amount, note, source)
                    VALUES (?, ?, 2026, 8, ?, 20000.0, 'Independence Day Project Bonus', 'excel')
                ''', (org_id, branch_id, uid))

            if idx % 4 == 0:
                c.execute('''
                    INSERT INTO payroll_adjustments (
                        organization_id, branch_id, user_id, payroll_month, adj_type, pay_mode, amount, note, created_by
                    ) VALUES (?, ?, ?, '2026-08', 'earning', 'fixed', 7500.0, 'Travel Reimbursement', ?)
                ''', (org_id, branch_id, uid, admin_email))
                c.execute('''
                    INSERT INTO payroll_adjustments (
                        organization_id, branch_id, user_id, payroll_month, adj_type, pay_mode, amount, note, created_by
                    ) VALUES (?, ?, ?, '2026-09', 'deduction', 'fixed', 1500.0, 'Cafeteria Charge Recovery', ?)
                ''', (org_id, branch_id, uid, admin_email))

            if idx < 25:
                revised_basic = round(sal_val * 0.75, 2)
                revised_allowances = round(sal_val * 0.25, 2)
                c.execute('''
                    INSERT INTO salary_revisions (
                        organization_id, branch_id, user_id, effective_from, basic_salary, allowances, deductions, ot_rate
                    ) VALUES (?, ?, ?, '2026-07-01', ?, ?, 0, 0)
                ''', (org_id, branch_id, uid, revised_basic, revised_allowances))

        c.executemany('''
            INSERT INTO payroll_employee_components (
                organization_id, branch_id, user_id, component_type_id, amount, percent, effective_from, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        ''', comp_assignments)

        conn.commit()

    # -------------------------------------------------------------
    # 8. DIVERSE LEAVE REQUESTS (ALL 7 LEAVE TYPES + MULTIPLE STATUSES)
    # -------------------------------------------------------------
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM leave_requests WHERE organization_id = ?", (org_id,))

        diverse_leave_types = [
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

        base_dates = [
            (date(2026, 7, 5), date(2026, 7, 6)),
            (date(2026, 7, 12), date(2026, 7, 12)),
            (date(2026, 7, 18), date(2026, 7, 22)),
            (date(2026, 7, 28), date(2026, 7, 28)),
            (date(2026, 8, 3), date(2026, 8, 4)),
            (date(2026, 8, 10), date(2026, 8, 10)),
            (date(2026, 8, 15), date(2026, 8, 19)),
            (date(2026, 8, 24), date(2026, 8, 25)),
            (date(2026, 9, 2), date(2026, 9, 2)),
            (date(2026, 9, 8), date(2026, 9, 9)),
            (date(2026, 9, 15), date(2026, 9, 18)),
            (date(2026, 9, 22), date(2026, 9, 22)),
            (date(2026, 9, 28), date(2026, 9, 29)),
            (date(2026, 10, 5), date(2026, 10, 5)),
            (date(2026, 10, 12), date(2026, 10, 13)),
        ]

        req_count = 0
        for uid, uname, usal, ujdate, uedate in users_in_org:
            if req_count >= 140:
                break

            chosen_lt_spec = diverse_leave_types[req_count % len(diverse_leave_types)]
            lt_name, lt_is_paid, reasons = chosen_lt_spec
            s_date, e_date = base_dates[req_count % len(base_dates)]

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
                reason, status, admin_email if status == 'Approved' else None,
                created_at, paid_d, unpaid_d, quota_charge,
                s_date.strftime("%Y-%m-%d") if lt_is_paid else None,
                e_date.strftime("%Y-%m-%d") if lt_is_paid else None,
                duration_str, None, quota_charge, org_id, branch_id,
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
    # 9. RE-CALCULATE AND FINALIZE MONTHLY PAYROLL WITH ALL 9 FEATURES
    # -------------------------------------------------------------
    for pmonth in ['2026-07', '2026-08', '2026-09']:
        try:
            prev = database.calculate_monthly_payroll(
                payroll_month=pmonth,
                organization_id=org_id,
                branch_id=branch_id,
                force_recalculate=True
            )
            database.finalize_monthly_payroll(
                preview=prev,
                organization_id=org_id,
                branch_id=branch_id,
                finalized_by=admin_email
            )
            print(f"[CloudSeed] Successfully finalized {pmonth} payroll for {org_id} with all 9 optional features!")
        except Exception as p_err:
            print(f"[CloudSeed] Payroll calculation note for {org_id} ({pmonth}): {p_err}")

if __name__ == '__main__':
    ensure_cloud_mercury_seeded(force=True)
