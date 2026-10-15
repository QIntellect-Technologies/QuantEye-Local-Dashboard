#!/usr/bin/env python
"""Phase A review proofs: E5 seed, S10 diffs, S9 mode, audit, tenant report, snapshot, shift flag."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weekday_utils import DEFAULT_WORKING_DAYS, scheduled_days_in_month


def money(value: float) -> float:
    return round(float(value), 2)


def formula_expected(mode: str) -> dict:
    n = 26
    basic = 60000.0
    e8_end = money(basic + 4 * (basic / 208))
    e8_first = money(basic + 4 * money(basic / 208))
    if mode == "working_days_round_at_end":
        e6, e7, e8 = money(basic * 17 / n), money(basic * 16 / n), e8_end
    else:
        e6, e7, e8 = money(basic * 20 / 30), money(basic * 18 / 30), e8_first
    return {
        "E1 Full Attend": money(basic),
        "E2 Two Absent": money(basic * 24 / n),
        "E3 Six Absent": money(basic * 20 / n),
        "E4 Late Half": money(basic),
        "E5 Leave Mix": money(basic * 25 / n),
        "E6 Join Mid": e6,
        "E7 Exit Mid": e7,
        "E8 Overtime": e8,
        "E9 Flex Pay": money(basic),
        "E10 Salary Change": 45000.0,
        "E11 No Salary": 0.0,
        "E12 Deactivate": money(basic),
        "E13 Extra": 42000.0,
        "E14 Absurd QA": 10000000.0,
    }


def slim(payroll):
    out = {}
    for row in (payroll or {}).get("records") or []:
        out[row.get("employee_name")] = {
            "net_salary": row.get("net_salary"),
            "payable_days": row.get("payable_days"),
            "present_days": row.get("present_days"),
            "paid_leave_days": row.get("paid_leave_days") or row.get("approved_leave_days"),
            "unpaid_leave_days": row.get("unpaid_leave_days"),
            "unpaid_leave_deduction": row.get("unpaid_leave_deduction"),
            "overtime_amount": row.get("overtime_amount"),
            "overtime_rate": row.get("overtime_rate"),
            "basic_salary": row.get("basic_salary"),
            "attendance_pay_mode": row.get("attendance_pay_mode"),
            "shift_history_missing": row.get("shift_history_missing"),
            "status": row.get("status"),
            "bonus_amount": row.get("bonus_amount"),
            "pay_lines": row.get("pay_lines"),
            "matched_rule": row.get("matched_rule"),
        }
    return out


def cent_diff(expected: dict, live: dict) -> list:
    rows = []
    for name, exp_net in expected.items():
        got = live.get(name) or {}
        live_net = got.get("net_salary")
        delta = None if live_net is None else money(float(live_net) - float(exp_net))
        pending_c4 = name.startswith("E10")
        rows.append({
            "employee": name,
            "expected_net": exp_net,
            "live_net": live_net,
            "diff_to_cent": delta,
            "match": delta == 0.0,
            "pending_c4": pending_c4,
            "live": got,
        })
    return rows


def main() -> int:
    from tests.fixtures.expected_catalog import dump_expected, scenario_s10
    from tests.run_payroll_scenario import make_clean_snapshot, make_legacy_snapshot
    from tests.fixtures.payroll_audit_seed import (
        ALL_MODULES, BR1, ORG1, LiveAPI, add_absence_brackets, bulk_present,
        get_payroll, put_pay_mode, seed_core_org, seed_september_patterns,
    )
    from payroll_policy_schema import report_salary_config_tenant_mismatches
    import config as db_config
    import database as db
    import app as app_module

    dest = ROOT / "evidence" / "after" / "PHASEA_REVIEW"
    dest.mkdir(parents=True, exist_ok=True)
    dump_expected(ROOT / "tests" / "fixtures" / "expected")
    s10_sheet = scenario_s10()
    e5_sheet = next(r for r in s10_sheet["expected"] if r["code"] == "E5")

    work = Path(tempfile.mkdtemp(prefix="qe-review-"))
    snap = work / "clean.db"
    make_clean_snapshot(snap)
    app_module.app.config["TESTING"] = True
    out: dict = {"e5_catalog": {
        "payable_days": e5_sheet["payable_days"],
        "present_days": e5_sheet["present_days"],
        "paid_leave_days": e5_sheet["paid_leave_days"],
        "unpaid_leave_days": e5_sheet["unpaid_leave_days"],
        "unpaid_leave_deduction": e5_sheet["unpaid_leave_deduction"],
        "expected_net": e5_sheet["expected_net"],
        "notes": e5_sheet["notes"],
    }}

    def run_s10_fresh(tag: str, extra: dict):
        db_path = work / f"s10-{tag}.db"
        shutil.copy2(snap, db_path)
        os.environ["QINTELLECT_DB_PATH"] = str(db_path)
        db_config.DB_PATH = str(db_path)
        db.DB_PATH = str(db_path)
        api = LiveAPI(app_module.app.test_client(), app_module)
        try:
            core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
            add_absence_brackets(api, "percentage")
            put_pay_mode(api, "payable_days", extra={
                "deduct_unpaid_leave": True,
                **extra,
            })
            patterns = seed_september_patterns(api, core["ids"])
            payroll = get_payroll(api, "2026-09")
            return slim(payroll), payroll, patterns, db_path, core
        finally:
            api.close()

    live_wd, payroll_wd, patterns, db_wd, core_wd = run_s10_fresh("working_days", {
        "proration_basis": "working_days", "ot_rounding": "round_at_end",
    })
    e5_live_wd = live_wd.get("E5 Leave Mix")
    out["e5_seed"] = {
        "skipped_dates": ["2026-09-08", "2026-09-09", "2026-09-10"],
        "paid_leave": "2026-09-08..09 casual approved",
        "unpaid_leave": "2026-09-10 unpaid approved",
        "present_on_other_scheduled_days": True,
        "live": e5_live_wd,
        "payable_already_excludes_unpaid": bool(
            e5_live_wd
            and int(e5_live_wd.get("payable_days") or 0) == 25
            and int(e5_live_wd.get("unpaid_leave_days") or 0) == 1
            and float(e5_live_wd.get("unpaid_leave_deduction") or 0) == 0
        ),
    }
    out["s10_working_days_round_at_end"] = {
        "db": str(db_wd),
        "engine_settings": {
            "proration_basis": payroll_wd.get("proration_basis"),
            "ot_rounding": payroll_wd.get("ot_rounding"),
        },
        "diff": cent_diff(formula_expected("working_days_round_at_end"), live_wd),
    }
    live_cal, payroll_cal, _, db_cal, _ = run_s10_fresh("calendar_days", {
        "proration_basis": "calendar_days", "ot_rounding": "round_rate_first",
    })
    out["s10_calendar_days_round_rate_first"] = {
        "db": str(db_cal),
        "engine_settings": {
            "proration_basis": payroll_cal.get("proration_basis"),
            "ot_rounding": payroll_cal.get("ot_rounding"),
        },
        "diff": cent_diff(formula_expected("calendar_days_round_rate_first"), live_cal),
    }

    db_path = work / "attendance.db"
    shutil.copy2(db_wd, db_path)
    os.environ["QINTELLECT_DB_PATH"] = str(db_path)
    db_config.DB_PATH = str(db_path)
    db.DB_PATH = str(db_path)
    api = LiveAPI(app_module.app.test_client(), app_module)
    api.login(ORG1, BR1, ALL_MODULES)
    core = core_wd
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("DELETE FROM employee_shift_history WHERE user_id = ?", (core["ids"]["E12"],))
            conn.commit()
        missing_payroll = get_payroll(api, "2026-09")
        e12 = next((r for r in (missing_payroll.get("records") or []) if r.get("employee_name") == "E12 Deactivate"), None)
        out["shift_history_fallback_fixed"] = bool(e12 and e12.get("shift_history_missing"))
        out["shift_history_e12"] = {
            "shift_history_missing": None if not e12 else e12.get("shift_history_missing"),
            "status": None if not e12 else e12.get("status"),
        }

        s9 = seed_s9(api)
        out["s9"] = s9
        ui_db = dest / "ui.db"
        shutil.copy2(db_path, ui_db)
        out["ui_db"] = str(ui_db)
        out["ids"] = core["ids"]

        put_pay_mode(api, "payable_days", extra={
            "proration_basis": "working_days", "ot_rounding": "round_at_end",
        })

        others = [{"user_id": uid, "reason": "sample"} for code, uid in core["ids"].items() if code not in ("E1",)]
        fin = api.request("POST", "/api/payroll/finalize", json_body={
            "month": "2026-09", "excluded": others,
        })
        snap_before = None
        if isinstance(fin[1], dict):
            snap_before = (fin[1].get("payroll") or {}).get("snapshot") or fin[1].get("snapshot")
        if not snap_before:
            got = api.request("GET", "/api/payroll?month=2026-09")[1] or {}
            snap_before = got.get("snapshot")
        put_pay_mode(api, "payable_days", extra={
            "proration_basis": "calendar_days", "ot_rounding": "round_rate_first",
        })
        after = api.request("GET", "/api/payroll?month=2026-09")[1] or {}
        snap_after = after.get("snapshot") or {}
        out["snapshot_immutability"] = {
            "finalize_status": fin[0],
            "still_finalized": after.get("is_finalized"),
            "snapshot_before_settings": (snap_before or {}).get("settings") if isinstance(snap_before, dict) else snap_before,
            "snapshot_after_settings_change": snap_after.get("settings") if isinstance(snap_after, dict) else snap_after,
            "settings_blob_unchanged": (
                isinstance(snap_before, dict) and isinstance(snap_after, dict)
                and (snap_before.get("settings") == snap_after.get("settings"))
            ),
            "live_settings_now": api.request("GET", "/api/settings/payroll")[1],
        }

        oct_days = scheduled_days_in_month(2026, 10, working_days=DEFAULT_WORKING_DAYS)[:3]
        bulk_present(api, [core["ids"]["E1"]], oct_days)
        early = api.request("POST", "/api/payroll/finalize", json_body={
            "month": "2026-10", "finalize_early": True, "excluded": others,
        })
        audits = db.list_payroll_audit("2026-10", organization_id=ORG1, branch_id=BR1)
        out["finalize_early"] = {
            "http": {"status": early[0], "success": (early[1] or {}).get("success") if isinstance(early[1], dict) else None},
            "audit_rows": audits,
            "finalized_early_row": next((a for a in audits if a.get("action") == "payroll_finalized"), None),
        }

        out["patterns"] = patterns
    finally:
        api.close()
    (dest / "partial.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    legacy_path = work / "legacy.db"
    make_legacy_snapshot(legacy_path)
    os.environ["QINTELLECT_DB_PATH"] = str(legacy_path)
    db_config.DB_PATH = str(legacy_path)
    db.DB_PATH = str(legacy_path)
    db.init_db()
    with sqlite3.connect(str(legacy_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO users (name, email, organization_id, branch_id, salary, join_date, active) VALUES (?,?,?,?,?,?,1)",
            ("Legacy Staff", "legacy@org.example", "ORG-LEGACY", "BR-LEGACY", 50000, "2026-09-01"),
        )
        uid = conn.execute("SELECT id FROM users WHERE email='legacy@org.example'").fetchone()[0]
        conn.execute(
            "INSERT INTO salary_configs (user_id, basic_salary, organization_id, branch_id) VALUES (?,?,?,?)",
            (uid, 50000, "DEFAULT_ORG", "DEFAULT_BRANCH"),
        )
        report_salary_config_tenant_mismatches(conn.cursor())
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM salary_config_tenant_report").fetchone()[0]
        rows = [dict(r) for r in conn.execute("SELECT * FROM salary_config_tenant_report").fetchall()]
    out["legacy_tenant_report"] = {"count": count, "rows": rows, "auto_fixed": False}

    (dest / "result.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    def table(diff):
        return [{
            "employee": r["employee"],
            "expected_net": r["expected_net"],
            "live_net": r["live_net"],
            "diff_to_cent": r["diff_to_cent"],
            "match": r["match"],
            "pending_c4": r.get("pending_c4"),
            "matched_rule": (r.get("live") or {}).get("matched_rule"),
        } for r in diff]

    print(json.dumps({
        "s10_working_days_round_at_end": table(out["s10_working_days_round_at_end"]["diff"]),
        "s10_calendar_days_round_rate_first": table(out["s10_calendar_days_round_rate_first"]["diff"]),
        "s10_wd_mismatches": [r for r in out["s10_working_days_round_at_end"]["diff"] if not r["match"]],
        "s10_cal_mismatches": [r for r in out["s10_calendar_days_round_rate_first"]["diff"] if not r["match"]],
        "e5": out["e5_seed"]["live"],
        "s9": out["s9"],
        "ui_db": out["ui_db"],
    }, indent=2, default=str))
    return 0


def seed_s9(api) -> dict:
    from tests.fixtures.payroll_audit_seed import ORG1, BR1, ALL_MODULES, put_pay_mode, get_payroll, bulk_present, seed_core_org
    from weekday_utils import DEFAULT_WORKING_DAYS, scheduled_days_in_month
    # Same org already logged in.
    put = put_pay_mode(api, "full_salary", extra={"enable_salary_revisions": 1})
    settings = api.request("GET", "/api/settings/payroll", expected=200)[1]
    payroll = get_payroll(api, "2026-09")
    e10 = next((r for r in (payroll.get("records") or []) if r.get("employee_name") == "E10 Salary Change"), None)
    return {
        "put_mode": put.get("response_settings_mode"),
        "get_settings_mode": None if not isinstance(settings, dict) else settings.get("attendance_pay_mode"),
        "engine_preview_mode": payroll.get("attendance_pay_mode"),
        "e10_mode": None if not e10 else e10.get("attendance_pay_mode"),
        "returns_full_salary": (
            put.get("response_settings_mode") == "full_salary"
            and (settings or {}).get("attendance_pay_mode") == "full_salary"
            and payroll.get("attendance_pay_mode") == "full_salary"
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
