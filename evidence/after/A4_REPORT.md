# FIX-A4 after

S7: duplicate HRA POST 400 specific overlap message. E1 HRA 6000, Tax 2000, deduction_amount 2000 (was 0), net 64000 = 60000+6000-2000.

S17: department Finance HRA 200. Duplicate department 400. E1 employee 20% override allowances 12000. E2 Ops 0. E9 excluded 0.

Migration: ALTER columns on payroll_employee_components / types; payroll_duplicate_component_report (no auto-delete). Rollback: restore backup DB.
