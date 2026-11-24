"""单申请时间线：两个时钟、暂停段、重办链与预约状态。

口径约定（README 同步）：

* **办理义务钟（含暂停规则）**：受理当天不计；其后每个服务日按"日末状态"判断——
  暂停事件所在服务日的日末已处于暂停，该日不计活跃服务日；回流事件所在服务日
  的日末已恢复，该日照常计 1。第 ``limit_days`` 个活跃服务日即到期日。到期日是
  纯日历+暂停事实的推演结果，与是否已办结无关，因此可以判断办结是否逾期。
* **等待钟（登记到预约成功）**：受理日（不含）到首次**有效**预约通知日（含）
  之间的原始服务日数，不做暂停扣减。站点取消（site-cancel）使该预约作废、
  申请人回到未预约状态；申请人未到场（no-show）不抹除"曾取得明确办理时段"
  的事实。两个时钟刻意分开。
* **重办链**：新受理记录 ``restart=no`` 且带 ``prior`` 时，义务钟沿用来源申请
  的受理锚点与暂停事实；``restart=yes`` 才另起时限。等待钟按每条受理记录单独
  观察。来源申请若已撤回，其撤回事实被重办承接，不终止整条链的义务。
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .calendar import OutsideCalendar, ServiceCalendar
from .model import (
    NO_SHOW,
    PAUSE,
    RESUME,
    SITE_CANCEL,
    TRANSFER,
    WITHDRAW,
    COMPLETED,
    Appointment,
    Event,
    Intake,
    Portfolio,
    Slot,
)

# 观察分桶
OVERDUE = "overdue"            # 已逾期且无有效未来预约
DUE_TODAY = "due-today"        # 观察日当天到期
DUE_SOON = "due-soon"          # 临近到期（默认未来 2 个服务日内）
SCHEDULED = "scheduled"        # 持有有效未来预约
UNSCHEDULED = "unscheduled"    # 无预约、尚未临近到期
PAUSED = "paused"              # 日末仍在暂停，义务钟冻结
COMPLETED = "completed"
WITHDRAWN = "withdrawn"
CLOSED_BUCKETS = frozenset({COMPLETED, WITHDRAWN})

# 预约视图状态
APT_ACTIVE = "active"
APT_SITE_CANCELLED = "site-cancelled"
APT_NO_SHOW = "no-show"
APT_FULFILLED = "fulfilled"


@dataclass(frozen=True)
class PauseEpisode:
    case: str
    start: date                  # 暂停事件所在服务日
    end: Optional[date]          # 回流事件所在服务日；None=观察日仍暂停
    resume_receipt: Optional[str]
    hold_until: Optional[date]   # start 之后第 hold_days 个服务日；0/未发布时为 None
    release_due: bool            # 观察日已超过 hold_until，保留时段应还池


@dataclass(frozen=True)
class AppointmentView:
    appointment: str
    case: str
    slot: str
    site: str
    day: date
    start_min: int
    end_min: int
    notified: date
    state: str
    as_of: date

    @property
    def effective(self) -> bool:
        """有效预约：未被站点取消。no-show 仍算曾取得明确时段。"""
        return self.state != APT_SITE_CANCELLED

    @property
    def future(self) -> bool:
        """相对观察日仍可执行：观察日当天的预约在日末视角仍是当日承诺（>= 当日）。"""
        return self.day >= self.as_of


@dataclass(frozen=True)
class CaseView:
    case: str
    service: str
    anchor_case: str
    anchor_accepted: date
    policy_limit: int
    due: Optional[date]
    due_incomplete: bool         # 日历不足以推出到期日
    elapsed_active: int          # 截至观察日已用活跃服务日
    paused_eod: bool
    current_site: str
    status: str                  # open / completed / withdrawn
    bucket: str
    wait_days: Optional[int]     # 首次有效预约的等待服务日；None=尚未观察到
    wait_censored: bool
    completion_day: Optional[date]
    overdue_at_completion: Optional[bool]
    appointments: tuple[AppointmentView, ...]
    pause_episodes: tuple[PauseEpisode, ...]
    chain: tuple[str, ...]
    note: str = ""

    @property
    def closed(self) -> bool:
        return self.status != "open"

    @property
    def held_appointments(self) -> tuple[AppointmentView, ...]:
        """暂停期间仍保留（未被站点取消、且已通知）的预约。"""
        if not self.paused_eod:
            return ()
        return tuple(a for a in self.appointments if a.effective and a.notified <= a.as_of)

    @property
    def active_future_appointments(self) -> tuple[AppointmentView, ...]:
        return tuple(a for a in self.appointments if a.effective and a.future)


def chain_of(intakes: dict[str, Intake], case: str) -> tuple[str, ...]:
    """从重办记录回溯到义务钟锚点（restart=no 才继续向上找）。"""
    chain = [case]
    seen = {case}
    cur = case
    while True:
        intake = intakes[cur]
        if not intake.prior or intake.restart:
            break
        nxt = intake.prior
        if nxt in seen:
            raise ValueError(f"重办链存在环: {case}")
        seen.add(nxt)
        chain.append(nxt)
        cur = nxt
    return tuple(chain)


def _chain_events(portfolio: Portfolio, chain: tuple[str, ...]) -> list[Event]:
    out: list[Event] = []
    anchor = chain[-1]
    for member in chain:
        for e in portfolio.by_case_events.get(member, ()):
            # 被重办承接的旧申请，其撤回不终止整条链的义务
            if e.kind == WITHDRAW and member != anchor:
                continue
            out.append(e)
    out.sort(key=lambda e: (e.at, e.event))
    return out


def _paused_at_eod(events: list[Event], day: date) -> bool:
    """日末状态：按规则在服务日结束时统一切换。

    暂停当天日末处于暂停 -> 该日不计活跃服务日；回流当天日末已恢复 -> 该日计。
    同一时刻既有暂停又有回流时，以日末最终状态为准（事件按时间排序后逐条翻转）。
    """
    paused = False
    for e in events:
        if e.at.date() > day:
            break
        if e.kind == PAUSE:
            paused = True
        elif e.kind == RESUME:
            paused = False
    return paused


def compute_due(cal: ServiceCalendar, portfolio: Portfolio, chain: tuple[str, ...],
                limit: int) -> tuple[Optional[date], bool]:
    """推演到期日。返回 (到期日, 是否因日历不完整而无法确定)。"""
    anchor_accepted = portfolio.intakes[chain[-1]].accepted
    events = _chain_events(portfolio, chain)
    active = 0
    try:
        for d in cal.service_days_after(anchor_accepted):
            if not _paused_at_eod(events, d):
                active += 1
                if active == limit:
                    return d, False
    except OutsideCalendar:
        pass
    # 日历耗尽仍未凑满活跃日
    return None, True


def elapsed_active_days(cal: ServiceCalendar, portfolio: Portfolio,
                        chain: tuple[str, ...], as_of: date) -> int:
    anchor_accepted = portfolio.intakes[chain[-1]].accepted
    events = _chain_events(portfolio, chain)
    total = 0
    for d in cal.service_days_after(anchor_accepted, as_of):
        if not _paused_at_eod(events, d):
            total += 1
    return total


def _pause_episodes(cal: ServiceCalendar, portfolio: Portfolio, chain: tuple[str, ...],
                    as_of: date) -> tuple[PauseEpisode, ...]:
    policy = portfolio.policies[portfolio.intakes[chain[0]].service]
    episodes: list[PauseEpisode] = []
    current: Optional[Event] = None
    resume_receipt = None
    for e in _chain_events(portfolio, chain):
        if e.at.date() > as_of:
            break
        if e.kind == PAUSE:
            current = e
            resume_receipt = None
        elif e.kind == RESUME and current is not None:
            episodes.append((current, e))
            current = None
            resume_receipt = e.receipt
    open_episode = current
    out: list[PauseEpisode] = []
    for start_event, end_event in episodes:
        out.append(PauseEpisode(
            case=start_event.case, start=start_event.at.date(),
            end=end_event.at.date(), resume_receipt=end_event.receipt,
            hold_until=None, release_due=False))
    if open_episode is not None:
        hold_until = None
        release_due = False
        if policy.hold_days > 0:
            try:
                hold_until = cal.add_service_days(open_episode.at.date(), policy.hold_days)
                # hold_until 服务日日末才到决策时点；其后第一个服务日仍未回流才应还池
                release_due = as_of > hold_until
            except OutsideCalendar:
                hold_until = None
        out.append(PauseEpisode(
            case=open_episode.case, start=open_episode.at.date(),
            end=None, resume_receipt=None,
            hold_until=hold_until, release_due=release_due))
    return tuple(out)


def slot_status_as_of(portfolio: Portfolio, slot: Slot, as_of: date) -> str:
    """窗口在观察日的有效状态：取消事件发生在观察日之后则尚不可知，按开放处理。"""
    if slot.status != "cancelled":
        return slot.status
    for apt in portfolio.by_slot_appointments.get(slot.slot, ()):
        for e in portfolio.by_case_events.get(apt.case, ()):
            if e.kind == SITE_CANCEL and e.at.date() == slot.day and e.at.date() <= as_of:
                return slot.status
    # 取消事实尚未发生（观察点早于取消日）
    return "open"


def _appointment_views(cal: ServiceCalendar, portfolio: Portfolio, case: str,
                       as_of: date) -> tuple[AppointmentView, ...]:
    slot_index = {s.slot: s for s in portfolio.slots}
    case_events = [e for e in portfolio.by_case_events.get(case, ()) if e.at.date() <= as_of]
    # 站点取消事件按申请匹配：该申请所有被站点取消的预约窗口
    cancelled_appointments = {
        apt.appointment
        for e in case_events if e.kind == SITE_CANCEL
        for apt in portfolio.by_case_appointments.get(case, ())
    }
    views: list[AppointmentView] = []
    for apt in sorted(portfolio.by_case_appointments.get(case, ()), key=lambda a: a.notified):
        slot = slot_index[apt.slot]
        state = APT_ACTIVE
        day_events = [e for e in case_events if e.at.date() == slot.day]
        if apt.appointment in cancelled_appointments:
            state = APT_SITE_CANCELLED
        elif any(e.kind == NO_SHOW for e in day_events):
            state = APT_NO_SHOW
        elif any(e.kind == COMPLETED for e in day_events):
            state = APT_FULFILLED
        view = AppointmentView(
            appointment=apt.appointment, case=case, slot=apt.slot, site=slot.site,
            day=slot.day, start_min=apt.start_min, end_min=apt.end_min,
            notified=apt.notified, state=state, as_of=as_of)
        views.append(view)
    return tuple(views)


def _current_site(portfolio: Portfolio, chain: tuple[str, ...], fallback: str) -> str:
    site = fallback
    transfers = [e for e in _chain_events(portfolio, chain) if e.kind == TRANSFER]
    if transfers:
        site = transfers[-1].site
    return site


def build_case_view(cal: ServiceCalendar, portfolio: Portfolio, case: str,
                    as_of: date, soon_days: int = 2) -> CaseView:
    cal.require_service_day(as_of)
    intake = portfolio.intakes[case]
    policy = portfolio.policies[intake.service]
    chain = chain_of(portfolio.intakes, case)
    anchor_accepted = portfolio.intakes[chain[-1]].accepted
    events = [e for e in _chain_events(portfolio, chain) if e.at.date() <= as_of]

    completed = next((e for e in events if e.kind == COMPLETED), None)
    withdrawn = next((e for e in events if e.kind == WITHDRAW), None)
    if completed is not None:
        status, completion_day = "completed", completed.at.date()
    elif withdrawn is not None and case == chain[-1] and not chain[1:]:
        # 锚点申请自身撤回、且没有重办承接
        status, completion_day = "withdrawn", withdrawn.at.date()
    else:
        # 被重办承接的旧申请撤回（_chain_events 已过滤该事件）或链上仍有有效申请
        status, completion_day = "open", None

    due, incomplete = compute_due(cal, portfolio, chain, policy.limit_days)
    elapsed = elapsed_active_days(cal, portfolio, chain, as_of)
    paused_eod = _paused_at_eod(events, as_of) if status == "open" else False
    episodes = _pause_episodes(cal, portfolio, chain, as_of)
    views = _appointment_views(cal, portfolio, case, as_of)
    current_site = _current_site(portfolio, chain, intake.station)

    # 等待钟：首次有效预约，且通知事实在观察日（含）之前已经发生，杜绝未来信息
    effective = sorted((a for a in views
                        if a.effective and a.notified <= as_of),
                       key=lambda a: a.notified)
    if effective:
        wait_days = cal.count_service_days(intake.accepted, effective[0].notified)
        wait_censored = False
    else:
        wait_days, wait_censored = None, True

    overdue_at_completion = None
    if status == "completed" and due is not None:
        overdue_at_completion = completion_day > due

    bucket = _bucket(cal, as_of, status, paused_eod, views, due, incomplete, soon_days)
    note = ""
    if chain[1:]:
        note = f"重办链 {'→'.join(reversed(chain))}，restart={intake.restart}，义务钟锚点 {chain[-1]}（{anchor_accepted}）"

    return CaseView(
        case=case, service=intake.service, anchor_case=chain[-1],
        anchor_accepted=anchor_accepted, policy_limit=policy.limit_days,
        due=due, due_incomplete=incomplete, elapsed_active=elapsed,
        paused_eod=paused_eod, current_site=current_site, status=status,
        bucket=bucket, wait_days=wait_days, wait_censored=wait_censored,
        completion_day=completion_day, overdue_at_completion=overdue_at_completion,
        appointments=views, pause_episodes=episodes, chain=chain, note=note)


def _bucket(cal, as_of, status, paused_eod, views, due, incomplete, soon_days) -> str:
    if status == "completed":
        return COMPLETED
    if status == "withdrawn":
        return WITHDRAWN
    if paused_eod:
        return PAUSED
    future = [a for a in views if a.effective and a.future]
    if future:
        return SCHEDULED
    if incomplete:
        return UNSCHEDULED
    if due < as_of:
        return OVERDUE
    if due == as_of:
        return DUE_TODAY
    if cal.ordinal(due) - cal.ordinal(as_of) <= soon_days:
        return DUE_SOON
    return UNSCHEDULED


def build_portfolio_view(cal: ServiceCalendar, portfolio: Portfolio, as_of: date,
                         soon_days: int = 2) -> list[CaseView]:
    """观察日日末快照：只含观察日（含）之前受理的申请。"""
    cal.require_service_day(as_of)
    cases = [c for c, i in portfolio.intakes.items() if i.accepted <= as_of]
    return [build_case_view(cal, portfolio, c, as_of, soon_days) for c in sorted(cases)]
