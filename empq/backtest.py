"""事后回检：用后来的办结事实检查先前的压力判断，并审计改签原因。"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .calendar import ServiceCalendar
from .model import Portfolio
from .timeline import (
    APT_NO_SHOW,
    APT_SITE_CANCELLED,
    CaseView,
    OVERDUE,
    build_case_view,
    build_portfolio_view,
)


@dataclass(frozen=True)
class BacktestRow:
    case: str
    service: str
    forecast_bucket: str          # 观察点当时的分桶
    forecast_due: Optional[date]
    completion_day: Optional[date]
    overdue_at_completion: Optional[bool]
    outcome: str                  # confirmed / false-alarm / still-open / withdrawn / window-censored
    detail: str


@dataclass(frozen=True)
class BacktestResult:
    as_of: date
    eval_day: date
    rows: tuple[BacktestRow, ...]
    precision_note: str


def backtest(cal: ServiceCalendar, portfolio: Portfolio, as_of: date,
             eval_day: Optional[date] = None) -> BacktestResult:
    """比较 as_of 日末判断与 eval_day（默认日历末日）已知事实。"""
    cal.require_service_day(as_of)
    if eval_day is None:
        eval_day = cal.service_days[-1]
    cal.require_service_day(eval_day)

    past_views = build_portfolio_view(cal, portfolio, as_of)
    rows: list[BacktestRow] = []
    for past in past_views:
        later = build_case_view(cal, portfolio, past.case, eval_day)
        if later.bucket == "completed":
            if later.overdue_at_completion:
                outcome = "confirmed"
                detail = (f"观察点判定 {past.bucket}（到期日 {past.due}），"
                          f"实际 {later.completion_day} 办结，确属逾期")
            elif past.bucket in {OVERDUE}:
                outcome = "false-alarm"
                detail = (f"观察点判定逾期但 {later.completion_day} 在到期日 "
                          f"{later.due} 前办结，需复核当时暂停/取消口径")
            else:
                outcome = "confirmed"
                detail = f"按期办结（{later.completion_day} ≤ 到期日 {later.due}）"
        elif later.bucket == "withdrawn":
            outcome = "withdrawn"
            detail = "申请撤回（若为重办来源，义务由重办申请承接，本条不参与逾期判定）"
        else:
            if later.due_incomplete:
                outcome = "window-censored"
                detail = "到期日落入未发布日历，观察窗不完整，无法判定是否逾期"
            else:
                outcome = "still-open"
                detail = f"截至 {eval_day} 仍未办结，当前分桶 {later.bucket}"
        rows.append(BacktestRow(
            case=past.case, service=past.service, forecast_bucket=past.bucket,
            forecast_due=None if later.bucket == "withdrawn" else past.due,
            completion_day=later.completion_day,
            overdue_at_completion=later.overdue_at_completion,
            outcome=outcome, detail=detail))

    judged = [r for r in rows if r.forecast_bucket == OVERDUE]
    hits = [r for r in judged if r.outcome == "confirmed"]
    note = (f"观察点 {as_of} 共判逾期 {len(judged)} 件，事后确认 {len(hits)} 件；"
            "未办结与日历不完整的案件不计入命中率分母，避免幸存者偏差。")
    return BacktestResult(as_of=as_of, eval_day=eval_day, rows=tuple(rows),
                          precision_note=note)


@dataclass(frozen=True)
class RebookRecord:
    case: str
    appointment: str
    slot: str
    notified: date
    reason_code: str             # site-cancel / no-show / none
    reason: str
    current_state: str
    has_future_commitment: bool


def rebook_audit(cal: ServiceCalendar, portfolio: Portfolio, as_of: date,
                 case: Optional[str] = None) -> list[RebookRecord]:
    """已通知约定为何改签：站点取消与申请人未到场分开列示。"""
    views = build_portfolio_view(cal, portfolio, as_of)
    out: list[RebookRecord] = []
    slot_index = {s.slot: s for s in portfolio.slots}
    for v in views:
        if case and v.case != case:
            continue
        for apt in v.appointments:
            if apt.state not in {APT_SITE_CANCELLED, APT_NO_SHOW}:
                continue
            if apt.state == APT_SITE_CANCELLED:
                reason = ("站点取消（责任在站点）：原约定作废，等待钟重新计时，"
                          "须在容量规划中优先补位并向申请人重新发约")
            else:
                reason = ("申请人未到场（责任在申请人）：已取得时段的事实保留，"
                          "办理义务钟不暂停，重新预约按普通排队处理")
            out.append(RebookRecord(
                case=v.case, appointment=apt.appointment, slot=apt.slot,
                notified=apt.notified, reason_code=apt.state, reason=reason,
                current_state=apt.state,
                has_future_commitment=bool(v.active_future_appointments)))
    return out
