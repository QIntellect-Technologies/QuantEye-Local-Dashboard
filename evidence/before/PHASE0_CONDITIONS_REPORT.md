# Phase 0 conditions — live proof (STOP before FIX-A1)

Product engine was **not** changed. Evidence: `evidence/before/A0`, `S9`, `S15`–`S18`. Named-deduction notes: `tests/fixtures/A4_B1_TEST_LIST.md`.

## FIX-A0 — seeder vs product

**The seeder payload is not wrong.** The product silently remaps `full_salary` to `payable_days` when the tenant has no active absence rules.

Live PUT (`evidence/before/A0/result.json`):

| Item | Value |
|---|---|
| Request | `PUT /api/settings/payroll` `attendance_pay_mode=full_salary`, org `ORG-PAY-AUDIT-1`, branch `BR-PAY-AUDIT-1`, `id=7` |
| HTTP | 200 `Payroll settings saved.` |
| Response `settings.attendance_pay_mode` | `full_salary` |
| GET after PUT | `full_salary` |
| `get_payroll_settings(ORG1, BR1)` | `full_salary` |

`payroll_settings` rows (duplicates = two tenants, not two rows for the same org):

| id | organization_id | branch_id | attendance_pay_mode |
|---|---|---|---|
| 1 | DEFAULT_ORG | DEFAULT_BRANCH | payable_days (init migrate) |
| 7 | ORG-PAY-AUDIT-1 | BR-PAY-AUDIT-1 | **full_salary** after PUT |

Engine label **before/after** that PUT (E1, 26/26 present):

| | Mode | matched_rule | net |
|---|---|---|---|
| After PUT + `GET /api/payroll?month=2026-09&recalculate=1` | **payable_days** | Pay 26/26 working days | 60000 |

Cause in `database.calculate_monthly_payroll`:

```
if attendance_mode == 'full_salary' and not absence_rules_configured:
    attendance_mode = 'payable_days'
```

Same policy exists in `payroll_policy_schema.migrate_payroll_policy_tenant_scope` (`UPDATE … SET attendance_pay_mode = 'payable_days' WHERE … full_salary AND NOT EXISTS absence rules`). PUT still writes `full_salary` on the tenant row; calculate then overwrites the **runtime** mode. Perfect attendance hides money difference; S2/A2/A3 cannot prove `full_salary` until this remap is removed or gated.

**Blocks A2/A3.** Do not start FIX-A1 until this is approved as the A2/A3 prerequisite (or A0 is folded into A2).

## S15–S18 baseline (current code)

| ID | Live |
|---|---|
| S15 | Sundays 6/13/20/27 Sep 2026. E1 rest-day extra **6000** (4×1500). E6 join 11 Sep extra **4500** (13/20/27 only; 6 Sep before join not paid). |
| S16 | Finalize with E11 salary 0 → **400** `Set a basic salary for: E11 No Salary`. After salary → **201**. Mark absent while finalized → **200** (not blocked). `POST /api/payroll/reopen` → **404**. GET still `is_finalized: true`. |
| S17 | Department scope POST → **400** `Employee and pay line are required`. Employee scope E1 HRA 10% → **200**, E1 allowances 6000. E9 Finance **no** HRA. Department/employee override not implemented on that route. |
| S18 | Legacy sqlite copied, then `database` import/`init_db` **migrates** schema. GET 200, PUT 200, tenant row id=4 `full_salary`. This did **not** keep a true pre-tenant schema through the request. |

## S9 — exact request and hand-sheet assumption

Hand sheet assumes **calendar blend** `15/30×40000 + 15/30×50000 = 45000` **only if** `enable_salary_revisions=1` **and** `effective_from=2026-09-16` is applied. Otherwise last-write 50000.

Live:

1. Settings PUT extra `enable_salary_revisions: 1` with `full_salary`. Flag **before 0 / after 1**. SQL tenant row flag=1.
2. `POST /api/salary/10` `{"basic_salary": 50000, "effective_from": "2026-09-16"}` → **405** HTML.
3. `PUT /api/salary/10` `{"user_id": 10, "basic_salary": 50000, "allowances": 0, "deductions": 0, "ot_rate": 0, "effective_from": "2026-09-16"}` → **200**.
4. `salary_revisions`: one row user 10, `effective_from=2026-09-16`, `basic_salary=50000`.
5. `salary_configs.basic_salary=50000` (tenant columns DEFAULT_ORG / DEFAULT_BRANCH).
6. Payroll E10 **net 50000** (last-write; not 45000). Engine still labelled payable_days (A0 remap).

## S7 — Tax 2000 vs `deduction_amount` 0

E1: pay_lines HRA 6000 + Tax 2000 (HRA duplicated), `custom_deductions=2000`, `deduction_amount=0`, **net 70000** (72000−2000). Tax **does** reduce net.

`deduction_amount` = absence + unpaid + half + payable_days shortfall only. Named Tax is `custom_deductions`.

| Surface | Tax 2000 included? |
|---|---|
| Preview `total_deduction` | yes (sum of both) |
| Header card `#payrollDeductionTotal` | yes (`total_deduction`) |
| Finalize confirm | yes |
| Grid / review / print **Deduction** column | **no** (`row.deduction_amount`) |
| Payslip JSON `deductions.other` | yes (`custom_deductions`) |
| Payslip JSON `deduction_amount` | field absent; split absence/unpaid/half/other |

Added to `tests/fixtures/A4_B1_TEST_LIST.md` (A4/B1 cent-match list).

## S10 formulas

See chat paste and `tests/fixtures/expected/S10_hand_sheet_formulas.csv`. Values-only sheet remains `S10_hand_sheet.csv`.

**STOP. Wait for approval before FIX-A1.**
