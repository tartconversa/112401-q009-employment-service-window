"""CSV 读取与关联校验。

读取时做最小但明确的契约校验：表头、外键、枚举、日期/时刻可解析性。
所有外部资料都按 README 的"不信任输入"对待，错误以 :class:`DataContractError`
指出具体表与键，不做静默修补。
"""

import csv
from pathlib import Path

from .calendar import ServiceCalendar
from .model import (
    Appointment,
    Event,
    Intake,
    Policy,
    Portfolio,
    ScenarioBand,
    Slot,
    SLOT_CANCELLED,
    SLOT_OPEN,
    SLOT_PROPOSED,
    parse_bool,
    parse_day,
    parse_minutes,
    parse_ts,
    split_multi,
)

HEADERS = {
    "policy": ("service", "limit_days", "segments", "qualification", "can_suspend", "hold_days"),
    "calendar": ("date", "service_day"),
    "intakes": ("case", "service", "station", "alternates", "accepted", "prior", "restart"),
    "events": ("event", "case", "kind", "at", "site", "receipt"),
    "slots": ("slot", "site", "day", "start", "end", "qualification", "shared_staff", "status"),
    "appointments": ("appointment", "case", "slot", "start", "end", "notified"),
    "scenarios": ("scenario", "service", "day", "resuming_low", "resuming_high",
                  "arrivals_low", "arrivals_high"),
}
EVENT_KINDS = frozenset({
    "pause", "resume", "completed", "withdraw", "no-show", "site-cancel", "transfer",
})
SLOT_STATUSES = frozenset({SLOT_OPEN, SLOT_PROPOSED, SLOT_CANCELLED})


class DataContractError(ValueError):
    """输入资料不满足读取契约。"""


def _rows(directory: Path, table: str) -> list[dict[str, str]]:
    path = directory / f"{table}.csv"
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, strict=True)
        if tuple(reader.fieldnames or ()) != HEADERS[table]:
            raise DataContractError(
                f"{table}.csv 表头不符: 实际 {reader.fieldnames}，期望 {HEADERS[table]}")
        rows = list(reader)
    for row in rows:
        if None in row or any(v is None for v in row.values()):
            raise DataContractError(f"{table}.csv 存在超出表头的列或缺失单元格")
    if not rows:
        raise DataContractError(f"{table}.csv 为空")
    return rows


def load_calendar(directory: Path) -> ServiceCalendar:
    days, closed = [], []
    for row in _rows(directory, "calendar"):
        d = parse_day(row["date"])
        if parse_bool(row["service_day"]):
            days.append(d)
        else:
            closed.append(d)
    return ServiceCalendar.build(days, closed)


def load_portfolio(directory: str | Path) -> tuple[ServiceCalendar, Portfolio]:
    directory = Path(directory)
    cal = load_calendar(directory)

    policies: dict[str, Policy] = {}
    for row in _rows(directory, "policy"):
        service = row["service"]
        if service in policies:
            raise DataContractError(f"policy.csv 业务 {service} 重复")
        policies[service] = Policy(
            service=service,
            limit_days=int(row["limit_days"]),
            segments=int(row["segments"]),
            qualification=row["qualification"],
            can_suspend=parse_bool(row["can_suspend"]),
            hold_days=int(row["hold_days"]),
        )

    intakes: dict[str, Intake] = {}
    for row in _rows(directory, "intakes"):
        case = row["case"]
        if case in intakes:
            raise DataContractError(f"intakes.csv 申请 {case} 重复")
        if row["service"] not in policies:
            raise DataContractError(f"intakes.csv {case} 的业务 {row['service']} 在 policy 中不存在")
        intakes[case] = Intake(
            case=case,
            service=row["service"],
            station=row["station"],
            alternates=split_multi(row["alternates"]),
            accepted=parse_day(row["accepted"]),
            prior=row["prior"] or None,
            restart=parse_bool(row["restart"]),
        )

    events: list[Event] = []
    for row in _rows(directory, "events"):
        if row["case"] not in intakes:
            raise DataContractError(f"events.csv {row['event']} 的申请 {row['case']} 未登记")
        if row["kind"] not in EVENT_KINDS:
            raise DataContractError(f"events.csv {row['event']} 未知 kind={row['kind']}")
        events.append(Event(
            event=row["event"], case=row["case"], kind=row["kind"],
            at=parse_ts(row["at"]), site=row["site"], receipt=row["receipt"],
        ))
    events.sort(key=lambda e: (e.at, e.event))

    slots: list[Slot] = []
    seen_slots: set[str] = set()
    for row in _rows(directory, "slots"):
        slot_id = row["slot"]
        if slot_id in seen_slots:
            raise DataContractError(f"slots.csv 窗口 {slot_id} 重复")
        seen_slots.add(slot_id)
        if row["status"] not in SLOT_STATUSES:
            raise DataContractError(f"slots.csv {slot_id} 未知 status={row['status']}")
        start, end = parse_minutes(row["start"]), parse_minutes(row["end"])
        if end <= start:
            raise DataContractError(f"slots.csv {slot_id} 结束时刻不晚于开始时刻")
        day = parse_day(row["day"])
        if not cal.is_published(day):
            raise DataContractError(f"slots.csv {slot_id} 的日期 {day} 不在已发布日历内")
        slots.append(Slot(slot_id, row["site"], day, start, end,
                          row["qualification"], row["shared_staff"], row["status"]))

    appointments: list[Appointment] = []
    slot_index = {s.slot: s for s in slots}
    for row in _rows(directory, "appointments"):
        if row["case"] not in intakes:
            raise DataContractError(f"appointments.csv {row['appointment']} 的申请未登记")
        if row["slot"] not in slot_index:
            raise DataContractError(f"appointments.csv {row['appointment']} 的窗口 {row['slot']} 不存在")
        slot = slot_index[row["slot"]]
        start, end = parse_minutes(row["start"]), parse_minutes(row["end"])
        if not (slot.start_min <= start < end <= slot.end_min):
            raise DataContractError(
                f"appointments.csv {row['appointment']} 时段超出窗口 {slot.slot} 边界")
        appointments.append(Appointment(
            row["appointment"], row["case"], row["slot"], start, end, parse_day(row["notified"])))

    scenarios: list[ScenarioBand] = []
    for row in _rows(directory, "scenarios"):
        if row["service"] not in policies:
            raise DataContractError(f"scenarios.csv 业务 {row['service']} 在 policy 中不存在")
        scenarios.append(ScenarioBand(
            row["scenario"], row["service"], parse_day(row["day"]),
            int(row["resuming_low"]), int(row["resuming_high"]),
            int(row["arrivals_low"]), int(row["arrivals_high"]),
        ))

    by_case_events: dict[str, list[Event]] = {}
    for e in events:
        by_case_events.setdefault(e.case, []).append(e)
    by_case_appointments: dict[str, list[Appointment]] = {}
    by_slot_appointments: dict[str, list[Appointment]] = {}
    for a in appointments:
        by_case_appointments.setdefault(a.case, []).append(a)
        by_slot_appointments.setdefault(a.slot, []).append(a)

    _validate_references(intakes, events, policies, cal, slot_index, appointments)

    portfolio = Portfolio(
        policies=policies, intakes=intakes, events=events, slots=slots,
        appointments=appointments, scenarios=scenarios,
        by_case_events=by_case_events,
        by_case_appointments=by_case_appointments,
        by_slot_appointments=by_slot_appointments,
    )
    return cal, portfolio


def _validate_references(intakes, events, policies, cal, slot_index, appointments) -> None:
    # 重办链必须指向已登记申请，且同业务
    for case, intake in intakes.items():
        if intake.prior:
            if intake.prior not in intakes:
                raise DataContractError(f"intakes.csv {case} 的 prior {intake.prior} 未登记")
            if intakes[intake.prior].service != intake.service:
                raise DataContractError(f"intakes.csv {case} 与 prior 业务不一致")
    # 暂停只能发生在允许暂停的业务
    for e in events:
        if e.kind == "pause" and not policies[intakes[e.case].service].can_suspend:
            raise DataContractError(f"events.csv {e.event}: 业务 {intakes[e.case].service} 不允许暂停")
    # 日历覆盖受理日
    for case, intake in intakes.items():
        if not cal.is_published(intake.accepted):
            raise DataContractError(f"intakes.csv {case} 受理日 {intake.accepted} 超出已发布日历")
    # 同窗口的已承诺预约不得重叠
    for slot_id, booked in _group(appointments, lambda a: a.slot).items():
        ordered = sorted(booked, key=lambda a: a.start_min)
        for a, b in zip(ordered, ordered[1:]):
            if a.end_min > b.start_min:
                raise DataContractError(f"窗口 {slot_id} 的已承诺预约 {a.appointment}/{b.appointment} 重叠")
    # 同 shared_staff 不同窗口的已承诺预约也不得时间重叠
    staff_groups: dict[str, list[tuple[Slot, Appointment]]] = {}
    for a in appointments:
        slot = slot_index[a.slot]
        staff_groups.setdefault(slot.shared_staff, []).append((slot, a))
    for staff, items in staff_groups.items():
        items.sort(key=lambda item: (item[0].day, item[1].start_min))
        for (s1, a1), (s2, a2) in zip(items, items[1:]):
            if s1.day == s2.day and a1.end_min > a2.start_min:
                raise DataContractError(
                    f"共用人员 {staff} 在 {s1.day} 被 {a1.appointment}/{a2.appointment} 重复占用")


def _group(items, key):
    out: dict[str, list] = {}
    for item in items:
        out.setdefault(key(item), []).append(item)
    return out
