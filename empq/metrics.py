"""等待分布、逾期压力与回流分组。

等待分布刻意给出两套口径并并列展示：

* ``naive_completed_median``——业务组旧做法：只取近窗内已办结申请的等待天数中位
  数。它把仍在排队、被站点取消后重新等待的人系统性地丢掉，且与暂停/取消口径无关。
* :func:`wait_distribution`——把"取得首次有效预约"作为事件、把观察日仍未取得预约
  的申请作为右删失（Kaplan–Meier 乘积极限）。暂停中、未预约的人不被当作流失；
  若中位生存期不可估计（半数以上仍删失），``median`` 为 ``None`` 并在报告中说明。
"""

from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Optional

from .calendar import ServiceCalendar
from .timeline import (
    CaseView,
    PAUSED,
    SCHEDULED,
    OVERDUE,
    DUE_TODAY,
    DUE_SOON,
    UNSCHEDULED,
    CLOSED_BUCKETS,
)

OPEN_BUCKETS = (OVERDUE, DUE_TODAY, DUE_SOON, SCHEDULED, UNSCHEDULED, PAUSED)


@dataclass(frozen=True)
class WaitPoint:
    day: int
    at_risk: int
    events: int
    censored: int
    survival: float          # 到该时刻之后仍未取得预约的比例


@dataclass(frozen=True)
class WaitDistribution:
    population: int
    events: int
    censored: int
    points: tuple[WaitPoint, ...]
    median: Optional[int]    # 最小的使 S<=0.5 的等待天数；不可估计为 None
    median_note: str
    censored_cases: tuple[str, ...]
    event_cases: tuple[tuple[str, int], ...]


def wait_distribution(views: list[CaseView]) -> WaitDistribution:
    """Kaplan–Meier：事件=首次取得有效预约（等待天数），其余为右删失。"""
    event_cases = sorted(((v.case, v.wait_days) for v in views
                          if not v.wait_censored and v.wait_days is not None),
                         key=lambda x: (x[1], x[0]))
    censored_cases = tuple(v.case for v in views if v.wait_censored)
    n = len(views)
    points: list[WaitPoint] = []
    at_risk = n
    survival = 1.0
    all_days = sorted({d for _, d in event_cases})
    # 删失个体没有等待天数，统一在最后一个观察时刻之后处理
    for day in all_days:
        ev = [c for c, d in event_cases if d == day]
        n_events = len(ev)
        if n_events == 0:
            continue
        survival *= (at_risk - n_events) / at_risk
        points.append(WaitPoint(day=day, at_risk=at_risk, events=n_events,
                                censored=0, survival=survival))
        at_risk -= n_events
    if censored_cases:
        points.append(WaitPoint(day=points[-1].day if points else 0, at_risk=at_risk,
                                events=0, censored=len(censored_cases),
                                survival=points[-1].survival if points else 1.0))
    med = next((p.day for p in points if p.survival <= 0.5), None)
    if n == 0:
        note = "观察点之前没有受理申请，等待分布为空。"
    elif med is None:
        note = (f"{len(censored_cases)}/{n} 仍未取得有效预约（右删失），"
                "删失比例过高，中位等待天数不可估计；下表给出各分位生存曲线与上界。")
    else:
        note = f"KM 中位等待 {med} 个服务日；{len(censored_cases)} 例删失已参与风险集。"
    return WaitDistribution(
        population=n, events=len(event_cases), censored=len(censored_cases),
        points=tuple(points), median=med, median_note=note,
        censored_cases=censored_cases,
        event_cases=tuple(event_cases))


def naive_completed_median(cal: ServiceCalendar, views: list[CaseView],
                           as_of: date, window_days: int = 14) -> tuple[Optional[float], tuple[str, ...]]:
    """旧口径：近 window_days 个日历日内办结者的等待天数中位数。"""
    lo = date.fromordinal(as_of.toordinal() - window_days)
    included_views = [v for v in views
                      if v.bucket == "completed" and v.completion_day is not None
                      and lo <= v.completion_day <= as_of and not v.wait_censored]
    days = sorted(v.wait_days for v in included_views if v.wait_days is not None)
    note_cases = tuple(v.case for v in included_views)
    return (median(days) if days else None), note_cases


@dataclass(frozen=True)
class BucketStat:
    bucket: str
    count: int
    cases: tuple[str, ...]


@dataclass(frozen=True)
class PressureRow:
    service: str
    site: str
    total_open: int
    overdue: int
    due_today: int
    due_soon: int
    scheduled: int
    unscheduled: int
    paused: int
    cases: tuple[str, ...]


@dataclass(frozen=True)
class PressureReport:
    as_of: date
    rows: tuple[PressureRow, ...]
    buckets: tuple[BucketStat, ...]
    wait: WaitDistribution
    naive_median: Optional[float]
    naive_cases: tuple[str, ...]
    outside_calendar_cases: tuple[str, ...]
    reflow: "ReflowCohorts"


def pressure_report(cal: ServiceCalendar, views: list[CaseView], as_of: date,
                    window_days: int = 14) -> PressureReport:
    open_views = [v for v in views if v.bucket not in CLOSED_BUCKETS]
    buckets = []
    for bucket in OPEN_BUCKETS:
        members = tuple(v.case for v in views if v.bucket == bucket)
        buckets.append(BucketStat(bucket, len(members), members))

    rows: dict[tuple[str, str], list[CaseView]] = {}
    for v in open_views:
        rows.setdefault((v.service, v.current_site), []).append(v)
    pressure_rows = []
    for (service, site), members in sorted(rows.items()):
        def cnt(b: str) -> int:
            return sum(1 for v in members if v.bucket == b)
        pressure_rows.append(PressureRow(
            service=service, site=site, total_open=len(members),
            overdue=cnt(OVERDUE), due_today=cnt(DUE_TODAY), due_soon=cnt(DUE_SOON),
            scheduled=cnt(SCHEDULED), unscheduled=cnt(UNSCHEDULED),
            paused=cnt(PAUSED), cases=tuple(v.case for v in members)))

    outside = tuple(v.case for v in open_views if v.due_incomplete)
    return PressureReport(
        as_of=as_of, rows=tuple(pressure_rows), buckets=tuple(buckets),
        wait=wait_distribution(views),
        naive_median=naive_completed_median(cal, views, as_of, window_days)[0],
        naive_cases=naive_completed_median(cal, views, as_of, window_days)[1],
        outside_calendar_cases=outside,
        reflow=reflow_cohorts(views))


@dataclass(frozen=True)
class ReflowGroup:
    service: str
    site: str
    paused_cases: tuple[str, ...]
    due_on_resume: tuple[tuple[str, Optional[date]], ...]  # 回流后剩余到期日未知的标注 None
    note: str


@dataclass(frozen=True)
class ReflowCohorts:
    groups: tuple[ReflowGroup, ...]

    @property
    def total_paused(self) -> int:
        return sum(len(g.paused_cases) for g in self.groups)


def reflow_cohorts(views: list[CaseView]) -> ReflowCohorts:
    """暂停分组：这批申请不是流失，而是可能在同一回执下同时回流的压力。"""
    groups: dict[tuple[str, str], list[CaseView]] = {}
    for v in views:
        if v.bucket == PAUSED:
            groups.setdefault((v.service, v.current_site), []).append(v)
    out = []
    for (service, site), members in sorted(groups.items()):
        due_on_resume = tuple((v.case, v.due) for v in members)
        unknown = [c for c, d in due_on_resume if d is None]
        note = (f"{len(members)} 例暂停等外部回件，义务钟冻结；回流当天重新计活跃日，"
                f"可能同时回流形成瞬时压力，不得计为流失。"
                + (f" 其中 {unknown} 的到期日超出已发布日历。" if unknown else ""))
        out.append(ReflowGroup(service, site, tuple(v.case for v in members),
                               due_on_resume, note))
    return ReflowCohorts(tuple(out))
