"""Deterministic payroll audit seeder.

Every write goes through the real Local-Dashboard HTTP API (Flask test client).
Weekdays come from weekday_utils — never hard-coded 1-6 ISO numbers.

Enrollment: POST /api/users is expected to return 403 until POST /api/enrollment/unlock
with the org enrollment key. Main-dashboard key verify is stubbed with a local
success payload so scratch tests never call production Railway.
"""
from __future__ import annotations

import io
import json
from datetime import date
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import patch

from weekday_utils import DEFAULT_WORKING_DAYS, scheduled_days_in_month

ORG1 = "ORG-PAY-AUDIT-1"
BR1 = "BR-PAY-AUDIT-1"
ORG2 = "ORG-PAY-AUDIT-2"
BR2 = "BR-PAY-AUDIT-2"
ORG3 = "ORG-PAY-AUDIT-3"
BR3 = "BR-PAY-AUDIT-3"

ENROLLMENT_KEYS = {
    ORG1: "ENR-AUDIT-ORG1",
    ORG2: "ENR-AUDIT-ORG2",
    ORG3: "ENR-AUDIT-ORG3",
}

ALL_MODULES = ["attendance", "shifts", "leave", "overtime", "payroll"]
NO_LEAVE_OT = ["attendance", "shifts", "payroll"]
NO_PAYROLL = ["attendance", "shifts"]

SEP_MONTH = "2026-09"
OCT_MONTH = "2026-10"

CATALOG_14 = [
    {"code": "E1", "name": "E1 Full Attend", "salary": 60000, "department": "Finance", "join_date": "2026-09-01"},
    {"code": "E2", "name": "E2 Two Absent", "salary": 60000, "department": "Ops", "join_date": "2026-09-01"},
    {"code": "E3", "name": "E3 Six Absent", "salary": 60000, "department": "Ops", "join_date": "2026-09-01"},
    {"code": "E4", "name": "E4 Late Half", "salary": 60000, "department": "Support", "join_date": "2026-09-01"},
    {"code": "E5", "name": "E5 Leave Mix", "salary": 60000, "department": "HR", "join_date": "2026-09-01"},
    {"code": "E6", "name": "E6 Join Mid", "salary": 60000, "department": "Ops", "join_date": "2026-09-11"},
    {"code": "E7", "name": "E7 Exit Mid", "salary": 60000, "department": "Ops", "join_date": "2026-09-01"},
    {"code": "E8", "name": "E8 Overtime", "salary": 60000, "department": "Support", "join_date": "2026-09-01"},
    {"code": "E9", "name": "E9 Flex Pay", "salary": 60000, "department": "Finance", "join_date": "2026-09-01"},
    {"code": "E10", "name": "E10 Salary Change", "salary": 40000, "department": "Admin", "join_date": "2026-09-01"},
    {"code": "E11", "name": "E11 No Salary", "salary": 0, "department": "QA", "join_date": "2026-09-01"},
    {"code": "E12", "name": "E12 Deactivate", "salary": 60000, "department": "Ops", "join_date": "2026-09-01"},
    {"code": "E13", "name": "E13 Extra", "salary": 42000, "department": "Admin", "join_date": "2026-09-01"},
    {"code": "E14", "name": "E14 Absurd QA", "salary": 10_000_000, "department": "QA", "join_date": "2026-09-01"},
]


class ApiError(RuntimeError):
    pass


class LiveAPI:
    def __init__(self, client, app_module):
        self.client = client
        self.app_module = app_module
        self.log: List[Dict[str, Any]] = []
        self._config_patch = None
        self._urlopen_patch = None

    def login(self, org_id: str, branch_id: str, modules: Sequence[str]) -> None:
        if self._config_patch:
            self._config_patch.stop()
        self._config_patch = patch.object(
            self.app_module,
            "load_integration_config",
            return_value={
                "linked": True,
                "organization_id": "ORG-FILE-MUST-NOT-WIN",
                "branch_id": "BR-FILE-MUST-NOT-WIN",
                "licensed_modules": ["attendance"],
            },
        )
        self._config_patch.start()
        with self.client.session_transaction() as session:
            session.clear()
            session["client_user"] = True
            session["admin_unlocked"] = True
            session["organization_id"] = org_id
            session["branch_id"] = branch_id
            session["licensed_modules"] = list(modules)
            session["organization_name"] = org_id

    def close(self) -> None:
        if self._config_patch:
            self._config_patch.stop()
            self._config_patch = None
        if self._urlopen_patch:
            self._urlopen_patch.stop()
            self._urlopen_patch = None

    def request(self, method: str, path: str, json_body=None, data=None, content_type=None, expected=None):
        kwargs = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if content_type:
            kwargs["content_type"] = content_type
        response = getattr(self.client, method.lower())(path, **kwargs)
        try:
            body = response.get_json(silent=True)
        except Exception:
            body = None
        if body is None:
            raw = response.get_data(as_text=False)
            ctype = (response.headers or {}).get("Content-Type", "")
            if "json" not in ctype.lower() and raw:
                body = {
                    "binary": True,
                    "content_type": ctype,
                    "bytes": len(raw),
                }
            else:
                try:
                    body = raw.decode("utf-8")[:4000]
                except UnicodeDecodeError:
                    body = {"binary": True, "bytes": len(raw or b"")}
        rec = {"method": method, "path": path, "status": response.status_code, "body": _clip(body)}
        self.log.append(rec)
        if expected is not None:
            exp = expected if isinstance(expected, (list, tuple, set)) else (expected,)
            if response.status_code not in exp:
                raise ApiError(f"{method} {path} -> {response.status_code}: {body}")
        return response.status_code, body, response

    def unlock_enrollment(self, org_id: str) -> Dict[str, Any]:
        key = ENROLLMENT_KEYS[org_id]
        denied_status, denied_body, _ = self.request("POST", "/api/users", json_body={"name": "Should Be Locked"})
        if self._urlopen_patch:
            self._urlopen_patch.stop()
        self._urlopen_patch = patch("urllib.request.urlopen")
        mocked = self._urlopen_patch.start()
        mocked.return_value.__enter__.return_value.read.return_value = json.dumps({"success": True}).encode("utf-8")
        st, body, _ = self.request(
            "POST",
            "/api/enrollment/unlock",
            json_body={"enrollment_key": key},
            expected=200,
        )
        return {
            "pre_unlock_post_users": {"status": denied_status, "body": denied_body},
            "unlock": {"status": st, "body": body, "key_used": key},
        }


def _clip(body):
    if isinstance(body, dict) and "records" in body and isinstance(body["records"], list) and len(body["records"]) > 30:
        slim = dict(body)
        slim["records"] = body["records"][:3]
        slim["_records_clipped"] = len(body["records"])
        return slim
    return body


def users_by_name(api: LiveAPI) -> Dict[str, int]:
    st, body, _ = api.request("GET", "/api/users", expected=200)
    rows = body.get("users") if isinstance(body, dict) else body
    out = {}
    for row in rows or []:
        out[row["name"]] = row["id"]
    return out


def create_mon_sat_shift(api: LiveAPI, name: str = "Audit Mon-Sat") -> int:
    st, body, _ = api.request(
        "POST",
        "/api/shifts",
        json_body={
            "name": name,
            "start_time": "09:00",
            "end_time": "17:00",
            "grace_period_minutes": 15,
            "working_days": list(DEFAULT_WORKING_DAYS),
        },
        expected=(200, 201),
    )
    return int(body.get("shift_id") or body.get("id"))


def put_pay_mode(api: LiveAPI, mode: str, extra: Optional[dict] = None) -> Any:
    st, current, _ = api.request("GET", "/api/settings/payroll", expected=200)
    payload = dict(current)
    payload["deduct_unpaid_leave"] = bool(payload.get("deduct_unpaid_leave"))
    payload["attendance_pay_mode"] = mode
    if extra:
        payload.update(extra)
    st, saved, _ = api.request("PUT", "/api/settings/payroll", json_body=payload, expected=200)
    return saved


def create_employee(api: LiveAPI, spec: dict, shift_id: int, email_domain: str) -> int:
    payload = {
        "name": spec["name"],
        "email": f"{spec['code'].lower()}@{email_domain}",
        "department": spec.get("department") or "Ops",
        "position": spec.get("position") or "Staff",
        "salary": spec.get("salary") or 0,
        "join_date": spec.get("join_date") or "2026-09-01",
        "shift_id": shift_id,
    }
    st, body, _ = api.request("POST", "/api/users", json_body=payload, expected=201)
    user_id = int(body["user_id"])
    api.request(
        "PUT",
        f"/api/salary/{user_id}",
        json_body={
            "user_id": user_id,
            "basic_salary": spec.get("salary") or 0,
            "allowances": 0,
            "deductions": 0,
            "ot_rate": 0,
        },
        expected=(200, 201),
    )
    api.request(
        "POST",
        f"/api/users/{user_id}/assign-shift",
        json_body={"shift_id": shift_id, "effective_from": spec.get("join_date") or "2026-09-01"},
        expected=(200, 201),
    )
    return user_id


def create_catalog(api: LiveAPI, shift_id: int, domain: str, people=None) -> Dict[str, int]:
    ids = {}
    for spec in people or CATALOG_14:
        ids[spec["code"]] = create_employee(api, spec, shift_id, domain)
    created = users_by_name(api)
    missing = [p["name"] for p in (people or CATALOG_14) if p["name"] not in created]
    if missing:
        raise ApiError(f"Seeder did not create all employees: {missing}")
    return ids


def mark_present(api: LiveAPI, user_id: int, day: date, check_in: str = "09:05") -> None:
    api.request(
        "POST",
        f"/api/users/{user_id}/mark-present",
        json_body={"date": day.isoformat(), "check_in_time": check_in, "check_out_time": "17:05"},
        expected=(200, 201),
    )


def bulk_present(api: LiveAPI, user_ids: Sequence[int], days: Sequence[date]) -> None:
    ids = [int(x) for x in user_ids]
    for day in days:
        api.request(
            "POST",
            "/api/attendance/bulk-mark",
            json_body={"action": "present", "date": day.isoformat(), "user_ids": ids},
            expected=(200, 207),
        )


def add_absence_brackets(api: LiveAPI, kind: str = "percentage") -> None:
    if kind == "percentage":
        rules = [
            {"min_absent_days": 1, "max_absent_days": 2, "deduction_type": "percentage", "deduction_percent": 5, "active": True},
            {"min_absent_days": 3, "max_absent_days": 5, "deduction_type": "percentage", "deduction_percent": 10, "active": True},
            {"min_absent_days": 6, "max_absent_days": None, "deduction_type": "percentage", "deduction_percent": 20, "active": True},
        ]
    else:
        rules = [
            {"min_absent_days": 1, "max_absent_days": 2, "deduction_type": "fixed", "fixed_amount": 2000, "deduction_percent": 0, "active": True},
            {"min_absent_days": 3, "max_absent_days": 5, "deduction_type": "fixed", "fixed_amount": 4000, "deduction_percent": 0, "active": True},
            {"min_absent_days": 6, "max_absent_days": None, "deduction_type": "fixed", "fixed_amount": 8000, "deduction_percent": 0, "active": True},
        ]
    for rule in rules:
        api.request("POST", "/api/payroll/rules", json_body=rule, expected=(200, 201))


def get_payroll(api: LiveAPI, month: str) -> Any:
    st, body, _ = api.request("GET", f"/api/payroll?month={month}&recalculate=1", expected=200)
    return body


def slim_records(payroll: Any) -> List[dict]:
    keys = (
        "employee_name", "basic_salary", "allowances", "present_days", "absent_days",
        "late_days", "payable_days", "month_scheduled_workdays", "off_day_extra_pay",
        "overtime_amount", "overtime_hours", "bonus_amount", "absence_deduction",
        "unpaid_leave_deduction", "half_leave_deduction", "custom_deductions",
        "pay_lines", "gross_salary", "deduction_amount", "net_salary", "matched_rule",
        "attendance_pay_mode", "issues", "remarks",
    )
    out = []
    for row in (payroll or {}).get("records") or []:
        out.append({k: row.get(k) for k in keys})
    return out


def seed_core_org(api: LiveAPI, org_id: str, branch_id: str, modules: Sequence[str], domain: str) -> Dict[str, Any]:
    api.login(org_id, branch_id, modules)
    unlock = api.unlock_enrollment(org_id)
    shift_id = create_mon_sat_shift(api)
    ids = create_catalog(api, shift_id, domain)
    return {"unlock": unlock, "shift_id": shift_id, "ids": ids}


def _skip_first(scheduled: List[date], n: int) -> List[date]:
    return scheduled[n:]


def seed_september_patterns(api: LiveAPI, ids: Dict[str, int], holidays=()) -> Dict[str, Any]:
    scheduled = scheduled_days_in_month(2026, 9, working_days=DEFAULT_WORKING_DAYS, holidays=holidays)
    by_code = ids
    # Perfect / extra / absurd / deactivate / salary-change / no-salary / flex
    bulk_present(api, [by_code[c] for c in ("E1", "E9", "E10", "E11", "E12", "E13", "E14") if c in by_code], scheduled)
    if "E2" in by_code:
        bulk_present(api, [by_code["E2"]], _skip_first(scheduled, 2))
    if "E3" in by_code:
        bulk_present(api, [by_code["E3"]], _skip_first(scheduled, 6))
    if "E4" in by_code:
        for day in scheduled[:3]:
            mark_present(api, by_code["E4"], day, "10:30")
        bulk_present(api, [by_code["E4"]], scheduled[3:])
    if "E5" in by_code:
        skip = {date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)}
        bulk_present(api, [by_code["E5"]], [d for d in scheduled if d not in skip])
        st, lv, _ = api.request(
            "POST",
            "/api/leaves",
            json_body={
                "user_id": by_code["E5"],
                "leave_type": "casual",
                "leave_duration": "full",
                "start_date": "2026-09-08",
                "end_date": "2026-09-09",
                "reason": "audit paid",
            },
        )
        lid = lv.get("id") if isinstance(lv, dict) else None
        if lid:
            api.request("PUT", f"/api/leaves/{lid}", json_body={"status": "approved"}, expected=(200, 201))
        st, lv2, _ = api.request(
            "POST",
            "/api/leaves",
            json_body={
                "user_id": by_code["E5"],
                "leave_type": "unpaid",
                "leave_duration": "full",
                "start_date": "2026-09-10",
                "end_date": "2026-09-10",
                "reason": "audit unpaid",
            },
        )
        lid2 = lv2.get("id") if isinstance(lv2, dict) else None
        if lid2:
            api.request("PUT", f"/api/leaves/{lid2}", json_body={"status": "approved"}, expected=(200, 201))
    if "E6" in by_code:
        join = date(2026, 9, 11)
        bulk_present(api, [by_code["E6"]], [d for d in scheduled if d >= join])
    if "E7" in by_code:
        exit_d = date(2026, 9, 18)
        bulk_present(api, [by_code["E7"]], [d for d in scheduled if d <= exit_d])
        # Real API: try to set exit_date (A8 documents if ignored).
        api.request(
            "PUT",
            f"/api/users/{by_code['E7']}",
            json_body={
                "name": "E7 Exit Mid",
                "email": "e7@org1.audit",
                "department": "Ops",
                "join_date": "2026-09-01",
                "exit_date": "2026-09-18",
            },
        )
    if "E8" in by_code:
        bulk_present(api, [by_code["E8"]], scheduled)
        st, ot1, _ = api.request(
            "POST",
            "/api/overtime",
            json_body={"user_id": by_code["E8"], "ot_date": "2026-09-02", "hours": 4, "reason": "approved OT"},
            expected=(200, 201),
        )
        if isinstance(ot1, dict) and ot1.get("id"):
            api.request("PUT", f"/api/overtime/{ot1['id']}", json_body={"status": "approved"}, expected=200)
        st, ot2, _ = api.request(
            "POST",
            "/api/overtime",
            json_body={"user_id": by_code["E8"], "ot_date": "2026-09-03", "hours": 2, "reason": "pending OT"},
            expected=(200, 201),
        )
        st, ot3, _ = api.request(
            "POST",
            "/api/overtime",
            json_body={"user_id": by_code["E8"], "ot_date": "2026-09-04", "hours": 3, "reason": "rejected OT"},
            expected=(200, 201),
        )
        if isinstance(ot3, dict) and ot3.get("id"):
            api.request("PUT", f"/api/overtime/{ot3['id']}", json_body={"status": "rejected"}, expected=200)
    return {"scheduled": [d.isoformat() for d in scheduled], "count": len(scheduled)}


def bonus_xlsx(rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["employee_id", "name", "amount", "note"])
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def seed_scenario(api: LiveAPI, scenario_id: str) -> Dict[str, Any]:
    sid = scenario_id.upper()
    if sid == "S1":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        sched = scheduled_days_in_month(2026, 9, working_days=DEFAULT_WORKING_DAYS)
        bulk_present(api, [core["ids"]["E1"]], sched)
        put_pay_mode(api, "payable_days")
        payable = get_payroll(api, SEP_MONTH)
        put_pay_mode(api, "full_salary")
        full = get_payroll(api, SEP_MONTH)
        return {"core": {"unlock": core["unlock"], "shift_id": core["shift_id"], "ids": core["ids"]},
                "scheduled_count": len(sched),
                "payable_days": slim_records(payable),
                "full_salary": slim_records(full),
                "raw_payable": payable,
                "raw_full": full}

    if sid == "S2":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        add_absence_brackets(api, "percentage")
        people = [
            {"code": "A2", "name": "E-ABS-2", "salary": 60000, "department": "Ops", "join_date": "2026-09-01"},
            {"code": "A3", "name": "E-ABS-3", "salary": 60000, "department": "Ops", "join_date": "2026-09-01"},
            {"code": "A6", "name": "E-ABS-6", "salary": 60000, "department": "Ops", "join_date": "2026-09-01"},
            {"code": "A1", "name": "E-ABS-1", "salary": 60000, "department": "Ops", "join_date": "2026-09-01"},
            {"code": "A5", "name": "E-ABS-5", "salary": 60000, "department": "Ops", "join_date": "2026-09-01"},
        ]
        extra = {}
        for spec in people:
            extra[spec["code"]] = create_employee(api, spec, core["shift_id"], "org1.audit")
        scheduled = scheduled_days_in_month(2026, 9, working_days=DEFAULT_WORKING_DAYS)
        skips = {"A2": 2, "A3": 3, "A6": 6, "A1": 1, "A5": 5}
        for code, n in skips.items():
            bulk_present(api, [extra[code]], scheduled[n:])
        put_pay_mode(api, "payable_days")
        payable = get_payroll(api, SEP_MONTH)
        put_pay_mode(api, "full_salary")
        full = get_payroll(api, SEP_MONTH)
        return {"unlock": core["unlock"], "payable_days": slim_records(payable), "full_salary": slim_records(full)}

    if sid == "S3":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        scheduled = scheduled_days_in_month(2026, 9, working_days=DEFAULT_WORKING_DAYS)
        e4 = core["ids"]["E4"]
        for day in scheduled[:3]:
            mark_present(api, e4, day, "10:30")
        bulk_present(api, [e4], scheduled[4:])
        st, lv, _ = api.request(
            "POST",
            "/api/leaves",
            json_body={
                "user_id": e4,
                "leave_type": "casual",
                "leave_duration": "half",
                "start_date": scheduled[3].isoformat(),
                "end_date": scheduled[3].isoformat(),
                "reason": "half leave",
            },
        )
        lid = lv.get("id") if isinstance(lv, dict) else None
        if lid:
            api.request("PUT", f"/api/leaves/{lid}", json_body={"status": "approved"})
        put_pay_mode(api, "payable_days")
        return {"unlock": core["unlock"], "leave_post": {"status": st, "body": lv}, "payroll": slim_records(get_payroll(api, SEP_MONTH))}

    if sid == "S4":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        put_pay_mode(api, "payable_days", extra={"deduct_unpaid_leave": True})
        api.request("POST", "/api/holidays", json_body={"name": "Audit Holiday", "holiday_date": "2026-09-05"}, expected=(200, 201, 409))
        patterns = seed_september_patterns(api, core["ids"], holidays=(date(2026, 9, 5),))
        boundary = api.request(
            "POST",
            "/api/leaves",
            json_body={
                "user_id": core["ids"]["E5"],
                "leave_type": "annual",
                "leave_duration": "full",
                "start_date": "2026-08-31",
                "end_date": "2026-09-01",
                "reason": "month boundary",
            },
        )
        holiday_leave = api.request(
            "POST",
            "/api/leaves",
            json_body={
                "user_id": core["ids"]["E5"],
                "leave_type": "casual",
                "leave_duration": "full",
                "start_date": "2026-09-05",
                "end_date": "2026-09-05",
                "reason": "leave on holiday",
            },
        )
        return {
            "unlock": core["unlock"],
            "patterns": patterns,
            "boundary_leave": {"status": boundary[0], "body": boundary[1]},
            "holiday_leave": {"status": holiday_leave[0], "body": holiday_leave[1]},
            "payroll": slim_records(get_payroll(api, SEP_MONTH)),
        }

    if sid == "S5":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        put_pay_mode(api, "payable_days")
        seed_september_patterns(api, core["ids"])
        sunday = date(2026, 9, 6)
        mark_present(api, core["ids"]["E8"], sunday, "09:05")
        ot_sun = api.request(
            "POST",
            "/api/overtime",
            json_body={"user_id": core["ids"]["E8"], "ot_date": "2026-09-06", "hours": 5, "reason": "Sunday OT"},
        )
        if isinstance(ot_sun[1], dict) and ot_sun[1].get("id"):
            api.request("PUT", f"/api/overtime/{ot_sun[1]['id']}", json_body={"status": "approved"})
        over_limit = api.request(
            "POST",
            "/api/overtime",
            json_body={"user_id": core["ids"]["E8"], "ot_date": "2026-09-08", "hours": 80, "reason": "over max"},
        )
        return {
            "unlock": core["unlock"],
            "sunday_ot": {"status": ot_sun[0], "body": ot_sun[1]},
            "over_max": {"status": over_limit[0], "body": over_limit[1]},
            "payroll": slim_records(get_payroll(api, SEP_MONTH)),
        }

    if sid == "S6":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        seed_september_patterns(api, core["ids"])
        results = {}
        for basis in ("calendar_days", "working_days", "30_day"):
            put_pay_mode(api, "payable_days", extra={"proration_basis": basis})
            results[basis] = slim_records(get_payroll(api, SEP_MONTH))
        return {"unlock": core["unlock"], "by_basis": results}

    if sid == "S7":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        seed_september_patterns(api, {"E1": core["ids"]["E1"], "E2": core["ids"]["E2"]})
        put_pay_mode(api, "full_salary", extra={"enable_named_components": 1, "enable_tax": 1})
        hra = api.request(
            "POST",
            "/api/payroll/component-types",
            json_body={"name": "HRA", "code": "hra", "kind": "earning", "category": "allowance", "calc_mode": "percent_basic", "default_amount": 10},
            expected=(200, 201),
        )
        tax = api.request(
            "POST",
            "/api/payroll/component-types",
            json_body={"name": "Tax", "code": "tax", "kind": "deduction", "category": "tax", "calc_mode": "fixed", "default_amount": 2000},
            expected=(200, 201),
        )
        types = api.request("GET", "/api/payroll/component-types", expected=200)[1].get("types") or []
        hra_id = next((t["id"] for t in types if str(t.get("code")).lower() == "hra"), None)
        tax_id = next((t["id"] for t in types if str(t.get("code")).lower() == "tax"), None)
        assign_hra = api.request(
            "POST",
            "/api/payroll/employee-components",
            json_body={"user_id": core["ids"]["E1"], "component_type_id": hra_id, "percent": 10, "amount": 0, "effective_from": "2026-09-01"},
        )
        assign_tax = api.request(
            "POST",
            "/api/payroll/employee-components",
            json_body={"user_id": core["ids"]["E1"], "component_type_id": tax_id, "percent": 0, "amount": 2000, "effective_from": "2026-09-01"},
        )
        dup = api.request(
            "POST",
            "/api/payroll/employee-components",
            json_body={"user_id": core["ids"]["E1"], "component_type_id": hra_id, "percent": 10, "amount": 0, "effective_from": "2026-09-01"},
        )
        return {
            "unlock": core["unlock"],
            "types": types,
            "assign_hra": {"status": assign_hra[0], "body": assign_hra[1]},
            "assign_tax": {"status": assign_tax[0], "body": assign_tax[1]},
            "duplicate_assign": {"status": dup[0], "body": dup[1]},
            "payroll": slim_records(get_payroll(api, SEP_MONTH)),
        }

    if sid == "S8":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        seed_september_patterns(api, {"E1": core["ids"]["E1"], "E9": core["ids"]["E9"]})
        put_pay_mode(api, "payable_days", extra={"enable_bonuses": 1, "enable_individual_adjustments": 1})
        blob = io.BytesIO(bonus_xlsx([[core["ids"]["E1"], "E1 Full Attend", 500, "ok"]]))
        blob.seek(0)
        upload = api.client.post(
            "/api/payroll/bonuses/upload?month=2026-09",
            data={"file": (blob, "bonus.xlsx")},
            content_type="multipart/form-data",
        )
        try:
            up_body = upload.get_json()
        except Exception:
            up_body = upload.get_data(as_text=True)[:500]
        api.log.append({"method": "POST", "path": "/api/payroll/bonuses/upload", "status": upload.status_code, "body": up_body})
        adj = api.request(
            "POST",
            "/api/payroll/adjustments?month=2026-09",
            json_body={"user_id": core["ids"]["E9"], "adj_type": "bonus", "pay_mode": "fixed", "amount": 1000, "note": "exception"},
        )
        invalid = {}
        for label, rows, month in (
            ("negative", [[core["ids"]["E1"], "E1", -10, "neg"]], "2026-09"),
            ("unknown", [[99999, "Nobody", 50, "x"]], "2026-09"),
            ("huge", [[core["ids"]["E1"], "E1", 999999999, "huge"]], "2026-09"),
        ):
            buf = io.BytesIO(bonus_xlsx(rows))
            buf.seek(0)
            r = api.client.post(
                f"/api/payroll/bonuses/upload?month={month}",
                data={"file": (buf, f"{label}.xlsx")},
                content_type="multipart/form-data",
            )
            try:
                b = r.get_json()
            except Exception:
                b = r.get_data(as_text=True)[:400]
            invalid[label] = {"status": r.status_code, "body": b}
        return {
            "unlock": core["unlock"],
            "valid_upload": {"status": upload.status_code, "body": up_body},
            "exception": {"status": adj[0], "body": adj[1]},
            "invalid_uploads": invalid,
            "payroll": slim_records(get_payroll(api, SEP_MONTH)),
        }

    if sid == "S9":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        put_pay_mode(api, "full_salary", extra={"enable_salary_revisions": 1})
        scheduled = scheduled_days_in_month(2026, 9, working_days=DEFAULT_WORKING_DAYS)
        bulk_present(api, [core["ids"]["E10"]], scheduled)
        post_wrong = api.request(
            "POST",
            f"/api/salary/{core['ids']['E10']}",
            json_body={"basic_salary": 50000, "effective_from": "2026-09-16"},
        )
        put_ok = api.request(
            "PUT",
            f"/api/salary/{core['ids']['E10']}",
            json_body={"basic_salary": 50000, "allowances": 0, "deductions": 0, "ot_rate": 0, "effective_from": "2026-09-16"},
        )
        return {
            "unlock": core["unlock"],
            "post_salary_id": {"status": post_wrong[0], "body": post_wrong[1]},
            "put_salary": {"status": put_ok[0], "body": put_ok[1]},
            "payroll": slim_records(get_payroll(api, SEP_MONTH)),
        }

    if sid == "S10":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        add_absence_brackets(api, "percentage")
        put_pay_mode(api, "payable_days", extra={"deduct_unpaid_leave": True, "enable_bonuses": 1, "enable_individual_adjustments": 1})
        patterns = seed_september_patterns(api, core["ids"])
        api.request(
            "POST",
            "/api/payroll/adjustments?month=2026-09",
            json_body={"user_id": core["ids"]["E9"], "adj_type": "bonus", "pay_mode": "fixed", "amount": 1000, "note": "S10 exception"},
        )
        payroll = get_payroll(api, SEP_MONTH)
        return {"unlock": core["unlock"], "patterns": patterns, "employee_count": payroll.get("employee_count"), "payroll": slim_records(payroll), "raw": payroll}

    if sid == "S11":
        core = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        put_pay_mode(api, "payable_days")
        oct_days = scheduled_days_in_month(2026, 10, working_days=DEFAULT_WORKING_DAYS)[:3]
        bulk_present(api, [core["ids"]["E1"]], oct_days)
        payroll = get_payroll(api, OCT_MONTH)
        e1 = next((r for r in slim_records(payroll) if r.get("employee_name") == "E1 Full Attend"), None)
        return {
            "unlock": core["unlock"],
            "seeded_dates": [d.isoformat() for d in oct_days],
            "e1": e1,
            "shows_full_basic_as_net": bool(e1 and float(e1.get("net_salary") or 0) == 60000),
        }

    if sid == "S12":
        api.login(ORG1, BR1, ALL_MODULES)
        unlock = api.unlock_enrollment(ORG1)
        shift_id = create_mon_sat_shift(api, "Perf Mon-Sat")
        people = [
            {"code": f"P{i:03d}", "name": f"Perf {i:03d}", "salary": 50000, "department": "Ops", "join_date": "2026-09-01"}
            for i in range(1, 201)
        ]
        ids = []
        for spec in people:
            ids.append(create_employee(api, spec, shift_id, "perf.audit"))
        scheduled = scheduled_days_in_month(2026, 9, working_days=DEFAULT_WORKING_DAYS)
        import time
        t0 = time.perf_counter()
        bulk_present(api, ids, scheduled)
        t_att = time.perf_counter() - t0
        put_pay_mode(api, "payable_days")
        t1 = time.perf_counter()
        payroll = get_payroll(api, SEP_MONTH)
        t_calc = time.perf_counter() - t1
        t2 = time.perf_counter()
        export = api.request("GET", f"/api/payroll/export.xlsx?month={SEP_MONTH}")
        t_export = time.perf_counter() - t2
        return {
            "unlock": unlock,
            "employee_count": payroll.get("employee_count"),
            "record_len": len(payroll.get("records") or []),
            "timings_seconds": {"attendance_seed": round(t_att, 3), "calculate": round(t_calc, 3), "export": round(t_export, 3)},
            "export_status": export[0],
        }

    if sid == "S13":
        core1 = seed_core_org(api, ORG1, BR1, ALL_MODULES, "org1.audit")
        scheduled = scheduled_days_in_month(2026, 9, working_days=DEFAULT_WORKING_DAYS)
        bulk_present(api, [core1["ids"]["E1"]], scheduled)
        put_pay_mode(api, "payable_days")
        p1 = get_payroll(api, SEP_MONTH)
        core2 = seed_core_org(api, ORG2, BR2, ALL_MODULES, "org2.audit")
        bulk_present(api, [core2["ids"]["E1"]], scheduled[:10])
        put_pay_mode(api, "payable_days")
        p2 = get_payroll(api, SEP_MONTH)
        api.login(ORG1, BR1, ALL_MODULES)
        p1_again = get_payroll(api, SEP_MONTH)
        return {
            "org1": slim_records(p1),
            "org2": slim_records(p2),
            "org1_after_org2": slim_records(p1_again),
            "unlock_org1": core1["unlock"],
            "unlock_org2": core2["unlock"],
        }

    if sid == "S14":
        routes = [
            ("GET", "/api/payroll?month=2026-09"),
            ("GET", "/api/settings/payroll"),
            ("GET", "/api/payroll-settings/scoped-rules/half-leave"),
            ("GET", "/api/leaves"),
            ("GET", "/api/overtime"),
            ("GET", "/api/salary"),
        ]
        table = {}
        for label, org, br, modules in (
            ("org1_all", ORG1, BR1, ALL_MODULES),
            ("org2_no_leave_ot", ORG2, BR2, NO_LEAVE_OT),
            ("org3_no_payroll", ORG3, BR3, NO_PAYROLL),
        ):
            api.login(org, br, modules)
            table[label] = {}
            for method, path in routes:
                st, body, _ = api.request(method, path)
                table[label][path] = {"status": st, "error": (body.get("error") or body.get("message") if isinstance(body, dict) else str(body)[:200])}
        return {"route_table": table}

    raise ApiError(f"Unknown scenario {scenario_id}")
