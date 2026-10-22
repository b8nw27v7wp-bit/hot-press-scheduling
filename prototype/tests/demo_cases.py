"""演示脚本：构造若干有代表性的任务数据集，跑引擎并打印排程结果。

每个用例对应设计文档里的一类典型场景，便于直观核对算法在不同输入下的
机台/模具/批次/延期/归因表现。仅打印，不改任何引擎代码。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import (  # noqa: E402
    CalendarDay,
    Mold,
    MoldSet,
    MoldType,
    Params,
    Press,
    ProductType,
    WorkOrder,
    WorkOrderStatus,
)
from engine.scheduler import Engine  # noqa: E402


def _calendar(start: date, days: int, shifts: int = 2, holidays: set[date] | None = None):
    holidays = holidays or set()
    out = {}
    cur = start
    for _ in range(days):
        out[cur] = CalendarDay(cur, shifts=0 if cur in holidays else shifts,
                              is_holiday=cur in holidays)
        cur += timedelta(days=1)
    return out


def _mold(no, set_no, prio, ratios, kind=MoldType.MULTI):
    return Mold(
        mold_no=no, set_no=set_no, mold_type=kind, priority=prio,
        ratios={p: Decimal(str(r)) for p, r in ratios.items()},
    )


def _wo(seq, sales, ptype, pattern, qty, due, status=WorkOrderStatus.NOT_STARTED,
         remaining=None):
    return WorkOrder(
        work_order_no=f"WO{seq:04d}", sales_order_no=sales, product_type_code=ptype,
        pattern_code=pattern, qty=Decimal(str(qty)), uom="张", due_date=due,
        status=status,
        remaining_qty=None if remaining is None else Decimal(str(remaining)),
    )


def _print_table(result):
    if not result.assignments:
        print("  （无排程结果）")
        return
    header = (f"  {'工单':<8}{'牌型':<5}{'机台':<5}{'模具':<5}{'批次':<8}"
              f"{'模数':>4}  {'开始':<14}{'结束':<14}{'延期':>4}  {'原因码'}")
    print(header)
    rows = sorted(result.assignments, key=lambda a: (a.press_no, a.start_slot))
    for a in rows:
        s = result.calendar.slot(a.start_slot)
        e = result.calendar.slot(a.end_slot)
        print(
            f"  {a.work_order_no:<8}{a.pattern_code:<5}{a.press_no:<5}{a.mold_no:<5}"
            f"{a.batch_no:<8}{a.cycles:>4}  "
            f"{s.day}/{s.shift:<12}{e.day}/{e.shift:<12}"
            f"{a.delay_days:>4}  {','.join(a.reason_codes) or '-'}"
        )


def _print_extras(result, show_reasons=1):
    print()
    print("  订单齐套与延期 (§4.3):")
    for o in result.order_delivery:
        flag = f"延期 {o.delay_days} 天" if o.delayed else "按期"
        print(f"    {o.sales_order_no:<8} 应交 {o.due_date}  "
              f"预计完成 {o.expected_finish_date}  {flag}  {o.reason_code or ''}")
    if result.exceptions:
        print("  异常数据 (§4.6):")
        for e in result.exceptions:
            print(f"    {e.work_order_no:<8} {e.code}  {e.detail}")
    if show_reasons and result.reasons:
        r = result.reasons[0]
        print(f"  排程理由样例 (§4.5, {r.work_order_no}):")
        print(f"    机台: {r.machine_reason}")
        print(f"    时间: {r.time_reason}")
        print(f"    批次: {r.batch_reason}")
        print(f"    模具: {r.mold_reason}")


def _summary(result, presses):
    from verify.constraint_checker import summarize
    st = summarize(result.assignments, result.batches, presses, result.calendar)
    delayed = sum(1 for a in result.assignments if a.delayed)
    print()
    print(f"  汇总: 工单 {len(result.assignments)} 张，批次 {len(result.batches)} 个，"
          f"延期 {delayed} 张，总延期 {sum(a.delay_days for a in result.assignments)} 天，"
          f"异常 {len(result.exceptions)} 条，换模 {st['changeover_count']} 次，"
          f"利用率 {st['press_utilization']:.1%}")


# ---------------------------------------------------------------------------
# 用例 1：正常批次合并 + 多点模具
# 同一销售订单内的 X/Y 工单可由一副多点模具一次成型，应合并为一个批次。
# ---------------------------------------------------------------------------
def case_batch_merge():
    start = date(2026, 1, 5)
    params = Params(plan_start=start, group_size=2, pre_offset_days=15)
    ptypes = {"P01": ProductType("P01", "三角牌", ("A01",))}
    sets = {
        "S1": MoldSet("S1", "P01", 1, [
            _mold("M1", "S1", 1, {"X": 2, "Y": 2}),
            _mold("M2", "S1", 2, {"X": 4}),
        ]),
    }
    presses = {"R1": Press("R1", 20, "P01"), "R2": Press("R2", 20, "P01")}
    due = date(2026, 2, 10)
    orders = [
        _wo(1, "SO-01", "P01", "X", 120, due),
        _wo(2, "SO-01", "P01", "Y", 90, due),
        _wo(3, "SO-01", "P01", "X", 200, due),
        _wo(4, "SO-02", "P01", "Y", 60, date(2026, 2, 12)),
        _wo(5, "SO-02", "P01", "X", 80, date(2026, 2, 12)),
    ]
    result = Engine(params, ptypes, sets, presses, _calendar(start, 90), orders).run()
    _print_table(result)
    _print_extras(result)
    _summary(result, presses)


# ---------------------------------------------------------------------------
# 用例 2：产能不足 → 延期 + 瓶颈归因
# 交期极紧、机台与模具都少，逼出 R005/R006/R007 类归因。
# ---------------------------------------------------------------------------
def case_capacity_shortage():
    start = date(2026, 1, 5)
    params = Params(plan_start=start, group_size=2, pre_offset_days=5)
    ptypes = {"P01": ProductType("P01", "三角牌", ("A01",))}
    sets = {
        "S1": MoldSet("S1", "P01", 1, [
            _mold("M1", "S1", 1, {"X": 2, "Y": 2}),
        ]),
    }
    # 只有一台机，产能被压到极限
    presses = {"R1": Press("R1", 20, "P01")}
    orders = [
        _wo(1, "SO-01", "P01", "X", 800, date(2026, 1, 12)),
        _wo(2, "SO-01", "P01", "Y", 600, date(2026, 1, 12)),
        _wo(3, "SO-02", "P01", "X", 500, date(2026, 1, 15)),
    ]
    result = Engine(params, ptypes, sets, presses, _calendar(start, 60), orders).run()
    _print_table(result)
    _print_extras(result)
    _summary(result, presses)


# ---------------------------------------------------------------------------
# 用例 3：真实换模
# 一副模具只覆盖单牌型 + group_size=1，同一台机上 X/Y 交替必然换模，
# 用于观察换模折算（C08 口径）。
# ---------------------------------------------------------------------------
def case_changeover():
    start = date(2026, 1, 5)
    params = Params(plan_start=start, group_size=1, pre_offset_days=15,
                    changeover_hours=2)
    ptypes = {"P01": ProductType("P01", "三角牌", ("A01",))}
    sets = {
        "S1": MoldSet("S1", "P01", 1, [
            _mold("M1", "S1", 1, {"X": 2}, MoldType.SINGLE),
            _mold("M2", "S1", 2, {"Y": 2}, MoldType.SINGLE),
        ]),
    }
    presses = {"R1": Press("R1", 20, "P01"), "R2": Press("R2", 20, "P01")}
    due = date(2026, 2, 10)
    orders = [
        _wo(i, f"SO-{i:02d}", "P01", "X" if i % 2 else "Y", 60, due)
        for i in range(1, 11)
    ]
    result = Engine(params, ptypes, sets, presses, _calendar(start, 90), orders).run()
    _print_table(result)
    _summary(result, presses)


# ---------------------------------------------------------------------------
# 用例 4：脏数据 + 状态过滤
# 已完成/已取消不排；缺交期、数量非正、重复工单号、产品类型未定义报异常。
# ---------------------------------------------------------------------------
def case_dirty_data():
    start = date(2026, 1, 5)
    params = Params(plan_start=start, group_size=2, pre_offset_days=15)
    ptypes = {"P01": ProductType("P01", "三角牌", ("A01",))}
    sets = {
        "S1": MoldSet("S1", "P01", 1, [_mold("M1", "S1", 1, {"X": 2, "Y": 2})]),
    }
    presses = {"R1": Press("R1", 20, "P01"), "R2": Press("R2", 20, "P01")}
    due = date(2026, 2, 10)
    orders = [
        _wo(1, "SO-01", "P01", "X", 120, due),
        _wo(2, "SO-01", "P01", "Y", 90, due),
        # 已完成 / 已取消：应被排除，不进结果
        _wo(3, "SO-01", "P01", "X", 100, due, WorkOrderStatus.COMPLETED),
        _wo(4, "SO-01", "P01", "X", 100, due, WorkOrderStatus.CANCELLED),
        # 缺交期
        WorkOrder("WO0005", "SO-02", "P01", "X", Decimal("80"), "张", None),
        # 数量非正
        _wo(6, "SO-02", "P01", "Y", 0, due),
        # 在制：按剩余数量排
        _wo(7, "SO-02", "P01", "X", 200, due, WorkOrderStatus.IN_PROGRESS, 120),
        # 重复工单号
        _wo(1, "SO-03", "P01", "Y", 50, due),
        # 产品类型未定义
        _wo(8, "SO-03", "P99", "X", 70, due),
    ]
    result = Engine(params, ptypes, sets, presses, _calendar(start, 90), orders).run()
    _print_table(result)
    _print_extras(result, show_reasons=0)
    _summary(result, presses)


# ---------------------------------------------------------------------------
# 用例 5：时间上限截断（§20.2）
# 大量工单 + 极短时间上限 → 输出部分结果、置 truncated，未排工单逐条报 R007。
# ---------------------------------------------------------------------------
def case_time_limit():
    """多个产品类型 + 适中的时间上限：前几个类型排得完，后面的被截断。

    时间上限是按「产品类型」为粒度检查的（scheduler 里每次进入一个类型前判一次），
    所以要演示「排进一部分、截断一部分」必须给多个类型；只给一个类型时结果只会是
    「全排」或「一张不排」两种极端。
    """
    start = date(2026, 1, 5)
    params = Params(plan_start=start, group_size=2, pre_offset_days=15,
                    time_limit_seconds=0.03)
    n_types = 6
    ptypes = {f"P{i:02d}": ProductType(f"P{i:02d}", f"牌型{i}", (f"A{i:02d}",))
              for i in range(1, n_types + 1)}
    sets = {
        f"S{i:02d}": MoldSet(f"S{i:02d}", f"P{i:02d}", 1,
                            [_mold(f"M{i:02d}A", f"S{i:02d}", 1, {"X": 2, "Y": 2})])
        for i in range(1, n_types + 1)
    }
    presses = {f"R{i:02d}": Press(f"R{i:02d}", 20, f"P{i:02d}")
               for i in range(1, n_types + 1)}
    due = date(2026, 3, 1)
    # 每张工单一张销售订单，逼引擎做大量分组尝试，让每个类型的耗时都可观
    orders = []
    seq = 1
    for i in range(1, n_types + 1):
        for j in range(1, 201):
            orders.append(_wo(seq, f"SO-{seq:04d}", f"P{i:02d}",
                              "X" if j % 2 else "Y", 20, due))
            seq += 1
    result = Engine(params, ptypes, sets, presses, _calendar(start, 120), orders).run()
    scheduled_types = sorted({a.product_type_code for a in result.assignments})
    unscheduled = {e.work_order_no for e in result.exceptions if e.code == "R007"}
    print(f"  truncated = {result.meta.get('truncated')}  "
          f"耗时 {result.meta.get('elapsed_seconds')}s "
          f"(上限 {params.time_limit_seconds}s)")
    print(f"  已排 {len(result.assignments)} 张（覆盖类型 {scheduled_types}），"
          f"因截断未排 {len(unscheduled)} 张（逐条报 R007）")
    _summary(result, presses)


def main():
    cases = [
        ("用例 1：正常批次合并 + 多点模具", case_batch_merge),
        ("用例 2：产能不足 → 延期 + 瓶颈归因", case_capacity_shortage),
        ("用例 3：真实换模", case_changeover),
        ("用例 4：脏数据 + 状态过滤", case_dirty_data),
        ("用例 5：时间上限截断（§20.2）", case_time_limit),
    ]
    for title, fn in cases:
        print("=" * 78)
        print(title)
        print("=" * 78)
        fn()
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
