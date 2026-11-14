"""Smoke: QA-118 Late after half leave, QA-122 preview wiring, QA-123 final lock."""
import pathlib
import re

from payroll_policy_schema import classify_checkin_late_status


def mins(t):
    h, m = map(int, str(t).split(':')[:2])
    return h * 60 + m


def test_qa118_classify():
    # Option B: leave ends 12:04 — 12:03 Present, 12:05 Late
    r = classify_checkin_late_status(
        mins('12:03'), mins('09:00'), 30,
        half_leave_start_mins=mins('09:00'), half_leave_end_mins=mins('12:04'), mode='no_grace',
    )
    assert r['status'] == 'present', r
    r = classify_checkin_late_status(
        mins('12:04'), mins('09:00'), 30,
        half_leave_start_mins=mins('09:00'), half_leave_end_mins=mins('12:04'), mode='no_grace',
    )
    assert r['status'] == 'present', r
    r = classify_checkin_late_status(
        mins('12:05'), mins('09:00'), 30,
        half_leave_start_mins=mins('09:00'), half_leave_end_mins=mins('12:04'), mode='no_grace',
    )
    assert r['status'] == 'late', r
    r = classify_checkin_late_status(
        mins('15:00'), mins('09:00'), 30,
        half_leave_start_mins=mins('09:00'), half_leave_end_mins=mins('12:04'), mode='no_grace',
    )
    assert r['status'] == 'late', r

    # Option A: grace 30 after 12:04 → 12:34 Present, 12:35 Late
    r = classify_checkin_late_status(
        mins('12:34'), mins('09:00'), 30,
        half_leave_start_mins=mins('09:00'), half_leave_end_mins=mins('12:04'), mode='apply_grace',
    )
    assert r['status'] == 'present', r
    r = classify_checkin_late_status(
        mins('12:35'), mins('09:00'), 30,
        half_leave_start_mins=mins('09:00'), half_leave_end_mins=mins('12:04'), mode='apply_grace',
    )
    assert r['status'] == 'late', r

    # Morning punch before afternoon half leave still uses shift start + grace
    r = classify_checkin_late_status(
        mins('09:20'), mins('09:00'), 30,
        half_leave_start_mins=mins('13:00'), half_leave_end_mins=mins('17:00'), mode='apply_grace',
    )
    assert r['status'] == 'present', r
    r = classify_checkin_late_status(
        mins('09:31'), mins('09:00'), 30,
        half_leave_start_mins=mins('13:00'), half_leave_end_mins=mins('17:00'), mode='apply_grace',
    )
    assert r['status'] == 'late', r


def test_qa122_123_wiring():
    root = pathlib.Path(__file__).resolve().parent
    html = (root / 'templates' / 'index.html').read_text(encoding='utf-8')
    db = (root / 'database.py').read_text(encoding='utf-8')
    app = (root / 'app.py').read_text(encoding='utf-8')
    assert 'leaveApproveExcessModal' in html
    assert '/api/leaves/preview-approve' in html
    assert 'already_decided' in db or 'already been decided' in db
    assert '_leave_decision_is_final' in db
    assert 'already_decided' in app
    # Rejected must NOT be treated as selectable pending
    assert "s.pending || s.status.toLowerCase() === 'rejected'" not in html
    assert "paidSt === 'rejected'" not in html or 'isFinalStatus' in html
    assert re.search(r'isFinalStatus', html)
    assert 'classify_checkin_late_status' in db
    assert "status = 'Late'" in db or "status = 'Late'" in db.replace('"', "'")


if __name__ == '__main__':
    test_qa118_classify()
    test_qa122_123_wiring()
    print('QA-118/122/123 smoke OK')
