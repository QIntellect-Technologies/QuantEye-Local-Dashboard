"""Flask server for Phase A UI screenshots. Does not warm InsightFace beyond import."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FLASK_SECRET_KEY", "phase-a-ui-review-secret-key-32bytes")
os.environ.setdefault("QINTELLECT_UI_REVIEW", "1")
os.environ.setdefault("QINTELLECT_UI_ORG", "ORG-PAY-AUDIT-1")
os.environ.setdefault("QINTELLECT_UI_BRANCH", "BR-PAY-AUDIT-1")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import session
import app as app_module

app = app_module.app


@app.before_request
def _ui_review_session():
    if os.environ.get("QINTELLECT_UI_REVIEW") != "1":
        return
    session["client_user"] = True
    session["admin_unlocked"] = True
    session["organization_id"] = os.environ.get("QINTELLECT_UI_ORG") or "ORG-PAY-AUDIT-1"
    session["branch_id"] = os.environ.get("QINTELLECT_UI_BRANCH") or "BR-PAY-AUDIT-1"
    session["licensed_modules"] = ["attendance", "shifts", "leave", "overtime", "payroll"]
    session["organization_name"] = session["organization_id"]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5057"))
    app.run(host="127.0.0.1", port=port, use_reloader=False, debug=False)
