"""窗口容量、互援状态机、连续段、共用人员互斥与情景测算。"""

import unittest
from datetime import date

from empq.capacity import (
    ACCEPT,
    CONDITIONAL,
    DEFER,
    FIRM,
    RETURN,
    build_plan,
    compare_aid_options,
    scenario_projection,
)
from empq.load import load_portfolio
from empq.timeline import build_portfolio_view

D09 = date(2026, 11, 9)


class CapacityFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal, cls.pf = load_portfolio("samples")
        cls.views = build_portfolio_view(cls.cal, cls.pf, D09)

    def plan(self, decisions=None):
        return build_plan(self.cal, self.pf, D09, self.views, decisions)


class CommitmentProtectionTests(CapacityFixture):
    def test_existing_valid_appointments_are_frozen_first(self):
        plan = self.plan()
        protected = {i.case: i for i in plan.protected}
        self.assertEqual(set(protected), {"C09", "C10"})
        self.assertTrue(all(i.firmness == FIRM for i in plan.protected))

    def test_cancelled_slots_never_reoffered(self):
        plan = self.plan()
        used_slots = {i.slot for i in plan.firm_invitations}
        self.assertNotIn("S1", used_slots)
        self.assertNotIn("S2", used_slots)

    def test_future_notified_appointments_not_frozen_early(self):
        # 11-04 视角：S3/S4 的预约在 11-06 才通知，当时不可冻结，窗口可用于补位
        early_views = build_portfolio_view(self.cal, self.pf, date(2026, 11, 4))
        early_plan = build_plan(self.cal, self.pf, date(2026, 11, 4), early_views)
        protected_slots = {i.slot for i in early_plan.protected}
        self.assertNotIn("S3", protected_slots)
        self.assertNotIn("S4", protected_slots)

    def test_case_receives_at_most_one_invitation(self):
        plan = self.plan({s: ACCEPT for s in ("S6",)})
        invited = [i.case for i in plan.firm_invitations]
        self.assertEqual(len(invited), len(set(invited)))


class MutualAidTests(CapacityFixture):
    def test_proposed_slot_cannot_issue_invitation_until_accepted(self):
        plan = self.plan()
        offer = next(o for o in plan.offers if o.offer == "S6")
        self.assertEqual(offer.decision, DEFER)
        self.assertEqual(offer.usable_for, ())
        self.assertFalse(any(i.slot == "S6" and i.firmness == FIRM
                             for i in plan.firm_invitations))

    def test_accepted_aid_only_books_feasible_cases(self):
        plan = self.plan({"S6": ACCEPT})
        offer = next(o for o in plan.offers if o.offer == "S6")
        self.assertEqual(offer.decision, ACCEPT)
        # S6 与 S7 同属 P3：接受 S6 后，C07 改走 S6，S7 被让出，C01 再占 S6 后段
        booked_slots = {i.case: i.slot for i in plan.firm_invitations}
        self.assertIn("C07", booked_slots)
        self.assertTrue(set(offer.usable_for) <= set(booked_slots))

    def test_returned_aid_contributes_zero_capacity(self):
        plan = self.plan({"S6": RETURN})
        self.assertEqual(plan.conditional_invitations, ())
        offer = next(o for o in plan.offers if o.offer == "S6")
        self.assertEqual(offer.decision, RETURN)

    def test_aid_options_ranked_by_risk_improvement(self):
        options = {o["option"]: o for o in
                   compare_aid_options(self.cal, self.pf, D09, self.views)}
        self.assertGreaterEqual(options["defer-all"]["risk_score"],
                                options["accept-all"]["risk_score"])
        self.assertEqual(options["return-all"]["conditional_capacity"], 0)


class HardConstraintTests(CapacityFixture):
    def test_shared_staff_overlap_is_mutually_exclusive(self):
        # S6 与 S7 都是 P3、11-10 上午重叠；无论怎么排，同一人员不得双重承诺
        plan = self.plan({"S6": ACCEPT})
        staff_usage = []
        for inv in (*plan.protected, *plan.firm_invitations):
            slot = next(s for s in self.pf.slots if s.slot == inv.slot)
            staff_usage.append((slot.shared_staff, inv.day, inv.start_min, inv.end_min))
        p3 = sorted(x for x in staff_usage if x[0] == "P3")
        for a, b in zip(p3, p3[1:]):
            if a[1] == b[1]:
                self.assertGreaterEqual(a[3], b[2], f"P3 重叠: {a} {b}")

    def test_two_segment_service_needs_contiguous_slot(self):
        # appeal/verify 需 60 分钟连续；S5（60 分钟 QR）不能承接 verify
        plan = self.plan()
        self.assertFalse(any(i.case == "C04" for i in plan.firm_invitations))
        unmet = next(u for u in plan.unmet if u.case == "C04")
        self.assertIn("QA", unmet.reason)

    def test_qualification_and_site_options_enforced(self):
        # C06 register 只能在 N 站，未来无 N 站 QR 窗口 -> 未满足
        plan = self.plan()
        unmet_cases = {u.case for u in plan.unmet}
        self.assertIn("C06", unmet_cases)

    def test_conditional_capacity_never_overlaps_firm_commitments(self):
        plan = self.plan()
        firm_intervals = {(i.slot, i.start_min, i.end_min)
                          for i in (*plan.protected, *plan.firm_invitations)}
        for inv in plan.conditional_invitations:
            self.assertNotIn((inv.slot, inv.start_min, inv.end_min), firm_intervals)
            self.assertEqual(inv.firmness, CONDITIONAL)


class HoldTests(unittest.TestCase):
    def test_paused_hold_kept_within_hold_days_and_released_after(self):
        cal, pf = load_portfolio("samples")
        # 11-05 日末：C02 暂停中且持有 A1，hold_until=11-06，未到期 -> 保留
        views05 = build_portfolio_view(cal, pf, date(2026, 11, 5))
        plan05 = build_plan(cal, pf, date(2026, 11, 5), views05)
        hold = next(h for h in plan05.hold_decisions if h.case == "C02")
        self.assertEqual(hold.action, "keep")
        self.assertEqual(hold.hold_until, date(2026, 11, 6))
        # 11-06 == hold_until：日末是决策时点，仍保留
        views06 = build_portfolio_view(cal, pf, date(2026, 11, 6))
        at_deadline = next(h for h in build_plan(cal, pf, date(2026, 11, 6),
                                                 views06).hold_decisions
                           if h.case == "C02")
        self.assertEqual(at_deadline.action, "keep")

    def test_release_after_hold_until_returns_capacity_to_pool(self):
        # 合成资料：暂停后超过 hold_days 仍未回流，保留预约应释放并可再分配
        from datetime import datetime
        from empq.model import (
            Appointment, Event, Intake, Policy, Portfolio, Slot,
        )
        cal, pf = load_portfolio("samples")
        policy = Policy("verify", 4, 2, "QV", True, 1)
        intake = Intake("Z1", "verify", "N", (), date(2026, 11, 2), None, True)
        pause = Event("ZE1", "Z1", "pause", datetime(2026, 11, 3, 10, 0), "N", "PA")
        slot = Slot("ZS1", "N", date(2026, 11, 10), 540, 600, "QV", "P9", "open")
        apt = Appointment("ZA1", "Z1", "ZS1", 540, 600, date(2026, 11, 4))
        synth = Portfolio(
            policies={"verify": policy}, intakes={"Z1": intake}, events=[pause],
            slots=[slot], appointments=[apt], scenarios=[],
            by_case_events={"Z1": [pause]},
            by_case_appointments={"Z1": [apt]},
            by_slot_appointments={"ZS1": [apt]})
        # hold_until = 11-04；11-09 视角已过期
        views = build_portfolio_view(cal, synth, date(2026, 11, 9))
        plan = build_plan(cal, synth, date(2026, 11, 9), views)
        decision = next(h for h in plan.hold_decisions if h.case == "Z1")
        self.assertEqual(decision.action, "release")
        self.assertFalse(any(i.case == "Z1" for i in plan.protected))

    def test_resume_closes_hold_obligation(self):
        # 11-09 回流后，暂停段闭合，不再产生保留/释放决策
        cal, pf = load_portfolio("samples")
        views = build_portfolio_view(cal, pf, D09)
        plan = build_plan(cal, pf, D09, views)
        self.assertEqual(plan.hold_decisions, ())


class ScenarioTests(CapacityFixture):
    def test_projection_distinguishes_firm_and_conditional(self):
        plan = self.plan()
        rows = scenario_projection(self.cal, self.pf, D09, self.views, plan)
        self.assertTrue(rows)
        for row in rows:
            self.assertGreaterEqual(row.unmet_high, row.unmet_low)
            self.assertIn("firm", row.note)
            # 观察日当天的 band 也必须纳入（不得因 <= 过滤丢失）
        days = {r.day for r in rows}
        self.assertIn(date(2026, 11, 9), days)


if __name__ == "__main__":
    unittest.main()
