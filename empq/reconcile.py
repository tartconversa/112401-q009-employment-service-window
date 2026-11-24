"""同一服务日的批次变化核对。

场景：同一份外部回件（如 ``EV``）可能让多名暂停申请同时重新计时，而接收站
当天又取消一批预约（如 ``EG``）。系统必须把两类变化放在一起核对，并据此重算
容量——尤其不能把互援能力继续算在仍保留/已取消的预约上。

处理顺序（日末口径）：回流 -> 站点取消/转接 -> 再做规划。规划输入同时反映
两种变化，输出中显式列出"受回流影响"与"受取消影响"的申请及其处置差异。
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .calendar import ServiceCalendar
from .capacity import PlanResult, build_plan
from .model import RESUME, SITE_CANCEL, TRANSFER, Portfolio
from .timeline import APT_SITE_CANCELLED, CaseView, build_portfolio_view


@dataclass(frozen=True)
class ReceiptBatch:
    receipt: str
    day: date
    kind: str                  # resume / site-cancel / transfer / other
    cases: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class ReconcileItem:
    case: str
    service: str
    resumed: bool
    site_cancelled: bool
    transferred_to: Optional[str]
    affected_receipts: tuple[str, ...]
    lost_capacity_slot: Optional[str]   # 取消导致释放的窗口
    restarted_due: Optional[date]       # 回流后的到期日
    plan_firmness: str                  # 新规划中该申请得到 firm/conditional/none
    detail: str


@dataclass(frozen=True)
class ReconcileResult:
    day: date
    batches: tuple[ReceiptBatch, ...]
    items: tuple[ReconcileItem, ...]
    plan: PlanResult
    guard_note: str


def reconcile_day(cal: ServiceCalendar, portfolio: Portfolio, day: date,
                  aid_decisions: Optional[dict[str, str]] = None) -> ReconcileResult:
    cal.require_service_day(day)
    day_events = [e for e in portfolio.events if e.at.date() == day]
    receipts: dict[str, list] = {}
    for e in day_events:
        receipts.setdefault(e.receipt, []).append(e)

    batches: list[ReceiptBatch] = []
    affected: dict[str, dict] = {}
    for receipt, evs in sorted(receipts.items()):
        kinds = {e.kind for e in evs}
        if RESUME in kinds:
            kind = "resume"
        elif SITE_CANCEL in kinds:
            kind = "site-cancel"
        elif TRANSFER in kinds:
            kind = "transfer"
        else:
            kind = "other"
        cases = tuple(sorted({e.case for e in evs}))
        batches.append(ReceiptBatch(
            receipt=receipt, day=day, kind=kind, cases=cases,
            note=f"回执 {receipt} 在 {day} 同时影响 {len(cases)} 件申请: {', '.join(cases)}"))
        for e in evs:
            rec = affected.setdefault(e.case, {"resumed": False, "cancel": False,
                                               "transfer": None, "receipts": []})
            rec["receipts"].append(receipt)
            if e.kind == RESUME:
                rec["resumed"] = True
            elif e.kind == SITE_CANCEL:
                rec["cancel"] = True
            elif e.kind == TRANSFER:
                rec["transfer"] = e.site

    # 日末视图与规划：暂停状态、取消窗口、转接站点已全部生效，互援不会算到旧预约上
    views = build_portfolio_view(cal, portfolio, day)
    view_index = {v.case: v for v in views}
    plan = build_plan(cal, portfolio, day, views, aid_decisions)
    firm_map = {i.case: i for i in plan.firm_invitations}
    cond_map = {i.case: i for i in plan.conditional_invitations}
    protected_cases = {i.case for i in plan.protected}

    items: list[ReconcileItem] = []
    for case in sorted(affected):
        rec = affected[case]
        v = view_index.get(case)
        if v is None:
            continue
        lost_slot = None
        if rec["cancel"]:
            lost_slot = next((a.slot for a in v.appointments
                              if a.state == APT_SITE_CANCELLED), None)
        if case in firm_map:
            firmness, detail = "firm", f"已取得真实时段 {firm_map[case].slot}"
        elif case in cond_map:
            firmness, detail = "conditional", "仅有回流假设下的条件容量，未发约定"
        elif case in protected_cases:
            firmness, detail = "protected", "仍持有有效的已承诺预约"
        else:
            firmness, detail = "none", "日末规划中无任何可用容量"
        items.append(ReconcileItem(
            case=case, service=v.service, resumed=rec["resumed"],
            site_cancelled=rec["cancel"], transferred_to=rec["transfer"],
            affected_receipts=tuple(sorted(set(rec["receipts"]))),
            lost_capacity_slot=lost_slot, restarted_due=v.due,
            plan_firmness=firmness, detail=detail))

    guard = ("规划在全部日末事件生效后统一重算：被取消窗口的容量不再计入，"
             "互援人员的空档只与仍有效的承诺互斥；回流与取消同批核对，避免重复承诺。")
    return ReconcileResult(day=day, batches=tuple(batches), items=tuple(items),
                           plan=plan, guard_note=guard)
