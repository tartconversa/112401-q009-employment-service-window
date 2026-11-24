"""等待分布（KM 对旧中位数）、压力分组与回流队列。"""

import unittest
from datetime import date

from empq.load import load_portfolio
from empq.metrics import pressure_report, reflow_cohorts, wait_distribution
from empq.timeline import build_portfolio_view

D09 = date(2026, 11, 9)
D05 = date(2026, 11, 5)


class MetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal, cls.pf = load_portfolio("samples")

    def test_km_handles_empty_and_all_censored_population(self):
        empty = wait_distribution([])
        self.assertEqual(empty.population, 0)
        self.assertIsNone(empty.median)
        self.assertEqual(empty.points, ())
        # 观察点当天受理、尚无任何人取得预约 -> 全部右删失，存活曲线恒为 1
        views = build_portfolio_view(self.cal, self.pf, date(2026, 10, 19))
        all_censored = wait_distribution(views)
        self.assertEqual(all_censored.censored, all_censored.population)
        self.assertIsNone(all_censored.median)
        self.assertTrue(all(p.survival == 1.0 for p in all_censored.points))

    def test_km_includes_censored_in_risk_set_and_median_may_be_unknown(self):
        views = build_portfolio_view(self.cal, self.pf, D09)
        dist = wait_distribution(views)
        self.assertEqual(dist.population, 18)
        self.assertGreater(dist.censored, 0)
        # 风险集只减不增；删失后仍保留在最后风险集口径中
        self.assertTrue(all(p.at_risk >= p.events for p in dist.points))
        # 样例在 11-09 删失过半，中位数不可估计时必须明确告知而非编造
        self.assertIsNone(dist.median)
        self.assertIn("删失", dist.median_note)

    def test_km_median_when_majority_served(self):
        # 更早观察点 11-05：多数已取得预约，中位数可估计
        views = build_portfolio_view(self.cal, self.pf, D05)
        dist = wait_distribution(views)
        self.assertIsNotNone(dist.median)
        self.assertEqual(dist.median, 4)
        self.assertAlmostEqual(min(p.survival for p in dist.points), 0.5)

    def test_naive_median_drops_the_waiting(self):
        views = build_portfolio_view(self.cal, self.pf, D09)
        report = pressure_report(self.cal, views, D09)
        # 旧口径只含已办结者；KM 总体覆盖全部受理
        self.assertLess(len(report.naive_cases), report.wait.population)
        self.assertEqual(set(report.naive_cases) <= {v.case for v in views}, True)

    def test_reflow_is_cohort_not_churn(self):
        views = build_portfolio_view(self.cal, self.pf, D05)
        cohorts = reflow_cohorts(views)
        self.assertEqual(cohorts.total_paused, 2)
        group = next(g for g in cohorts.groups if g.service == "verify")
        self.assertEqual(set(group.paused_cases), {"C01", "C02"})
        self.assertIn("不得计为流失", group.note)

    def test_overdue_and_incomplete_calendar_separated(self):
        views = build_portfolio_view(self.cal, self.pf, D09)
        report = pressure_report(self.cal, views, D09)
        overdue = next(b for b in report.buckets if b.bucket == "overdue")
        self.assertEqual(overdue.cases, ("H06",))
        self.assertIn("C10", report.outside_calendar_cases)

    def test_pressure_rows_grouped_by_service_and_responsible_site(self):
        views = build_portfolio_view(self.cal, self.pf, D09)
        report = pressure_report(self.cal, views, D09)
        appeal_s = next(r for r in report.rows if r.service == "appeal" and r.site == "S")
        # C04 转接后由 S 站负责，C10 在 N 站
        self.assertEqual(appeal_s.total_open, 2)


if __name__ == "__main__":
    unittest.main()
