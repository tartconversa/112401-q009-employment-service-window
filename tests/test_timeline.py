"""两个时钟、暂停段、重办链与预约状态的口径测试（以样例事实为锚）。"""

import unittest
from datetime import date

from empq.calendar import ServiceCalendar
from empq.load import load_portfolio
from empq.timeline import (
    APT_SITE_CANCELLED,
    DUE_SOON,
    OVERDUE,
    PAUSED,
    SCHEDULED,
    UNSCHEDULED,
    build_case_view,
    build_portfolio_view,
    chain_of,
)

DATA = "samples"
D09 = date(2026, 11, 9)
D06 = date(2026, 11, 6)


class TimelineFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal, cls.pf = load_portfolio(DATA)

    def view(self, case, as_of=D09):
        return build_case_view(self.cal, self.pf, case, as_of)


class ObligationClockTests(TimelineFixture):
    def test_pause_days_excluded_resume_day_counts(self):
        # C01：11-02 受理，11-03 暂停，11-09 回流，limit=4
        v = self.view("C01")
        # 日末口径：11-03..11-06 不计；活跃日 11-09,10,11,12 -> 到期 11-12
        self.assertEqual(v.due, date(2026, 11, 12))
        # 截至 11-09 只用了 1 个活跃服务日
        self.assertEqual(v.elapsed_active, 1)

    def test_historical_pause_episode_same_arithmetic(self):
        # H01：10-19 受理，10-20 暂停（当日不计），10-23 回流（当日计），limit=4
        # 活跃日 10-21,23,26,27 -> 到期 10-28；10-26 办结，不逾期
        v = build_case_view(self.cal, self.pf, "H01", date(2026, 10, 30))
        self.assertEqual(v.due, date(2026, 10, 28))
        self.assertFalse(v.overdue_at_completion)
        self.assertEqual(v.completion_day, date(2026, 10, 26))

    def test_no_suspend_service_runs_through(self):
        # register 不可暂停；H06：10-28 受理 limit=3
        # 活跃日 10-29,30,11-02 -> 到期 11-02，11-09 已逾期
        v = self.view("H06")
        self.assertEqual(v.due, date(2026, 11, 2))
        self.assertEqual(v.bucket, OVERDUE)

    def test_restart_no_inherits_anchor_and_clock(self):
        # C07 重办 C05，restart=no：义务钟锚点仍是 C05 的 11-04，到期 11-10
        chain = chain_of(self.pf.intakes, "C07")
        self.assertEqual(chain, ("C07", "C05"))
        v = self.view("C07")
        self.assertEqual(v.anchor_case, "C05")
        self.assertEqual(v.anchor_accepted, date(2026, 11, 4))
        self.assertEqual(v.due, date(2026, 11, 10))
        self.assertIn("C05→C07", v.note)

    def test_withdrawn_source_does_not_terminate_chain(self):
        # C05 已撤回但 C07 承接，链上义务继续；C05 自身视角是 withdrawn
        v05 = self.view("C05")
        self.assertEqual(v05.bucket, "withdrawn")
        v07 = self.view("C07")
        self.assertEqual(v07.status, "open")

    def test_incomplete_calendar_flagged_not_guessed(self):
        # C10 appeal limit=6，日历只到 11-13，凑不满活跃日
        v = self.view("C10")
        self.assertTrue(v.due_incomplete)
        self.assertIsNone(v.due)
        self.assertNotEqual(v.bucket, OVERDUE)

    def test_overdue_completion_detected(self):
        # C12：到期 11-06，11-09 才办结
        v = self.view("C12")
        self.assertTrue(v.overdue_at_completion)
        # C03：到期与办结同为 11-06，不逾期
        c03 = self.view("C03")
        self.assertFalse(c03.overdue_at_completion)


class WaitingClockTests(TimelineFixture):
    def test_wait_days_are_raw_service_days_no_pause_exclusion(self):
        # H01：受理 10-19（一），通知 10-23（五）；中间含暂停，等待钟不扣减 -> 4
        v = build_case_view(self.cal, self.pf, "H01", date(2026, 10, 30))
        self.assertEqual(v.wait_days, 4)

    def test_site_cancel_invalidates_appointment_back_to_censored(self):
        v = self.view("C02")
        states = {a.appointment: a.state for a in v.appointments}
        self.assertEqual(states["A1"], APT_SITE_CANCELLED)
        self.assertTrue(v.wait_censored)
        self.assertIsNone(v.wait_days)

    def test_no_show_keeps_clock_running_without_pausing(self):
        # C06 有 no-show 事件但资料中无对应预约行（演练刻意保留的不完整记录）：
        # 申请人未到场不暂停义务钟，案件仍 open、仍按原到期日计逾期压力
        v = self.view("C06")
        self.assertEqual(v.status, "open")
        self.assertEqual(v.bucket, DUE_SOON)
        self.assertFalse(v.paused_eod)

    def test_same_day_appointment_is_still_commitment_at_eod(self):
        v09 = self.view("C09")
        self.assertEqual(v09.bucket, SCHEDULED)
        self.assertTrue(v09.appointments[0].future)

    def test_wait_clock_ignores_appointments_notified_after_as_of(self):
        # 10-19 视角：H01/H02 的预约通知分别在 10-23/10-20，尚未发生 -> 右删失
        early = build_portfolio_view(self.cal, self.pf, date(2026, 10, 19))
        for v in early:
            self.assertTrue(v.wait_censored)
            self.assertIsNone(v.wait_days)


class PauseAndBucketTests(TimelineFixture):
    def test_paused_bucket_while_awaiting_receipt(self):
        views = {v.case: v for v in build_portfolio_view(self.cal, self.pf, date(2026, 11, 5))}
        self.assertEqual(views["C01"].bucket, PAUSED)
        self.assertEqual(views["C02"].bucket, PAUSED)

    def test_resume_same_day_leaves_paused_bucket(self):
        v = self.view("C01")  # 11-09 日末已回流
        self.assertNotEqual(v.bucket, PAUSED)
        self.assertEqual(v.bucket, UNSCHEDULED)

    def test_transfer_updates_responsible_site(self):
        v = self.view("C04")
        self.assertEqual(v.current_site, "S")

    def test_hold_until_and_release_flag(self):
        early = build_case_view(self.cal, self.pf, "C01", date(2026, 11, 4))
        ep = early.pause_episodes[0]
        self.assertEqual(ep.hold_until, date(2026, 11, 5))
        self.assertFalse(ep.release_due)
        # hold_until 日末是决策时点，当天仍保留；下一服务日仍未回流才应还池
        at_deadline = build_case_view(self.cal, self.pf, "C01", date(2026, 11, 5))
        self.assertFalse(at_deadline.pause_episodes[0].release_due)
        later = build_case_view(self.cal, self.pf, "C01", date(2026, 11, 6))
        self.assertTrue(later.pause_episodes[0].release_due)


if __name__ == "__main__":
    unittest.main()
