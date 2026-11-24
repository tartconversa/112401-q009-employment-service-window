"""服务日日历与服务日算术。

calendar.csv 中 ``service_day=yes`` 的日期才是服务工作日；显式 ``no`` 的日期
是公布过的非服务日；表中不存在的日期属于尚未发布的日历，投影到该区域时抛出
:class:`OutsideCalendar`，由调用方作为"观察窗不完整"如实上报，而不是假设节奏。
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional


class OutsideCalendar(LookupError):
    """请求的服务日落在已发布日历之外。"""


@dataclass(frozen=True)
class ServiceCalendar:
    service_days: tuple[date, ...]
    closed_days: frozenset[date]

    @classmethod
    def build(cls, days: list[date], closed: list[date]) -> "ServiceCalendar":
        days = tuple(sorted(days))
        if len(days) != len(set(days)):
            raise ValueError("服务日存在重复日期")
        overlap = set(days) & set(closed)
        if overlap:
            raise ValueError(f"同一日期不能既是服务日又是非服务日: {sorted(overlap)}")
        return cls(days, frozenset(closed))

    @property
    def _index(self) -> dict[date, int]:
        return {d: i for i, d in enumerate(self.service_days)}

    def is_published(self, d: date) -> bool:
        return d in self._index or d in self.closed_days

    def is_service_day(self, d: date) -> bool:
        return d in self._index

    def require_service_day(self, d: date) -> None:
        if not self.is_service_day(d):
            raise OutsideCalendar(f"{d} 不是已发布的服务日")

    def ordinal(self, d: date) -> int:
        """服务日序号（从 0 开始），非服务日或未发布则报错。"""
        try:
            return self._index[d]
        except KeyError as exc:
            raise OutsideCalendar(f"{d} 不在已发布服务日表内") from exc

    def count_service_days(self, lo_exclusive: date, hi_inclusive: date) -> int:
        """满足 ``lo < d <= hi`` 的服务日数量（等待钟的原始口径）。"""
        idx = self._index
        return sum(1 for d in idx if lo_exclusive < d <= hi_inclusive)

    def add_service_days(self, d: date, n: int) -> date:
        """d 之后第 n 个服务日（n>=1）；超出日历时抛 :class:`OutsideCalendar`。"""
        if n < 1:
            raise ValueError("n 必须为正整数")
        i = self.ordinal(d) + n
        try:
            return self.service_days[i]
        except IndexError as exc:
            raise OutsideCalendar(f"{d} 之后第 {n} 个服务日超出已发布日历") from exc

    def service_days_after(self, d: date, horizon: Optional[date] = None) -> list[date]:
        """d 之后（不含）的服务日，可选截止到 horizon（含）。"""
        start = self.ordinal(d) + 1
        days = list(self.service_days[start:])
        if horizon is not None:
            days = [x for x in days if x <= horizon]
        return days

    def days_until(self, target: date, as_of: date) -> Optional[int]:
        """还剩几个服务日：as_of 之后（不含）到 target（含）的服务日数。

        target 在 as_of 之前时为负数（已逾期的服务日数）。两端都必须是服务日。
        """
        if target is None:
            return None
        return self.ordinal(target) - self.ordinal(as_of)
