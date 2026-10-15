"""One Playwright UI test per accepted Phase A follow-up fix."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

EVIDENCE = Path(__file__).resolve().parents[2] / "evidence" / "after" / "PHASEA_REVIEW"


def _open_payroll_settings(page, ui_server):
    page.goto(ui_server["url"], wait_until="domcontentloaded")
    page.evaluate("typeof switchTab === 'function' && switchTab('settings-view')")
    page.evaluate("typeof switchSettingsTab === 'function' && switchSettingsTab('payroll-rules-tab')")
    page.wait_for_selector("#payrollAttendancePayMode", timeout=15000)
    page.wait_for_timeout(800)


def test_no_stray_e1_bonus(ui_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        _open_payroll_settings(page, ui_server)
        bonuses = page.evaluate(
            """async () => {
              const res = await fetch('/api/payroll/bonuses?month=2026-09');
              return await res.json();
            }"""
        )
        assert bonuses.get("count") == 0, bonuses
        assert not bonuses.get("bonuses")
        page.evaluate("typeof previewPayrollSettingsImpact === 'function' && previewPayrollSettingsImpact()")
        page.wait_for_timeout(400)
        preview = page.locator("#payrollSettingsImpactPreview")
        text = preview.inner_text() if preview.count() else ""
        assert "60,500" not in text
        assert "60500" not in text.replace(",", "")
        banner = page.locator("#payrollSettingsMessage").inner_text()
        assert "_allDepartments" not in banner
        assert "is not defined" not in banner
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(EVIDENCE / "settings-preview.png"), full_page=False)
        browser.close()


def test_absence_brackets_disabled_in_payable_days(ui_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        _open_payroll_settings(page, ui_server)
        banner = page.locator("#payrollAbsenceUnusedBanner")
        expect(banner).to_be_visible()
        expect(banner).to_contain_text("Unpaid leave")
        expect(banner).to_contain_text("half-leave")
        expect(banner).to_contain_text("pay for days present")
        fieldset = page.locator("#payrollAbsenceBracketsFieldset")
        assert fieldset.get_attribute("disabled") is not None
        expect(page.locator("#deductUnpaidLeaveToggle")).to_be_enabled()
        page.locator("#payrollAbsenceSection").screenshot(path=str(EVIDENCE / "absence-payable-mode.png"))
        browser.close()


def test_pay_line_scope_is_real_dropdown(ui_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        _open_payroll_settings(page, ui_server)
        expect(page.locator("#payLineScopeValue")).to_have_count(1)
        assert page.locator("#payLineScopeId").count() == 0
        page.select_option("#payLineScopeType", "department")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#payLineScopeValue option')].some(o => /Finance|Ops|HR/.test(o.textContent || ''))",
            timeout=15000,
        )
        options = page.locator("#payLineScopeValue option").all_text_contents()
        joined = " ".join(options)
        assert "Finance" in joined or "Ops" in joined or "HR" in joined
        page.select_option("#payLineScopeType", "employee")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#payLineScopeValue option')].some(o => /E1|Full Attend/.test(o.textContent || ''))",
            timeout=15000,
        )
        emp_opts = page.locator("#payLineScopeValue option").all_text_contents()
        assert any("E1" in opt or "Full Attend" in opt for opt in emp_opts)
        browser.close()


def test_pay_line_form_method_caps_end_date(ui_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        _open_payroll_settings(page, ui_server)
        modes = page.locator("#payrollComponentMode option").all_text_contents()
        assert any("Percent of basic" in m for m in modes)
        assert any("Same amount every month" in m for m in modes)
        expect(page.locator("#payLineMinAmount")).to_be_visible()
        expect(page.locator("#payLineMaxAmount")).to_be_visible()
        expect(page.locator("#payLineEffectiveTo")).to_be_visible()
        page.locator("#payLineScopeType").locator("xpath=ancestor::form[1]").screenshot(
            path=str(EVIDENCE / "named-payline-scope-form.png")
        )
        page.locator("section.payroll-panel", has_text="Extra pay and deductions").screenshot(
            path=str(EVIDENCE / "payline-form-methods-caps-enddate.png")
        )
        browser.close()
