"""
Phase 1 Tenant Isolation Security Verification Suite
Tests 10 Security Verification Dimensions:
1. Header Tampering
2. Cross-Tenant API Access
3. Direct ID Manipulation
4. Branch Isolation
5. Biometric Isolation
6. Camera Isolation
7. Write-Operation Isolation
8. Session / Auth Security
9. Regression Testing (Standalone DEFAULT_ORG)
10. Database Integrity Check
"""

import sys
import os
import json
import sqlite3
import numpy as np
from datetime import date

# Ensure path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import database as db
import app as app_module
from app import app, refresh_embedding_cache, match_embedding_vectorized

results = []

def record(test_name, dimension, passed, details=""):
    results.append({
        "test": test_name,
        "dimension": dimension,
        "passed": passed,
        "details": details
    })
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] [{dimension}] {test_name}: {details}")

def setup_test_data():
    print("\n--- Setting up Multi-Tenant Test Fixtures ---")
    with sqlite3.connect(db.DB_PATH) as conn:
        cursor = conn.cursor()
        
        # 1. Clean existing test entities
        cursor.execute("DELETE FROM attendance WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA')")
        cursor.execute("DELETE FROM overtime WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA')")
        cursor.execute("DELETE FROM company_holidays WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA')")
        cursor.execute("DELETE FROM leave_requests WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA')")
        cursor.execute("DELETE FROM embeddings WHERE user_id IN (SELECT id FROM users WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR name IN ('Alice Alpha1', 'Aaron Alpha2', 'Bob Beta1', 'Trojan User'))")
        cursor.execute("DELETE FROM employee_shift_history WHERE user_id IN (SELECT id FROM users WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR name IN ('Alice Alpha1', 'Aaron Alpha2', 'Bob Beta1', 'Trojan User'))")
        cursor.execute("DELETE FROM users WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR name IN ('Alice Alpha1', 'Aaron Alpha2', 'Bob Beta1', 'Trojan User')")
        cursor.execute("DELETE FROM departments WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR name IN ('Alpha Engineering', 'Alpha Operations', 'Beta Engineering')")
        cursor.execute("DELETE FROM designations WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR title IN ('Alpha Senior Dev', 'Beta Senior Dev')")
        cursor.execute("DELETE FROM shifts WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR name IN ('Alpha Morning', 'Beta Morning')")
        cursor.execute("DELETE FROM cameras WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR camera_id IN ('cam_alpha_1', 'cam_beta_1')")
        conn.commit()

    # 2. Departments
    dept_a1 = db.add_department("Alpha Engineering", "ENG-A", "Alpha Eng", organization_id="ORG-ALPHA", branch_id="BR-ALPHA-1")
    dept_a2 = db.add_department("Alpha Operations", "OPS-A", "Alpha Ops", organization_id="ORG-ALPHA", branch_id="BR-ALPHA-2")
    dept_b1 = db.add_department("Beta Engineering", "ENG-B", "Beta Eng", organization_id="ORG-BETA", branch_id="BR-BETA-1")

    # 3. Designations
    desig_a1 = db.add_designation(dept_a1, "Alpha Senior Dev", "Sr Dev", organization_id="ORG-ALPHA", branch_id="BR-ALPHA-1")
    desig_b1 = db.add_designation(dept_b1, "Beta Senior Dev", "Sr Dev", organization_id="ORG-BETA", branch_id="BR-BETA-1")

    # 4. Shifts
    shift_a1 = db.add_shift("Alpha Morning", "09:00", "17:00", 15, organization_id="ORG-ALPHA", branch_id="BR-ALPHA-1")
    shift_b1 = db.add_shift("Beta Morning", "10:00", "18:00", 15, organization_id="ORG-BETA", branch_id="BR-BETA-1")

    # 5. Users
    user_a1 = db.add_user(
        name="Alice Alpha1",
        email="alice@alpha.com",
        department="Alpha Engineering",
        position="Alpha Senior Dev",
        salary=60000.0,
        organization_id="ORG-ALPHA",
        branch_id="BR-ALPHA-1"
    )
    user_a2 = db.add_user(
        name="Aaron Alpha2",
        email="aaron@alpha.com",
        department="Alpha Operations",
        position="Alpha Specialist",
        salary=55000.0,
        organization_id="ORG-ALPHA",
        branch_id="BR-ALPHA-2"
    )
    user_b1 = db.add_user(
        name="Bob Beta1",
        email="bob@beta.com",
        department="Beta Engineering",
        position="Beta Senior Dev",
        salary=70000.0,
        organization_id="ORG-BETA",
        branch_id="BR-BETA-1"
    )

    # 6. Biometrics / Embeddings
    # Generate distinct synthetic embeddings (512-dim unit vectors)
    vec_a = np.zeros(512, dtype=np.float32)
    vec_a[0] = 1.0  # Unit vector along axis 0
    vec_b = np.zeros(512, dtype=np.float32)
    vec_b[1] = 1.0  # Unit vector along axis 1

    db.store_embedding(user_a1, vec_a.tolist(), organization_id="ORG-ALPHA", branch_id="BR-ALPHA-1")
    db.store_embedding(user_b1, vec_b.tolist(), organization_id="ORG-BETA", branch_id="BR-BETA-1")

    # 7. Cameras
    cam_a1 = db.add_camera("cam_alpha_1", "Alpha Cam 1", "Room A1", "ip_camera", "rtsp://alpha/cam1", organization_id="ORG-ALPHA", branch_id="BR-ALPHA-1")
    cam_b1 = db.add_camera("cam_beta_1", "Beta Cam 1", "Room B1", "ip_camera", "rtsp://beta/cam1", organization_id="ORG-BETA", branch_id="BR-BETA-1")

    # 8. Attendance Records
    db.log_attendance(user_a1, "Alice Alpha1", 0.99, source="cam_alpha_1", bypass_shift_check=True)
    db.log_attendance(user_b1, "Bob Beta1", 0.98, source="cam_beta_1", bypass_shift_check=True)
    payroll_date = date.today().isoformat()
    overtime_a1 = db.add_overtime(
        user_a1, "Alice Alpha1", payroll_date, 2.0, "Alpha branch 1",
        organization_id="ORG-ALPHA", branch_id="BR-ALPHA-1",
    )
    overtime_a2 = db.add_overtime(
        user_a2, "Aaron Alpha2", payroll_date, 3.0, "Alpha branch 2",
        organization_id="ORG-ALPHA", branch_id="BR-ALPHA-2",
    )
    overtime_b1 = db.add_overtime(
        user_b1, "Bob Beta1", payroll_date, 4.0, "Beta branch 1",
        organization_id="ORG-BETA", branch_id="BR-BETA-1",
    )
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.executemany(
            "UPDATE overtime SET status = 'approved' WHERE id = ?",
            [(overtime_a1,), (overtime_a2,), (overtime_b1,)],
        )

    holiday_alpha = db.add_company_holiday(
        "Alpha Branch 1 Holiday", payroll_date, organization_id="ORG-ALPHA",
        branch_id="BR-ALPHA-1",
    )
    holiday_alpha_branch2 = db.add_company_holiday(
        "Alpha Branch 2 Holiday", "2026-10-20", organization_id="ORG-ALPHA",
        branch_id="BR-ALPHA-2",
    )
    holiday_beta = db.add_company_holiday(
        "Beta Holiday", "2026-10-21", organization_id="ORG-BETA",
        branch_id="BR-BETA-1",
    )

    # 9. Leave Request
    leave_a1 = db.add_leave_request(user_a1, "Alice Alpha1", "Vacation", "2026-10-15", "2026-10-16", reason="Vacation")
    leave_b1 = db.add_leave_request(user_b1, "Bob Beta1", "Vacation", "2026-10-15", "2026-10-16", reason="Beta Trip")

    fixtures = {
        "user_a1": user_a1,
        "user_a2": user_a2,
        "user_b1": user_b1,
        "dept_a1": dept_a1,
        "dept_a2": dept_a2,
        "dept_b1": dept_b1,
        "desig_a1": desig_a1,
        "desig_b1": desig_b1,
        "shift_a1": shift_a1,
        "shift_b1": shift_b1,
        "cam_a1": cam_a1,
        "cam_b1": cam_b1,
        "leave_a1": leave_a1,
        "leave_b1": leave_b1,
        "overtime_a1": overtime_a1,
        "overtime_a2": overtime_a2,
        "overtime_b1": overtime_b1,
        "holiday_alpha": holiday_alpha,
        "holiday_alpha_branch2": holiday_alpha_branch2,
        "holiday_beta": holiday_beta,
        "vec_a": vec_a,
        "vec_b": vec_b
    }
    print("Fixtures created successfully:", fixtures)
    return fixtures

def run_tests():
    if not db.init_db():
        raise RuntimeError("Could not initialize the isolated tenant-test database.")
    app_module.load_integration_config = lambda: {
        "linked": False,
        "organization_id": None,
        "branch_id": None,
        "licensed_modules": list(app_module.CANONICAL_MODULES),
        "local_instance_id": "LOC-TEST-INSTANCE",
    }
    fixtures = setup_test_data()
    app.config['TESTING'] = True
    client = app.test_client()

    print("\n==================================================")
    print("RUNNING 10 TENANT ISOLATION SECURITY AUDIT CHECKS")
    print("==================================================")

    # ----------------------------------------------------
    # DIMENSION 1: Header Tampering
    # Test: Session cookie bound to ORG-ALPHA while request header sends ORG-BETA
    # ----------------------------------------------------
    with client.session_transaction() as sess:
        sess['admin_unlocked'] = True
        sess['enrollment_enabled'] = True
        sess['client_user'] = 'alpha_admin'
        sess['user'] = 'alpha_admin'
        sess['role'] = 'admin'
        sess['organization_id'] = 'ORG-ALPHA'
        sess['branch_id'] = 'BR-ALPHA-1'

    # Spoof header X-Organization-Id: ORG-BETA
    res = client.get('/api/users', headers={'X-Organization-Id': 'ORG-BETA', 'X-Branch-Id': 'BR-BETA-1'})
    res_data = res.get_json() or {}
    data = res_data.get('users', []) if isinstance(res_data, dict) else res_data
    # All returned users MUST belong to ORG-ALPHA, NOT ORG-BETA
    org_ids = [u.get('organization_id') for u in data if u.get('id') in (fixtures['user_a1'], fixtures['user_b1'])]
    has_b = any(u.get('id') == fixtures['user_b1'] for u in data)
    has_a = any(u.get('id') == fixtures['user_a1'] for u in data)
    passed_dim1 = (has_a and not has_b)
    record(
        "Header Tampering Resistance",
        "1. Header Tampering",
        passed_dim1,
        f"Session Org-A ignored header Org-B spoofing. Found Alpha: {has_a}, Found Beta: {has_b}"
    )

    # ----------------------------------------------------
    # DIMENSION 2: Cross-Tenant API Access
    # Test: ORG-ALPHA client tries to query ORG-BETA resources
    # ----------------------------------------------------
    # A) Query users list as ORG-ALPHA
    res = client.get('/api/users')
    res_u = res.get_json() or {}
    users = res_u.get('users', []) if isinstance(res_u, dict) else res_u
    beta_users_visible = [u for u in users if u.get('organization_id') == 'ORG-BETA']

    # B) Query shifts as ORG-ALPHA
    res_s = client.get('/api/shifts')
    res_shifts = res_s.get_json() or {}
    shifts = res_shifts.get('shifts', []) if isinstance(res_shifts, dict) else res_shifts
    beta_shifts_visible = [s for s in shifts if s.get('organization_id') == 'ORG-BETA']

    # C) Query departments as ORG-ALPHA
    res_d = client.get('/api/departments')
    res_depts = res_d.get_json() or []
    depts = res_depts.get('departments', []) if isinstance(res_depts, dict) else res_depts
    beta_depts_visible = [d for d in depts if d.get('organization_id') == 'ORG-BETA']

    passed_dim2 = (len(beta_users_visible) == 0 and len(beta_shifts_visible) == 0 and len(beta_depts_visible) == 0)
    record(
        "Cross-Tenant Listing Isolation",
        "2. Cross-Tenant API Access",
        passed_dim2,
        f"Beta users visible: {len(beta_users_visible)}, shifts: {len(beta_shifts_visible)}, depts: {len(beta_depts_visible)}"
    )

    # ----------------------------------------------------
    # DIMENSION 3: Direct ID Manipulation (IDOR)
    # Test: Session ORG-ALPHA directly accesses/modifies user_b1, leave_b1, cam_b1
    # ----------------------------------------------------
    # A) GET user_b1 details
    res_idor_get = client.get(f"/api/users/{fixtures['user_b1']}")
    # Should return 404 or access denied
    idor_get_denied = (res_idor_get.status_code == 404)

    # B) PUT user_b1 details
    res_idor_put = client.put(f"/api/users/{fixtures['user_b1']}", json={"name": "Hacked Bob"})
    idor_put_denied = (res_idor_put.status_code == 404)

    # C) DELETE user_b1
    res_idor_del = client.delete(f"/api/users/{fixtures['user_b1']}")
    idor_del_denied = (res_idor_del.status_code == 404)

    # D) DELETE leave_b1
    res_idor_leave = client.delete(f"/api/leaves/{fixtures['leave_b1']}")
    idor_leave_denied = (res_idor_leave.status_code in (403, 404))

    # E) PUT cam_b1
    res_idor_cam = client.put(f"/api/cameras/{fixtures['cam_b1']}", json={"name": "Tampered Cam", "room_name": "Tampered Room", "camera_type": "ip_camera", "rtsp_url": "rtsp://tampered"})
    idor_cam_denied = (res_idor_cam.status_code == 404)

    passed_dim3 = (idor_get_denied and idor_put_denied and idor_del_denied and idor_leave_denied and idor_cam_denied)
    record(
        "Direct ID Manipulation (IDOR)",
        "3. Direct ID Manipulation",
        passed_dim3,
        f"GET denied: {idor_get_denied}, PUT denied: {idor_put_denied}, DEL denied: {idor_del_denied}, Leave DEL denied: {idor_leave_denied}, Cam PUT denied: {idor_cam_denied}"
    )

    # ----------------------------------------------------
    # DIMENSION 4: Branch Isolation
    # Test: BR-ALPHA-1 vs BR-ALPHA-2 in same organization
    # ----------------------------------------------------
    # Query branch 1 cameras vs branch 2 cameras
    cam_br1 = db.get_all_cameras(organization_id="ORG-ALPHA", branch_id="BR-ALPHA-1")
    cam_br2 = db.get_all_cameras(organization_id="ORG-ALPHA", branch_id="BR-ALPHA-2")
    passed_dim4 = (len(cam_br1) == 1 and len(cam_br2) == 0 and cam_br1[0]['branch_id'] == 'BR-ALPHA-1')
    record(
        "Branch Isolation",
        "4. Branch Isolation",
        passed_dim4,
        f"BR-ALPHA-1 has {len(cam_br1)} cameras; BR-ALPHA-2 has {len(cam_br2)} cameras"
    )

    # ----------------------------------------------------
    # DIMENSION 5: Biometric Isolation
    # Test: Embedding search with ORG-ALPHA tenant filter
    # ----------------------------------------------------
    # Match vector_b (Bob Beta's vector) against ORG-ALPHA cache
    from app import match_embedding_vectorized, refresh_embedding_cache
    refresh_embedding_cache()
    
    # When searching as ORG-ALPHA, even with Bob Beta's exact vector, it must NOT match Bob Beta
    match_result_alpha = match_embedding_vectorized(fixtures['vec_b'], threshold=0.5, organization_id="ORG-ALPHA")
    # When searching as ORG-BETA, it MUST match Bob Beta
    match_result_beta = match_embedding_vectorized(fixtures['vec_b'], threshold=0.5, organization_id="ORG-BETA")

    alpha_matched_id = match_result_alpha[0] if match_result_alpha else None
    beta_matched_id = match_result_beta[0] if match_result_beta else None
    passed_dim5 = (alpha_matched_id is None and beta_matched_id == fixtures['user_b1'])
    record(
        "Biometric Embedding Isolation",
        "5. Biometric Isolation",
        passed_dim5,
        f"Alpha query with Beta vector: {alpha_matched_id} (expected None); Beta query with Beta vector: {beta_matched_id}"
    )

    # ----------------------------------------------------
    # DIMENSION 6: Camera Isolation
    # Test: ORG-ALPHA cannot list or read ORG-BETA cameras
    # ----------------------------------------------------
    res_cams = client.get('/api/cameras')
    cams = res_cams.get_json() or []
    has_cam_b = any(c.get('id') == fixtures['cam_b1'] or c.get('camera_id') == 'cam_beta_1' for c in cams)
    has_cam_a = any(c.get('id') == fixtures['cam_a1'] or c.get('camera_id') == 'cam_alpha_1' for c in cams)
    passed_dim6 = (has_cam_a and not has_cam_b)
    record(
        "Camera Isolation",
        "6. Camera Isolation",
        passed_dim6,
        f"Alpha Cam visible: {has_cam_a}, Beta Cam visible: {has_cam_b}"
    )

    # ----------------------------------------------------
    # DIMENSION 7: Write-Operation Isolation
    # Test: Injecting organization_id in request payload does NOT override caller's tenant
    # ----------------------------------------------------
    # POST /api/users with payload organization_id: ORG-BETA while session is ORG-ALPHA
    res_spoof_user = client.post('/api/users', json={
        "name": "Trojan User",
        "email": "trojan@alpha.com",
        "department": "Alpha Engineering",
        "position": "Alpha Senior Dev",
        "organization_id": "ORG-BETA",  # Attacker attempt
        "branch_id": "BR-BETA-1"
    })
    created_id = None
    if res_spoof_user.status_code in (200, 201):
        res_json = res_spoof_user.get_json() or {}
        created_id = res_json.get('user_id') or res_json.get('id')
    
    # Verify in DB: what organization_id was assigned?
    actual_org = None
    if created_id:
        with sqlite3.connect(db.DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT organization_id, branch_id FROM users WHERE id = ?", (created_id,))
            row = cursor.fetchone()
            if row:
                actual_org = row[0]
                actual_branch = row[1]

    passed_dim7 = (actual_org == "ORG-ALPHA" and actual_branch == "BR-ALPHA-1")
    record(
        "Write Payload Tampering Resistance",
        "7. Write-Operation Isolation",
        passed_dim7,
        f"Payload tenant/branch spoof ignored; DB binding is '{actual_org}' / '{actual_branch}'"
    )

    # ----------------------------------------------------
    # DIMENSION 8: Session / Auth Security
    # Test: Session immutability and unauthenticated fallback
    # ----------------------------------------------------
    # Clear session to verify forged client and tenant headers do not authenticate.
    with client.session_transaction() as sess:
        sess.clear()
    
    # Request with unauthenticated X-Client-Auth and tenant headers must be rejected.
    res_unauth = client.get('/api/users', headers={'X-Organization-Id': 'ORG-BETA', 'X-Client-Auth': 'true'})
    passed_dim8 = res_unauth.status_code == 401
    record(
        "Forged Client Header Rejected",
        "8. Session / Auth Security",
        passed_dim8,
        f"Forged X-Client-Auth request returned HTTP {res_unauth.status_code} (expected 401)"
    )

    # ----------------------------------------------------
    # DIMENSION 9: Regression Testing (Standalone DEFAULT_ORG)
    # Test: Legacy requests with no session and no headers safely default to DEFAULT_ORG
    # ----------------------------------------------------
    res_legacy = client.get('/api/users')
    passed_dim9 = res_legacy.status_code == 401
    record(
        "Unauthenticated Request Rejected",
        "9. Regression Testing",
        passed_dim9,
        f"Request without a session returned HTTP {res_legacy.status_code} (expected 401)"
    )

    # ----------------------------------------------------
    # DIMENSION 11: Branch-Scoped Employees, Holidays, and Overtime
    # ----------------------------------------------------
    def authenticate_scope(organization_id, branch_id):
        with client.session_transaction() as sess:
            sess.clear()
            sess['admin_unlocked'] = True
            sess['organization_id'] = organization_id
            sess['branch_id'] = branch_id

    authenticate_scope("ORG-ALPHA", "BR-ALPHA-1")
    branch_users_response = client.get(
        "/api/users",
        headers={"X-Organization-Id": "ORG-BETA", "X-Branch-Id": "BR-BETA-1"},
    )
    branch_users = (branch_users_response.get_json() or {}).get("users", [])
    branch_users_scoped = (
        branch_users_response.status_code == 200
        and any(user.get('id') == fixtures['user_a1'] for user in branch_users)
        and not any(user.get('id') in (fixtures['user_a2'], fixtures['user_b1']) for user in branch_users)
    )

    branch_overtime = client.get("/api/overtime").get_json() or []
    branch_overtime_scoped = (
        any(row.get('id') == fixtures['overtime_a1'] for row in branch_overtime)
        and not any(row.get('id') in (fixtures['overtime_a2'], fixtures['overtime_b1']) for row in branch_overtime)
    )

    foreign_overtime_create = client.post("/api/overtime", json={
        "user_id": fixtures['user_b1'],
        "ot_date": date.today().isoformat(),
        "hours": 1,
        "reason": "forged employee",
    })
    foreign_overtime_update = client.put(
        f"/api/overtime/{fixtures['overtime_b1']}",
        json={"status": "rejected"},
    )
    foreign_overtime_delete = client.delete(f"/api/overtime/{fixtures['overtime_b1']}")
    other_branch_overtime_update = client.put(
        f"/api/overtime/{fixtures['overtime_a2']}",
        json={"status": "rejected"},
    )
    other_branch_overtime_delete = client.delete(f"/api/overtime/{fixtures['overtime_a2']}")

    holidays = client.get("/api/holidays").get_json() or []
    holidays_scoped = (
        any(row.get('id') == fixtures['holiday_alpha'] for row in holidays)
        and not any(row.get('id') in (fixtures['holiday_alpha_branch2'], fixtures['holiday_beta']) for row in holidays)
    )
    foreign_holiday_delete = client.delete(f"/api/holidays/{fixtures['holiday_beta']}")
    other_branch_holiday_delete = client.delete(f"/api/holidays/{fixtures['holiday_alpha_branch2']}")
    foreign_records_unchanged = (
        foreign_overtime_update.status_code == 404
        and foreign_overtime_delete.status_code == 404
        and other_branch_overtime_update.status_code == 404
        and other_branch_overtime_delete.status_code == 404
        and foreign_holiday_delete.status_code == 200
        and foreign_holiday_delete.get_json().get('success') is False
        and other_branch_holiday_delete.status_code == 200
        and other_branch_holiday_delete.get_json().get('success') is False
        and db.get_overtime_by_id(fixtures['overtime_b1']) is not None
        and db.get_overtime_by_id(fixtures['overtime_a2']) is not None
    )
    record(
        "Tenant/Branch Scoped Data and ID Operations",
        "11. Tenant / Branch Isolation",
        branch_users_scoped and branch_overtime_scoped and holidays_scoped and foreign_records_unchanged
        and foreign_overtime_create.status_code == 404,
        f"users={branch_users_scoped}, overtime={branch_overtime_scoped}, holidays={holidays_scoped}, IDOR rejected={foreign_records_unchanged}, foreign employee OT HTTP={foreign_overtime_create.status_code}"
    )

    # A valid scoped operation remains available.
    valid_overtime = client.post("/api/overtime", json={
        "user_id": fixtures['user_a1'],
        "ot_date": date.today().isoformat(),
        "hours": 1,
        "reason": "valid branch request",
    })
    valid_holiday = client.post("/api/holidays", json={
        "name": "Valid Alpha Branch Holiday",
        "holiday_date": "2026-12-22",
    })
    record(
        "Valid Authenticated Tenant Operations",
        "12. Authentication Regression",
        valid_overtime.status_code == 201 and valid_holiday.status_code == 201,
        f"overtime HTTP={valid_overtime.status_code}, holiday HTTP={valid_holiday.status_code}"
    )

    # ----------------------------------------------------
    # DIMENSION 13: Payroll organization and branch scope
    # ----------------------------------------------------
    payroll_month = date.today().strftime("%Y-%m")
    payroll_alpha_1 = db.calculate_monthly_payroll(
        payroll_month, organization_id="ORG-ALPHA", branch_id="BR-ALPHA-1"
    )
    payroll_alpha_2 = db.calculate_monthly_payroll(
        payroll_month, organization_id="ORG-ALPHA", branch_id="BR-ALPHA-2"
    )
    payroll_beta = db.calculate_monthly_payroll(
        payroll_month, organization_id="ORG-BETA", branch_id="BR-BETA-1"
    )
    alpha_1_records = payroll_alpha_1.get('records', []) if isinstance(payroll_alpha_1, dict) else payroll_alpha_1
    alpha_2_records = payroll_alpha_2.get('records', []) if isinstance(payroll_alpha_2, dict) else payroll_alpha_2
    beta_records = payroll_beta.get('records', []) if isinstance(payroll_beta, dict) else payroll_beta
    alpha_1_by_id = {row.get('employee_id'): row for row in alpha_1_records}
    alpha_2_by_id = {row.get('employee_id'): row for row in alpha_2_records}
    beta_by_id = {row.get('employee_id'): row for row in beta_records}
    payroll_scoped = (
        set(alpha_1_by_id) == {fixtures['user_a1'], created_id}
        and set(alpha_2_by_id) == {fixtures['user_a2']}
        and set(beta_by_id) == {fixtures['user_b1']}
        and bool(alpha_1_records)
        and bool(alpha_2_records)
        and bool(beta_records)
        and alpha_1_by_id[fixtures['user_a1']].get('overtime_hours') == 2.0
        and alpha_2_by_id[fixtures['user_a2']].get('overtime_hours') == 3.0
        and beta_by_id[fixtures['user_b1']].get('overtime_hours') == 4.0
    )
    record(
        "Payroll Employee and Overtime Scope",
        "13. Payroll Branch Isolation",
        payroll_scoped,
        f"Org-A/Branch-1 employees={len(alpha_1_records)}, Branch-2={len(alpha_2_records)}, Org-B={len(beta_records)}; OT hours scoped={payroll_scoped}"
    )

    # ----------------------------------------------------
    # DIMENSION 14: Password hashing and tenant binding
    # ----------------------------------------------------
    strong_password = "LocalDashboard!TestPassword2026"
    auth_user_id = db.add_user(
        name="Auth Regression User",
        email="auth-regression@alpha.example",
        password=strong_password,
        organization_id="ORG-ALPHA",
        branch_id="BR-ALPHA-1",
    )
    with sqlite3.connect(db.DB_PATH) as conn:
        stored_password, stored_hash = conn.execute(
            "SELECT password, password_hash FROM users WHERE id = ?",
            (auth_user_id,),
        ).fetchone()
    login_response = client.post("/api/login", json={
        "email": "auth-regression@alpha.example",
        "password": strong_password,
    })
    wrong_password_response = client.post("/api/login", json={
        "email": "auth-regression@alpha.example",
        "password": "123456",
    })
    other_user_password_response = client.post("/api/change-password", json={
        "user_id": fixtures['user_a1'],
        "new_password": "AnotherStrongPassword2026!",
    })
    record(
        "Password Hashing and Authenticated Tenant Context",
        "14. Password Security",
        auth_user_id is not None
        and stored_password is None
        and stored_hash
        and login_response.status_code == 200
        and wrong_password_response.status_code == 401
        and other_user_password_response.status_code == 403
        and client.get("/api/users", headers={"X-Organization-Id": "ORG-BETA"}).get_json().get("organization_id") == "ORG-ALPHA",
        f"plaintext cleared={stored_password is None}, hash stored={bool(stored_hash)}, valid login HTTP={login_response.status_code}, invalid HTTP={wrong_password_response.status_code}, cross-user password change HTTP={other_user_password_response.status_code}"
    )

    # ----------------------------------------------------
    # DIMENSION 15: Main Dashboard integration sync authentication
    # ----------------------------------------------------
    original_integration_key = os.environ.pop('LOCAL_DASHBOARD_API_KEY', None)
    original_config_saver = app_module.save_integration_config
    saved_configs = []
    app_module.save_integration_config = lambda config: saved_configs.append(dict(config)) or True
    integration_payload = {
        "organization_id": "ORG-ALPHA",
        "organization_name": "Alpha Organization",
        "branch_id": "BR-ALPHA-1",
        "branch_name": "Alpha Branch 1",
        "licensed_modules": ["attendance", "payroll"],
    }
    unconfigured_sync = client.post("/api/integration/sync", json=integration_payload)
    os.environ['LOCAL_DASHBOARD_API_KEY'] = "Group2-Integration-Test-Key-2026-Secure"
    forged_sync = client.post(
        "/api/integration/sync",
        json=integration_payload,
        headers={"X-Integration-Key": "not-the-shared-key"},
    )
    valid_sync = client.post(
        "/api/integration/sync",
        json=integration_payload,
        headers={"X-Integration-Key": os.environ['LOCAL_DASHBOARD_API_KEY']},
    )
    app_module.save_integration_config = original_config_saver
    if original_integration_key is None:
        os.environ.pop('LOCAL_DASHBOARD_API_KEY', None)
    else:
        os.environ['LOCAL_DASHBOARD_API_KEY'] = original_integration_key
    record(
        "Authenticated Main-to-Local Integration Sync",
        "15. Integration Authentication",
        unconfigured_sync.status_code == 503
        and forged_sync.status_code == 401
        and valid_sync.status_code == 200
        and bool(saved_configs)
        and saved_configs[-1].get('organization_id') == "ORG-ALPHA"
        and saved_configs[-1].get('branch_id') == "BR-ALPHA-1",
        f"unconfigured HTTP={unconfigured_sync.status_code}, forged HTTP={forged_sync.status_code}, valid HTTP={valid_sync.status_code}"
    )

    # ----------------------------------------------------
    # DIMENSION 10: Database Integrity Check
    # Test: PRAGMA integrity_check and foreign keys on attendance.db
    # ----------------------------------------------------
    with sqlite3.connect(db.DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check")
        integrity_row = cursor.fetchone()
        integrity_ok = (integrity_row and integrity_row[0] == "ok")

        # Verify that all test fixtures have complete and valid relationships
        cursor.execute("""
            SELECT COUNT(*) FROM users u 
            WHERE u.organization_id IN ('ORG-ALPHA', 'ORG-BETA') 
              AND u.department IS NOT NULL
        """)
        user_integrity = cursor.fetchone()[0] >= 3

    passed_dim10 = (integrity_ok and user_integrity)
    record(
        "Database Storage & Integrity Health",
        "10. Database Integrity",
        passed_dim10,
        f"Database PRAGMA integrity: {integrity_row[0] if integrity_row else 'None'}, Multi-Tenant fixtures consistency: OK"
    )

    # ----------------------------------------------------
    # SUMMARY
    # ----------------------------------------------------
    print("\n==================================================")
    total_tests = len(results)
    passed_tests = sum(1 for r in results if r['passed'])
    failed_tests = total_tests - passed_tests
    print(f"AUDIT SUMMARY: {passed_tests}/{total_tests} Passed ({failed_tests} Failed)")
    print("==================================================\n")

    # Cleanup test entities
    with sqlite3.connect(db.DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM attendance WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA')")
        cursor.execute("DELETE FROM overtime WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA')")
        cursor.execute("DELETE FROM company_holidays WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA')")
        cursor.execute("DELETE FROM leave_requests WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA')")
        cursor.execute("DELETE FROM embeddings WHERE user_id IN (SELECT id FROM users WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR name IN ('Alice Alpha1', 'Aaron Alpha2', 'Bob Beta1', 'Trojan User'))")
        cursor.execute("DELETE FROM employee_shift_history WHERE user_id IN (SELECT id FROM users WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR name IN ('Alice Alpha1', 'Aaron Alpha2', 'Bob Beta1', 'Trojan User'))")
        cursor.execute("DELETE FROM users WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR name IN ('Alice Alpha1', 'Aaron Alpha2', 'Bob Beta1', 'Trojan User', 'Test Alpha')")
        cursor.execute("DELETE FROM departments WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR name IN ('Alpha Engineering', 'Alpha Operations', 'Beta Engineering')")
        cursor.execute("DELETE FROM designations WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR title IN ('Alpha Senior Dev', 'Beta Senior Dev')")
        cursor.execute("DELETE FROM shifts WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR name IN ('Alpha Morning', 'Beta Morning')")
        cursor.execute("DELETE FROM cameras WHERE organization_id IN ('ORG-ALPHA', 'ORG-BETA') OR camera_id IN ('cam_alpha_1', 'cam_beta_1')")
        conn.commit()

    return results

if __name__ == '__main__':
    run_tests()
    if any(not result['passed'] for result in results):
        sys.exit(1)
