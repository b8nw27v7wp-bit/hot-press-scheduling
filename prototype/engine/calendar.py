"""生产日历：把自然日展开成可排程的槽位（生产日 × 班次）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from domain.models import CalendarDay, Params


@dataclass(frozen=True)
class Slot:
    index: int
    day: date
    shift: int  # 1-based


class Calendar:
    def __init__(self, days: dict[date, CalendarDay], params: Params, horizon_end: date):
        self._params = params
        self._missing: list[date] = []
        self._slots: list[Slot] = []
        self._build(days, horizon_end)

    def _build(self, days: dict[date, CalendarDay], horizon_end: date) -> None:
        idx = 0
        cur = self._params.plan_start
        while cur <= horizon_end:
            rec = days.get(cur)
            if rec is None:
                # 日历缺失：按不生产处理，并记录缺失
                self._missing.append(cur)
                cur += timedelta(days=1)
                continue
            if rec.is_working:
                for s in range(1, rec.shifts + 1):
                    self._slots.append(Slot(index=idx, day=cur, shift=s))
                    idx += 1
            cur += timedelta(days=1)

    @property
    def slots(self) -> list[Slot]:
        return self._slots

    @property
    def missing_days(self) -> list[date]:
        return self._missing

    def missing_ranges(self, until: date | None = None) -> list[tuple[date, date]]:
        """把连续缺失的日期并成区间，便于按「一段日历缺失」报告（§13.4）。

        until 用于只报计划窗口内的缺失：地平线是悲观外推出来的，其后的空白日
        并不影响本次计划，逐日上报只会淹没真正有影响的缺口。
        """
        ranges: list[tuple[date, date]] = []
        for d in self._missing:  # 已按日期升序
            if until is not None and d > until:
                break
            if ranges and d == ranges[-1][1] + timedelta(days=1):
                ranges[-1] = (ranges[-1][0], d)
            else:
                ranges.append((d, d))
        return ranges

    def __len__(self) -> int:
        return len(self._slots)

    def slot(self, index: int) -> Slot:
        if index >= len(self._slots):
            raise IndexError(f"slot {index} beyond horizon ({len(self._slots)})")
        return self._slots[index]

    def day_of(self, index: int) -> date:
        return self.slot(index).day

    def first_slot_on_or_after(self, day: date) -> int:
        for s in self._slots:
            if s.day >= day:
                return s.index
        return len(self._slots)
