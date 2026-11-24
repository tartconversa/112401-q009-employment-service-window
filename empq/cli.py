"""命令行：以任意服务日为观察点的压力、容量与回检。

示例：
    python3 -m empq.cli observe samples --as-of 2026-11-09
    python3 -m empq.cli plan samples --as-of 2026-11-09 --accept S6 --json
    python3 -m empq.cli reconcile samples --day 2026-11-09
    python3 -m empq.cli backtest samples --as-of 2026-11-06
    python3 -m empq.cli case samples --case C02 --as-of 2026-11-09
"""

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any

from .backtest import backtest, rebook_audit
from .calendar import OutsideCalendar
from .capacity import (
    ACCEPT,
    DEFER,
    RETURN,
    build_plan,
    compare_aid_options,
    scenario_projection,
)
from .load import DataContractError, load_portfolio
from .metrics import pressure_report
from .reconcile import reconcile_day
from .timeline import build_case_view

BUCKET_LABELS = {
    "overdue": "已逾期", "due-today": "今日到期", "due-soon": "临近到期",
    "scheduled": "已有有效预约", "unscheduled": "未预约", "paused": "暂停等回件",
    "completed": "已办结", "withdrawn": "已撤回",
}


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if is_dataclass(obj):
        return asdict(obj)
    raise TypeError(f"不可序列化: {type(obj)}")


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default))


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = lambda cells: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))
    parts = [line(headers), "  ".join("-" * w for w in widths)]
    parts.extend(line(r) for r in rows)
    return "\n".join(parts)


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _load(directory: str):
    try:
        return load_portfolio(directory)
    except DataContractError as exc:
        print(f"资料契约错误: {exc}", file=sys.stderr)
        raise SystemExit(2)


def cmd_observe(args) -> None:
    cal, pf = _load(args.data)
    as_of = date.fromisoformat(args.as_of)
    try:
        views = [build_case_view(cal, pf, c, as_of) for c in sorted(pf.intakes)
                 if pf.intakes[c].accepted <= as_of]
        report = pressure_report(cal, views, as_of, args.window)
    except OutsideCalendar as exc:
        print(f"观察点超出已发布日历: {exc}", file=sys.stderr)
        raise SystemExit(2)

    if args.json:
        _print_json(report)
        return
    print(f"== 观察点 {as_of} 服务日日末：等待分布与逾期压力 ==\n")
    print(f"旧口径（近 {args.window} 天已办结等待中位数）: {report.naive_median} "
          f"服务日；样本 {list(report.naive_cases)}")
    print(f"KM 口径: 总体 {report.wait.population}，事件 {report.wait.events}，"
          f"右删失 {report.wait.censored}，中位数 {report.wait.median} 服务日")
    print(f"  {report.wait.median_note}\n")
    print(_table(["等待服务日", "风险集", "取得预约", "删失", "留存比例 S(t)"],
                 [[str(p.day), str(p.at_risk), str(p.events), str(p.censored),
                   f"{p.survival:.3f}"] for p in report.wait.points]))
    print("\n分桶:")
    print(_table(["分桶", "件数", "申请"],
                 [[BUCKET_LABELS[b.bucket], str(b.count), ", ".join(b.cases) or "-"]
                  for b in report.buckets]))
    print("\n按业务×当前责任站点:")
    print(_table(["业务", "站点", "在办", "逾期", "今日到期", "临近", "已预约", "未预约", "暂停"],
                 [[r.service, r.site, str(r.total_open), str(r.overdue), str(r.due_today),
                   str(r.due_soon), str(r.scheduled), str(r.unscheduled), str(r.paused)]
                  for r in report.rows]))
    print("\n暂停回流分组（不是流失）:")
    for g in report.reflow.groups:
        print(f"  {g.service}@{g.site}: {', '.join(g.paused_cases)} — {g.note}")
    print("\n口径提示:")
    for c in report.outside_calendar_cases:
        print(f"  - {c} 的到期日超出已发布日历，逾期判断暂不可下结论（观察窗不完整）")
    print("  - 右删失申请仍参与风险集；未办结不等于流失，暂停件回流当天重新计活跃日。")


def _aid_map(args) -> dict[str, str]:
    decisions = {}
    for slot in args.accept or []:
        decisions[slot] = ACCEPT
    for slot in args.defer or []:
        decisions[slot] = DEFER
    for slot in args.return_ or []:
        decisions[slot] = RETURN
    return decisions


def cmd_plan(args) -> None:
    cal, pf = _load(args.data)
    as_of = date.fromisoformat(args.as_of)
    views = [build_case_view(cal, pf, c, as_of) for c in sorted(pf.intakes)
             if pf.intakes[c].accepted <= as_of]
    plan = build_plan(cal, pf, as_of, views, _aid_map(args))
    scenarios = scenario_projection(cal, pf, as_of, views, plan)
    options = compare_aid_options(cal, pf, as_of, views)

    if args.json:
        _print_json({"plan": plan, "scenarios": scenarios, "aid_options": options})
        return
    print(f"== {as_of} 日末预约建议（先守承诺，再按逾期风险补位） ==\n")
    print("已守住的有效承诺:")
    print(_table(["申请", "窗口", "站点", "日期", "时段"],
                 [[i.case, i.slot, i.site, str(i.day), f"{_hm(i.start_min)}-{_hm(i.end_min)}"]
                  for i in plan.protected]))
    print("\n新增确定邀请（确有时段，可发约定）:")
    print(_table(["申请", "窗口", "站点", "日期", "时段", "依据"],
                 [[i.case, i.slot, i.site, str(i.day),
                   f"{_hm(i.start_min)}-{_hm(i.end_min)}", i.reason]
                  for i in plan.firm_invitations] or [["-", "-", "-", "-", "-", "-"]]))
    print("\n条件容量（仅回流/互援假设，不是邀请）:")
    print(_table(["申请", "窗口", "站点", "日期", "时段"],
                 [[i.case, i.slot, i.site, str(i.day), f"{_hm(i.start_min)}-{_hm(i.end_min)}"]
                  for i in plan.conditional_invitations] or [["-", "-", "-", "-", "-"]]))
    print("\n暂停保留处置:")
    print(_table(["申请", "预约", "窗口", "处置", "保留至", "理由"],
                 [[h.case, h.appointment, h.slot,
                   "还池" if h.action == "release" else "保留",
                   str(h.hold_until or "-"), h.reason] for h in plan.hold_decisions]
                 or [["-", "-", "-", "-", "-", "-"]]))
    print("\n互援提议:")
    print(_table(["窗口", "决策", "可承接申请", "说明"],
                 [[o.offer, o.decision, ", ".join(o.usable_for) or "-", o.note]
                  for o in plan.offers]))
    print("\n未满足需求:")
    print(_table(["申请", "业务", "分桶", "到期日", "原因"],
                 [[u.case, u.service, BUCKET_LABELS.get(u.bucket, u.bucket),
                   str(u.due or "日历外"), u.reason] for u in plan.unmet]
                 or [["-", "-", "-", "-", "-"]]))
    print("\n互援方案比较（风险分越低越好）:")
    print(_table(["方案", "风险分", "较默认改善", "确定邀请", "条件容量", "未满足"],
                 [[o["option"], str(o["risk_score"]), str(o["risk_improvement"]),
                   str(o["firm_invitations"]), str(o["conditional_capacity"]),
                   ", ".join(o["unmet_cases"]) or "-"] for o in options]))
    print("\n情景测算:")
    print(_table(["情景", "业务", "日期", "积压", "回流低-高", "新受理低-高",
                  "确定容量", "条件容量", "缺口低-高"],
                 [[r.scenario, r.service, str(r.day), str(r.backlog_unmet),
                   f"{r.resuming_low}-{r.resuming_high}",
                   f"{r.arrivals_low}-{r.arrivals_high}", str(r.firm_capacity),
                   str(r.conditional_capacity), f"{r.unmet_low}-{r.unmet_high}"]
                  for r in scenarios]))


def cmd_reconcile(args) -> None:
    cal, pf = _load(args.data)
    result = reconcile_day(cal, pf, date.fromisoformat(args.day), _aid_map(args))
    if args.json:
        _print_json(result)
        return
    print(f"== {args.day} 批次变化核对 ==\n")
    for b in result.batches:
        print(f"回执 {b.receipt}（{b.kind}）: {b.note}")
    print("\n合并处置:")
    print(_table(["申请", "回流", "站点取消", "转接", "失效窗口", "回流后到期日", "规划结果"],
                 [[i.case, "是" if i.resumed else "-",
                   "是" if i.site_cancelled else "-", i.transferred_to or "-",
                   i.lost_capacity_slot or "-", str(i.restarted_due or "日历外"),
                   i.plan_firmness] for i in result.items]))
    print(f"\n护栏: {result.guard_note}")


def cmd_backtest(args) -> None:
    cal, pf = _load(args.data)
    result = backtest(cal, pf, date.fromisoformat(args.as_of),
                      date.fromisoformat(args.eval_day) if args.eval_day else None)
    audit = rebook_audit(cal, pf, result.eval_day)
    if args.json:
        _print_json({"backtest": result, "rebook_audit": audit})
        return
    print(f"== 回检：{result.as_of} 的判断对照 {result.eval_day} 的事实 ==\n")
    print(_table(["申请", "当时分桶", "当时到期日", "办结日", "办结是否逾期", "结论", "说明"],
                 [[r.case, BUCKET_LABELS.get(r.forecast_bucket, r.forecast_bucket),
                   str(r.forecast_due or "日历外"), str(r.completion_day or "-"),
                   ("是" if r.overdue_at_completion else
                    "否" if r.overdue_at_completion is False else "-"),
                   r.outcome, r.detail] for r in result.rows]))
    print(f"\n{result.precision_note}")
    print("\n改签审计:")
    print(_table(["申请", "预约", "窗口", "通知日", "原因", "现有未来承诺"],
                 [[r.case, r.appointment, r.slot, str(r.notified), r.reason,
                   "有" if r.has_future_commitment else "无"] for r in audit]
                 or [["-", "-", "-", "-", "-", "-"]]))


def cmd_case(args) -> None:
    cal, pf = _load(args.data)
    view = build_case_view(cal, pf, args.case, date.fromisoformat(args.as_of))
    if args.json:
        _print_json(view)
        return
    print(f"== 申请 {args.case}（{view.service}）截至 {args.as_of} 日末 ==")
    print(f"义务钟锚点: {view.anchor_case} 受理于 {view.anchor_accepted}；"
          f"时限 {view.policy_limit} 活跃服务日；到期日 {view.due or '超出已发布日历'}"
          f"{'（观察窗不完整）' if view.due_incomplete else ''}")
    print(f"已用活跃服务日 {view.elapsed_active}；日末状态 {view.status}/"
          f"{BUCKET_LABELS.get(view.bucket, view.bucket)}；暂停中 {view.paused_eod}；"
          f"当前责任站点 {view.current_site}")
    if view.note:
        print(view.note)
    print(f"等待钟: {view.wait_days if view.wait_days is not None else '尚未取得有效预约（右删失）'} 服务日")
    print("预约记录:")
    print(_table(["预约", "窗口", "站点", "日期", "时段", "通知日", "状态", "有效", "未来"],
                 [[a.appointment, a.slot, a.site, str(a.day),
                   f"{_hm(a.start_min)}-{_hm(a.end_min)}", str(a.notified), a.state,
                   "是" if a.effective else "否", "是" if a.future else "否"]
                  for a in view.appointments] or [["-"] * 9]))
    print("暂停段:")
    print(_table(["开始", "回流", "回件回执", "保留至", "应还池"],
                 [[str(e.start), str(e.end or "未回流"), e.resume_receipt or "-",
                   str(e.hold_until or "-"), "是" if e.release_due else "否"]
                  for e in view.pause_episodes] or [["-"] * 5]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="empq", description="就业服务预约积压压力与窗口承接")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("data", help="CSV 资料目录")
        p.add_argument("--json", action="store_true", help="输出 JSON")

    p = sub.add_parser("observe", help="观察点压力与等待分布")
    add_common(p)
    p.add_argument("--as-of", required=True)
    p.add_argument("--window", type=int, default=14, help="旧口径近窗日历日数")
    p.set_defaults(func=cmd_observe)

    p = sub.add_parser("plan", help="预约建议、互援比较与情景测算")
    add_common(p)
    p.add_argument("--as-of", required=True)
    p.add_argument("--accept", nargs="*", default=[], help="接受的互援窗口")
    p.add_argument("--defer", nargs="*", default=[], help="暂缓的互援窗口")
    p.add_argument("--return-", nargs="*", default=[], help="退回的互援窗口")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("reconcile", help="某日批次变化核对")
    add_common(p)
    p.add_argument("--day", required=True)
    p.add_argument("--accept", nargs="*", default=[])
    p.add_argument("--defer", nargs="*", default=[])
    p.add_argument("--return-", nargs="*", default=[])
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("backtest", help="事后回检与改签审计")
    add_common(p)
    p.add_argument("--as-of", required=True)
    p.add_argument("--eval-day", default=None)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("case", help="单申请到期口径与补位动作查询")
    add_common(p)
    p.add_argument("--case", required=True)
    p.add_argument("--as-of", required=True)
    p.set_defaults(func=cmd_case)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
