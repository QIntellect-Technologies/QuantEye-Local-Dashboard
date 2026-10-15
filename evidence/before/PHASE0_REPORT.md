# Phase 0 — payroll test foundation (STOP here)

Branch: `payroll-fix-20261015`
Dev DB backup: `Local-Dashboard/backup/attendance.db.20261015.bak` (gitignored)
Product payroll engine: **not changed**. InsightFace / Client Node / Main / EXE: **not touched**.

## How to run

From `Local-Dashboard`, conda env `env310tfgpu`:

```
python tests/run_payroll_scenario.py S1 --evidence-dir evidence/before
python tests/run_payroll_scenario.py --all --evidence-dir evidence/before
python tests/test_weekday_utils.py
```

Each named scenario restores a **fresh** `init_db` snapshot. Scenarios do not share sqlite files.

Enrollment: `POST /api/users` is **403** until `POST /api/enrollment/unlock` with `ENR-AUDIT-ORG*`. Main verify is stubbed locally so scratch runs never hit Railway.

Weekdays: seeder imports `weekday_utils.DEFAULT_WORKING_DAYS` (`0=Monday` … `5=Saturday`). It never posts `[1,2,3,4,5,6]`.

## F0 status

| ID | Status | Notes |
|---|---|---|
| F0.1 | DONE | `weekday_utils.py` + seeder import; unit tests green |
| F0.2 | DONE | one command, fresh DB per scenario |
| F0.3 | DONE | S1–S14 JSON + `S10_hand_sheet.csv` under `tests/fixtures/expected/` |
| F0.4 | DONE | live results in `evidence/before/S*/result.json` |
| F0.5 | DONE | every org seed logs 403 then unlock 200 then 14 catalog users (S12: 200) |

## Live baseline (what Phase A must beat)

| Scenario | Live outcome vs hand sheet |
|---|---|
| S1 | E1 present 26/26 Mon–Sat Sep 2026. **payable_days net = 60000** (match). PUT `full_salary` still returned `attendance_pay_mode: payable_days` on payroll rows — mode switch not proven. Perfect attendance hides the difference. |
| S2 | payable_days 1/2/3/5/6 absences: nets 57692.31 / 55384.62 / 53076.92 / … / 46153.85; **absence_deduction 0**. full_salary PUT did not stick in S1; S2 full_salary block in expected sheet is the intended target. |
| S3 | 3 lates + half leave posted. Late money deduction **0** (FIX-C1). |
| S4 | Holiday leave 2026-09-05 **rejected** with server message naming the holiday. `exit_date` on PUT user is ignored (no field in `update_user`). |
| S5 | Approved OT posts; pending/rejected exist. Over-max POST captured. |
| S6 | E6 join 11 Sep: 17 eligible days. E7 exit_date **not persisted via API**. |
| S7 | Duplicate HRA assignment **accepted 200**. E1 pay_lines two HRA 6000 + Tax 2000, **net 70000** vs hand sheet **64000**. Tax deducted from net but `deduction_amount` 0. Mode still labelled payable_days. |
| S8 | Valid bonus 500 imported. Negative → 200 imported 0 unmatched `E1` (no field error). Unknown → 200 unmatched `Nobody`. Huge **999999999 imported 200**. |
| S9 | `POST /api/salary/<id>` **405** HTML. PUT 200 overwrites to 50000 (no blended 45000 in response). |
| S10 | 14 employees calculated on isolated Sep; line-by-line vs `S10_hand_sheet.csv` in result.json. |
| S11 | Oct 1–3 present, month_wd 27, **net 6666.67**, `shows_full_basic_as_net: false`. API already honest; UI labels still FIX-A7. |
| S12 | 200 employees. attendance_seed 104.1s, calculate 1.284s, export 1.868s, export HTTP 200. |
| S13 | 28 users (14+14 same names), org payroll isolated. |
| S14 | Org1 payroll 200. Org2 leave/OT 403, payroll 200. Org3 payroll/settings/salary **403**. |

## Phase A entry bugs already reproduced on clean DBs

- Duplicate named pay lines (S7)
- Bonus 999,999,999 accepted (S8)
- Negative bonus not a 400 with field errors (S8)
- POST salary by id 405 (S9)
- No exit_date on staff API (S4/S6)
- Attendance pay mode PUT may not change engine label (S1)

**STOP. Do not start FIX-A1 until this phase is reviewed.**
