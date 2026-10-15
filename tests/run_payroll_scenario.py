#!/usr/bin/env python
"""Reset a scratch DB, seed one named payroll scenario through the real API, compare the hand sheet.

Usage (from Local-Dashboard):

  python tests/run_payroll_scenario.py --backup-dev-db
  python tests/run_payroll_scenario.py S1 --evidence-dir evidence/before
  python tests/run_payroll_scenario.py --all --evidence-dir evidence/before

Each scenario uses a FRESH sqlite file restored from a clean init_db snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCENARIOS = ["A0"] + [f"S{i}" for i in range(1, 19)]


def backup_dev_db(dest_dir: Path) -> str:
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = ROOT / "attendance.db"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"attendance.db.{stamp}.bak"
    if src.exists():
        shutil.copy2(src, dest)
        return str(dest)
    return f"NO_DEV_DB:{src}"


def make_legacy_snapshot(path: Path) -> None:
    """Pre-tenant singleton payroll_settings (no org/branch, no attendance_pay_mode)."""
    if path.exists():
        path.unlink()
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE payroll_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                deduct_unpaid_leave INTEGER NOT NULL DEFAULT 0,
                half_leave_quota_mode TEXT NOT NULL DEFAULT 'category',
                half_leaves_per_quota_day INTEGER NOT NULL DEFAULT 2,
                holiday_ot_premium_percent REAL NOT NULL DEFAULT 0,
                non_working_day_ot_premium_percent REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("INSERT INTO payroll_settings (id, deduct_unpaid_leave) VALUES (1, 0)")
        conn.commit()


def make_clean_snapshot(path: Path) -> None:
    if path.exists():
        path.unlink()
    os.environ["QINTELLECT_DB_PATH"] = str(path)
    import config as db_config
    import database as db
    db_config.DB_PATH = str(path)
    db.DB_PATH = str(path)
    if not db.init_db():
        raise RuntimeError("init_db failed while building clean snapshot")


def compare_s1(result, expected) -> dict:
    exp_pay = next(r for r in expected["expected"] if r["attendance_pay_mode"] == "payable_days")
    exp_full = next(r for r in expected["expected"] if r["attendance_pay_mode"] == "full_salary")
    live_pay = next((r for r in result.get("payable_days") or [] if r.get("employee_name") == "E1 Full Attend"), None)
    live_full = next((r for r in result.get("full_salary") or [] if r.get("employee_name") == "E1 Full Attend"), None)
    return {
        "payable_days": {
            "expected_net": exp_pay["expected_net"],
            "live_net": None if not live_pay else live_pay.get("net_salary"),
            "match": bool(live_pay) and float(live_pay.get("net_salary") or 0) == float(exp_pay["expected_net"]),
            "live": live_pay,
        },
        "full_salary": {
            "expected_net": exp_full["expected_net"],
            "live_net": None if not live_full else live_full.get("net_salary"),
            "match": bool(live_full) and float(live_full.get("net_salary") or 0) == float(exp_full["expected_net"]),
            "live": live_full,
        },
        "scheduled_count_live": result.get("scheduled_count"),
        "scheduled_count_expected": exp_pay["month_scheduled_workdays"],
        "enrollment": result.get("core", {}).get("unlock") or result.get("unlock"),
    }


def compare_generic(result, expected) -> dict:
    return {"expected": expected, "live_keys": list(result.keys())}


COMPARERS = {
    "S1": compare_s1,
}


def run_isolated(scenario: str, evidence_dir: Path, snapshot: Path) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"qe-payroll-{scenario}-"))
    db_path = work / "attendance.db"
    shutil.copy2(snapshot, db_path)
    os.environ["QINTELLECT_DB_PATH"] = str(db_path)
    os.environ.setdefault("MAIN_DASHBOARD_URL", "http://127.0.0.1:9")

    # Fresh process imports: drop cached dashboard modules bound to another DB.
    doomed = [name for name in list(sys.modules) if name in {
        "app", "database", "config", "payroll_extensions", "payroll_policy_schema",
        "tests.fixtures.payroll_audit_seed", "tests.fixtures.expected_catalog",
    } or name.startswith("tests.fixtures.")]
    for name in doomed:
        sys.modules.pop(name, None)

    import config as db_config
    import database as db
    import app as app_module
    from tests.fixtures.expected_catalog import SCENARIOS as SHEETS, dump_expected
    from tests.fixtures.payroll_audit_seed import LiveAPI, seed_scenario

    db_config.DB_PATH = str(db_path)
    db.DB_PATH = str(db_path)
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False

    expected_dir = ROOT / "tests" / "fixtures" / "expected"
    dump_expected(expected_dir)
    expected = SHEETS[scenario]()

    api = LiveAPI(app_module.app.test_client(), app_module)
    started = datetime.now().isoformat()
    error = None
    result = None
    try:
        result = seed_scenario(api, scenario)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc), "trace": traceback.format_exc()}
    finally:
        api.close()

    comparer = COMPARERS.get(scenario, compare_generic)
    comparison = comparer(result or {}, expected)
    payload = {
        "scenario": scenario,
        "started": started,
        "finished": datetime.now().isoformat(),
        "db_path": str(db_path),
        "error": error,
        "comparison": comparison,
        "result": result,
        "api_log_tail": api.log[-40:],
        "users_in_db": _count_users(db_path),
        "db_rows": _dump_db_rows(db_path),
        "payroll_settings_rows": _dump_settings(db_path),
    }
    out_dir = evidence_dir / scenario
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    (out_dir / "expected.json").write_text(json.dumps(expected, indent=2, default=str), encoding="utf-8")
    return payload


def _dump_settings(db_path: Path) -> list:
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM payroll_settings ORDER BY id").fetchall()]
    except sqlite3.Error as exc:
        return [{"error": str(exc)}]


def _count_users(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(row[0]) if row else 0


def _dump_db_rows(db_path: Path) -> dict:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute(
            "SELECT id, name, salary, join_date, exit_date, active, organization_id, branch_id FROM users ORDER BY id"
        ).fetchall()
        att = conn.execute(
            "SELECT date(timestamp) AS day, COUNT(*) AS n FROM attendance GROUP BY date(timestamp) ORDER BY day"
        ).fetchall()
        return {
            "users": [dict(r) for r in users[:50]],
            "user_count": len(users),
            "attendance_by_day": [dict(r) for r in att],
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Isolated payroll scenario runner")
    parser.add_argument("scenario", nargs="?", help="S1 .. S14")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--evidence-dir", default="evidence/before")
    parser.add_argument("--backup-dev-db", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    evidence_dir = Path(args.evidence_dir)
    if not evidence_dir.is_absolute():
        evidence_dir = ROOT / evidence_dir
    evidence_dir.mkdir(parents=True, exist_ok=True)

    if args.backup_dev_db:
        dest = ROOT / "backup"
        print(backup_dev_db(dest))
        if not args.scenario and not args.all:
            return 0

    wanted = SCENARIOS if args.all else [str(args.scenario).upper()]
    for sid in wanted:
        if sid not in SCENARIOS:
            print(f"Unknown scenario {sid}", file=sys.stderr)
            return 2

    snapshot = Path(tempfile.mkdtemp(prefix="qe-snap-")) / "clean.db"
    if wanted == ["S18"] or (len(wanted) == 1 and wanted[0] == "S18"):
        make_legacy_snapshot(snapshot)
    else:
        make_clean_snapshot(snapshot)

    summaries = []
    if args.all and not args.child:
        for sid in wanted:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__)), sid, "--evidence-dir", str(evidence_dir), "--child"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            summaries.append({
                "scenario": sid,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            })
            print(proc.stdout)
            if proc.stderr:
                print(proc.stderr, file=sys.stderr)
        (evidence_dir / "SUMMARY.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        return 0 if all(item["returncode"] == 0 for item in summaries) else 1

    scenario = wanted[0]
    payload = run_isolated(scenario, evidence_dir, snapshot)
    print(json.dumps({
        "scenario": scenario,
        "error": payload.get("error"),
        "users_in_db": payload.get("users_in_db"),
        "comparison_match": payload.get("comparison"),
    }, indent=2, default=str)[:8000])
    return 1 if payload.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
