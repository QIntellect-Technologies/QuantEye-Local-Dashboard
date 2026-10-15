# FIX-A0 after

Local `attendance.db` (no org column): id=1 `attendance_pay_mode=full_salary` — this file was never flipped by the old migrate.

Railway/production not queried (out of scope). Scratch DBs created before this fix stored DEFAULT_ORG as payable_days because migrate rewrote full_salary when no absence rules existed. After this fix, new rows keep the column default (`full_salary` until FIX-A2).

Live AFTER (`GET /api/payroll?month=2026-09&recalculate=1`):
- PUT full_salary → 200, SQL tenant `full_salary`
- engine_preview_mode `full_salary`
- notices `No absence rules configured`
- E2 two absences: payable_days net 55384.62 vs full_salary net 60000.00
- audit action `full_salary_no_absence_rules`

Rollback: restore the migrate UPDATE and the calculate remap.
