"""批次核对与事后回检。"""

import unittest
from datetime import date

from empq.backtest import backtest, rebook_audit
from empq.load import load_portfolio
from empq.reconcile import reconcile_day

D09 = date(2026, 11, 9)
D06 = date(2026, 11, 6)


class ReconcileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal, cls.pf = load_portfolio("samples")

    def test_same_day_resume_and_cancel_checked_together(self):
        result = reconcile_day(self.cal, self.pf, D09)
        receipts = {b.receipt: b for b in result.batches}
        self.assertEqual(set(receipts["EV"].cases), {"C01", "C02"})
        self.assertEqual(set(receipts["EG"].cases), {"C02", "C04"})
        items = {i.case: i for i in result.items}
        # C02 同时受回流与取消影响，两类变化在同一条记录里核对
        self.assertTrue(items["C02"].resumed and items["C02"].site_cancelled)
        self.assertEqual(items["C02"].lost_capacity_slot, "S1")
        self.assertEqual(items["C04"].lost_capacity_slot, "S2")

    def test_cancelled_capacity_not_counted_in_replan(self):
        result = reconcile_day(self.cal, self.pf, D09)
        used = {i.slot for i in result.plan.firm_invitations}
        self.assertNotIn("S1", used)
        self.assertNotIn("S2", used)
        self.assertIn("统一重算", result.guard_note)


class BacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal, cls.pf = load_portfolio("samples")

    def test_late_completions_flagged_against_prior_forecast(self):
        result = backtest(self.cal, self.pf, D06)
        rows = {r.case: r for r in result.rows}
        self.assertTrue(rows["C12"].overdue_at_completion)
        self.assertEqual(rows["C12"].outcome, "confirmed")
        # C03 当日到期当日办结，不算逾期
        self.assertFalse(rows["C03"].overdue_at_completion)

    def test_window_censored_not_counted_as_miss(self):
        result = backtest(self.cal, self.pf, D06)
        rows = {r.case: r for r in result.rows}
        self.assertEqual(rows["C10"].outcome, "window-censored")
        self.assertIn("观察窗不完整", rows["C10"].detail)

    def test_withdrawn_chain_case_excluded_from_due_judgment(self):
        result = backtest(self.cal, self.pf, D06)
        c05 = next(r for r in result.rows if r.case == "C05")
        self.assertEqual(c05.outcome, "withdrawn")
        self.assertIsNone(c05.forecast_due)

    def test_rebook_audit_separates_site_cancel_from_no_show(self):
        records = rebook_audit(self.cal, self.pf, D09)
        idx = {r.case: r for r in records}
        # 站点取消：预约作废、等待钟重新计时、无未来承诺
        self.assertEqual(idx["C02"].reason_code, "site-cancelled")
        self.assertFalse(idx["C02"].has_future_commitment)
        self.assertEqual(idx["C04"].reason_code, "site-cancelled")
        # C06 的 no-show 在资料中无预约行，审计只基于已通知约定，故不计入改签审计；
        # 但其义务钟不暂停的事实由时间线测试覆盖
        self.assertNotIn("C06", idx)


if __name__ == "__main__":
    unittest.main()
