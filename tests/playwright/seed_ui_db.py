"""Build a fresh S10 scratch DB with no bonuses for UI screenshots/tests."""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def seed_fresh_ui_db(dest: Path) -> Path:
    from tests.run_payroll_scenario import make_clean_snapshot
    from tests.fixtures.payroll_audit_seed import (
        ALL_MODULES, BR1, ORG1, LiveAPI, add_absence_brackets, get_payroll,
        put_pay_mode, seed_core_org, seed_september_patterns, SEP_MONTH,
    )
    import config as db_config
    import database as db
    import app as app_module

    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="qe-ui-fresh-"))
    snap = work / "clean.db"
    make_clean_snapshot(snap)
    db_path = work / "attendance.db"
    shutil.copy2(snap, db_path)
    os.environ["QINTELLECT_DB_PATH"] = str(db_path)
    db_config.DB_PATH = str(db_path)
    db.DB_PATH = str(db_path)
    app_module.app.config["TESTING"] = True
    api = LiveAPI(app_module.app.test_client(), app_module)
    try:
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        add_absence_brackets(api, "percentage")
        put_pay_mode(api, "payable_days", extra={
            "deduct_unpaid_leave": True,
            "enable_named_components": 1,
            "enable_bonuses": 1,
            "proration_basis": "working_days",
            "ot_rounding": "round_at_end",
        })
        seed_september_patterns(api, core["ids"])
        get_payroll(api, SEP_MONTH)
    finally:
        api.close()
    with sqlite3.connect(str(db_path)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM payroll_bonuses").fetchone()[0]
        if n:
            raise RuntimeError(f"fresh UI db still has {n} bonus row(s)")
    shutil.copy2(db_path, dest)
    return dest


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "evidence" / "after" / "PHASEA_REVIEW" / "ui.db"
    print(seed_fresh_ui_db(target))
