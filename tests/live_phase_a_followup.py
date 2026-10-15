#!/usr/bin/env python
"""Live Phase A follow-up: leave/OT lock, finalize-early, snapshot, settings, salary report."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weekday_utils import DEFAULT_WORKING_DAYS, scheduled_days_in_month


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="qe-phase-a-followup-"))
    db_path = scratch / "attendance.db"
    os.environ["QINTELLECT_DB_PATH"] = str(db_path)
    os.environ["QINTELLECT_SKIP_AUTO_INIT"] = "0"

    import config as db_config
    import database as db
    import app as app_module
    from tests.fixtures.expected_catalog import dump_expected, scenario_s10
    from tests.fixtures.payroll_audit_seed import (
        ALL_MODULES,
        BR1,
        ORG1,
        LiveAPI,
        bulk_present,
        get_payroll,
        put_pay_mode,
        seed_core_org,
    )

    db_config.DB_PATH = str(db_path)
    db.DB_PATH = str(db_path)
    if not db.init_db():
        raise SystemExit("init_db failed")

    expected_dir = ROOT / "tests" / "fixtures" / "expected"
    dump_expected(expected_dir)
    s10 = scenario_s10()

    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    api = LiveAPI(app_module.app.test_client(), app_module)
    out: dict = {"db_path": str(db_path), "s10_formula_expected": {
        row["code"]: {"expected_net": row["expected_net"], "notes": row.get("notes")}
        for row in s10["expected"]
    }, "s10_e6_e7_bases": s10["assumptions"].get("e6_e7_bases")}
    try:
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        put_pay_mode(api, "payable_days")
        scheduled = scheduled_days_in_month(2026, 9, working_days=DEFAULT_WORKING_DAYS)
        e1 = core["ids"]["E1"]
        bulk_present(api, [e1, core["ids"]["E11"]], scheduled)

        leave_st, leave_body, _ = api.request("POST", "/api/leaves", json_body={
            "user_id": e1, "leave_type": "casual", "leave_duration": "full",
            "start_date": "2026-09-10", "end_date": "2026-09-10", "reason": "pre-finalize",
        })
        leave_id = (leave_body or {}).get("id") if isinstance(leave_body, dict) else None
        if leave_id:
            api.request("PUT", f"/api/leaves/{leave_id}", json_body={"status": "pending"})
        ot_st, ot_body, _ = api.request("POST", "/api/overtime", json_body={
            "user_id": e1, "ot_date": "2026-09-08", "hours": 2, "reason": "pre-finalize",
        })
        ot_id = (ot_body or {}).get("id") if isinstance(ot_body, dict) else None

        fin = api.request("POST", "/api/payroll/finalize", json_body={
            "month": "2026-09",
            "excluded": [{"user_id": core["ids"]["E11"], "reason": "No salary configured this month"}],
        })
        payroll = api.request("GET", "/api/payroll?month=2026-09")
        snap = None
        if isinstance(payroll[1], dict):
            snap = payroll[1].get("snapshot") or payroll[1].get("snapshot_json")

        leave_lock = api.request("PUT", f"/api/leaves/{leave_id}", json_body={"status": "approved"}) if leave_id else (None, None, None)
        leave_add_lock = api.request("POST", "/api/leaves", json_body={
            "user_id": e1, "leave_type": "casual", "leave_duration": "full",
            "start_date": "2026-09-14", "end_date": "2026-09-14", "reason": "after finalize",
        })
        ot_lock = api.request("PUT", f"/api/overtime/{ot_id}", json_body={"status": "approved"}) if ot_id else (None, None, None)

        settings = api.request("GET", "/api/settings/payroll")
        preview = api.request("POST", "/api/settings/payroll/preview", json_body={
            "proration_basis": "working_days", "ot_rounding": "round_at_end", "month": "2026-09",
        })
        salary_report = api.request("GET", "/api/salary/tenant-report")
        put_settings = put_pay_mode(api, "payable_days", extra={
            "proration_basis": "working_days", "ot_rounding": "round_at_end",
        })
        after_put = api.request("GET", "/api/settings/payroll")
        still_final = api.request("GET", "/api/payroll?month=2026-09")

        oct_days = scheduled_days_in_month(2026, 10, working_days=DEFAULT_WORKING_DAYS)[:3]
        bulk_present(api, [e1], oct_days)
        others = [{"user_id": uid, "reason": "Not in early-run sample"} for code, uid in core["ids"].items() if code != "E1"]
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("DELETE FROM employee_shift_history WHERE user_id = ?", (e1,))
            conn.execute("UPDATE users SET shift_id = 0 WHERE id = ?", (e1,))
            conn.commit()
        missing = get_payroll(api, "2026-10")
        missing_flag = any(r.get("shift_history_missing") for r in (missing.get("records") or []) if r.get("employee_name") == "E1 Full Attend")
        shift_dialog = "Missing shift assignment history" in (
            (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        )
        api.request(
            "POST",
            f"/api/users/{e1}/assign-shift",
            json_body={"shift_id": core["shift_id"], "effective_from": "2026-09-01"},
            expected=(200, 201),
        )
        early_blocked = api.request("POST", "/api/payroll/finalize", json_body={"month": "2026-10", "excluded": others})
        early_ok = api.request("POST", "/api/payroll/finalize", json_body={
            "month": "2026-10", "finalize_early": True, "excluded": others,
        })

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "SELECT organization_id, proration_basis, ot_rounding FROM payroll_settings WHERE organization_id = ?",
                (ORG1,),
            ).fetchone()
            conn.execute(
                "UPDATE salary_configs SET organization_id = 'DEFAULT_ORG', branch_id = 'DEFAULT_BRANCH' WHERE user_id = ?",
                (e1,),
            )
            from payroll_policy_schema import report_salary_config_tenant_mismatches
            report_salary_config_tenant_mismatches(conn.cursor())
            conn.commit()
            mismatches = [dict(r) for r in conn.execute("SELECT * FROM salary_config_tenant_report").fetchall()]

        api.login("ORG-NEW-DEFAULTS", "BR-NEW", ALL_MODULES)
        new_settings = api.request("GET", "/api/settings/payroll")

        out.update({
            "pre_finalize_leave": {"status": leave_st, "id": leave_id},
            "pre_finalize_ot": {"status": ot_st, "id": ot_id},
            "finalize_sep": {"status": fin[0], "success": (fin[1] or {}).get("success") if isinstance(fin[1], dict) else None},
            "snapshot_keys": sorted((snap or {}).keys()) if isinstance(snap, dict) else type(snap).__name__,
            "snapshot_settings": (snap or {}).get("settings") if isinstance(snap, dict) else None,
            "leave_approve_finalized": {"status": leave_lock[0], "body": leave_lock[1]},
            "leave_add_finalized": {"status": leave_add_lock[0], "body": leave_add_lock[1]},
            "ot_approve_finalized": {"status": ot_lock[0], "body": ot_lock[1]},
            "settings_notices": (settings[1] or {}).get("notices") if isinstance(settings[1], dict) else None,
            "existing_org_settings": dict(existing) if existing else None,
            "preview": preview[1] if preview[0] == 200 else {"status": preview[0], "body": preview[1]},
            "salary_report_api": salary_report[1],
            "put_settings_http": put_settings.get("http_status"),
            "after_put_rounding": (after_put[1] or {}).get("ot_rounding") if isinstance(after_put[1], dict) else None,
            "finalized_still_locked": (still_final[1] or {}).get("is_finalized") if isinstance(still_final[1], dict) else None,
            "finalize_oct_blocked": {"status": early_blocked[0], "error": (early_blocked[1] or {}).get("error") if isinstance(early_blocked[1], dict) else early_blocked[1]},
            "finalize_oct_early": {"status": early_ok[0], "body": early_ok[1] if not isinstance(early_ok[1], dict) else {k: early_ok[1].get(k) for k in ("success", "error", "issues")}},
            "shift_history_missing_after_delete": missing_flag,
            "shift_history_confirm_dialog_in_ui": shift_dialog,
            "salary_mismatch_rows": mismatches,
            "new_org_defaults": {
                "proration_basis": (new_settings[1] or {}).get("proration_basis") if isinstance(new_settings[1], dict) else None,
                "ot_rounding": (new_settings[1] or {}).get("ot_rounding") if isinstance(new_settings[1], dict) else None,
            },
            "today": date.today().isoformat(),
        })
    finally:
        api.close()

    dest = ROOT / "evidence" / "after" / "PHASEA_FOLLOWUP"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "result.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
