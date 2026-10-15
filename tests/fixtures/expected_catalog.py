"""Hand-calculated expected payroll values for scenarios S1-S14.

Formulas (correct product intent, independent of current engine bugs):
- Weekdays: weekday_utils (0=Monday). Default shift Mon-Sat.
- S1 holidays: none, so September 2026 has 26 Mon-Sat days.
- payable_days: net = money(basic * payable_days / whole_month_scheduled).
  Divisor is ALWAYS whole-month scheduled workdays (not join-window days).
- full_salary: net starts at prorated basic; absence brackets apply; payable
  shortfall does not reduce basic.
- Absence brackets (percentage): 1-2 days 5%, 3-5 days 10%, 6+ days 20%.
- Absence brackets (fixed PKR): 1-2 = 2000, 3-5 = 4000, 6+ = 8000.
- Brackets apply only in full_salary (FIX-A3). payable_days: deduction 0.
- Late arrivals currently do not reduce pay (FIX-C1 will change this); the
  hand sheet records late_days and expected_net_without_late_deduction.
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from payroll_policy_schema import money, proration_factor
from weekday_utils import DEFAULT_WORKING_DAYS, month_date_range, scheduled_days_in_month

SEP = (2026, 9)
OCT = (2026, 10)
SEP_START, SEP_END = month_date_range(*SEP)
E1_BASIC = 60000.0
ABSURD_BASIC = 10_000_000.0
OT_DIVISOR = 208.0

PERCENT_BRACKETS = (
    (1, 2, 5.0),
    (3, 5, 10.0),
    (6, None, 20.0),
)
FIXED_BRACKETS = (
    (1, 2, 2000.0),
    (3, 5, 4000.0),
    (6, None, 8000.0),
)


def _bracket(absences: int, table) -> float:
    for lo, hi, val in table:
        if absences >= lo and (hi is None or absences <= hi):
            return float(val)
    return 0.0


def sep_scheduled(holidays=(), join_on=None, exit_on=None):
    return scheduled_days_in_month(
        *SEP,
        working_days=DEFAULT_WORKING_DAYS,
        holidays=holidays,
        join_on=join_on,
        exit_on=exit_on,
    )


def oct_scheduled(holidays=(), join_on=None, exit_on=None):
    return scheduled_days_in_month(
        *OCT,
        working_days=DEFAULT_WORKING_DAYS,
        holidays=holidays,
        join_on=join_on,
        exit_on=exit_on,
    )


def payable_net(basic: float, payable_days: float, month_wd: int) -> float:
    if month_wd <= 0:
        return 0.0
    return money(basic * payable_days / month_wd)


def row(
    *,
    code: str,
    name: str,
    basic: float,
    mode: str,
    present: int,
    late: int = 0,
    paid_leave: float = 0,
    unpaid_leave: float = 0,
    absences: int = 0,
    half_leave: float = 0,
    ot_hours_approved: float = 0,
    bonus: float = 0,
    hra: float = 0,
    tax: float = 0,
    rest_day_extra: float = 0,
    join_on: Optional[date] = None,
    exit_on: Optional[date] = None,
    proration_basis: str = "calendar_days",
    notes: str = "",
) -> Dict[str, Any]:
    month_wd = len(sep_scheduled())
    eligible_wd = len(sep_scheduled(join_on=join_on, exit_on=exit_on))
    factor = proration_factor(
        proration_basis,
        max(SEP_START, join_on or SEP_START),
        min(SEP_END, exit_on or SEP_END),
        SEP_START,
        SEP_END,
        eligible_wd,
        month_wd,
    )
    basic_p = money(basic * factor)
    payable = present + late + paid_leave
    absence_pct = _bracket(absences, PERCENT_BRACKETS) if absences else 0.0
    absence_fixed = _bracket(absences, FIXED_BRACKETS) if absences else 0.0
    if mode == "payable_days":
        absence_deduction = 0.0
        net_from_attendance = payable_net(basic_p, payable, month_wd)
    else:
        absence_deduction = money(basic_p * absence_pct / 100.0)
        net_from_attendance = basic_p
    unpaid_ded = 0.0 if mode == "payable_days" else (
        money(basic_p * unpaid_leave / month_wd) if unpaid_leave and month_wd else 0.0
    )
    ot_rate = money(basic_p / OT_DIVISOR) if basic_p else 0.0
    ot_amount = money(ot_hours_approved * ot_rate)
    net = money(
        net_from_attendance
        + hra
        + ot_amount
        + bonus
        + rest_day_extra
        - absence_deduction
        - unpaid_ded
        - tax
    )
    return {
        "code": code,
        "employee_name": name,
        "basic": basic,
        "basic_prorated": basic_p,
        "attendance_pay_mode": mode,
        "month_scheduled_workdays": month_wd,
        "eligible_scheduled_workdays": eligible_wd,
        "proration_factor": round(factor, 6),
        "present_days": present,
        "late_days": late,
        "paid_leave_days": paid_leave,
        "unpaid_leave_days": unpaid_leave,
        "unexcused_absent_days": absences,
        "half_leave_days": half_leave,
        "payable_days": payable,
        "absence_deduction_percent": absence_pct if mode == "full_salary" else 0.0,
        "absence_deduction_fixed_pkr": absence_fixed if mode == "full_salary" else 0.0,
        "absence_deduction": absence_deduction,
        "unpaid_leave_deduction": unpaid_ded,
        "hra": hra,
        "tax": tax,
        "overtime_hours_approved": ot_hours_approved,
        "overtime_amount": ot_amount,
        "bonus": bonus,
        "rest_day_extra": rest_day_extra,
        "expected_net": net,
        "notes": notes,
    }


def scenario_s1() -> Dict[str, Any]:
    wd = sep_scheduled()
    present = len(wd)
    return {
        "id": "S1",
        "title": "Perfect attendance September 2026",
        "assumptions": {
            "shift": "Mon-Sat",
            "working_days": list(DEFAULT_WORKING_DAYS),
            "holidays": [],
            "calendar_mon_sat": present,
            "employee": "E1 Full Attend",
            "basic": E1_BASIC,
        },
        "expected": [
            row(code="E1", name="E1 Full Attend", basic=E1_BASIC, mode="payable_days", present=present, notes="26/26 of 60000"),
            row(code="E1", name="E1 Full Attend", basic=E1_BASIC, mode="full_salary", present=present, notes="full salary 60000"),
        ],
    }


def scenario_s2() -> Dict[str, Any]:
    wd = len(sep_scheduled())
    cases = [("E-ABS-2", 2), ("E-ABS-3", 3), ("E-ABS-6", 6), ("E-ABS-1", 1), ("E-ABS-5", 5)]
    expected = []
    for name, n in cases:
        present = wd - n
        expected.append(row(code=name, name=name, basic=E1_BASIC, mode="payable_days", present=present, absences=n, notes=f"{n} unexcused; brackets not applied"))
        expected.append(row(code=name, name=name, basic=E1_BASIC, mode="full_salary", present=present, absences=n, notes=f"{n} unexcused; percent bracket"))
    return {
        "id": "S2",
        "title": "Unexcused absences vs brackets",
        "assumptions": {"percent_brackets": PERCENT_BRACKETS, "fixed_brackets": FIXED_BRACKETS, "month_wd": wd},
        "expected": expected,
    }


def scenario_s3() -> Dict[str, Any]:
    wd = len(sep_scheduled())
    # 3 lates + 1 half-day leave; remaining days present on time.
    present_on_time = wd - 3 - 1
    return {
        "id": "S3",
        "title": "Late arrivals, half-day, half-leave",
        "assumptions": {
            "lates": 3,
            "half_day_present": 1,
            "half_leave": 1,
            "grace_minutes": 15,
            "late_check_in": "10:30",
            "shift": "09:00-17:00",
            "late_deduction_current_product": 0,
            "late_deduction_intended_after_C1": "pending FIX-C1",
        },
        "expected": [
            row(
                code="E4",
                name="E4 Late Half",
                basic=E1_BASIC,
                mode="payable_days",
                present=present_on_time,
                late=3,
                paid_leave=0.5,
                half_leave=1,
                notes="Lates count as payable; no late money deduction until FIX-C1",
            )
        ],
    }


def scenario_s4() -> Dict[str, Any]:
    wd = len(sep_scheduled())
    return {
        "id": "S4",
        "title": "Paid, unpaid, partial leave; month boundary; holiday leave",
        "assumptions": {
            "paid_leave": ["2026-09-08", "2026-09-09"],
            "unpaid_leave": ["2026-09-10"],
            "boundary_leave": ["2026-08-31", "2026-09-01"],
            "holiday_leave_attempt": "2026-09-05 if holiday configured else documented skip",
        },
        "expected": [
            row(
                code="E5",
                name="E5 Leave Mix",
                basic=E1_BASIC,
                mode="payable_days",
                present=wd - 3,
                paid_leave=2,
                unpaid_leave=1,
                notes="2 paid + 1 unpaid. payable_days already excludes unpaid; do not subtract a second unpaid-day deduction on top of 25/26 (that would double-count). Extra unpaid deduction only if deduct_unpaid_leave is on AND still counting unpaid as payable.",
            )
        ],
    }


def scenario_s5() -> Dict[str, Any]:
    wd = len(sep_scheduled())
    ot_hours = 4.0
    return {
        "id": "S5",
        "title": "Overtime statuses and rest-day OT",
        "assumptions": {
            "approved_hours": 4,
            "pending_hours": 2,
            "rejected_hours": 3,
            "only_approved_pays": True,
            "ot_divisor": OT_DIVISOR,
        },
        "expected": [
            row(
                code="E8",
                name="E8 Overtime",
                basic=E1_BASIC,
                mode="payable_days",
                present=wd,
                ot_hours_approved=ot_hours,
                notes="pending and rejected OT must not pay",
            )
        ],
    }


def scenario_s6() -> Dict[str, Any]:
    join = date(2026, 9, 11)
    exit_d = date(2026, 9, 18)
    month_wd = len(sep_scheduled())
    join_present = len(sep_scheduled(join_on=join))
    exit_present = len(sep_scheduled(exit_on=exit_d))
    expected = []
    for basis in ("calendar_days", "working_days", "30_day"):
        expected.append(
            row(
                code="E6",
                name="E6 Join Mid",
                basic=E1_BASIC,
                mode="payable_days",
                present=join_present,
                join_on=join,
                proration_basis=basis,
                notes=f"join 2026-09-11 present {join_present}/{month_wd} divisor whole month",
            )
        )
        expected.append(
            row(
                code="E7",
                name="E7 Exit Mid",
                basic=E1_BASIC,
                mode="payable_days",
                present=exit_present,
                exit_on=exit_d,
                proration_basis=basis,
                notes="exit_date via API if supported; else baseline records missing field",
            )
        )
    return {
        "id": "S6",
        "title": "Mid-month join and exit, every proration basis",
        "assumptions": {"join": "2026-09-11", "exit": "2026-09-18", "month_wd": month_wd},
        "expected": expected,
    }


def scenario_s7() -> Dict[str, Any]:
    wd = len(sep_scheduled())
    hra = money(E1_BASIC * 0.10)
    tax = 2000.0
    return {
        "id": "S7",
        "title": "Named pay lines HRA 10% and Tax 2000",
        "assumptions": {
            "hra": "10% of basic -> 6000",
            "tax": 2000,
            "effective_from": "employee join_date, never today",
            "net": "basic + HRA - tax = 64000 in full_salary perfect attendance",
        },
        "expected": [
            row(
                code="E1",
                name="E1 Full Attend",
                basic=E1_BASIC,
                mode="full_salary",
                present=wd,
                hra=hra,
                tax=tax,
                notes="net 64000 if lines apply; catalog-without-assignment must not silently zero without remark",
            )
        ],
    }


def scenario_s8() -> Dict[str, Any]:
    wd = len(sep_scheduled())
    return {
        "id": "S8",
        "title": "Bonus upload and one-person exception",
        "expected": [
            row(code="E1", name="E1 Full Attend", basic=E1_BASIC, mode="payable_days", present=wd, bonus=500, notes="Excel bonus 500"),
            row(code="E9", name="E9 Flex Pay", basic=E1_BASIC, mode="payable_days", present=wd, bonus=1000, notes="in-UI exception 1000"),
        ],
    }


def scenario_s9() -> Dict[str, Any]:
    wd = len(sep_scheduled())
    # Mid-month 40000 until 15 Sep, 50000 from 16 Sep. Calendar proration:
    # 15/30 * 40000 + 15/30 * 50000 = 20000 + 25000 = 45000
    blended = money(40000 * 15 / 30 + 50000 * 15 / 30)
    return {
        "id": "S9",
        "title": "Mid-month salary revision",
        "assumptions": {"from": 40000, "to": 50000, "effective_from": "2026-09-16", "blended_calendar": blended},
        "expected": [
            {
                "code": "E10",
                "employee_name": "E10 Salary Change",
                "attendance_pay_mode": "full_salary",
                "present_days": wd,
                "expected_basic_used": blended,
                "expected_net": blended,
                "notes": "pending C4 — engine currently last-write 50000; do not implement salary revision proration in Phase A",
            }
        ],
    }


def scenario_s10() -> Dict[str, Any]:
    wd = sep_scheduled()
    n = len(wd)
    join = date(2026, 9, 11)
    exit_d = date(2026, 9, 18)
    join_p = len(sep_scheduled(join_on=join))
    exit_p = len(sep_scheduled(exit_on=exit_d))
    rows = [
        row(code="E1", name="E1 Full Attend", basic=E1_BASIC, mode="payable_days", present=n),
        row(code="E2", name="E2 Two Absent", basic=E1_BASIC, mode="payable_days", present=n - 2, absences=2),
        row(code="E3", name="E3 Six Absent", basic=E1_BASIC, mode="payable_days", present=n - 6, absences=6),
        row(code="E4", name="E4 Late Half", basic=E1_BASIC, mode="payable_days", present=n - 3, late=3),
        row(code="E5", name="E5 Leave Mix", basic=E1_BASIC, mode="payable_days", present=n - 3, paid_leave=2, unpaid_leave=1),
        row(code="E6", name="E6 Join Mid", basic=E1_BASIC, mode="payable_days", present=join_p, join_on=join),
        row(code="E7", name="E7 Exit Mid", basic=E1_BASIC, mode="payable_days", present=exit_p, exit_on=exit_d),
        row(code="E8", name="E8 Overtime", basic=E1_BASIC, mode="payable_days", present=n, ot_hours_approved=4),
        row(code="E9", name="E9 Flex Pay", basic=E1_BASIC, mode="payable_days", present=n, bonus=1000),
        row(code="E10", name="E10 Salary Change", basic=40000, mode="payable_days", present=n, notes="pending C4"),
        row(code="E11", name="E11 No Salary", basic=0, mode="payable_days", present=n, notes="salary 0 must appear and block finalize"),
        row(code="E12", name="E12 Deactivate", basic=E1_BASIC, mode="payable_days", present=n),
        row(code="E13", name="E13 Extra", basic=42000, mode="payable_days", present=n),
        row(code="E14", name="E14 Absurd QA", basic=ABSURD_BASIC, mode="payable_days", present=n),
    ]
    by = {item["code"]: item for item in rows}
    # Strict CSV formulas from S10_hand_sheet_formulas.csv. Do not match the engine.
    e5 = by["E5"]
    e5["unpaid_leave_deduction"] = money(E1_BASIC * 1 / n)
    e5["expected_net"] = money(money(E1_BASIC * 25 / n) - e5["unpaid_leave_deduction"])
    e5["notes"] = "CSV ROUND(60000*25/26)-ROUND(60000*1/26)"
    e6 = by["E6"]
    e6["proration_basis"] = "working_days"
    e6["proration_factor"] = round(join_p / n, 6)
    e6["basic_prorated"] = money(E1_BASIC * join_p / n)
    e6["expected_net"] = money(E1_BASIC * join_p / n)
    e6["calendar_days_expected_net"] = money(E1_BASIC * 20 / 30)
    e6["working_days_expected_net"] = e6["expected_net"]
    e6["notes"] = "CSV ROUND(60000*17/26) working_days; calendar_days ROUND(60000*20/30)=40000"
    e7 = by["E7"]
    e7["proration_basis"] = "working_days"
    e7["proration_factor"] = round(exit_p / n, 6)
    e7["basic_prorated"] = money(E1_BASIC * exit_p / n)
    e7["expected_net"] = money(E1_BASIC * exit_p / n)
    e7["calendar_days_expected_net"] = money(E1_BASIC * 18 / 30)
    e7["working_days_expected_net"] = e7["expected_net"]
    e7["notes"] = "CSV ROUND(60000*16/26) working_days; calendar_days ROUND(60000*18/30)=36000"
    e8 = by["E8"]
    e8["overtime_amount"] = money(4 * (E1_BASIC / OT_DIVISOR))
    e8["expected_net"] = money(E1_BASIC + 4 * (E1_BASIC / OT_DIVISOR))
    e8["notes"] = "CSV ROUND(60000+4*(60000/208)) round_at_end"
    e10 = by["E10"]
    e10["expected_net"] = money(40000 * 15 / 30 + 50000 * 15 / 30)
    e10["notes"] = "pending C4; CSV ROUND(40000*15/30+50000*15/30)=45000"
    return {
        "id": "S10",
        "title": "Full 14-employee September hand sheet",
        "assumptions": {
            "month_scheduled_workdays": n,
            "scheduled_dates": [d.isoformat() for d in wd],
            "expected_source": "S10_hand_sheet_formulas.csv",
            "e6_e7_bases": {
                "working_days": {"E6": money(E1_BASIC * join_p / n), "E7": money(E1_BASIC * exit_p / n)},
                "calendar_days": {"E6": money(E1_BASIC * 20 / 30), "E7": money(E1_BASIC * 18 / 30)},
            },
        },
        "expected": rows,
    }


def scenario_s11() -> Dict[str, Any]:
    oct_wd = oct_scheduled()
    seeded = oct_wd[:3]
    basic = E1_BASIC
    earned = payable_net(basic, 3, len(oct_wd))
    projected = payable_net(basic, len(oct_wd), len(oct_wd))
    return {
        "id": "S11",
        "title": "October 2026 live preview honesty",
        "assumptions": {
            "month_scheduled_workdays": len(oct_wd),
            "seeded_dates": [d.isoformat() for d in seeded],
            "must_not_show_full_salary_as_earned": True,
        },
        "expected": [
            {
                "code": "E1",
                "employee_name": "E1 Full Attend",
                "attendance_pay_mode": "payable_days",
                "present_days": 3,
                "month_scheduled_workdays": len(oct_wd),
                "earned_so_far": earned,
                "projected_month_end": projected,
                "expected_net_must_not_equal_full_basic": True,
                "full_basic": basic,
            }
        ],
    }


def scenario_s12() -> Dict[str, Any]:
    return {
        "id": "S12",
        "title": "200 employees full September (performance)",
        "expected": {
            "employee_count": 200,
            "month": "2026-09",
            "record_timings": ["calculate", "preview", "export"],
        },
    }


def scenario_s13() -> Dict[str, Any]:
    return {
        "id": "S13",
        "title": "Two organizations, identical employee names/codes",
        "expected": {
            "org1_e1_net_isolated": True,
            "org2_e1_net_isolated": True,
            "identical_names": ["E1 Full Attend"],
        },
    }


def scenario_s14() -> Dict[str, Any]:
    return {
        "id": "S14",
        "title": "Module gates for three organizations",
        "expected": {
            "org1": {"modules": ["attendance", "shifts", "leave", "overtime", "payroll"], "payroll": 200},
            "org2": {"modules": ["attendance", "shifts", "payroll"], "leave": 403, "overtime": 403, "payroll": 200},
            "org3": {"modules": ["attendance", "shifts"], "payroll": 403},
        },
    }


def scenario_a0() -> Dict[str, Any]:
    return {
        "id": "A0",
        "title": "full_salary PUT vs engine mode",
        "expected": {
            "put_response_mode": "full_salary",
            "engine_preview_mode": "full_salary",
            "sql_attendance_pay_mode": "full_salary",
        },
    }


def scenario_s15() -> Dict[str, Any]:
    sundays = 4
    return {
        "id": "S15",
        "title": "Rest-day extra including join-month",
        "assumptions": {
            "method": "fixed_day 1500",
            "sundays_in_sep_2026": sundays,
            "e1_expected_extra": 1500 * sundays,
            "e6_join": "2026-09-11",
            "e6_sundays_after_join": ["2026-09-13", "2026-09-20", "2026-09-27"],
            "e6_sunday_before_join_must_not_count": "2026-09-06",
        },
        "expected": {
            "e1_off_day_extra_pay": 6000.0,
            "e6_off_day_extra_pay": 4500.0,
        },
    }


def scenario_s16() -> Dict[str, Any]:
    return {
        "id": "S16",
        "title": "Finalize, lock, reopen",
        "expected": {
            "finalize_with_salary_0": 400,
            "finalize_after_fix": 201,
            "attendance_edit_while_finalized": "blocked with reopen message",
            "reopen_endpoint": "POST /api/payroll/reopen",
        },
    }


def scenario_s17() -> Dict[str, Any]:
    return {
        "id": "S17",
        "title": "Pay-line scopes",
        "expected": {
            "department_Finance_gets_HRA": True,
            "ops_does_not": True,
            "employee_override_supported": True,
        },
    }


def scenario_s18() -> Dict[str, Any]:
    return {
        "id": "S18",
        "title": "Legacy-schema payroll settings save",
        "expected": {
            "put_must_not_500": True,
            "ui_must_show_server_field_error": True,
        },
    }


SCENARIOS = {
    "A0": scenario_a0,
    "S1": scenario_s1,
    "S2": scenario_s2,
    "S3": scenario_s3,
    "S4": scenario_s4,
    "S5": scenario_s5,
    "S6": scenario_s6,
    "S7": scenario_s7,
    "S8": scenario_s8,
    "S9": scenario_s9,
    "S10": scenario_s10,
    "S11": scenario_s11,
    "S12": scenario_s12,
    "S13": scenario_s13,
    "S14": scenario_s14,
    "S15": scenario_s15,
    "S16": scenario_s16,
    "S17": scenario_s17,
    "S18": scenario_s18,
}


def dump_expected(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for sid, fn in SCENARIOS.items():
        payload = fn()
        (dest / f"{sid}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    s10 = scenario_s10()
    csv_path = dest / "S10_hand_sheet.csv"
    rows: List[Dict[str, Any]] = list(s10["expected"])
    if rows:
        fieldnames: List[str] = []
        seen = set()
        for item in rows:
            for key in item.keys():
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
