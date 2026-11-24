"""窗口容量、互援决策与可落实的预约建议。

硬约束（违反即不产生建议）：

1. **先守住已承诺预约**：观察日仍有效的未来预约全部冻结，优先占用其窗口与
   ``shared_staff`` 时间，规划不得挪动或重复承诺。
2. **资质**：窗口 ``qualification`` 必须等于业务所需资质。
3. **连续段**：业务需要 ``segments`` 个连续半小时段，必须在**同一个**窗口内
   找到连续空档；窗口是具体时段，不是可以随意切开的总工时。
4. **共用人员互斥**：``shared_staff`` 相同的不同窗口，时间重叠时同一人员只能
   承接一处。
5. **互援状态机**：``proposed`` 窗口是互援提议，窗口负责人可 ``accept``、
   ``defer``、``return``；只有被接受、且通过同一套可行性检查后，才能对具体
   申请发出约定。未接受时只能给"回流假设下的条件容量估计"，不得发邀请。
6. **暂停保留到期处置**：暂停期间保留的预约，到 ``hold_days`` 对应服务日日末
   决定继续保留或还池；逾期未回流则建议释放，释放后容量才可再次分配。
7. **一份申请只承诺一次**：同一申请在同一轮规划中至多收到一条邀请。
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .calendar import ServiceCalendar
from .model import (
    Portfolio,
    SLOT_OPEN,
    SLOT_PROPOSED,
    SLOT_CANCELLED,
)
from .timeline import (
    CaseView,
    OVERDUE,
    DUE_TODAY,
    DUE_SOON,
    PAUSED,
    SCHEDULED,
    APT_SITE_CANCELLED,
    slot_status_as_of,
)

ACCEPT = "accept"
DEFER = "defer"
RETURN = "return"
AID_DECISIONS = frozenset({ACCEPT, DEFER, RETURN})

FIRM = "firm"            # 确有时段、可发出的约定
CONDITIONAL = "conditional"  # 仅回流假设成立时的容量估计，不是邀请


@dataclass(frozen=True)
class DemandEntry:
    case: str
    service: str
    qualification: str
    duration_min: int
    site_options: tuple[str, ...]
    due: Optional[date]
    bucket: str
    risk_weight: int       # 越大越紧急：逾期 3 / 临近 2 / 其余 1；暂停件 0（暂不发邀请）

    @property
    def key(self) -> tuple:
        # 紧急度降序、到期日升序、案件号稳定次序
        urgency = {OVERDUE: 0, DUE_TODAY: 1, DUE_SOON: 2}.get(self.bucket, 3)
        return (urgency, self.due or date.max, self.case)


@dataclass(frozen=True)
class Invitation:
    case: str
    slot: str
    site: str
    day: date
    start_min: int
    end_min: int
    firmness: str           # FIRM / CONDITIONAL
    basis: str              # open-capacity / mutual-aid:<slot> / reflow-assumption
    reason: str


@dataclass(frozen=True)
class HoldDecision:
    case: str
    appointment: str
    slot: str
    hold_until: Optional[date]
    action: str             # keep / release
    reason: str


@dataclass(frozen=True)
class AidOffer:
    offer: str
    slots: tuple[str, ...]
    qualification: str
    staff: str
    decision: str           # accept / defer / return（默认 defer）
    usable_for: tuple[str, ...]   # 接受后通过可行性检查、确实能承接的申请
    note: str


@dataclass(frozen=True)
class UnmetDemand:
    case: str
    service: str
    bucket: str
    due: Optional[date]
    reason: str


@dataclass(frozen=True)
class ScenarioRow:
    scenario: str
    service: str
    day: date
    backlog_unmet: int
    resuming_low: int
    resuming_high: int
    arrivals_low: int
    arrivals_high: int
    firm_capacity: int          # 已确有时段可承接数
    conditional_capacity: int   # 互援/回流假设下的估计容量
    unmet_low: int
    unmet_high: int
    note: str


@dataclass
class _Block:
    """一个窗口内已被占用的连续分钟区间。"""
    start_min: int
    end_min: int
    who: str


class Planner:
    """单轮规划的可变状态：所有分配经过同一冲突检查，杜绝重复承诺。"""

    def __init__(self, cal: ServiceCalendar, portfolio: Portfolio, as_of: date,
                 views: list[CaseView], aid_decisions: Optional[dict[str, str]] = None):
        self.cal = cal
        self.pf = portfolio
        self.as_of = as_of
        self.views = {v.case: v for v in views}
        self.aid_decisions = dict(aid_decisions or {})
        self.slot_index = {s.slot: s for s in portfolio.slots}

        self.slot_blocks: dict[str, list[_Block]] = {}
        self.staff_blocks: dict[str, list[tuple[date, int, int, str]]] = {}
        # 条件容量使用独立账本：不影响真实邀请，且条件邀请之间也不得重复承诺同一空档
        self.slot_cond_blocks: dict[str, list[_Block]] = {}
        self.staff_cond_blocks: dict[str, list[tuple[date, int, int, str]]] = {}
        self._released_keys: set[tuple[str, int, int]] = set()
        self.hold_decisions: list[HoldDecision] = []
        self._evaluate_holds()

        self.protected: list[Invitation] = []
        self._freeze_commitments()

        self.invitations: list[Invitation] = []
        self.assigned_cases: set[str] = set()

    # ---- 初始化：冻结已承诺预约 ------------------------------------------------
    def _freeze_commitments(self) -> None:
        for v in self.views.values():
            for apt in v.appointments:
                # 只冻结观察日（含）之前已通知、且仍有效、尚未过期的承诺
                if not apt.effective or not apt.future or apt.notified > self.as_of:
                    continue
                key = (apt.slot, apt.start_min, apt.end_min)
                if key in self._released_keys:
                    continue  # 暂停保留到期、已决定还池的预约不再占用
                self._consume(apt.slot, apt.day, apt.start_min, apt.end_min,
                              f"已承诺预约 {apt.appointment}")
                self.protected.append(Invitation(
                    case=apt.case, slot=apt.slot, site=apt.site, day=apt.day,
                    start_min=apt.start_min, end_min=apt.end_min,
                    firmness=FIRM, basis="protected-commitment",
                    reason=f"已通知并仍有效的承诺（{apt.appointment}）"))

    def _evaluate_holds(self) -> None:
        for v in self.views.values():
            for ep in v.pause_episodes:
                if ep.end is not None:
                    continue  # 已回流，保留义务结束
                for apt in v.held_appointments:
                    if ep.hold_until is None:
                        action, reason = "keep", "规则未规定保留天数，继续保留至回件到达"
                    elif ep.release_due:
                        action, reason = (
                            "release",
                            f"暂停自 {ep.start}，保留 {self.pf.policies[v.service].hold_days} "
                            f"个服务日至 {ep.hold_until} 日末仍未回流，按规则还池")
                    else:
                        action, reason = (
                            "keep",
                            f"保留至 {ep.hold_until} 服务日日末再决定，回件到达前继续预留")
                    self.hold_decisions.append(HoldDecision(
                        case=v.case, appointment=apt.appointment, slot=apt.slot,
                        hold_until=ep.hold_until, action=action, reason=reason))
                    if action == "release":
                        # 还池：移除该预约占用，使其容量可再分配（事实层面的释放仍需经理确认）
                        self._released_keys.add((apt.slot, apt.start_min, apt.end_min))
                        self._release(apt.slot, apt.day, apt.start_min, apt.end_min)

    # ---- 冲突账本 --------------------------------------------------------------
    def _consume(self, slot_id: str, day: date, start: int, end: int, who: str) -> None:
        slot = self.slot_index[slot_id]
        self.slot_blocks.setdefault(slot_id, []).append(_Block(start, end, who))
        self.staff_blocks.setdefault(slot.shared_staff, []).append((day, start, end, who))
        # 真实占用同时镜像到条件账本：条件估计永远不能与确定承诺/邀请重复承诺
        self.slot_cond_blocks.setdefault(slot_id, []).append(_Block(start, end, who))
        self.staff_cond_blocks.setdefault(slot.shared_staff, []).append((day, start, end, who))

    def _release(self, slot_id: str, day: date, start: int, end: int) -> None:
        slot = self.slot_index[slot_id]
        self.slot_blocks[slot_id] = [b for b in self.slot_blocks.get(slot_id, [])
                                     if not (b.start_min == start and b.end_min == end)]
        self.staff_blocks[slot.shared_staff] = [
            b for b in self.staff_blocks.get(slot.shared_staff, [])
            if not (b[0] == day and b[1] == start and b[2] == end)]

    def _free_in_slot(self, slot_id: str, duration: int, conditional: bool = False) -> Optional[int]:
        """在窗口内找最早的连续空档起点；同时检查共用人员在其他窗口的冲突。"""
        slot = self.slot_index[slot_id]
        blocks = sorted((self.slot_cond_blocks if conditional else self.slot_blocks)
                        .get(slot_id, []), key=lambda b: b.start_min)
        cursor = slot.start_min
        for block in blocks:
            if block.start_min - cursor >= duration:
                candidate = cursor
                if self._staff_free(slot, candidate, candidate + duration, conditional):
                    return candidate
            cursor = max(cursor, block.end_min)
        if slot.end_min - cursor >= duration and self._staff_free(slot, cursor, cursor + duration, conditional):
            return cursor
        return None

    def _staff_free(self, slot, start: int, end: int, conditional: bool = False) -> bool:
        ledger = self.staff_cond_blocks if conditional else self.staff_blocks
        for day, b_start, b_end, _ in ledger.get(slot.shared_staff, ()):
            if day == slot.day and start < b_end and b_start < end:
                return False
        return True

    def _consume_conditional(self, slot_id: str, day: date, start: int, end: int, who: str) -> None:
        slot = self.slot_index[slot_id]
        self.slot_cond_blocks.setdefault(slot_id, []).append(_Block(start, end, who))
        self.staff_cond_blocks.setdefault(slot.shared_staff, []).append((day, start, end, who))

    # ---- 候选窗口 --------------------------------------------------------------
    def _effective_status(self, slot) -> str:
        if slot.day <= self.as_of:
            return SLOT_CANCELLED  # 观察日日末，过去窗口不再可规划
        return slot_status_as_of(self.pf, slot, self.as_of)

    def _candidate_slots(self, entry: DemandEntry, firmness: str) -> list:
        out = []
        for slot in self.pf.slots:
            status = self._effective_status(slot)
            if slot.qualification != entry.qualification or slot.site not in entry.site_options:
                continue
            if slot.length_min < entry.duration_min:
                continue
            if firmness == FIRM:
                if status == SLOT_OPEN:
                    out.append(slot)
                elif status == SLOT_PROPOSED and self.aid_decisions.get(slot.slot) == ACCEPT:
                    out.append(slot)
            elif status == SLOT_PROPOSED and self.aid_decisions.get(slot.slot) != RETURN:
                out.append(slot)  # 条件估计：暂缓也算潜在容量，但不发邀请
        return sorted(out, key=lambda s: (s.day, s.start_min, s.slot))

    # ---- 主规划 ----------------------------------------------------------------
    def build_demand(self) -> list[DemandEntry]:
        entries = []
        for v in self.views.values():
            if v.closed or v.bucket == PAUSED:
                continue  # 暂停件不发新邀请；其保留预约由 hold 决策处理
            if any(a.effective and a.future and a.notified <= self.as_of
                   for a in v.appointments):
                continue  # 已有观察日前通知的有效未来承诺
            intake = self.pf.intakes[v.case]
            options = tuple(dict.fromkeys((intake.station, *intake.alternates, v.current_site)))
            policy = self.pf.policies[v.service]
            weight = {OVERDUE: 3, DUE_TODAY: 3, DUE_SOON: 2}.get(v.bucket, 1)
            entries.append(DemandEntry(
                case=v.case, service=v.service, qualification=policy.qualification,
                duration_min=policy.duration_minutes, site_options=options,
                due=None if v.due_incomplete else v.due, bucket=v.bucket,
                risk_weight=weight))
        return sorted(entries, key=lambda e: e.key)

    def run(self) -> "PlanResult":
        demand = self.build_demand()
        unmet: list[UnmetDemand] = []
        conditional: list[Invitation] = []

        for entry in demand:
            placed = self._place(entry, firmness=FIRM)
            if placed is None:
                # 条件容量：仅用于情景测算，明确不是邀请
                maybe = self._place(entry, firmness=CONDITIONAL, dry_run=True)
                if maybe is not None:
                    conditional.append(maybe)
                reason = self._unmet_reason(entry)
                unmet.append(UnmetDemand(entry.case, entry.service, entry.bucket,
                                         entry.due, reason))

        offers = self._aid_offers(demand)
        return PlanResult(
            as_of=self.as_of,
            protected=tuple(self.protected),
            firm_invitations=tuple(self.invitations),
            conditional_invitations=tuple(conditional),
            unmet=tuple(unmet),
            hold_decisions=tuple(self.hold_decisions),
            offers=tuple(offers),
            demand=tuple(demand))

    def _place(self, entry: DemandEntry, firmness: str, dry_run: bool = False) -> Optional[Invitation]:
        for slot in self._candidate_slots(entry, firmness):
            start = self._free_in_slot(slot.slot, entry.duration_min, conditional=dry_run)
            if start is None:
                continue
            end = start + entry.duration_min
            if slot.status == SLOT_PROPOSED:
                basis = f"mutual-aid:{slot.slot}"
                reason = f"互援窗口 {slot.slot}（{slot.site} 站，人员 {slot.shared_staff}）"
            else:
                basis, reason = "open-capacity", f"{slot.site} 站开放窗口 {slot.slot}"
            invite = Invitation(
                case=entry.case, slot=slot.slot, site=slot.site, day=slot.day,
                start_min=start, end_min=end, firmness=firmness, basis=basis,
                reason=reason + ("；回流假设下的条件估计，非真实邀请" if dry_run else ""))
            if not dry_run:
                self._consume(slot.slot, slot.day, start, end, f"邀请 {entry.case}")
                self.assigned_cases.add(entry.case)
                self.invitations.append(invite)
            else:
                self._consume_conditional(slot.slot, slot.day, start, end,
                                          f"条件估计 {entry.case}")
            return invite
        return None

    def _unmet_reason(self, entry: DemandEntry) -> str:
        candidates = self._candidate_slots(entry, CONDITIONAL)
        if not candidates:
            return (f"无满足资质 {entry.qualification} 且地点属于 {entry.site_options} "
                    "的未来窗口（互援提议退回或不存在）")
        blocking = []
        for slot in candidates:
            if slot.length_min < entry.duration_min:
                blocking.append(f"{slot.slot} 长度不足 {entry.duration_min} 分钟")
            elif (self._free_in_slot(slot.slot, entry.duration_min) is None
                  and self._free_in_slot(slot.slot, entry.duration_min, conditional=True) is None):
                blocking.append(f"{slot.slot} 无连续空档或人员 {slot.shared_staff} 冲突")
        return "；".join(blocking) or "无可行窗口"

    # ---- 互援提议 --------------------------------------------------------------
    def _aid_offers(self, demand: list[DemandEntry]) -> list[AidOffer]:
        offers = []
        proposed = [s for s in self.pf.slots
                    if s.status == SLOT_PROPOSED and s.day > self.as_of]
        for slot in sorted(proposed, key=lambda s: s.slot):
            decision = self.aid_decisions.get(slot.slot, DEFER)
            eligible = tuple(e.case for e in demand
                             if slot.qualification == e.qualification
                             and slot.site in e.site_options
                             and slot.length_min >= e.duration_min)
            if decision == ACCEPT:
                # 接受后仍须通过实际可行性检查：只列确实能承接者
                usable = tuple(c for c in eligible
                               if any(i.case == c and i.slot == slot.slot
                                      for i in self.invitations))
                note = f"已接受；仅对通过连续段与人员冲突检查的 {usable} 发出约定"
            elif decision == RETURN:
                usable, note = (), "已退回，不计入任何容量"
            else:
                usable = ()
                note = (f"暂缓中；{eligible} 具备资质与地点匹配，仅作条件容量估计，"
                        "未接受前不得发出约定")
            offers.append(AidOffer(
                offer=slot.slot, slots=(slot.slot,), qualification=slot.qualification,
                staff=slot.shared_staff, decision=decision, usable_for=usable, note=note))
        return offers


@dataclass(frozen=True)
class PlanResult:
    as_of: date
    protected: tuple[Invitation, ...]
    firm_invitations: tuple[Invitation, ...]
    conditional_invitations: tuple[Invitation, ...]
    unmet: tuple[UnmetDemand, ...]
    hold_decisions: tuple[HoldDecision, ...]
    offers: tuple[AidOffer, ...]
    demand: tuple[DemandEntry, ...]

    @property
    def risk_score(self) -> int:
        """未满足需求的逾期风险分（权重和），用于互援方案比较。"""
        weights = {e.case: e.risk_weight for e in self.demand}
        return sum(weights.get(u.case, 1) for u in self.unmet)


def build_plan(cal: ServiceCalendar, portfolio: Portfolio, as_of: date,
               views: list[CaseView], aid_decisions: Optional[dict[str, str]] = None) -> PlanResult:
    for slot_id, decision in (aid_decisions or {}).items():
        if decision not in AID_DECISIONS:
            raise ValueError(f"互援决策 {decision} 非法；可选 {sorted(AID_DECISIONS)}")
        if slot_id not in {s.slot for s in portfolio.slots}:
            raise ValueError(f"互援窗口 {slot_id} 不存在")
    return Planner(cal, portfolio, as_of, views, aid_decisions).run()


def compare_aid_options(cal: ServiceCalendar, portfolio: Portfolio, as_of: date,
                        views: list[CaseView]) -> list[dict]:
    """比较三种互援口径对逾期风险的改善：不互援 / 全部接受 / 全部暂缓（默认）。"""
    proposed = [s.slot for s in portfolio.slots if s.status == SLOT_PROPOSED and s.day > as_of]
    options = [
        ("defer-all", {s: DEFER for s in proposed}),
        ("accept-all", {s: ACCEPT for s in proposed}),
        ("return-all", {s: RETURN for s in proposed}),
    ]
    out = []
    for name, decisions in options:
        result = build_plan(cal, portfolio, as_of, views, decisions)
        out.append({
            "option": name,
            "risk_score": result.risk_score,
            "firm_invitations": len(result.firm_invitations),
            "conditional_capacity": len(result.conditional_invitations),
            "unmet": len(result.unmet),
            "unmet_cases": tuple(u.case for u in result.unmet),
        })
    baseline = out[0]["risk_score"]
    for row in out:
        row["risk_improvement"] = baseline - row["risk_score"]
    return out


def scenario_projection(cal: ServiceCalendar, portfolio: Portfolio, as_of: date,
                        views: list[CaseView], plan: PlanResult) -> list[ScenarioRow]:
    """情景测算：现有积压 + 回流/到达区间，对照确有时段与条件容量。

    resuming_* 是回流假设（暂停后可能同时回流），对应容量一律标注条件性质；
    arrivals_* 是新受理假设。firm_capacity 只统计真实可发邀请的时段。
    """
    rows = []
    unmet_by_service: dict[str, int] = {}
    for u in plan.unmet:
        unmet_by_service[u.service] = unmet_by_service.get(u.service, 0) + 1
    firm_cum: dict[tuple[date, str], int] = {}
    cond_cum: dict[tuple[date, str], int] = {}
    for inv in plan.firm_invitations:
        key = (inv.day, _service_of(portfolio, inv.case))
        firm_cum[key] = firm_cum.get(key, 0) + 1
    for inv in plan.conditional_invitations:
        key = (inv.day, _service_of(portfolio, inv.case))
        cond_cum[key] = cond_cum.get(key, 0) + 1

    def cumulative(table: dict[tuple[date, str], int], day: date, service: str) -> int:
        return sum(n for (d, s), n in table.items() if s == service and d <= day)

    for band in portfolio.scenarios:
        if band.day < as_of:
            continue
        backlog = unmet_by_service.get(band.service, 0)
        firm = cumulative(firm_cum, band.day, band.service)
        conditional = cumulative(cond_cum, band.day, band.service)
        demand_low = backlog + band.resuming_low + band.arrivals_low
        demand_high = backlog + band.resuming_high + band.arrivals_high
        total_capacity = firm + conditional
        rows.append(ScenarioRow(
            scenario=band.scenario, service=band.service, day=band.day,
            backlog_unmet=backlog,
            resuming_low=band.resuming_low, resuming_high=band.resuming_high,
            arrivals_low=band.arrivals_low, arrivals_high=band.arrivals_high,
            firm_capacity=firm, conditional_capacity=conditional,
            unmet_low=max(0, demand_low - total_capacity),
            unmet_high=max(0, demand_high - total_capacity),
            note=("firm 为确有时段；conditional 依赖互援接受或回流假设，"
                  "回流件可能同批到达，不能视为已锁定容量")))
    return rows


def _service_of(portfolio: Portfolio, case: str) -> str:
    return portfolio.intakes[case].service
