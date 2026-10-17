"""
Self-contained cloud seeding module for Mercury (ORG-708405).
Runs safely on Railway container startup if the employee count is under 200.
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

def ensure_cloud_mercury_seeded(db_path=None):
    # Only run in production runtime (e.g. Railway) or when SEED_CLOUD is set
    is_prod = (
        bool(os.environ.get('RAILWAY_ENVIRONMENT'))
        or os.environ.get('APP_ENV', '').lower() == 'production'
        or os.environ.get('FLASK_ENV', '').lower() == 'production'
        or bool(os.environ.get('SEED_CLOUD'))
    )
    if not is_prod:
        return

    # Never run during unit tests
    if 'unittest' in sys.modules or 'pytest' in sys.modules or os.environ.get('TESTING'):
        return

    if not db_path:
        db_path = os.environ.get('QINTELLECT_DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'attendance.db'))

    if not os.path.exists(db_path):
        return

    try:
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            try:
                c.execute("SELECT COUNT(*) FROM users WHERE organization_id = ?", (ORG_ID,))
                row = c.fetchone()
                existing_count = row[0] if row else 0
            except Exception:
                return

            if existing_count >= 200:
                return  # Already seeded, zero work needed

        print(f"[CloudSeed] Seeding 200 employees and 3 months history for {ORG_NAME} ({ORG_ID})...")
        _execute_seed(db_path)
        print(f"[CloudSeed] Seeding completed successfully for {ORG_ID}!")
    except Exception as e:
        print(f"[CloudSeed] Warning: Cloud seeding failed: {e}")

def _execute_seed(db_path):
    random.seed(42)
    import database

    # 1. Shifts, settings, leave types
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()

        # Payroll Settings
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
                0, 'multiplier', 1.5,
                1, 1, 1, 1,
                1, 1, 1, 1,
                1, 'full_salary', 'round_rate_first',
                'Mercury', 'DRIVING THE FUTURE TECHNOLOGY POWERED BY QUANTUM & AI', '03006782867', 'mercury@gmail.com',
                'mercury.com', 'Lahore, Pakistan', 'MS-708405', '© 2026 Mercury. All rights reserved.', 'Powered by QuantEye',
                'Mercury Pvt Ltd', 'Habib Bank Limited (HBL)', 'PK36HABB0001234567890123'
            )
        ''', (ORG_ID, BRANCH_ID))

        # Rules covering 1 to 31 days absence
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

        # Leave types
        leave_types_spec = [
            ('Casual Leave', 'casual', 10.0, 1, 1),
            ('Sick Leave', 'sick', 8.0, 1, 1),
            ('Annual / Paid Leave', 'annual', 14.0, 1, 0),
            ('Unpaid Leave (LWP)', 'unpaid', 0.0, 0, 1)
        ]
        for lt_name, lt_code, lt_allowance, lt_paid, lt_half in leave_types_spec:
            c.execute("SELECT id FROM leave_types WHERE organization_id = ? AND code = ?", (ORG_ID, lt_code))
            if not c.fetchone():
                c.execute('''
                    INSERT INTO leave_types (name, code, yearly_allowance, is_paid, description, active, allowance_period, half_leave_enabled, half_leaves_per_quota_day, organization_id, branch_id)
                    VALUES (?, ?, ?, ?, 'Policy Leave', 1, 'yearly', ?, 2, ?, ?)
                ''', (lt_name, lt_code, lt_allowance, lt_paid, lt_half, ORG_ID, BRANCH_ID))

        # Holidays
        c.execute("SELECT id FROM company_holidays WHERE organization_id = ? AND holiday_date = '2026-08-14'", (ORG_ID,))
        if not c.fetchone():
            c.execute('''
                INSERT INTO company_holidays (name, holiday_date, end_date, description, is_recurring, overtime_premium_percent, organization_id, branch_id)
                VALUES ('Independence Day', '2026-08-14', '2026-08-14', 'Independence Day', 1, 100.0, ?, ?)
            ''', (ORG_ID, BRANCH_ID))

        # Cameras
        c.execute("SELECT id FROM cameras WHERE organization_id = ? AND camera_id = 'CAM-708-01'", (ORG_ID,))
        if not c.fetchone():
            c.execute('''
                INSERT INTO cameras (camera_id, name, room_name, camera_type, rtsp_url, active, organization_id, branch_id)
                VALUES ('CAM-708-01', 'Entrance Turnstile', 'Reception', 'nvr', 'rtsp://192.168.10.10:554/ch1', 1, ?, ?)
            ''', (ORG_ID, BRANCH_ID))
        c.execute("SELECT id FROM cameras WHERE organization_id = ? AND camera_id = 'CAM-708-02'", (ORG_ID,))
        if not c.fetchone():
            c.execute('''
                INSERT INTO cameras (camera_id, name, room_name, camera_type, rtsp_url, active, organization_id, branch_id)
                VALUES ('CAM-708-02', 'Lobby Exit Gate', 'Exit Corridor', 'nvr', 'rtsp://192.168.10.11:554/ch2', 1, ?, ?)
            ''', (ORG_ID, BRANCH_ID))

        conn.commit()

    # 2. Build 200 Employees
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
        ("Mercury Engineering", ["Software Engineer", "Senior Backend Dev", "DevOps Engineer", "QA Engineer", "Frontend Specialist"], 45, 90000, 220000),
        ("Mercury Operations", ["Operations Lead", "Fleet Coordinator", "Dispatcher", "Inventory Specialist", "Ops Assistant"], 55, 45000, 110000),
        ("Mercury Commercial", ["Sales Manager", "Account Executive", "Marketing Specialist", "Business Development Lead"], 35, 60000, 160000),
        ("Mercury Support", ["Customer Support Rep", "Senior CS Specialist", "Support Team Lead", "Technical Helpdesk"], 30, 40000, 85000),
        ("Mercury Finance", ["Senior Accountant", "Payroll Analyst", "Billing Officer", "Internal Auditor"], 18, 65000, 175000),
        ("Mercury HR", ["HR Manager", "Talent Acquisition Specialist", "HR Officer", "People Ops Lead"], 17, 55000, 150000)
    ]

    employees = []
    used_emails = set()
    used_cnics = set()

    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        for r in c.execute("SELECT email, cnic FROM users WHERE organization_id = ?", (ORG_ID,)).fetchall():
            if r[0]: used_emails.add(r[0].lower())
            if r[1]: used_cnics.add(r[1])

    emp_idx = 1
    for dept_name, roles, target_count, min_sal, max_sal in depts:
        for _ in range(target_count):
            fn = random.choice(first_names)
            ln = random.choice(last_names)
            name = f"{fn} {ln}"
            pos = random.choice(roles)
            sal = float(round(random.randint(min_sal, max_sal) / 1000) * 1000)

            email_base = f"{fn.lower()}.{ln.lower()}{emp_idx}@mercury.com"
            while email_base in used_emails:
                emp_idx += 1
                email_base = f"{fn.lower()}.{ln.lower()}{emp_idx}@mercury.com"
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

    # Insert users
    user_records = []
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        pwd = generate_password_hash('MercuryEmp2026!')
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
                emp['shift_id'], ORG_ID, BRANCH_ID, pwd, emp['join_date'], now_ts
            ))
            uid = c.lastrowid
            emp['id'] = uid
            user_records.append(emp)

            basic = round(emp['salary'] * 0.7, 2)
            allowances = round(emp['salary'] * 0.3, 2)
            c.execute('''
                INSERT INTO salary_configs (user_id, basic_salary, allowances, deductions, ot_rate, organization_id, branch_id)
                VALUES (?, ?, ?, 0, 0, ?, ?)
            ''', (uid, basic, allowances, ORG_ID, BRANCH_ID))

            c.execute('''
                INSERT INTO employee_shift_history (
                    user_id, shift_id, effective_from, shift_name, start_time, end_time,
                    grace_period_minutes, working_days, history_source, organization_id, branch_id
                ) VALUES (?, ?, ?, 'Morning Shift', '09:00', '17:00', 15, '[0, 1, 2, 3, 4, 5]', 'system_seed', ?, ?)
            ''', (uid, emp['shift_id'], emp['join_date'], ORG_ID, BRANCH_ID))

        conn.commit()

    # 3. Attendance simulation (2026-07-01 to 2026-09-30)
    start_dt = date(2026, 7, 1)
    end_dt = date(2026, 9, 30)
    total_days = (end_dt - start_dt).days + 1

    attendance_inserts = []
    leave_inserts = []
    ot_inserts = []

    for day_offset in range(total_days):
        cur_date = start_dt + timedelta(days=day_offset)
        is_sunday = cur_date.weekday() == 6
        is_holiday = cur_date.isoformat() == '2026-08-14'

        for emp in user_records:
            j_date = datetime.strptime(emp['join_date'], "%Y-%m-%d").date()
            if cur_date < j_date:
                continue
            if emp['exit_date']:
                e_date = datetime.strptime(emp['exit_date'], "%Y-%m-%d").date()
                if cur_date > e_date:
                    continue

            persona = emp['persona']
            uid = emp['id']
            uname = emp['name']

            if is_sunday or is_holiday:
                if persona == 'overtimer' and random.random() < 0.25:
                    in_time = f"{cur_date} 09:{random.randint(10, 30):02d}:00"
                    out_time = f"{cur_date} 15:{random.randint(10, 45):02d}:00"
                    attendance_inserts.append((
                        uid, uname, 0.94, in_time, 'camera_entrance', 0.92,
                        'Reception', 'CAM-708-01', 'CAM-708-02', 'Lobby Exit Gate',
                        emp['shift_id'], ORG_ID, BRANCH_ID
                    ))
                    ot_inserts.append((
                        uid, uname, cur_date.strftime("%Y-%m-%d"), 6.0,
                        'Weekend Special Operations', 'Approved', 'usman@gmail.com',
                        f"{cur_date} 16:00:00", ORG_ID, BRANCH_ID
                    ))
                continue

            rand_val = random.random()
            if persona == 'absentee' and rand_val < 0.20:
                continue

            if persona == 'leave_taker' and rand_val < 0.15:
                l_type = 'half' if random.random() < 0.4 else 'full'
                if l_type == 'half':
                    in_time = f"{cur_date} 08:50:00"
                    out_time = f"{cur_date} 13:05:00"
                    attendance_inserts.append((
                        uid, uname, 0.91, in_time, 'camera_entrance', 0.88,
                        'Reception', 'CAM-708-01', 'CAM-708-02', 'Lobby Exit Gate',
                        emp['shift_id'], ORG_ID, BRANCH_ID
                    ))
                    leave_inserts.append((
                        uid, uname, 'Casual Leave', cur_date.strftime("%Y-%m-%d"), cur_date.strftime("%Y-%m-%d"),
                        'Personal urgent work half day', 'Approved', 'usman@gmail.com', f"{cur_date} 08:00:00",
                        0.5, 0.0, 0.5, cur_date.strftime("%Y-%m-%d"), cur_date.strftime("%Y-%m-%d"),
                        'half_day', None, 0.5, ORG_ID, BRANCH_ID, 'paid', 'unpaid'
                    ))
                else:
                    leave_inserts.append((
                        uid, uname, 'Casual Leave', cur_date.strftime("%Y-%m-%d"), cur_date.strftime("%Y-%m-%d"),
                        'Family function full day', 'Approved', 'usman@gmail.com', f"{cur_date} 08:00:00",
                        1.0, 0.0, 1.0, cur_date.strftime("%Y-%m-%d"), cur_date.strftime("%Y-%m-%d"),
                        'full_day', None, 1.0, ORG_ID, BRANCH_ID, 'paid', 'unpaid'
                    ))
                continue

            if persona == 'latecomer' and rand_val < 0.35:
                in_time = f"{cur_date} 09:{random.randint(18, 42):02d}:{random.randint(10, 55):02d}"
            else:
                in_time = f"{cur_date} 08:{random.randint(45, 59):02d}:{random.randint(10, 55):02d}"

            if persona == 'overtimer' and rand_val < 0.30:
                ot_hrs = random.choice([2.0, 3.0, 4.0])
                out_hour = 17 + int(ot_hrs)
                out_time = f"{cur_date} {out_hour}:{random.randint(10, 45):02d}:00"
                ot_inserts.append((
                    uid, uname, cur_date.strftime("%Y-%m-%d"), ot_hrs,
                    'Production Sprint Overtime', 'Approved', 'usman@gmail.com',
                    f"{cur_date} 17:30:00", ORG_ID, BRANCH_ID
                ))
            else:
                out_time = f"{cur_date} 17:{random.randint(5, 30):02d}:{random.randint(10, 55):02d}"

            attendance_inserts.append((
                uid, uname, round(random.uniform(0.88, 0.98), 2), in_time, 'camera_entrance',
                round(random.uniform(0.85, 0.95), 2), 'Reception', 'CAM-708-01', 'CAM-708-02',
                'Lobby Exit Gate', emp['shift_id'], ORG_ID, BRANCH_ID
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
            INSERT INTO leave_requests (
                user_id, user_name, leave_type, start_date, end_date, reason, status, approved_by,
                created_at, paid_days, unpaid_days, quota_days, paid_through_date, quota_through_date,
                leave_duration, half_leave_quota_type_id, half_leave_quota_charge, organization_id, branch_id,
                paid_status, unpaid_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', leave_inserts)

        c.executemany('''
            INSERT INTO overtime (
                user_id, user_name, ot_date, hours, reason, status, approved_by,
                created_at, organization_id, branch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', ot_inserts)

        conn.commit()

    # 4. Finalize monthly payroll for 2026-07, 2026-08, 2026-09
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
        except Exception as p_err:
            print(f"[CloudSeed] Payroll error for {pmonth}: {p_err}")
