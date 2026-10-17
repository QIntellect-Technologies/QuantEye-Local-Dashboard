"""
Self-contained cloud seeding module for organizations:
- Mercury (ORG-708405 / BR-2174CB75459E453B)
- Hello World (ORG-873824 / BR-891820B1BF4A4030)

Features End-to-End Workforce Management:
1. Real isolated cameras (Entrance Turnstile & Lobby Exit Gate)
2. Real departments and designations in settings and employees
3. Real Check-In and Check-Out attendance punches for 3 months (July 1 to September 30, 2026)
4. Connected leave requests, half-day leaves, absences, and overtime logs
5. All 9 optional payroll features (HRA, Med, Util, Conv, Tax, Loans, Advances, PF, Ins, Bonuses, Adjustments)
6. Autorun dynamic monthly payroll calculations directly derived from attendance, leaves & overtime
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
    is_prod = (
        force
        or bool(os.environ.get('RAILWAY_ENVIRONMENT'))
        or os.environ.get('APP_ENV', '').lower() == 'production'
        or os.environ.get('FLASK_ENV', '').lower() == 'production'
        or bool(os.environ.get('SEED_CLOUD'))
    )
    if not is_prod:
        return

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
            with sqlite3.connect(db_path, timeout=60.0) as conn:
                conn.execute("PRAGMA busy_timeout = 60000")
                c = conn.cursor()
                try:
                    c.execute("SELECT COUNT(*) FROM departments WHERE organization_id = ?", (org_id,))
                    dept_count = c.fetchone()[0]

                    c.execute("SELECT COUNT(*) FROM designations WHERE organization_id = ?", (org_id,))
                    desig_count = c.fetchone()[0]

                    c.execute("SELECT COUNT(*) FROM attendance WHERE organization_id = ? AND timestamp >= '2026-10-01'", (org_id,))
                    oct_count = c.fetchone()[0]
                except Exception:
                    continue

                # If departments, designations and October attendance exist, skip
                if not force and dept_count >= 5 and desig_count >= 15 and oct_count >= 2000:
                    continue

            print(f"[CloudSeed] Running End-to-End Workforce Seeding for {org_name} ({org_id})...")
            _execute_seed_for_org(db_path, target)
            print(f"[CloudSeed] End-to-End Workforce Seeding completed successfully for {org_id}!")
        except Exception as e:
            print(f"[CloudSeed] Error during seeding for {org_id}: {e}")

def _execute_seed_for_org(db_path, target):
    org_id = target['org_id']
    branch_id = target['branch_id']
    org_name = target['org_name']
    domain = target['domain']
    admin_email = target['admin_email']

    random.seed(42)
    import database
    import payroll_extensions

    with sqlite3.connect(db_path, timeout=60.0) as conn:
        c = conn.cursor()
        payroll_extensions.ensure_payroll_extension_tables(c)

        # -------------------------------------------------------------
        # 1. ISOLATED CAMERAS
        # -------------------------------------------------------------
        c.execute("DELETE FROM cameras WHERE organization_id = ?", (org_id,))
        ent_cam_id = f"CAM-{org_id}-ENT"
        ext_cam_id = f"CAM-{org_id}-EXT"
        c.execute('''
            INSERT INTO cameras (camera_id, name, room_name, camera_type, rtsp_url, active, organization_id, branch_id)
            VALUES (?, 'Main Entrance Turnstile', 'Reception Lobby', 'nvr', 'rtsp://192.168.10.10:554/ch1', 1, ?, ?)
        ''', (ent_cam_id, org_id, branch_id))
        c.execute('''
            INSERT INTO cameras (camera_id, name, room_name, camera_type, rtsp_url, active, organization_id, branch_id)
            VALUES (?, 'Lobby Exit Gate', 'Exit Corridor', 'nvr', 'rtsp://192.168.10.11:554/ch2', 1, ?, ?)
        ''', (ext_cam_id, org_id, branch_id))

        # -------------------------------------------------------------
        # 2. DEPARTMENTS & DESIGNATIONS
        # -------------------------------------------------------------
        c.execute("DELETE FROM designations WHERE organization_id = ?", (org_id,))
        c.execute("DELETE FROM departments WHERE organization_id = ?", (org_id,))

        dept_desig_specs = [
            ("Core Engineering" if org_id == 'ORG-873824' else "Mercury Engineering", "ENG", "Software and AI Engineering", [
                ("Software Engineer", "Core Software Engineering"),
                ("Senior Backend Dev", "Backend Architecture & Systems"),
                ("DevOps Engineer", "Cloud Infrastructure & CI/CD"),
                ("QA Engineer", "Quality Assurance & Automated Testing"),
                ("Frontend Specialist", "User Interface & Experience")
            ]),
            ("Operations & Logistics" if org_id == 'ORG-873824' else "Mercury Operations", "OPS", "Fleet, Supply & Operations", [
                ("Operations Lead", "Operations Supervision"),
                ("Fleet Coordinator", "Fleet Dispatch & Route Planning"),
                ("Dispatcher", "Daily Delivery Dispatch"),
                ("Inventory Specialist", "Stock & Warehouse Tracking"),
                ("Ops Assistant", "Operational Coordination")
            ]),
            ("Sales & Commercial" if org_id == 'ORG-873824' else "Mercury Commercial", "COM", "Commercial Sales & BD", [
                ("Sales Manager", "Regional Sales Management"),
                ("Account Executive", "Client Relationship & Retention"),
                ("Marketing Specialist", "Campaigns & Growth"),
                ("Business Development Lead", "Strategic Enterprise Partnerships")
            ]),
            ("Customer Experience" if org_id == 'ORG-873824' else "Mercury Support", "SUP", "Customer Support & Solutions", [
                ("Customer Support Rep", "Customer Helpline & Assistance"),
                ("Senior CS Specialist", "Escalations & Resolution"),
                ("Support Team Lead", "Support Team Leadership"),
                ("Technical Helpdesk", "IT Support & Helpdesk")
            ]),
            ("Finance & Accounts" if org_id == 'ORG-873824' else "Mercury Finance", "FIN", "Corporate Finance & Payroll", [
                ("Senior Accountant", "General Ledger & Reporting"),
                ("Payroll Analyst", "Payroll Operations & Audit"),
                ("Billing Officer", "Invoicing & Receivables"),
                ("Internal Auditor", "Compliance & Internal Controls")
            ]),
            ("Human Resources" if org_id == 'ORG-873824' else "Mercury HR", "HR", "People & Culture Operations", [
                ("HR Manager", "People Operations & HR Strategy"),
                ("Talent Acquisition Specialist", "Recruitment & Sourcing"),
                ("HR Officer", "Employee Relations & Records"),
                ("People Ops Lead", "Workplace Culture & Benefits")
            ]),
        ]
        if org_id == 'ORG-873824':
            dept_desig_specs.append(
                ("Administration", "ADM", "Executive Administration", [
                    ("General Manager", "General Administration"),
                    ("Executive Officer", "Executive Affairs"),
                    ("Administrative Lead", "Office Administration")
                ])
            )
            dept_desig_specs.append(
                ("Finanace", "FIN-LEGACY", "Finance Legacy", [
                    ("Finance Manager", "Financial Planning")
                ])
            )

        dept_id_map = {}
        desig_by_dept = {}
        for d_name, d_code, d_desc, desigs in dept_desig_specs:
            c.execute("SELECT id FROM departments WHERE name = ?", (d_name,))
            row_d = c.fetchone()
            if row_d:
                did = row_d[0]
                c.execute('''
                    UPDATE departments SET code = ?, description = ?, active = 1, organization_id = ?, branch_id = ?
                    WHERE id = ?
                ''', (d_code, d_desc, org_id, branch_id, did))
            else:
                c.execute('''
                    INSERT INTO departments (name, code, description, active, organization_id, branch_id)
                    VALUES (?, ?, ?, 1, ?, ?)
                ''', (d_name, d_code, d_desc, org_id, branch_id))
                did = c.lastrowid
            dept_id_map[d_name] = did
            desig_by_dept[d_name] = []
            for des_name, des_desc in desigs:
                c.execute('''
                    INSERT INTO designations (department_id, title, description, active, organization_id, branch_id)
                    VALUES (?, ?, ?, 1, ?, ?)
                ''', (did, des_name, des_desc, org_id, branch_id))
                desig_by_dept[d_name].append(des_name)

        # -------------------------------------------------------------
        # 3. SHIFTS
        # -------------------------------------------------------------
        shift_morning_name = f"{org_name} ({org_id}) Morning Shift"
        c.execute("SELECT id FROM shifts WHERE name = ?", (shift_morning_name,))
        row_sm = c.fetchone()
        if row_sm:
            shift_morning_id = row_sm[0]
            c.execute('''
                UPDATE shifts SET start_time = '09:00', end_time = '17:00', grace_period_minutes = 15, active = 1, is_default = 1, working_days = '[0, 1, 2, 3, 4, 5]', checkout_grace_period_minutes = 15, organization_id = ?, branch_id = ?
                WHERE id = ?
            ''', (org_id, branch_id, shift_morning_id))
        else:
            c.execute('''
                INSERT INTO shifts (name, start_time, end_time, grace_period_minutes, active, is_default, working_days, checkout_grace_period_minutes, organization_id, branch_id)
                VALUES (?, '09:00', '17:00', 15, 1, 1, '[0, 1, 2, 3, 4, 5]', 15, ?, ?)
            ''', (shift_morning_name, org_id, branch_id))
            shift_morning_id = c.lastrowid

        shift_general_name = f"{org_name} ({org_id}) General Shift"
        c.execute("SELECT id FROM shifts WHERE name = ?", (shift_general_name,))
        row_sg = c.fetchone()
        if row_sg:
            shift_general_id = row_sg[0]
            c.execute('''
                UPDATE shifts SET start_time = '09:00', end_time = '18:00', grace_period_minutes = 15, active = 1, is_default = 0, working_days = '[0, 1, 2, 3, 4, 5]', checkout_grace_period_minutes = 15, organization_id = ?, branch_id = ?
                WHERE id = ?
            ''', (org_id, branch_id, shift_general_id))
        else:
            c.execute('''
                INSERT INTO shifts (name, start_time, end_time, grace_period_minutes, active, is_default, working_days, checkout_grace_period_minutes, organization_id, branch_id)
                VALUES (?, '09:00', '18:00', 15, 1, 0, '[0, 1, 2, 3, 4, 5]', 15, ?, ?)
            ''', (shift_general_name, org_id, branch_id))
            shift_general_id = c.lastrowid

        # -------------------------------------------------------------
        # 4. ALL 7 LEAVE TYPES
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
            c.execute("SELECT id FROM leave_types WHERE organization_id = ? AND (code = ? OR name = ?)", (org_id, lt_code, lt_name))
            row_lt = c.fetchone()
            if row_lt:
                c.execute('''
                    UPDATE leave_types SET name = ?, code = ?, yearly_allowance = ?, is_paid = ?, half_leave_enabled = 1, description = ?
                    WHERE id = ?
                ''', (lt_name, lt_code, lt_allowance, lt_paid, lt_desc, row_lt[0]))
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

        # -------------------------------------------------------------
        # 5. PAYROLL SETTINGS (All 9 Optional Features Enabled)
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

        # -------------------------------------------------------------
        # 6. PAYROLL EXTENSION CATALOG
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
            c.execute("SELECT id FROM payroll_component_types WHERE organization_id = ? AND (code = ? OR name = ?)", (org_id, c_code, c_name))
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
    # 7. EMPLOYEES: BUILD OR MAP TO DEPARTMENTS & DESIGNATIONS
    # -------------------------------------------------------------
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE organization_id = ?", (org_id,))
        current_emp_count = c.fetchone()[0]

    # Pre-populate 200 realistic employees if under 200
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

        main_depts = [
            ("Core Engineering" if org_id == 'ORG-873824' else "Mercury Engineering", 45, 90000, 220000),
            ("Operations & Logistics" if org_id == 'ORG-873824' else "Mercury Operations", 55, 45000, 110000),
            ("Sales & Commercial" if org_id == 'ORG-873824' else "Mercury Commercial", 35, 60000, 160000),
            ("Customer Experience" if org_id == 'ORG-873824' else "Mercury Support", 30, 40000, 85000),
            ("Finance & Accounts" if org_id == 'ORG-873824' else "Mercury Finance", 18, 65000, 175000),
            ("Human Resources" if org_id == 'ORG-873824' else "Mercury HR", 17, 55000, 150000)
        ]

        used_emails = set()
        used_cnics = set()

        with sqlite3.connect(db_path, timeout=60.0) as conn:
            c = conn.cursor()
            for r in c.execute("SELECT email, cnic FROM users WHERE organization_id = ?", (org_id,)).fetchall():
                if r[0]: used_emails.add(r[0].lower())
                if r[1]: used_cnics.add(r[1])

        emp_idx = 1
        employees_to_insert = []
        for dept_name, target_count, min_sal, max_sal in main_depts:
            avail_desigs = desig_by_dept.get(dept_name, ["Associate Specialist"])
            for _ in range(target_count):
                fn = random.choice(first_names)
                ln = random.choice(last_names)
                name = f"{fn} {ln}"
                pos = random.choice(avail_desigs)
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

                employees_to_insert.append({
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

        with sqlite3.connect(db_path, timeout=60.0) as conn:
            c = conn.cursor()
            pwd = generate_password_hash(f"{clean_org_prefix.capitalize()}Emp2026!")
            now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for emp in employees_to_insert:
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
                    ) VALUES (?, ?, ?, ?, '09:00', '17:00', 15, '[0, 1, 2, 3, 4, 5]', 'system_seed', ?, ?)
                ''', (uid, emp['shift_id'], emp['join_date'], shift_morning_name, org_id, branch_id))

            conn.commit()

    # Align ALL existing and new users with valid departments, designations, and salary_configs
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, salary, join_date, exit_date, department, position FROM users WHERE organization_id = ?", (org_id,))
        all_users = c.fetchall()

        valid_dept_names = list(dept_id_map.keys())
        for uid, uname, usal, ujdate, uedate, udept, upos in all_users:
            # Ensure valid department and position
            fixed_dept = udept
            if fixed_dept not in valid_dept_names:
                fixed_dept = "Administration" if "admin" in str(udept).lower() else valid_dept_names[0]
            
            avail_pos = desig_by_dept.get(fixed_dept, ["General Executive"])
            fixed_pos = upos if upos in avail_pos else avail_pos[0]

            sal_val = float(usal or 50000.0)
            if sal_val <= 0: sal_val = 50000.0

            c.execute("UPDATE users SET department = ?, position = ?, salary = ?, shift_id = ? WHERE id = ?", (fixed_dept, fixed_pos, sal_val, shift_morning_id, uid))

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
    # 8. ASSIGN ALL 9 PAYROLL EXTENSION COMPONENTS & ADJUSTMENTS
    # -------------------------------------------------------------
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM payroll_employee_components WHERE organization_id = ?", (org_id,))
        c.execute("DELETE FROM payroll_adjustments WHERE organization_id = ?", (org_id,))
        c.execute("DELETE FROM payroll_bonuses WHERE organization_id = ?", (org_id,))
        c.execute("DELETE FROM salary_revisions WHERE organization_id = ?", (org_id,))

        c.execute("SELECT id, name, salary FROM users WHERE organization_id = ?", (org_id,))
        org_users = c.fetchall()

        comp_assignments = []
        now_date_str = "2026-01-01"

        for idx, (uid, uname, usal) in enumerate(org_users):
            sal_val = float(usal or 50000.0)

            # Core allowances
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
                    VALUES (?, ?, 2026, 7, ?, 15000.0, 'Quarterly Delivery Bonus', 'excel')
                ''', (org_id, branch_id, uid))
                c.execute('''
                    INSERT INTO payroll_bonuses (organization_id, branch_id, year, month, user_id, amount, note, source)
                    VALUES (?, ?, 2026, 8, ?, 20000.0, 'Project Milestone Bonus', 'excel')
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
    # 9. 3 MONTHS REAL ATTENDANCE (PROPER CHECK-IN & CHECK-OUT)
    # -------------------------------------------------------------
    with sqlite3.connect(db_path, timeout=60.0) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM attendance WHERE organization_id = ?", (org_id,))
        c.execute("DELETE FROM overtime WHERE organization_id = ?", (org_id,))
        c.execute("DELETE FROM leave_requests WHERE organization_id = ?", (org_id,))

        c.execute("SELECT id, name, join_date, exit_date FROM users WHERE organization_id = ?", (org_id,))
        user_list = c.fetchall()

        start_dt = date(2026, 7, 1)
        end_dt = max(date(2026, 10, 17), datetime.now().date())
        total_days = (end_dt - start_dt).days + 1

        attendance_rows = []
        overtime_rows = []
        leave_rows = []

        leave_categories = [
            ('Casual Leave', 1, 'Family wedding ceremony'),
            ('Sick Leave', 1, 'Seasonal viral flu and recovery'),
            ('Annual / Paid Leave', 1, 'Annual family vacation trip'),
            ('Unpaid Leave (LWP)', 0, 'Personal urgent matters beyond quota'),
            ('Emergency Leave', 1, 'Urgent household medical emergency'),
            ('Bereavement Leave', 1, 'Condolence gathering for close relative')
        ]

        for day_offset in range(total_days):
            cur_date = start_dt + timedelta(days=day_offset)
            cur_date_str = cur_date.strftime("%Y-%m-%d")
            is_sunday = cur_date.weekday() == 6
            is_holiday = (cur_date_str == '2026-08-14')

            for idx, (uid, uname, ujdate, uedate) in enumerate(user_list):
                j_date = datetime.strptime(ujdate or '2024-01-01', "%Y-%m-%d").date()
                if cur_date < j_date: continue
                if uedate:
                    e_date = datetime.strptime(uedate, "%Y-%m-%d").date()
                    if cur_date > e_date: continue

                # Sunday or Holiday: Weekend OT for selected team members
                if is_sunday or is_holiday:
                    if (idx % 6 == 0) and (day_offset % 3 == 0):
                        cin_ts = f"{cur_date_str} 09:{random.randint(10, 25):02d}:00"
                        cout_ts = f"{cur_date_str} 15:{random.randint(15, 45):02d}:00"
                        attendance_rows.append((
                            uid, uname, 0.95, cin_ts, 'camera_entrance', 0.92,
                            'Main Entrance Turnstile', ent_cam_id, ext_cam_id, 'Lobby Exit Gate',
                            shift_morning_id, org_id, branch_id
                        ))
                        attendance_rows.append((
                            uid, uname, 0.94, cout_ts, 'camera_exit', 0.91,
                            'Lobby Exit Gate', ext_cam_id, ext_cam_id, 'Lobby Exit Gate',
                            shift_morning_id, org_id, branch_id
                        ))
                        overtime_rows.append((
                            uid, uname, cur_date_str, 6.0,
                            'Weekend Special Deployment', 'Approved', admin_email,
                            f"{cur_date_str} 16:00:00", org_id, branch_id
                        ))
                    continue

                # Working day: Mon - Sat
                # Pattern based on index and day
                status_hash = (idx * 31 + day_offset) % 100

                # 1. Unapproved Absentee (3% of logs) -> NO attendance punch, NO leave
                if status_hash < 3:
                    continue

                # 2. Approved Full-day Leave (4% of logs) -> NO attendance punch, YES leave_requests
                if 3 <= status_hash < 7:
                    lt_spec = leave_categories[(idx + day_offset) % len(leave_categories)]
                    lt_name, lt_is_paid, lt_reason = lt_spec
                    leave_rows.append((
                        uid, uname, lt_name, cur_date_str, cur_date_str,
                        lt_reason, 'Approved', admin_email,
                        f"{cur_date_str} 08:00:00",
                        1.0 if lt_is_paid else 0.0,
                        0.0 if lt_is_paid else 1.0,
                        1.0, cur_date_str if lt_is_paid else None, cur_date_str if lt_is_paid else None,
                        'full_day', None, 1.0, org_id, branch_id,
                        'paid' if lt_is_paid else 'unpaid',
                        'unpaid' if not lt_is_paid else 'paid'
                    ))
                    continue

                # 3. Approved Half-day Leave (3% of logs) -> Check-In 08:52, Early Check-Out 13:00
                if 7 <= status_hash < 10:
                    cin_ts = f"{cur_date_str} 08:{random.randint(48, 56):02d}:00"
                    cout_ts = f"{cur_date_str} 13:{random.randint(5, 20):02d}:00"
                    attendance_rows.append((
                        uid, uname, 0.92, cin_ts, 'camera_entrance', 0.90,
                        'Main Entrance Turnstile', ent_cam_id, ext_cam_id, 'Lobby Exit Gate',
                        shift_morning_id, org_id, branch_id
                    ))
                    attendance_rows.append((
                        uid, uname, 0.91, cout_ts, 'camera_exit', 0.89,
                        'Lobby Exit Gate', ext_cam_id, ext_cam_id, 'Lobby Exit Gate',
                        shift_morning_id, org_id, branch_id
                    ))
                    leave_rows.append((
                        uid, uname, 'Casual Leave', cur_date_str, cur_date_str,
                        'Personal urgent task half day', 'Approved', admin_email,
                        f"{cur_date_str} 08:00:00", 0.5, 0.0, 0.5, cur_date_str, cur_date_str,
                        'half_day', None, 0.5, org_id, branch_id, 'paid', 'unpaid'
                    ))
                    continue

                # 4. Overtime day (12% of logs) -> Standard Check-In + Late Check-Out (19:30 or 20:00) + Approved OT
                if 10 <= status_hash < 22:
                    cin_ts = f"{cur_date_str} 08:{random.randint(45, 58):02d}:00"
                    ot_hrs = 2.5 if (status_hash % 2 == 0) else 3.0
                    cout_hour = 17 + int(ot_hrs)
                    cout_min = 30 if ot_hrs == 2.5 else random.randint(10, 25)
                    cout_ts = f"{cur_date_str} {cout_hour:02d}:{cout_min:02d}:00"

                    attendance_rows.append((
                        uid, uname, 0.96, cin_ts, 'camera_entrance', 0.94,
                        'Main Entrance Turnstile', ent_cam_id, ext_cam_id, 'Lobby Exit Gate',
                        shift_morning_id, org_id, branch_id
                    ))
                    attendance_rows.append((
                        uid, uname, 0.95, cout_ts, 'camera_exit', 0.93,
                        'Lobby Exit Gate', ext_cam_id, ext_cam_id, 'Lobby Exit Gate',
                        shift_morning_id, org_id, branch_id
                    ))
                    overtime_rows.append((
                        uid, uname, cur_date_str, ot_hrs,
                        'Production Sprint & Release', 'Approved', admin_email,
                        f"{cur_date_str} 17:30:00", org_id, branch_id
                    ))
                    continue

                # 5. Late Arrival (8% of logs) -> Check-In 09:18 to 09:42 + Standard Check-Out
                if 22 <= status_hash < 30:
                    cin_ts = f"{cur_date_str} 09:{random.randint(18, 42):02d}:{random.randint(10, 55):02d}"
                    cout_ts = f"{cur_date_str} 17:{random.randint(10, 30):02d}:00"
                    attendance_rows.append((
                        uid, uname, 0.93, cin_ts, 'camera_entrance', 0.91,
                        'Main Entrance Turnstile', ent_cam_id, ext_cam_id, 'Lobby Exit Gate',
                        shift_morning_id, org_id, branch_id
                    ))
                    attendance_rows.append((
                        uid, uname, 0.94, cout_ts, 'camera_exit', 0.92,
                        'Lobby Exit Gate', ext_cam_id, ext_cam_id, 'Lobby Exit Gate',
                        shift_morning_id, org_id, branch_id
                    ))
                    continue

                # 6. Standard Punctual Present (70% of logs) -> Check-In 08:45-08:58 + Check-Out 17:05-17:25
                cin_ts = f"{cur_date_str} 08:{random.randint(45, 58):02d}:{random.randint(10, 55):02d}"
                cout_ts = f"{cur_date_str} 17:{random.randint(5, 25):02d}:{random.randint(10, 55):02d}"
                attendance_rows.append((
                    uid, uname, round(random.uniform(0.91, 0.98), 2), cin_ts, 'camera_entrance',
                    round(random.uniform(0.88, 0.95), 2), 'Main Entrance Turnstile', ent_cam_id,
                    ext_cam_id, 'Lobby Exit Gate', shift_morning_id, org_id, branch_id
                ))
                attendance_rows.append((
                    uid, uname, round(random.uniform(0.90, 0.97), 2), cout_ts, 'camera_exit',
                    round(random.uniform(0.87, 0.94), 2), 'Lobby Exit Gate', ext_cam_id,
                    ext_cam_id, 'Lobby Exit Gate', shift_morning_id, org_id, branch_id
                ))

        # Insert batch
        c.executemany('''
            INSERT INTO attendance (
                user_id, detected_name, confidence, timestamp, source, frame_quality,
                location, device_id, checkout_camera_id, checkout_location, shift_id,
                organization_id, branch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', attendance_rows)

        c.executemany('''
            INSERT INTO overtime (
                user_id, user_name, ot_date, hours, reason, status, approved_by,
                created_at, organization_id, branch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', overtime_rows)

        c.executemany('''
            INSERT INTO leave_requests (
                user_id, user_name, leave_type, start_date, end_date, reason, status, approved_by,
                created_at, paid_days, unpaid_days, quota_days, paid_through_date, quota_through_date,
                leave_duration, half_leave_quota_type_id, half_leave_quota_charge, organization_id, branch_id,
                paid_status, unpaid_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', leave_rows)

        conn.commit()

    # -------------------------------------------------------------
    # 10. RE-CALCULATE AND FINALIZE MONTHLY PAYROLL
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
            print(f"[CloudSeed] Successfully finalized {pmonth} dynamic payroll for {org_id}!")
        except Exception as p_err:
            print(f"[CloudSeed] Payroll calculation note for {org_id} ({pmonth}): {p_err}")

    # Pre-calculate 2026-10 draft payroll so opening October payroll in the UI is instantaneous
    try:
        database.calculate_monthly_payroll(
            payroll_month='2026-10',
            organization_id=org_id,
            branch_id=branch_id,
            force_recalculate=True
        )
        print(f"[CloudSeed] Successfully pre-calculated 2026-10 payroll for {org_id}!")
    except Exception as oct_err:
        print(f"[CloudSeed] 2026-10 calculation note for {org_id}: {oct_err}")

if __name__ == '__main__':
    ensure_cloud_mercury_seeded(force=True)
