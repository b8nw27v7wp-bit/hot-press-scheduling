"""时间排布：机台/模具时间轴、候选评估、提交。

机台时间轴以「已提交区间序列」表示（而非单调游标），这样才支持 §12.6 的回填：
批次可以落入前面遗留的空闲区间，而换模按**紧邻前驱区间**判定，与落位先后无关。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from domain.models import Batch, Mold, Params, Press
from engine.calendar import Calendar
from engine.capacity import changeover_cycles


@dataclass(frozen=True)
class Interval:
    """机台上一段已提交的占用：起止槽位、所用模具、该段起始处的换模消耗。"""

    start: int
    end: int
    mold_no: str
    changeover: int


@dataclass
class PressTimeline:
    press: Press
    used: list[int] = field(default_factory=list)  # 每槽位已用模数
    intervals: list[Interval] = field(default_factory=list)  # 按 start 升序，互不重叠
    cursor: int = 0  # 严格追加指针：不回填时任务的搜索起点
    first_free: int = 0  # 首个仍有剩余产能的槽位：回填时的搜索起点

    def ensure(self, n: int) -> None:
        if len(self.used) < n:
            self.used.extend([0] * (n - len(self.used)))

    def mold_before(self, slot: int) -> str | None:
        """紧邻 slot 之前那一段所占用的模具；此前无任务则为 None（未装模）。

        intervals 按 start 升序且互不重叠，故 slot 之前最近的一段即 start < slot 的最后一段。
        """
        i = bisect_left(self.intervals, slot, key=lambda iv: iv.start)
        return self.intervals[i - 1].mold_no if i > 0 else None

    def successor_of(self, slot: int) -> Interval | None:
        """slot 之后最近的一段占用。"""
        i = bisect_right(self.intervals, slot, key=lambda iv: iv.start)
        return self.intervals[i] if i < len(self.intervals) else None


@dataclass
class MoldTimeline:
    mold_no: str
    busy: list[bool] = field(default_factory=list)

    def ensure(self, n: int) -> None:
        if len(self.busy) < n:
            self.busy.extend([False] * (n - len(self.busy)))


@dataclass(frozen=True)
class Placement:
    start: int
    end: int
    changeover: int


@dataclass
class PlacementDecision:
    """一次落位决策及其依据，供 §14 理由生成使用。"""

    press: PressTimeline
    mold: Mold
    placement: Placement
    cycles: int
    switched: bool
    press_options: list[tuple[str, int]]  # 每台候选机台的最早开工槽位，按机台号升序


class Scheduler:
    def __init__(self, calendar: Calendar, params: Params):
        self.calendar = calendar
        self.params = params
        self._n = len(calendar)

    # ---------- 评估（只读） ----------

    def _free_run_end(self, press: PressTimeline, mold_tl: MoldTimeline, start: int) -> int:
        """从 start 起的「机台空且模具空」连续区间的结束槽位（含）；start 本身不空闲则为 start-1。"""
        s = start
        while s < self._n and press.used[s] == 0 and not mold_tl.busy[s]:
            s += 1
        return s - 1

    def _run_covers(
        self, press: PressTimeline, mold_tl: MoldTimeline, start: int, need: int
    ) -> bool:
        """从 start 起的连续空闲区间是否至少有 need 个槽位。

        只需确认「够长」即停，不测量区间全长——否则每个候选都要扫到区间末尾，
        在空闲区间很长时会退化成 O(槽位数)/次。
        """
        limit = min(self._n, start + need)
        s = start
        while s < limit and press.used[s] == 0 and not mold_tl.busy[s]:
            s += 1
        return s - start >= need

    def _changeover_for(
        self, press: PressTimeline, start: int, mold_no: str, base_chg: int
    ) -> int:
        prev_mold = press.mold_before(start)
        return base_chg if (prev_mold is not None and prev_mold != mold_no) else 0

    def _insertion_ok(
        self, press: PressTimeline, end: int, mold_no: str, base_chg: int
    ) -> bool:
        """插入后不得让后继批次的换模记账失真。

        后继批次的换模记的是「它的前驱→它」是否为换模。若我们插到它与它的前驱之间，
        换模需求可能从 0 变成 base_chg，而该值已经写死在已提交的区间里，改不了。
        因此这种插入必须放弃，换一个位置。
        """
        succ = press.successor_of(end)
        if succ is None:
            return True
        required = base_chg if mold_no != succ.mold_no else 0
        return required == succ.changeover

    def evaluate(
        self,
        press: PressTimeline,
        mold: Mold,
        mold_tl: MoldTimeline,
        cycles: int,
    ) -> Placement | None:
        press.ensure(self._n)
        mold_tl.ensure(self._n)
        capacity = press.press.capacity_per_shift
        if capacity <= 0:
            return None
        base_chg = changeover_cycles(self.params, capacity)

        # 回填：从首个仍有剩余产能的槽位起找空闲区间，允许填入前面遗留的空档；
        # 不回填：只能从追加游标起单调追加（§12.6）。
        start = press.first_free if self.params.backfill_gaps else press.cursor
        while start < self._n:
            if press.used[start] != 0 or mold_tl.busy[start]:
                start += 1
                continue
            chg = self._changeover_for(press, start, mold.mold_no, base_chg)
            # 区间内每个槽位都是全空的，故每槽位提供 capacity 模；换算出所需槽位数
            slots_needed = -(-(chg + cycles) // capacity)
            if self._run_covers(press, mold_tl, start, slots_needed):
                end = start + slots_needed - 1
                if self._insertion_ok(press, end, mold.mold_no, base_chg):
                    return Placement(start, end, chg)
                # 插入被拒的原因只取决于后继区间，与在本区间内的起点无关，
                # 因此整段区间都不必再试，直接跳过。
            # 放不下或不可插入，跳到该空闲区间之后继续找
            start = self._free_run_end(press, mold_tl, start) + 1
        return None

    # ---------- 提交 ----------

    def commit(
        self,
        press: PressTimeline,
        mold_tl: MoldTimeline,
        placement: Placement,
        cycles: int,
    ) -> list[int]:
        """提交占用，返回「每消耗 1 模对应的槽位」（长度 = 换模 + 模数）。"""
        capacity = press.press.capacity_per_shift
        rem = placement.changeover + cycles
        cur = placement.start
        slot_per_unit: list[int] = []
        while rem > 0:
            free = capacity - press.used[cur]
            take = min(free, rem)
            press.used[cur] += take
            slot_per_unit.extend([cur] * take)
            rem -= take
            if rem > 0:
                cur += 1
        for s in range(placement.start, placement.end + 1):
            mold_tl.busy[s] = True
        press.intervals.append(
            Interval(placement.start, placement.end, mold_tl.mold_no, placement.changeover)
        )
        press.intervals.sort(key=lambda iv: iv.start)
        press.cursor = max(press.cursor, placement.end + 1)
        # first_free 是「首个可能作为新任务起点的槽位」。新任务要求起点槽位全空
        # （used == 0），因此半满的槽位同样不能作为起点——必须一并跳过，否则它会把
        # first_free 卡住，使每次评估都线性扫过后面所有已占用槽位。
        # used 只增不减，所以这个指针单调前进，整体是 O(1) 摊还。
        while press.first_free < self._n and press.used[press.first_free] != 0:
            press.first_free += 1
        return slot_per_unit

    # ---------- 批次选择 ----------

    def place_batch(
        self,
        batch: Batch,
        presses: list[PressTimeline],
        molds: list[Mold],
        mold_tls: dict[str, MoldTimeline],
    ) -> PlacementDecision | None:
        """选机台与模具（§12.3）。

        第一键是「开始槽位最早」，让组内空闲机台立刻取用当前可用的最高优先级模具，
        从而按 §5.6 的有效并行度 min(组内机台数, 套内模具副数) 并行开工；
        同一开始时间下先按模具 priority 升序，再按机台号升序。

        若最终没用上覆盖该批次牌型的最高优先级模具，即为「改用次优先模具」（R003）。
        """
        by_priority = sorted(molds, key=lambda m: (m.priority, m.mold_no))
        priority_index = {m.mold_no: i for i, m in enumerate(by_priority)}
        press_order = sorted(presses, key=lambda p: p.press.press_no)

        candidates: list[tuple[tuple, PressTimeline, Mold, Placement, int]] = []
        per_press_best: dict[str, tuple[int, str]] = {}
        for press in press_order:
            for mold in by_priority:
                cycles = batch.cycles_by_mold.get(mold.mold_no)
                if cycles is None:
                    continue
                pl = self.evaluate(press, mold, mold_tls[mold.mold_no], cycles)
                if pl is None:
                    continue
                key = (pl.start, priority_index[mold.mold_no], press.press.press_no)
                candidates.append((key, press, mold, pl, cycles))
                no = press.press.press_no
                if no not in per_press_best or pl.start < per_press_best[no][0]:
                    per_press_best[no] = (pl.start, mold.mold_no)

        if not candidates:
            return None
        best = min(candidates, key=lambda c: c[0])
        # R003 的基准是「覆盖该批次的最高优先级模具」，不是套内优先级最高的模具：
        # 后者可能根本不覆盖本批次的牌型，拿它比较会误报 R003。
        covering = [m for m in by_priority if m.mold_no in batch.cycles_by_mold]
        return PlacementDecision(
            press=best[1],
            mold=best[2],
            placement=best[3],
            cycles=best[4],
            switched=bool(covering) and best[2].mold_no != covering[0].mold_no,
            press_options=[(no, per_press_best[no][0]) for no in sorted(per_press_best)],
        )
