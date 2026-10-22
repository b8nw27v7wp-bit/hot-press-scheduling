"""§21.1 单元测试 U01–U10 的可执行实现。

设计稿 §21.1 的表此前只有「用例 / 断言」两列，没有可执行证据——U01/U03/U04/U07/U09
五条在 `tests/` 里查无实据，只能靠「文档说实现了」担保。本文件按该表原文逐条落地，
所需数据（节假日日历、单副模具的争用、空输入）全部现场构造。

每个用例都打印**输入数据 → 工单清单 → 中间过程 → 批次/排程结果 → 断言**，
便于脱离调试器直接核对算法行为；表格按显示宽度对齐（中文表头与 ASCII 数据列宽不同），
日历摘要只统计地平线以内的日期，与槽位数一致。

其余五条的证据在别处，设计稿 §21.1 已标注：
  U02 多点批次模数取最大 → `tests/golden.py`「批次模数 = 批次内工单模数最大值」
  U05 单工单超班产能     → `tests/golden.py` K2
  U06 批次不跨销售订单   → `tests/scenarios.py` 第 2 号
  U08 组数上限           → `tests/scenarios.py` 第 34 号
  U10 确定性             → `tests/scenarios.py` 第 1 号、`tests/stability.py`
"""

from __future__ import annotations

import sys
import unicodedata
from collections import Counter
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
from engine.capacity import changeover_cycles, cycles_for  # noqa: E402
from engine.scheduler import Engine  # noqa: E402
from verify.constraint_checker import check_all  # noqa: E402

D = Decimal
PLAN_START = date(2026, 1, 5)  # 周一
PRODUCT_TYPES = {"P01": ProductType("P01", "三角牌", ("A01",))}
RULE = "=" * 78


# ---------------- 数据构造 ----------------

def mold(no, set_no, ratios, prio=1, kind=MoldType.SINGLE):
    return Mold(
        mold_no=no,
        set_no=set_no,
        mold_type=kind,
        priority=prio,
        ratios={p: D(str(r)) for p, r in ratios.items()},
    )


def weekday_calendar(start: date, days: int) -> dict[date, CalendarDay]:
    """生产日 2 班、周末 0 班（周末记在表里，避免被当成「日历缺失」报 R011）。"""
    out = {}
    cur = start
    end = start + timedelta(days=days)
    while cur <= end:
        weekend = cur.weekday() >= 5
        out[cur] = CalendarDay(cur, 0 if weekend else 2, is_holiday=weekend)
        cur += timedelta(days=1)
    return out


def engine(orders, mold_sets, presses, calendar=None, plan_start=PLAN_START, **kw) -> Engine:
    params = Params(plan_start=plan_start, **kw)
    return Engine(
        params,
        PRODUCT_TYPES,
        mold_sets,
        presses,
        calendar if calendar is not None else weekday_calendar(plan_start, 120),
        orders,
    )


def check(res, eng: Engine, orders) -> list[str]:
    """对已有结果跑一遍约束校验器，返回错误列表（空表示通过）。"""
    return check_all(
        res.assignments,
        res.batches,
        {w.work_order_no: w for w in orders},
        eng.presses,
        res.calendar,
        eng.mold_sets,
        eng.params,
        reported={e.work_order_no for e in res.exceptions},
        product_types=eng.product_types,
    )


# ---------------- 打印 ----------------

def _width(text: str) -> int:
    """显示宽度：CJK 全角字符占 2 列。表格里中英混排必须按它对齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def col(value, width: int, align: str = "left") -> str:
    pad = " " * max(0, width - _width(str(value)))
    return pad + str(value) if align == "right" else str(value) + pad


def begin(code, title):
    print()
    print(RULE)
    print(f"{code}  {title}")
    print(RULE)


def dump_inputs(eng: Engine, note: str = ""):
    """打印本次用例的机台、模具套、日历与规则参数。"""
    print("输入数据")
    for code in sorted(eng.product_types):
        print(f"  产品类型  {code} {eng.product_types[code].name}")
    for no in sorted(eng.presses):
        p = eng.presses[no]
        print(f"  机台      {no}（{p.product_type_code}，每班 {p.capacity_per_shift} 模，"
              f"{p.status.value}）")
    for set_no in sorted(eng.mold_sets):
        s = eng.mold_sets[set_no]
        molds = "；".join(
            f"{m.mold_no}[{'多点' if len(m.patterns) > 1 else '单点'} 优先级{m.priority} "
            f"{'、'.join(f'{p} 配比 {r}' for p, r in sorted(m.ratios.items()))}"
            f"{'，保养中' if m.status.value != 'AVAILABLE' else ''}]"
            for m in sorted(s.molds, key=lambda m: (m.priority, m.mold_no))
        )
        print(f"  模具套    {set_no}（{s.product_type_code}，set_order {s.set_order}）"
              f" → {molds}")
    print(f"  日历      {_calendar_note(eng.calendar_days, eng.horizon_end)}，"
          f"地平线 {eng.horizon_end}，共 {len(eng.calendar)} 个槽位"
          f"{f'；{note}' if note else ''}")
    print(f"  参数      plan_start {eng.params.plan_start}，group_size {eng.params.group_size}，"
          f"前置偏移 {eng.params.pre_offset_days} 天，"
          f"换模 {eng.params.changeover_hours}h / {eng.params.shift_hours}h 班")


def _calendar_note(days: dict[date, CalendarDay], until: date | None = None) -> str:
    """把日历压成一行「生产日 N 班 × M 天」的分布，避免逐日罗列。

    until 取引擎地平线：日历表可以比地平线长，但引擎只把 plan_start..horizon_end
    展开成槽位（§19.1），不按地平线过滤会让「N 天」与槽位数对不上。
    """
    if until is not None:
        days = {d: rec for d, rec in days.items() if d <= until}
    if not days:
        return "空表（没有任何日历记录）"
    counted = Counter((rec.shifts, rec.is_holiday) for rec in days.values())
    parts = [
        f"{'节假日' if holiday else '生产日'} {shifts} 班 × {n} 天"
        for (shifts, holiday), n in sorted(counted.items(), key=lambda kv: -kv[0][0])
    ]
    return "、".join(parts)


def dump_orders(orders):
    """打印工单清单（排程前）。"""
    print("工单")
    if not orders:
        print("  （无）")
        return
    print("  " + col("工单号", 9) + col("销售订单", 11) + col("牌型", 7)
          + col("排程数量", 10, "right") + "  " + col("交期", 13) + "状态")
    for w in orders:
        print("  " + col(w.work_order_no, 9) + col(w.sales_order_no, 11)
              + col(w.pattern_code, 7) + col(w.plan_qty, 10, "right") + "  "
              + col(w.due_date or "-", 13) + w.status.value)


def dump_process(lines, label="过程", indent=""):
    print(f"{indent}{label}")
    for line in lines:
        print(f"{indent}  {line}")


def res_slot(result, index) -> str:
    """槽位文本一律用 日期/班次 的 ASCII 形式，便于按显示宽度对齐。"""
    s = result.calendar.slot(index)
    return f"{s.day}/{s.shift}"


def dump_schedule(result, title="排程结果", indent=""):
    print(f"{indent}{title}")
    if not result.assignments:
        print(f"{indent}  （无排程结果）")
        return
    print(indent + "  " + col("工单号", 9) + col("机台", 6) + col("模具", 6)
          + col("批次", 9) + col("模数", 6, "right") + "  " + col("开始", 15)
          + col("结束", 15) + col("延期", 6, "right") + "  原因码")
    for a in sorted(result.assignments, key=lambda x: (x.press_no, x.start_slot)):
        print(indent + "  " + col(a.work_order_no, 9) + col(a.press_no, 6)
              + col(a.mold_no, 6) + col(a.batch_no, 9)
              + col(a.cycles, 6, "right") + "  "
              + col(res_slot(result, a.start_slot), 15)
              + col(res_slot(result, a.end_slot), 15)
              + col(a.delay_days, 6, "right") + "  "
              + (",".join(a.reason_codes) or "-"))


def dump_batches(result, indent=""):
    print(f"{indent}批次")
    if not result.batches:
        print(f"{indent}  （无批次）")
        return
    print(indent + "  " + col("批次", 9) + col("机台", 6) + col("模具", 6)
          + col("牌型", 8) + col("模数", 6, "right") + col("换模", 6, "right")
          + "  " + col("槽位区间", 12) + "工单")
    for b in sorted(result.batches, key=lambda x: x.batch_no):
        print(indent + "  " + col(b.batch_no, 9) + col(b.press_no, 6) + col(b.mold_no, 6)
              + col("/".join(sorted(b.patterns)), 8) + col(b.cycles, 6, "right")
              + col(b.changeover_cycles, 6, "right") + "  "
              + col(f"{b.start_slot}-{b.end_slot}", 12)
              + "、".join(b.work_order_nos))


def report(checks, results):
    bad = [detail for passed, detail in checks if not passed]
    print("断言")
    for passed, detail in checks:
        print(f"  [{'ok' if passed else 'NG'}] {detail}")
    print(f"  → {len(checks) - len(bad)}/{len(checks)} 项通过")
    results.append(not bad)


# ---------------- U01 ----------------

def u01_cycles_round_up(results):
    """U01 模数向上取整：101 / 2 → 51。"""
    one_press = {"R1": Press("R1", 20, "P01")}
    one_mold = {"S1": MoldSet("S1", "P01", 1, [mold("M1", "S1", {"X": 2})])}
    orders = [WorkOrder("W1", "SO1", "P01", "X", D("101"), "张", date(2026, 2, 10))]
    eng = engine(orders, one_mold, one_press)
    res = eng.run()

    begin("U01", "模数向上取整")
    dump_inputs(eng)
    dump_orders(orders)
    dump_process([
        "工单模数 K(w) = ceil(qty / ratio)，除不尽时向上取整",
        f"  K(W1) = ceil(101 / 2) = ceil(50.5) = {cycles_for(D('101'), D('2'))}",
        f"  整除不多算一模：ceil(100 / 2) = {cycles_for(D('100'), D('2'))}",
        f"  不足一模也算一模：ceil(1 / 2) = {cycles_for(D('1'), D('2'))}",
        "51 模 ÷ 每班 20 模 = 2.55 → 占 3 个槽位（跨日连续生产，不拆分）",
    ])
    dump_schedule(res)
    report([
        (cycles_for(D("101"), D("2")) == 51, "101 / 2 → 51"),
        (cycles_for(D("100"), D("2")) == 50, "整除不多算一模：100 / 2 → 50"),
        (cycles_for(D("1"), D("2")) == 1, "不足一模也算一模：1 / 2 → 1"),
        (cycles_for(D("2.5"), D("2")) == 2, "非整数数量：2.5 / 2 → 2"),
        (res.assignments[0].cycles == 51,
         f"引擎侧 schedule_result.cycles 同口径（实际 {res.assignments[0].cycles}）"),
    ], results)


# ---------------- U03 ----------------

def u03_skip_holidays(results):
    """U03 跨节假日推进：周五 2 班 → 周一 1 班，跳过周末。"""
    friday = date(2026, 1, 9)   # plan_start，当天 2 班
    monday = date(2026, 1, 12)  # 周末之后的首个生产日，1 班

    cal = {}
    cur = friday
    while cur <= friday + timedelta(days=60):
        if cur.weekday() >= 5:
            cal[cur] = CalendarDay(cur, 0, is_holiday=True)
        else:
            cal[cur] = CalendarDay(cur, 2 if cur == friday else 1)
        cur += timedelta(days=1)

    one_press = {"R1": Press("R1", 20, "P01")}
    one_mold = {"S1": MoldSet("S1", "P01", 1, [mold("M1", "S1", {"X": 2})])}
    orders = [WorkOrder("W1", "SO1", "P01", "X", D("120"), "张", date(2026, 2, 10))]
    eng = engine(orders, one_mold, one_press, calendar=cal, plan_start=friday)
    res = eng.run()

    a = res.assignments[0]
    slots = [res.calendar.slot(i) for i in range(min(6, len(res.calendar)))]

    begin("U03", "跨节假日推进")
    dump_inputs(eng, note="plan_start 为周五，当天 2 班；其后生产日各 1 班")
    dump_orders(orders)
    dump_process([
        "槽位按日期、班次升序编号，非生产日不产生槽位：",
        "  " + "  ".join(f"[{s.index}] {s.day}/{s.shift}" for s in slots)
        + ("  …" if len(res.calendar) > len(slots) else ""),
        "120 张 ÷ 配比 2 = 60 模；每班 20 模 → ceil(60 / 20) = 3 个槽位",
        "占用 [0] 周五第 1 班、[1] 周五第 2 班、[2] 周一第 1 班 —— 中间两个周末槽位不存在",
    ])
    dump_schedule(res)
    report([
        (len(res.calendar) == 3 + sum(
            1 for d, r in cal.items() if r.is_working and d > monday
        ), "槽位只由生产日生成，周末不产生槽位"),
        ([(s.day, s.shift) for s in res.calendar.slots[:3]]
         == [(friday, 1), (friday, 2), (monday, 1)],
         f"槽位序列为 周五2班 → 周一1班（实际 "
         f"{[(str(s.day), s.shift) for s in res.calendar.slots[:3]]}）"),
        (a.start_slot == 0 and a.end_slot == 2, "60 模占 3 个槽位，不拆分"),
        ((a.planned_start_date, a.planned_start_shift) == (friday, 1),
         f"计划开始 = 周五第 1 班（实际 {a.planned_start_date}/{a.planned_start_shift}）"),
        ((a.planned_end_date, a.planned_end_shift) == (monday, 1),
         f"计划结束 = 周一第 1 班，跨过周末（实际 {a.planned_end_date}/{a.planned_end_shift}）"),
        (all(res.calendar.slot(i).day.weekday() < 5
             for i in range(a.start_slot, a.end_slot + 1)),
         "占用区间内没有任何周末槽位"),
    ], results)


# ---------------- U04 ----------------

def u04_changeover_conversion(results):
    """U04 换模折算：2h / 12h × 20 模 → 4 模。"""
    # 换模必须在真实排程里发生：两副单点模具各覆盖一个牌型，同一台机前后两批
    # 必然换模（同一副模具连续生产不换模，故不能靠「两批同模具」来测）。
    two_molds = {"S1": MoldSet("S1", "P01", 1, [
        mold("M1", "S1", {"X": 2}, prio=1),
        mold("M2", "S1", {"Y": 2}, prio=2),
    ])}
    orders = [
        WorkOrder("W1", "SO1", "P01", "X", D("40"), "张", date(2026, 2, 10)),
        WorkOrder("W2", "SO2", "P01", "Y", D("40"), "张", date(2026, 2, 10)),
    ]
    eng = engine(orders, two_molds, {"R1": Press("R1", 20, "P01")})
    res = eng.run()
    by_start = sorted(res.batches, key=lambda b: b.start_slot)
    switched = [b for b in by_start if b.changeover_cycles > 0]

    begin("U04", "换模折算")
    dump_inputs(eng)
    dump_orders(orders)
    dump_process([
        "换模以小时配置，按班产能折算为消耗模数：ceil(changeover_hours / shift_hours × 班产能)",
        f"  changeover_hours=2, shift_hours=12, 班产能=20  → ceil(2 × 20 / 12) = "
        f"ceil(3.33) = {changeover_cycles(Params(plan_start=PLAN_START, changeover_hours=2, shift_hours=12), 20)}",
        f"  changeover_hours=7, shift_hours=12, 班产能=108 → ceil(7 × 108 / 12) = "
        f"ceil(63) = {changeover_cycles(Params(plan_start=PLAN_START, changeover_hours=7, shift_hours=12), 108)}",
        "    （浮点路径 7/12×108 得 63.000000000000007，ceil 会多算成 64；整数运算避开）",
        "本用例两批各用一副不同模具，同一台机前后换模 → 第二批判为换模",
    ])
    dump_batches(res)
    dump_schedule(res)
    report([
        (changeover_cycles(Params(plan_start=PLAN_START, changeover_hours=2, shift_hours=12), 20) == 4,
         "2h / 12h × 20 模 → 4 模"),
        (changeover_cycles(Params(plan_start=PLAN_START, changeover_hours=2, shift_hours=12), 108) == 18,
         "2h / 12h × 108 模 → 18 模"),
        (changeover_cycles(Params(plan_start=PLAN_START, changeover_hours=7, shift_hours=12), 108) == 63,
         "整数上取整：7h / 12h × 108 → 63（浮点路径会多算成 64）"),
        (changeover_cycles(Params(plan_start=PLAN_START, changeover_hours=0), 20) == 0,
         "换模时长 0 → 消耗 0 模"),
        (len(switched) == 1 and switched[0].changeover_cycles == 4,
         f"引擎侧真实换模消耗 4 模（各批次换模 "
         f"{[(b.batch_no, b.changeover_cycles) for b in by_start]}）"),
        (not check(res, eng, orders), f"换模记账通过 C08（{check(res, eng, orders)}）"),
    ], results)


# ---------------- U07 ----------------

def u07_mold_contention(results):
    """U07 模具争用：同一副模具的任务串行；套内多副时才能并行。"""
    presses = {"R1": Press("R1", 20, "P01"), "R2": Press("R2", 20, "P01")}
    orders = [
        WorkOrder("W1", "SO1", "P01", "X", D("40"), "张", date(2026, 2, 10)),
        WorkOrder("W2", "SO2", "P01", "X", D("40"), "张", date(2026, 2, 10)),
    ]

    # (a) 套内只有 1 副：两台机都在组内，但同一副模具同一时刻只能装一台机
    single = {"S1": MoldSet("S1", "P01", 1, [mold("M1", "S1", {"X": 2})])}
    eng_a = engine(orders, single, presses)
    res_a = eng_a.run()
    a_batches = sorted(res_a.batches, key=lambda b: b.start_slot)

    # (b) 套内有 2 副：并行度 min(组内机台数, 套内副数) = 2，两批同时开工
    double = {"S1": MoldSet("S1", "P01", 1, [
        mold("M1", "S1", {"X": 2}, prio=1),
        mold("M2", "S1", {"X": 2}, prio=2),
    ])}
    eng_b = engine(orders, double, presses)
    res_b = eng_b.run()
    b_batches = sorted(res_b.batches, key=lambda b: b.start_slot)

    errs_a = check(res_a, eng_a, orders)
    errs_b = check(res_b, eng_b, orders)

    begin("U07", "模具争用")
    dump_inputs(eng_a, note="本用例两组对照：(a) 套内 1 副，(b) 套内 2 副")
    dump_orders(orders)
    dump_process([
        "两组数据只差「套内副数」，其余完全相同；两个销售订单 → 必然两个批次",
        "组内有效并行度 p_g = min(组内机台数, 套内可用副数)（§5.6）",
    ])

    print()
    print("(a) 套内 1 副 —— 并行度 = min(2, 1) = 1")
    dump_batches(res_a, indent="  ")
    dump_schedule(res_a, indent="  ")
    dump_process([
        "两台机都在组内，但 M1 同一时刻只能装一台：第二批只能等第一批让出模具。",
        f"批次 {a_batches[0].batch_no} 占槽位 {a_batches[0].start_slot}-{a_batches[0].end_slot}，"
        f"{a_batches[1].batch_no} 从槽位 {a_batches[1].start_slot} 起 → 串行，"
        f"R2 全程空闲也没用。",
    ], label="解读", indent="  ")

    print()
    print("(b) 套内 2 副 —— 并行度 = min(2, 2) = 2")
    dump_batches(res_b, indent="  ")
    dump_schedule(res_b, indent="  ")
    dump_process([
        "两批各取一副模具，在同一槽位同时开工："
        + "、".join(f"{b.batch_no}→{b.press_no}/{b.mold_no}@{b.start_slot}"
                    for b in b_batches),
        "W2 用的是套内次优先模具 M2（M1 被 W1 占用），故带 R003。",
    ], label="解读", indent="  ")

    report([
        (len(a_batches) == 2, f"两个销售订单 → 两个批次（实际 {len(a_batches)}）"),
        (len({b.mold_no for b in a_batches}) == 1, "两批只能用同一副模具 M1"),
        (a_batches[1].start_slot > a_batches[0].end_slot,
         f"单副模具下两批串行（{a_batches[0].start_slot}-{a_batches[0].end_slot} → "
         f"{a_batches[1].start_slot}-{a_batches[1].end_slot}）"),
        (not errs_a, f"单副模具下不违反 C02（{errs_a}）"),
        (len(b_batches) == 2 and len({b.mold_no for b in b_batches}) == 2,
         f"套内 2 副时各用一副（{sorted(b.mold_no for b in b_batches)}）"),
        (b_batches[0].start_slot == b_batches[1].start_slot == 0
         and b_batches[0].press_no != b_batches[1].press_no,
         f"套内 2 副时两批并行开工（{[(b.press_no, b.start_slot) for b in b_batches]}）"),
        (not errs_b, f"套内 2 副时约束仍全部通过（{errs_b}）"),
    ], results)


# ---------------- U09 ----------------

def u09_empty_input(results):
    """U09 空输入：空输出，无异常。"""
    molds = {"S1": MoldSet("S1", "P01", 1, [mold("M1", "S1", {"X": 2})])}
    presses = {"R1": Press("R1", 20, "P01")}

    eng = engine([], molds, presses)
    empty = eng.run()
    # 连日历都不给：没有有效工单就没有可排计划，不该报「日历缺失」
    no_cal = engine([], molds, presses, calendar={}).run()
    # 全部工单已完成/已取消：同样不进入排程
    terminal_orders = [
        WorkOrder("W1", "SO1", "P01", "X", D("60"), "张", date(2026, 2, 10),
                  status=WorkOrderStatus.COMPLETED),
        WorkOrder("W2", "SO2", "P01", "X", D("60"), "张", date(2026, 2, 10),
                  status=WorkOrderStatus.CANCELLED),
    ]
    terminal = engine(terminal_orders, molds, presses).run()

    errs = check(empty, eng, [])

    begin("U09", "空输入")
    print("情形 1：完全没有工单")
    dump_inputs(eng)
    dump_orders([])
    dump_schedule(empty)
    dump_batches(empty)

    print()
    print("情形 2：连日历也不给（空表）")
    dump_process(["没有有效工单 → 没有可排计划 → 也没有「日历缺失」可言"])

    print()
    print("情形 3：工单全部已完成 / 已取消")
    dump_orders(terminal_orders)
    dump_process([
        "P0 预处理直接排除 COMPLETED / CANCELLED，不进入排程，也不算异常",
        "（需求说明书第八章要求的是「报告异常数据」，而这两个状态是正常业务状态）",
    ])
    dump_schedule(terminal)

    dump_process([
        f"空工单       → 有效工单集为空 → 排程结果 {len(empty.assignments)} 条、"
        f"批次 {len(empty.batches)} 个、异常 {len(empty.exceptions)} 条",
        f"空工单 + 空日历 → 异常 {len(no_cal.exceptions)} 条",
        f"全终态工单   → 排程结果 {len(terminal.assignments)} 条、"
        f"异常 {len(terminal.exceptions)} 条",
        "三种情形都必须给出「空输出 + 无异常」，而不是报一堆缺数据。",
    ], label="小结")

    report([
        (empty.assignments == [] and empty.batches == [], "空工单 → 排程结果与批次表均为空"),
        (empty.order_delivery == [] and empty.release_plan == [] and empty.reasons == [],
         "订单齐套 / 释放计划 / 理由均为空"),
        (empty.exceptions == [],
         f"空输入不报任何异常（实际 {[e.code for e in empty.exceptions]}）"),
        (empty.meta["truncated"] is False, "空输入不触发截断"),
        (no_cal.exceptions == [],
         f"无工单且无日历时也不报 R011（实际 {[e.code for e in no_cal.exceptions]}）"),
        (terminal.assignments == [] and terminal.batches == []
         and terminal.exceptions == [],
         "已完成/已取消工单被排除，且不报异常"),
        (not errs, f"空结果通过 C01–C15（{errs}）"),
    ], results)


def main() -> int:
    results: list[bool] = []
    print("§21.1 单元测试 U01/U03/U04/U07/U09 —— 输入数据、过程与结果")
    u01_cycles_round_up(results)
    u03_skip_holidays(results)
    u04_changeover_conversion(results)
    u07_mold_contention(results)
    u09_empty_input(results)
    print()
    print(RULE)
    print(f"通过 {sum(results)}/{len(results)}")
    print(RULE)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
