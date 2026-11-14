"""End-to-end: QA-118 Late after half leave, QA-122 quota preview, QA-123 final lock."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime

# Isolate DB before importing database module internals that bind DB_PATH.
TMP = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
TMP.close()
os.environ['QINTELLECT_DB_PATH'] = TMP.name

import database as db  # noqa: E402
from payroll_policy_schema import classify_checkin_late_status  # noqa: E402


ORG = 'QA118_ORG'
BR = 'QA118_BR'


def mins(t: str) -> int:
    h, m = map(int, str(t).split(':')[:2])
    return h * 60 + m


def setup_tenant():
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO shifts
               (name, start_time, end_time, grace_period_minutes, organization_id, branch_id, is_default, active)
               VALUES ('Morning', '09:00', '17:00', 30, ?, ?, 1, 1)""",
            (ORG, BR),
        )
        shift_id = c.lastrowid
        # Ensure payroll settings row with Option B (no grace after half leave end).
        c.execute(
            """INSERT INTO payroll_settings (organization_id, branch_id, half_leave_late_grace_mode)
               VALUES (?, ?, 'no_grace')""",
            (ORG, BR),
        )
        c.execute(
            """UPDATE payroll_settings SET half_leave_late_grace_mode='no_grace'
               WHERE organization_id=? AND branch_id=?""",
            (ORG, BR),
        )
        c.execute(
            """INSERT INTO users (name, department, position, shift_id, organization_id, branch_id, active)
               VALUES ('HalfLeave Tester', 'QA', 'Staff', ?, ?, ?, 1)""",
            (shift_id, ORG, BR),
        )
        uid = c.lastrowid
        c.execute(
            """INSERT INTO employee_shift_history
               (user_id, shift_id, effective_from, shift_name, start_time, end_time,
                grace_period_minutes, working_days, organization_id, branch_id)
               VALUES (?, ?, '2026-01-01', 'Morning', '09:00', '17:00', 30, '0,1,2,3,4,5,6', ?, ?)""",
            (uid, shift_id, ORG, BR),
        )
        c.execute(
            """INSERT INTO leave_types (name, code, yearly_allowance, is_paid, allowance_period, organization_id, branch_id)
               VALUES ('Casual', 'casual', 1, 1, 'monthly', ?, ?)""",
            (ORG, BR),
        )
        conn.commit()
    return uid


def add_half_leave(uid, day, start_t, end_t, status='approved'):
    with sqlite3.connect(db.DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO leave_requests
               (user_id, user_name, leave_type, leave_duration, start_date, end_date,
                start_time, end_time, status, paid_status, unpaid_status, paid_days, unpaid_days, quota_days,
                organization_id, branch_id, reason)
               VALUES (?, 'HalfLeave Tester', 'casual', 'half', ?, ?, ?, ?, ?, ?, 'pending', 1, 0, 1, ?, ?, 'qa118')""",
            (uid, day, day, start_t, end_t, status, status, ORG, BR),
        )
        lid = c.lastrowid
        conn.commit()
    return lid


def user_shift_id(uid):
    with sqlite3.connect(db.DB_PATH) as conn:
        row = conn.execute('SELECT shift_id FROM users WHERE id=?', (uid,)).fetchone()
    return int(row[0]) if row and row[0] else None


def punch(uid, day, hhmm):
    sid = user_shift_id(uid)
    with sqlite3.connect(db.DB_PATH) as conn:
        c = conn.cursor()
        c.execute('DELETE FROM attendance WHERE user_id=? AND date(timestamp)=?', (uid, day))
        c.execute(
            """INSERT INTO attendance
               (user_id, detected_name, confidence, source, timestamp, notes, marked_by, shift_id, organization_id, branch_id)
               VALUES (?, 'HalfLeave Tester', 1.0, 'manual', ?, '', 'Admin', ?, ?, ?)""",
            (uid, f'{day} {hhmm}:00', sid, ORG, BR),
        )
        conn.commit()


def day_status(uid, day):
    report = db.get_detailed_attendance_report(
        target_date=day, user_id=uid, organization_id=ORG, branch_id=BR,
    )
    rec = (report.get('records') or [{}])[0]
    daily = rec.get('daily_records') or []
    if daily:
        return str(daily[0].get('status') or '').lower()
    return str(rec.get('status') or '').lower()


def light_status(uid, day):
    report = db.get_light_attendance_report(
        target_date=day, user_id=uid, organization_id=ORG, branch_id=BR,
    )
    rec = (report.get('records') or [{}])[0]
    return str(rec.get('status') or '').lower()


def test_qa118():
    uid = setup_tenant()
    day = '2026-11-10'
    add_half_leave(uid, day, '09:00', '12:04')

    # Helper contract
    assert classify_checkin_late_status(
        mins('12:03'), mins('09:00'), 30,
        half_leave_start_mins=mins('09:00'), half_leave_end_mins=mins('12:04'), mode='no_grace',
    )['status'] == 'present'
    assert classify_checkin_late_status(
        mins('12:05'), mins('09:00'), 30,
        half_leave_start_mins=mins('09:00'), half_leave_end_mins=mins('12:04'), mode='no_grace',
    )['status'] == 'late'

    punch(uid, day, '12:03')
    st = day_status(uid, day)
    lst = light_status(uid, day)
    print('12:03 detailed=', st, 'light=', lst)
    assert st == 'present', st
    assert lst == 'present', lst

    punch(uid, day, '12:05')
    st = day_status(uid, day)
    lst = light_status(uid, day)
    print('12:05 detailed=', st, 'light=', lst)
    assert st == 'late', st
    assert lst == 'late', lst

    punch(uid, day, '15:00')
    st = day_status(uid, day)
    lst = light_status(uid, day)
    print('15:00 detailed=', st, 'light=', lst)
    assert st == 'late', st
    assert lst == 'late', lst

    # Option A: within grace Present
    db.update_payroll_settings(
        organization_id=ORG, branch_id=BR, half_leave_late_grace_mode='apply_grace',
    )
    settings = db.get_payroll_settings(organization_id=ORG, branch_id=BR)
    print('settings after Option A', settings.get('half_leave_late_grace_mode'))
    assert settings.get('half_leave_late_grace_mode') == 'apply_grace'
    # Grace on our shift is 30 → deadline 12:34; at deadline Present, one minute later Late.
    punch(uid, day, '12:34')
    st = day_status(uid, day)
    print('12:34 Option A detailed=', st)
    assert st == 'present', st
    punch(uid, day, '12:35')
    st = day_status(uid, day)
    print('12:35 Option A detailed=', st)
    assert st == 'late', st

    # ISO / datetime end_time must still parse for Late (QA-118 always-Present regression).
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            "UPDATE leave_requests SET end_time='2026-11-10T12:04:00' WHERE user_id=? AND start_date=?",
            (uid, day),
        )
        conn.commit()
    punch(uid, day, '15:00')
    st = day_status(uid, day)
    print('15:00 ISO end_time detailed=', st)
    assert st == 'late', st

    # Missing end_time under Option B → synthetic midpoint (09:00–17:00 → 13:00).
    day2 = '2026-11-14'
    add_half_leave(uid, day2, '09:00', None)
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            "UPDATE leave_requests SET end_time=NULL WHERE user_id=? AND start_date=?",
            (uid, day2),
        )
        conn.commit()
    db.update_payroll_settings(
        organization_id=ORG, branch_id=BR, half_leave_late_grace_mode='no_grace',
    )
    punch(uid, day2, '12:59')
    st = day_status(uid, day2)
    print('12:59 no end_time midpoint Present=', st)
    assert st == 'present', st
    punch(uid, day2, '13:01')
    st = day_status(uid, day2)
    print('13:01 no end_time midpoint Late=', st)
    assert st == 'late', st


def test_qa122_123():
    uid = setup_tenant()
    # Two pending full-day leaves, quota 1/month
    with sqlite3.connect(db.DB_PATH) as conn:
        c = conn.cursor()
        for d in ('2026-11-11', '2026-11-12'):
            c.execute(
                """INSERT INTO leave_requests
                   (user_id, user_name, leave_type, leave_duration, start_date, end_date,
                    status, paid_status, unpaid_status, paid_days, unpaid_days, quota_days,
                    organization_id, branch_id, reason)
                   VALUES (?, 'HalfLeave Tester', 'casual', 'full', ?, ?, 'pending', 'pending', 'pending', 1, 0, 1, ?, ?, 'qa122')""",
                (uid, d, d, ORG, BR),
            )
        # One already rejected
        c.execute(
            """INSERT INTO leave_requests
               (user_id, user_name, leave_type, leave_duration, start_date, end_date,
                status, paid_status, unpaid_status, paid_days, unpaid_days, quota_days,
                organization_id, branch_id, reason)
               VALUES (?, 'HalfLeave Tester', 'casual', 'full', '2026-11-13', '2026-11-13',
                       'rejected', 'rejected', 'rejected', 1, 0, 1, ?, ?, 'qa123')""",
            (uid, ORG, BR),
        )
        conn.commit()
        ids = [r[0] for r in c.execute(
            "SELECT id FROM leave_requests WHERE user_id=? AND reason IN ('qa122','qa123') ORDER BY id",
            (uid,),
        ).fetchall()]

    pending_ids = ids[:2]
    rejected_id = ids[2]
    preview = db.preview_leave_approve_batch(pending_ids, organization_id=ORG, branch_id=BR)
    print('preview', preview)
    assert preview.get('success') is True
    assert preview.get('needs_excess_decision') is True, preview
    assert int(preview.get('total_excess_days') or 0) >= 1

    # Rejected must not flip to approved
    blocked = db.approve_leave_with_quota(rejected_id, organization_id=ORG, branch_id=BR)
    print('rejected approve', blocked)
    assert blocked.get('success') is False
    assert blocked.get('reason_code') == 'already_decided'

    # Approve first within quota without treatment
    ok = db.approve_leave_with_quota(pending_ids[0], organization_id=ORG, branch_id=BR)
    print('first approve', ok)
    assert ok.get('success') is True

    # Second needs excess treatment
    need = db.approve_leave_with_quota(pending_ids[1], organization_id=ORG, branch_id=BR)
    print('second without treatment', need)
    assert need.get('needs_excess_decision') is True

    batch = db.approve_leaves_batch_with_quota(
        [pending_ids[1]],
        organization_id=ORG,
        branch_id=BR,
        excess_treatment='unpaid',
    )
    print('batch unpaid', batch)
    assert batch.get('success') is True


if __name__ == '__main__':
    print('DB', db.DB_PATH)
    test_qa118()
    print('QA-118 E2E OK')
    # New isolated DB for leave approve tests (Windows file lock on prior connection).
    tmp2 = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp2.close()
    os.environ['QINTELLECT_DB_PATH'] = tmp2.name
    import importlib
    import config as cfg
    importlib.reload(cfg)
    db.DB_PATH = cfg.DB_PATH
    db.init_db()
    print('DB2', db.DB_PATH)
    test_qa122_123()
    print('E2E QA-118/122/123 OK')
