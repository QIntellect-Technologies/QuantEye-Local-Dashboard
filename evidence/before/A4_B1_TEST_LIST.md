# A4 / B1 live checks (from S7)

Named Tax 2,000 reduces **net** but `deduction_amount` stays 0.

Engine (`database.calculate_monthly_payroll`):
- `deduction_amount` = absence + unpaid-leave + half-leave + payable_days shortfall only
- Named deductions (Tax) go into `custom_deductions` via `named_component_totals`
- `net_salary` = gross - (deduction_amount + custom_deductions)
- Preview `total_deduction` = sum(deduction_amount + custom_deductions) — includes Tax
- Payslip JSON `deductions.other` = `custom_deductions` — includes Tax
- Payslip JSON has **no** `deduction_amount` field; it splits absence / unpaid / half / other

UI (`templates/index.html`):
- Header card `payrollDeductionTotal` uses `payroll.total_deduction` (includes Tax)
- **Per-employee Deduction column** uses `row.deduction_amount` only — **excludes Tax**
- Review modal / print table rows use `row.deduction_amount` — **exclude Tax**
- Finalize confirm uses `total_deduction` (includes Tax)

Must match to the cent after A4/B1: Payroll grid deduction column, review modal, print, payslip PDF, Excel export, API `deduction_amount` vs `custom_deductions` vs net.
