"""不可变领域记录与基础解析。"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


# events.csv 的 kind 取值
PAUSE = "pause"
RESUME = "resume"
COMPLETED = "completed"
WITHDRAW = "withdraw"
NO_SHOW = "no-show"
SITE_CANCEL = "site-cancel"
TRANSFER = "transfer"

# slots.csv 的 status 取值
SLOT_OPEN = "open"          # 可直接预约的本站窗口
SLOT_PROPOSED = "proposed"  # 互援方案中的窗口，须窗口负责人接受后才可发约定
SLOT_CANCELLED = "cancelled"  # 当天取消，容量不再可用

TERMINAL_EVENTS = frozenset({COMPLETED, WITHDRAW})
SITE_EVENTS = frozenset({SITE_CANCEL, TRANSFER})

YES = {"yes", "y", "true", "1"}


def parse_bool(value: str) -> bool:
    return (value or "").strip().lower() in YES


def parse_day(value: str) -> date:
    return date.fromisoformat(value.strip())


def parse_minutes(hhmm: str) -> int:
    """``HH:MM`` -> 当日分钟数。"""
    hour, minute = hhmm.strip().split(":")
    return int(hour) * 60 + int(minute)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.strip())


def split_multi(value: Optional[str]) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(v.strip() for v in value.split("|") if v.strip())


@dataclass(frozen=True)
class Policy:
    service: str
    limit_days: int           # 办理义务：第 N 个活跃服务日到期
    segments: int             # 完成所需连续半小时段数（segments*30 分钟）
    qualification: str
    can_suspend: bool
    hold_days: int            # 暂停后已通知预约保留多少服务日

    @property
    def duration_minutes(self) -> int:
        return self.segments * 30


@dataclass(frozen=True)
class Intake:
    case: str
    service: str
    station: str
    alternates: tuple[str, ...]
    accepted: date
    prior: Optional[str]      # 重办来源申请
    restart: bool             # True=正式回执确认另起时限；False=沿用来案责任


@dataclass(frozen=True)
class Event:
    event: str
    case: str
    kind: str
    at: datetime
    site: str
    receipt: str


@dataclass(frozen=True)
class Slot:
    slot: str
    site: str
    day: date
    start_min: int
    end_min: int
    qualification: str
    shared_staff: str
    status: str

    @property
    def length_min(self) -> int:
        return self.end_min - self.start_min


@dataclass(frozen=True)
class Appointment:
    appointment: str
    case: str
    slot: str
    start_min: int
    end_min: int
    notified: date


@dataclass(frozen=True)
class ScenarioBand:
    scenario: str
    service: str
    day: date
    resuming_low: int
    resuming_high: int
    arrivals_low: int
    arrivals_high: int


@dataclass(frozen=True)
class Portfolio:
    policies: dict[str, Policy]
    intakes: dict[str, Intake]
    events: list[Event]
    slots: list[Slot]
    appointments: list[Appointment]
    scenarios: list[ScenarioBand]
    by_case_events: dict[str, list[Event]] = field(default_factory=dict)
    by_case_appointments: dict[str, list[Appointment]] = field(default_factory=dict)
    by_slot_appointments: dict[str, list[Appointment]] = field(default_factory=dict)
