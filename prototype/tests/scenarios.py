"""场景测试：确定性、延期、边界情况、批次不跨订单。"""

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
    TypeDiagnostics,
    WorkOrder,
    WorkOrderStatus,
)
from engine.delay import attribute  # noqa: E402
from engine.scheduler import Engine  # noqa: E402
from verify.constraint_checker import check_all  # noqa: E402

PLAN_START = date(2026, 1, 5)
D = Decimal


def make_calendar(days: int = 120, skip_weekend: bool = True) -> dict[date, CalendarDay]:
    out = {}
    cur = PLAN_START
    end = PLAN_START + timedelta(days=days)
    while cur <= end:
        if not skip_weekend or cur.weekday() < 5:
            out[cur] = CalendarDay(cur, shifts=2)
        cur += timedelta(days=1)
    return out


def mold(no, set_no, prio, ratios, kind=MoldType.MULTI):
    return Mold(
        mold_no=no,
        set_no=set_no,
        mold_type=kind,
        priority=prio,
        ratios={p: D(str(r)) for p, r in ratios.items()},
    )


def build(orders, mold_sets, presses, calendar=None, **kw):
    params = Params(plan_start=PLAN_START, **kw)
    ptypes = {"P01": ProductType("P01", "三角牌", ("A01",))}
    return Engine(params, ptypes, mold_sets, presses, calendar or make_calendar(), orders)


def run_case(name: str, orders, mold_sets, presses, calendar=None, expect=(), **kw):
    engine = build(orders, mold_sets, presses, calendar, **kw)
    result = engine.run()
    errors = check_all(
        result.assignments,
        result.batches,
        {w.work_order_no: w for w in orders},
        presses,
        result.calendar,
        mold_sets,
        engine.params,
        reported={e.work_order_no for e in result.exceptions},
    )
    codes = {e.code for e in result.exceptions}
    ok = True
    notes = []
    for e in expect:
        if e not in codes:
            ok = False
            notes.append(f"缺少预期异常 {e}")
    if errors:
        ok = False
        notes.append(f"约束失败 {errors}")
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    for n in notes:
        print(f"       {n}")
    return ok, result


def main() -> int:
    results = []

    # 1. 确定性：同输入两次运行结果一致
    s1 = MoldSet("S1", "P01", 1, [mold("M1", "S1", 1, {"X": 2, "Y": 2})])
    presses = {"R1": Press("R1", 20, "P01"), "R2": Press("R2", 20, "P01")}
    orders = [
        WorkOrder(f"W{i:03d}", f"SO{i % 3}", "P01", "X" if i % 2 else "Y",
                  D(60), "张", date(2026, 2, 10))
        for i in range(1, 21)
    ]
    e1 = build(orders, {"S1": s1}, presses).run()
    e2 = build(orders, {"S1": s1}, presses).run()
    same = [(a.work_order_no, a.press_no, a.mold_no, a.start_slot, a.end_slot)
            for a in e1.assignments] == [
            (a.work_order_no, a.press_no, a.mold_no, a.start_slot, a.end_slot)
            for a in e2.assignments]
    print(f"[{'PASS' if same else 'FAIL'}] 确定性：相同输入两次运行结果一致")
    results.append(same)

    # 2. 批次不跨销售订单
    s2 = MoldSet("S2", "P01", 1, [mold("M9", "S2", 1, {"X": 2, "Y": 2})])
    cross_orders = [
        WorkOrder("WA", "SO-A", "P01", "X", D(40), "张", date(2026, 2, 10)),
        WorkOrder("WB", "SO-B", "P01", "Y", D(40), "张", date(2026, 2, 10)),
    ]
    _, r2 = run_case("批次不跨销售订单", cross_orders, {"S2": s2}, presses)
    cross_ok = all(
        len({next(w for w in cross_orders if w.work_order_no == n).sales_order_no
             for n in b.work_order_nos}) == 1
        for b in r2.batches
    )
    print(f"[{'PASS' if cross_ok else 'FAIL'}] 批次内工单同属一个销售订单")
    results.append(cross_ok)

    # 3. 多点模具合并：X/Y 同批
    merged = [
        WorkOrder("WX", "SO-M", "P01", "X", D(40), "张", date(2026, 2, 10)),
        WorkOrder("WY", "SO-M", "P01", "Y", D(40), "张", date(2026, 2, 10)),
    ]
    _, r3 = run_case("多点模具合并", merged, {"S2": s2}, presses)
    one_batch = len(r3.batches) == 1 and len(r3.batches[0].work_order_nos) == 2
    print(f"[{'PASS' if one_batch else 'FAIL'}] 同订单 X/Y 合并为一个批次")
    results.append(one_batch)

    # 4. 无模具 → R008
    ok4, _ = run_case("无可用模具", [
        WorkOrder("W1", "SO1", "P01", "X", D(10), "张", date(2026, 2, 10))
    ], {}, presses, expect=("R008",))
    results.append(ok4)

    # 5. 无适用机台 → R009
    ok5, _ = run_case("无适用机台", [
        WorkOrder("W1", "SO1", "P01", "X", D(10), "张", date(2026, 2, 10))
    ], {"S1": s1}, {}, expect=("R009",))
    results.append(ok5)

    # 6. 空输入 → 空结果
    _, r6 = run_case("空输入", [], {"S1": s1}, presses)
    empty_ok = not r6.assignments and not r6.exceptions
    print(f"[{'PASS' if empty_ok else 'FAIL'}] 空输入返回空结果")
    results.append(empty_ok)

    # 7. 已完成/已取消不参与
    _, r7 = run_case("已完成/已取消排除", [
        WorkOrder("W1", "SO1", "P01", "X", D(10), "张", date(2026, 2, 10),
                  WorkOrderStatus.COMPLETED),
        WorkOrder("W2", "SO1", "P01", "X", D(10), "张", date(2026, 2, 10),
                  WorkOrderStatus.CANCELLED),
        WorkOrder("W3", "SO1", "P01", "X", D(10), "张", date(2026, 2, 10)),
    ], {"S1": s1}, presses)
    only_w3 = [a.work_order_no for a in r7.assignments] == ["W3"]
    print(f"[{'PASS' if only_w3 else 'FAIL'}] 已完成与已取消工单不排程")
    results.append(only_w3)

    # 8. 在制工单按剩余数量
    _, r8 = run_case("在制工单剩余数量", [
        WorkOrder("W1", "SO1", "P01", "X", D(100), "张", date(2026, 2, 10),
                  WorkOrderStatus.IN_PROGRESS, remaining_qty=D(40)),
    ], {"S1": s1}, presses)
    # 40 张 / 配比 2 = 20 模
    inprog_ok = len(r8.assignments) == 1 and r8.assignments[0].cycles == 20
    print(f"[{'PASS' if inprog_ok else 'FAIL'}] 在制工单按剩余数量计算模数")
    results.append(inprog_ok)

    # 9. 延期：产能严重不足
    tight = {"R1": Press("R1", 5, "P01")}
    _, r9 = run_case("产能不足产生延期", [
        WorkOrder(f"W{i:02d}", f"SO{i}", "P01", "X", D(500), "张",
                  date(2026, 1, 20)) for i in range(1, 6)
    ], {"S1": s1}, tight)
    has_delay = any(a.delayed for a in r9.assignments)
    print(f"[{'PASS' if has_delay else 'FAIL'}] 产能不足时标记延期")
    results.append(has_delay)

    # 10. 日历缺失 → R011 语义（不崩溃，正常排程）
    cal = make_calendar()
    for d in [date(2026, 1, 12), date(2026, 1, 13)]:
        cal.pop(d, None)
    _, r10 = run_case("日历缺失不崩溃", [
        WorkOrder("W1", "SO1", "P01", "X", D(60), "张", date(2026, 2, 10))
    ], {"S1": s1}, presses, calendar=cal)
    missing_reported = bool(r10.calendar.missing_days)
    print(f"[{'PASS' if missing_reported else 'FAIL'}] 日历缺失日期被记录")
    results.append(missing_reported)

    # 11. 单工单超班产能 → 跨日连续，不拆分
    _, r11 = run_case("单工单超班产能", [
        WorkOrder("W1", "SO1", "P01", "X", D(1000), "张", date(2026, 2, 10))
    ], {"S1": s1}, {"R1": Press("R1", 10, "P01")})
    span = r11.assignments[0].end_slot - r11.assignments[0].start_slot
    no_split = len(r11.assignments) == 1 and span > 0
    print(f"[{'PASS' if no_split else 'FAIL'}] 超班产能工单不拆分，跨槽位连续执行")
    results.append(no_split)

    # 12. 订单齐套：同订单最晚完成日与延期天数
    _, r12 = run_case("订单齐套", [
        WorkOrder("WA", "SO-K", "P01", "X", D(40), "张", date(2026, 2, 10)),
        WorkOrder("WB", "SO-K", "P01", "Y", D(40), "张", date(2026, 2, 10)),
    ], {"S1": s1}, presses)
    od = r12.order_delivery
    finish = max(r12.calendar.day_of(a.end_slot) for a in r12.assignments)
    order_ok = (
        len(od) == 1
        and od[0].sales_order_no == "SO-K"
        and od[0].expected_finish_date == finish
        and od[0].delayed == any(a.delayed for a in r12.assignments)
    )
    print(f"[{'PASS' if order_ok else 'FAIL'}] 订单齐套记录最晚完成日与延期")
    results.append(order_ok)

    # 13. 改用次优先模具 → R003（组内第二台机取用套内第二副模具）
    s_two = MoldSet("S1", "P01", 1, [
        mold("M1", "S1", 1, {"X": 2}),
        mold("M2", "S1", 2, {"X": 4}),
    ])
    _, r13 = run_case("次优先模具", [
        WorkOrder("WX1", "SO-A", "P01", "X", D(400), "张", date(2026, 2, 10)),
        WorkOrder("WX2", "SO-B", "P01", "X", D(400), "张", date(2026, 2, 10)),
    ], {"S1": s_two}, presses)
    r003 = any("R003" in a.reason_codes for a in r13.assignments)
    print(f"[{'PASS' if r003 else 'FAIL'}] 未用最高优先级模具时记 R003")
    results.append(r003)

    # 14. 单工单超单班产能 → R004
    _, r14 = run_case("超班产能记 R004", [
        WorkOrder("W1", "SO1", "P01", "X", D(1000), "张", date(2026, 2, 10))
    ], {"S1": s1}, {"R1": Press("R1", 10, "P01"), "R2": Press("R2", 10, "P01")})
    r004 = any("R004" in a.reason_codes for a in r14.assignments)
    print(f"[{'PASS' if r004 else 'FAIL'}] 单工单超单班产能记 R004")
    results.append(r004)

    # 15. 严重度：延期工单的主原因码应为瓶颈类（R005/R006/R007），而非信息类
    tight2 = {"R1": Press("R1", 5, "P01"), "R2": Press("R2", 5, "P01")}
    _, r15 = run_case("主原因码取最严重者", [
        WorkOrder(f"W{i:02d}", f"SO{i}", "P01", "X", D(500), "张",
                  date(2026, 1, 20)) for i in range(1, 6)
    ], {"S1": s1}, tight2)
    delayed = [a for a in r15.assignments if a.delayed]
    severity_ok = bool(delayed) and all(
        a.reason_code in ("R005", "R006", "R007") for a in delayed
    )
    print(f"[{'PASS' if severity_ok else 'FAIL'}] 延期工单主原因码取瓶颈类")
    results.append(severity_ok)

    # 16-19. §13.3 归因分支（直接对归因函数做单元断言）
    wo = WorkOrder("W1", "SO1", "P01", "X", D(10), "张", date(2026, 2, 10))
    cases = [
        ("模具套数不足 → R005", TypeDiagnostics("P01", mold_set_count=2, press_count=8,
                                                max_groups=2, groups_needed=3, groups_used=2), "R005"),
        ("设备数量不足 → R006", TypeDiagnostics("P01", mold_set_count=4, press_count=2,
                                                max_groups=1, groups_needed=3, groups_used=1), "R006"),
        ("日历产能不足 → R007", TypeDiagnostics("P01", mold_set_count=4, press_count=8,
                                                max_groups=4, groups_needed=1, groups_used=4,
                                                working_days=10, total_cycles=10000,
                                                daily_capacity=100), "R007"),
        ("其余归模具产能 → R005", TypeDiagnostics("P01", mold_set_count=1, press_count=2,
                                                  max_groups=1, groups_needed=1, groups_used=1), "R005"),
        # 机台数封顶且已开满、日历够用 → 瓶颈是设备而非模具。若用 §9.3 的 groups_needed
        # 作判据（此处为 1，未超上限），会被误判为「受模具产能限制」的兜底 R005。
        ("机台数封顶且已开满 → R006", TypeDiagnostics("P01", mold_set_count=4, press_count=2,
                                                      max_groups=1, groups_needed=1, groups_used=1,
                                                      working_days=83, total_cycles=3013,
                                                      daily_capacity=80), "R006"),
    ]
    for name, diag, expected in cases:
        got, text = attribute(wo, diag, 2, date(2026, 2, 20))
        ok = got == expected and bool(text)
        print(f"[{'PASS' if ok else 'FAIL'}] 归因：{name}（得到 {got}）")
        results.append(ok)

    # 20. 交期缺失 → R010
    ok20, r20 = run_case("交期缺失", [
        WorkOrder("WNODUE", "SO1", "P01", "X", D(60), "张", None)
    ], {"S1": s1}, presses, expect=("R010",))
    results.append(ok20)

    # 21. 数量 ≤ 0 → R010（不得静默丢弃）
    _, r21 = run_case("数量非正", [
        WorkOrder("WZERO", "SO1", "P01", "X", D(0), "张", date(2026, 2, 10)),
        WorkOrder("WNEG", "SO1", "P01", "X", D(-5), "张", date(2026, 2, 10)),
    ], {"S1": s1}, presses, expect=("R010",))
    zero_reported = {"WZERO", "WNEG"} <= {e.work_order_no for e in r21.exceptions}
    print(f"[{'PASS' if zero_reported else 'FAIL'}] 数量非正工单逐张报 R010，不静默丢弃")
    results.append(zero_reported)

    # 22. 工单号重复 → 保留首条，其余报 R010
    _, r22 = run_case("工单号重复", [
        WorkOrder("WDUP", "SO1", "P01", "X", D(60), "张", date(2026, 2, 10)),
        WorkOrder("WDUP", "SO1", "P01", "Y", D(60), "张", date(2026, 2, 10)),
    ], {"S1": s1}, presses, expect=("R010",))
    dup_ok = [a.work_order_no for a in r22.assignments].count("WDUP") == 1
    print(f"[{'PASS' if dup_ok else 'FAIL'}] 重复工单号只排一条")
    results.append(dup_ok)

    # 23. 日历不覆盖交期时不崩溃（此前会 IndexError 掀翻整个运行）
    short_cal = {}
    d = PLAN_START
    while d <= date(2026, 2, 13):
        if d.weekday() < 5:
            short_cal[d] = CalendarDay(d, shifts=2)
        d += timedelta(days=1)
    try:
        _, r23 = run_case("日历不覆盖交期", [
            WorkOrder("W1", "SO1", "P01", "X", D(40), "张", date(2026, 4, 1))
        ], {"S1": s1}, presses, calendar=short_cal)
        no_crash = len(r23.assignments) == 1
    except Exception as exc:  # noqa: BLE001
        no_crash = False
        print("       ", type(exc).__name__, exc)
    print(f"[{'PASS' if no_crash else 'FAIL'}] 日历不覆盖交期时正常出结果而非崩溃")
    results.append(no_crash)

    # 24. R003 基准是「覆盖本批次的最高优先级模具」，套内更高优先级但不覆盖的模具不算
    s_two_pat = MoldSet("S1", "P01", 1, [
        mold("MA", "S1", 1, {"X": 2}),
        mold("MB", "S1", 2, {"X": 2, "Y": 2}),
    ])
    _, r24 = run_case("R003 基准是覆盖模具", [
        WorkOrder("WX", "SO-M", "P01", "X", D(40), "张", date(2026, 2, 10)),
        WorkOrder("WY", "SO-M", "P01", "Y", D(40), "张", date(2026, 2, 10)),
    ], {"S1": s_two_pat}, presses)
    no_spurious = all("R003" not in a.reason_codes for a in r24.assignments)
    print(f"[{'PASS' if no_spurious else 'FAIL'}] 只有唯一覆盖模具时不误报 R003")
    results.append(no_spurious)

    # 25. 订单齐套行内自洽：delay_days 必须等于本行 expected_finish - due_date
    _, r25 = run_case("订单齐套行内自洽", [
        WorkOrder("WA", "SO-1", "P01", "X", D(100), "张", date(2026, 1, 6)),
        WorkOrder("WB", "SO-1", "P01", "Y", D(400), "张", date(2026, 1, 20)),
    ], {"S1": MoldSet("S1", "P01", 1, [
        mold("M1", "S1", 1, {"X": 2}), mold("M2", "S1", 2, {"Y": 2}),
    ])}, {"R1": Press("R1", 5, "P01"), "R2": Press("R2", 5, "P01")})
    consistent = all(
        o.delay_days == max(0, (o.expected_finish_date - o.due_date).days)
        for o in r25.order_delivery
    )
    print(f"[{'PASS' if consistent else 'FAIL'}] 订单齐套的 delay_days 与行内日期自洽")
    results.append(consistent)

    # 26. 批次表与工单表交叉一致（C15）
    _, r26 = run_case("输出表交叉一致", [
        WorkOrder(f"W{i:02d}", f"SO{i}", "P01", "X", D(500), "张", date(2026, 1, 20))
        for i in range(1, 6)
    ], {"S1": s1}, {"R1": Press("R1", 5, "P01")})
    bnos = {b.batch_no for b in r26.batches}
    linked = all(a.batch_no in bnos for a in r26.assignments)
    print(f"[{'PASS' if linked else 'FAIL'}] 每条排程结果的批次号都存在于批次表")
    results.append(linked)

    # 27. 回填：机台时间轴上留有真实空隙时，回填应填入空隙，不回填应追加到末尾。
    #     引擎的贪心规则（总选最早开工）几乎不产生空隙，因此这里直接对调度器
    #     构造一个带空隙的时间轴来验证机制本身。
    from engine.calendar import Calendar as _Cal
    from engine.scheduling import (
        Interval,
        MoldTimeline,
        PressTimeline,
        Scheduler,
    )

    cal_days = make_calendar()
    p_bf = Params(plan_start=PLAN_START, backfill_gaps=True)
    sched = Scheduler(_Cal(cal_days, p_bf, PLAN_START + timedelta(days=119)), p_bf)
    n = len(sched.calendar)
    cap = 20

    def _press_with_hole() -> PressTimeline:
        tl = PressTimeline(press=Press("R1", cap, "P01"))
        tl.ensure(n)
        for s in list(range(0, 10)) + list(range(50, 60)):
            tl.used[s] = cap
        # 第二段用 M2、前驱是 M1，故其换模必须记为 4（配置值）——写成 0 会与
        # 前驱模具矛盾，插入守卫会（正确地）拒绝往空隙里放东西。
        tl.intervals = [Interval(0, 9, "M1", 0), Interval(50, 59, "M2", 4)]
        tl.cursor = 60
        tl.first_free = 10
        return tl

    probe = Mold("M3", "S1", MoldType.SINGLE, 3, {"X": D(2)})
    m_tl = MoldTimeline("M3")
    m_tl.ensure(n)

    pt_bf = _press_with_hole()
    got_bf = sched.evaluate(pt_bf, probe, m_tl, 100)  # 100 模 / 20 模每班 = 5 槽位

    sched_nb = Scheduler(_Cal(cal_days, Params(plan_start=PLAN_START, backfill_gaps=False),
                              PLAN_START + timedelta(days=119)),
                         Params(plan_start=PLAN_START, backfill_gaps=False))
    pt_nb = _press_with_hole()
    m_tl2 = MoldTimeline("M3")
    m_tl2.ensure(n)
    got_nb = sched_nb.evaluate(pt_nb, probe, m_tl2, 100)

    # 换模 4 + 100 模 = 104 模，每槽 20 模 → 占 6 个槽位
    backfill_ok = (
        got_bf is not None and got_bf.start == 10 and got_bf.end == 15
        and got_nb is not None and got_nb.start == 60 and got_nb.end == 65
    )
    print(f"[{'PASS' if backfill_ok else 'FAIL'}] 回填填入机台空隙（回填 {got_bf}，不回填 {got_nb}）")
    results.append(backfill_ok)

    # 28. 批号规则（§11.6）：全局唯一、从 1 起连续、同输入可复现。
    #     曾经的缺陷是「内层按创建的批次推进、外层按落位的批次推进」两套计数错位，
    #     只要有批次排不进去就会复用批号——这里用多类型 + 产能不足的场景复现该条件。
    multi = {
        f"P{i:02d}": ProductType(f"P{i:02d}", f"类型{i}") for i in range(1, 4)
    }
    multi_sets, multi_presses = {}, {}
    s_seq = p_seq = 0
    for code in sorted(multi):
        for _ in range(2):
            p_seq += 1
            multi_presses[f"R{p_seq:02d}"] = Press(f"R{p_seq:02d}", 20, code)
        for s in range(2):
            s_seq += 1
            multi_sets[f"S{s_seq:02d}"] = MoldSet(
                f"S{s_seq:02d}", code, s + 1,
                [mold(f"M{s_seq:02d}", f"S{s_seq:02d}", 1, {"X": 2, "Y": 2})],
            )
    multi_orders = [
        WorkOrder(f"WO{i:04d}", f"SO{i % 20:02d}", sorted(multi)[i % 3],
                  ("X", "Y")[i % 2], D(4000), "张", date(2026, 3, 1))
        for i in range(120)
    ]
    m_params = Params(plan_start=PLAN_START)
    m1 = Engine(m_params, multi, multi_sets, multi_presses, make_calendar(), multi_orders).run()
    m2 = Engine(m_params, multi, multi_sets, multi_presses, make_calendar(), multi_orders).run()
    nos = [b.batch_no for b in m1.batches]
    ints = sorted(int(x[1:]) for x in nos)
    unique_ok = len(set(nos)) == len(nos) and not any(
        a.batch_no not in set(nos) for a in m1.assignments
    )
    contiguous_ok = bool(ints) and ints[0] == 1 and ints == list(range(1, len(ints) + 1))
    determinism_ok = nos == [b.batch_no for b in m2.batches]
    partially_placed = 0 < len(m1.assignments) < len(multi_orders)
    print(f"[{'PASS' if unique_ok else 'FAIL'}] 批号全局唯一且无悬空引用"
          f"（{len(nos)} 个批次，排程 {len(m1.assignments)}/{len(multi_orders)}）")
    results.append(unique_ok)
    print(f"[{'PASS' if contiguous_ok else 'FAIL'}] 批号从 1 起连续"
          f"（范围 {ints[0] if ints else '-'}..{ints[-1] if ints else '-'}）")
    results.append(contiguous_ok)
    print(f"[{'PASS' if determinism_ok else 'FAIL'}] 批号同输入可复现")
    results.append(determinism_ok)
    print(f"[{'PASS' if partially_placed else 'FAIL'}] 场景确实存在未落位批次（覆盖原缺陷条件）")
    results.append(partially_placed)

    # 29. §10.2 按需启用更多模具套：已选中的套覆盖不到的牌型，若还有可覆盖的套
    #     且组数未达上限，应启用它，而不是误报 R008。
    s_x = MoldSet("S1", "P01", 1, [mold("M1", "S1", 1, {"X": 2})])
    s_y = MoldSet("S2", "P01", 2, [mold("M2", "S2", 1, {"Y": 2})])
    four = {f"R{i}": Press(f"R{i}", 20, "P01") for i in range(1, 5)}
    _, r29 = run_case("按需启用模具套", [
        WorkOrder("WX", "SO1", "P01", "X", D(40), "张", date(2026, 2, 10)),
        WorkOrder("WY", "SO2", "P01", "Y", D(40), "张", date(2026, 2, 10)),
    ], {"S1": s_x, "S2": s_y}, four)
    used_sets = {a.set_no for a in r29.assignments}
    expand_ok = len(r29.assignments) == 2 and used_sets == {"S1", "S2"} and not any(
        e.code == "R008" for e in r29.exceptions
    )
    print(f"[{'PASS' if expand_ok else 'FAIL'}] 覆盖不足时按需启用下一套（用到 {sorted(used_sets)}）")
    results.append(expand_ok)

    # 30–33. §10.1 DEMAND_CYCLES_FIRST 选套策略。上面所有用例的套都是同构的
    #        （副数与配比一致），新旧策略在其上结果相同，因此必须另造异构数据。
    #        2 台机 → max_groups = 1，只能启用一套，选错即暴露。
    two = {"R1": Press("R1", 20, "P01"), "R2": Press("R2", 20, "P01")}

    def chosen_set(result):
        return sorted({a.set_no for a in result.assignments})

    # 30. 副数多但配比低的套 vs 副数少但配比高的套：应按实需模数选后者。
    #     S_low 需 4×100/2=200 模、并行度 2 → 评分 100；S_high 需 40 模、并行度 1 → 评分 40。
    s_low = MoldSet("S_low", "P01", 1, [
        mold("L1", "S_low", 1, {"X": 2}), mold("L2", "S_low", 2, {"X": 2}),
        mold("L3", "S_low", 3, {"X": 2}),
    ])
    s_high = MoldSet("S_high", "P01", 2, [mold("H1", "S_high", 1, {"X": 10})])
    het_orders = [
        WorkOrder(f"H{i}", f"SO{i}", "P01", "X", D(100), "张", date(2026, 2, 10))
        for i in range(1, 5)
    ]
    _, r30 = run_case("异构多套：按实需模数选套而非按副数", list(het_orders),
                      {"S_low": s_low, "S_high": s_high}, two)
    _, r30b = run_case("异构多套：同输入两次运行一致", list(het_orders),
                       {"S_low": s_low, "S_high": s_high}, two)
    sig = lambda r: sorted((a.work_order_no, a.set_no, a.mold_no, a.cycles) for a in r.assignments)
    case30_ok = chosen_set(r30) == ["S_high"] and all(a.cycles == 10 for a in r30.assignments) and sig(r30) == sig(r30b)
    print(f"[{'PASS' if case30_ok else 'FAIL'}] 副数少但配比高的套被选中"
          f"（用到 {chosen_set(r30)}，模数 {[a.cycles for a in r30.assignments]}）")
    results.append(case30_ok)

    # 31. 配比高但牌型对不上的套不得被选中。两个方向都要防：按副数排序会选中它
    #     （它有 2 副），按「套内最高配比」排序也会选中它（配比 100 最高）。
    s_wrong = MoldSet("S_wrong", "P01", 1, [
        mold("W1", "S_wrong", 1, {"Y": 100}), mold("W2", "S_wrong", 2, {"Y": 100}),
    ])
    s_right = MoldSet("S_right", "P01", 2, [mold("Q1", "S_right", 1, {"X": 5})])
    _, r31 = run_case("配比高但牌型对不上的套不被选中",
                      [WorkOrder(f"H{i}", f"SO{i}", "P01", "X", D(100), "张", date(2026, 2, 10))
                       for i in range(1, 5)],
                      {"S_wrong": s_wrong, "S_right": s_right}, two)
    case31_ok = (
        chosen_set(r31) == ["S_right"] and len(r31.assignments) == 4
        and not any(e.code == "R008" for e in r31.exceptions)
    )
    print(f"[{'PASS' if case31_ok else 'FAIL'}] 牌型对不上的高配比套被排除"
          f"（用到 {chosen_set(r31)}，排程 {len(r31.assignments)}/4）")
    results.append(case31_ok)

    # 32. 覆盖面窄的高配比套 vs 覆盖面广的低配比套：Y 有需求时应选后者。
    #     两套副数相同（旧策略按 set_order 会选中 S_narrow），故只有「未覆盖工单数」
    #     这一维度能区分：选错则 Y 工单因组数上限挡住补套而直接 R008。
    s_narrow = MoldSet("S_narrow", "P01", 1, [
        mold("N1", "S_narrow", 1, {"X": 10}), mold("N2", "S_narrow", 2, {"X": 10}),
    ])
    s_wide = MoldSet("S_wide", "P01", 2, [
        mold("D1", "S_wide", 1, {"X": 4, "Y": 4}), mold("D2", "S_wide", 2, {"X": 4, "Y": 4}),
    ])
    wide_orders = [
        WorkOrder("WX1", "SO1", "P01", "X", D(100), "张", date(2026, 2, 10)),
        WorkOrder("WX2", "SO2", "P01", "X", D(100), "张", date(2026, 2, 10)),
        WorkOrder("WY1", "SO3", "P01", "Y", D(100), "张", date(2026, 2, 10)),
        WorkOrder("WY2", "SO4", "P01", "Y", D(100), "张", date(2026, 2, 10)),
    ]
    _, r32 = run_case("覆盖面广的套优先于窄覆盖的高配比套", wide_orders,
                      {"S_narrow": s_narrow, "S_wide": s_wide}, two)
    case32_ok = (
        chosen_set(r32) == ["S_wide"] and len(r32.assignments) == 4
        and not any(e.code == "R008" for e in r32.exceptions)
    )
    print(f"[{'PASS' if case32_ok else 'FAIL'}] 窄覆盖高配比套不因覆盖工单少而胜出"
          f"（用到 {chosen_set(r32)}，排程 {len(r32.assignments)}/4）")
    results.append(case32_ok)

    # 33. 全部套都覆盖不到待排牌型：评分全为 None，不得抛异常，应走 R008。
    s_other = MoldSet("S_only", "P01", 1, [mold("O1", "S_only", 1, {"Z": 2})])
    _, r33 = run_case("全部套覆盖不到待排牌型", [
        WorkOrder("WZ", "SO1", "P01", "X", D(40), "张", date(2026, 2, 10)),
    ], {"S_only": s_other}, two, expect=("R008",))
    case33_ok = not r33.assignments and any(e.code == "R008" for e in r33.exceptions)
    print(f"[{'PASS' if case33_ok else 'FAIL'}] 无任何套可覆盖时不抛异常并报 R008")
    results.append(case33_ok)

    # 34. §21.1 U08 组数上限：g ≤ min(套数, 机台数 / 每组机台数)。
    #     4 套模具、4 台机、每组 2 台 → 上限必须收敛到 2 组而不是 4 组；
    #     判据写错会让 §13.3 的资源归因指向错误的瓶颈（把「机台数封顶」说成套数封顶）。
    four_presses = {f"R{i}": Press(f"R{i}", 20, "P01") for i in range(1, 5)}
    four_sets = {
        f"T{i}": MoldSet(f"T{i}", "P01", i, [mold(f"P{i}", f"T{i}", 1, {"X": 4, "Y": 4})])
        for i in range(1, 5)
    }
    _, r34 = run_case("组数上限 = min(套数, 机台数/每组台数)", [
        WorkOrder(f"WG{i}", f"SO{i}", "P01", "X", D(200), "张", date(2026, 2, 10))
        for i in range(1, 9)
    ], four_sets, four_presses)
    d34 = r34.diagnostics["P01"]
    expected_cap = min(len(four_sets), len(four_presses) // 2)
    case34_ok = (
        d34.max_groups == expected_cap
        and all(d.groups_used <= expected_cap for d in r34.diagnostics.values())
    )
    print(f"[{'PASS' if case34_ok else 'FAIL'}] 组数上限 = min(套数, 机台数/每组台数)"
          f"（max_groups={d34.max_groups} 应为 {expected_cap}，groups_used={d34.groups_used}）")
    results.append(case34_ok)

    # 35. §20.2 时间上限：置 truncated，且未排入的有效工单逐条报 R007，不得静默丢弃。
    #     截断可能落在「产品类型边界」或「类型内部的批次循环」，落位多少张取决于机器速度，
    #     故只断言 §20.2 的契约本身，不断言已排张数。
    big_orders = [
        WorkOrder(f"WT{i:04d}", f"ST{i % 40:03d}", "P01", "X" if i % 2 else "Y",
                  D(300), "张", date(2026, 2, 10))
        for i in range(3000)
    ]
    ok35, r35 = run_case("时间上限截断（§20.2）", big_orders, {"S1": s1}, presses,
                         time_limit_seconds=0.05, expect=("R007",))
    sched35 = {a.work_order_no for a in r35.assignments}
    rep35 = {e.work_order_no for e in r35.exceptions if e.code == "R007"}
    unplaced35 = {w.work_order_no for w in big_orders} - sched35
    case35_ok = ok35 and r35.meta["truncated"] and not (unplaced35 - rep35)
    print(f"[{'PASS' if case35_ok else 'FAIL'}] 超时截断：truncated={r35.meta['truncated']}，"
          f"已排 {len(sched35)} 张，未排 {len(unplaced35)} 张全部报 R007"
          f"={not (unplaced35 - rep35)}")
    results.append(case35_ok)

    # 36. 模具号跨套重复（同一副物理模具出现在两个套里）：既要报出这个资料异常，
    #     又不得产出「同一副模具同时装在两台机台」的物理不可能结果——§5.6 的假设是
    #     一副模具同一时刻只在一台机上，因此必须跨组串行（C02 不得失败）。
    dup_sets = {
        "S1": MoldSet("S1", "P01", 1, [mold("MX", "S1", 1, {"X": 2}),
                                       mold("M1", "S1", 2, {"X": 2})]),
        "S2": MoldSet("S2", "P01", 2, [mold("MX", "S2", 1, {"X": 2}),
                                       mold("M2", "S2", 2, {"X": 2})]),
    }
    dup_orders = [WorkOrder(f"WD{i}", f"SD{i}", "P01", "X", D(200), "张", date(2026, 2, 10))
                  for i in range(1, 5)]
    dup_eng = build(dup_orders, dup_sets, two)
    r36 = dup_eng.run()
    dup_exc = [e for e in r36.exceptions if e.code == "R010" and "MX" in e.detail]
    dup_errs = check_all(r36.assignments, r36.batches,
                         {w.work_order_no: w for w in dup_orders}, two, r36.calendar,
                         dup_sets, dup_eng.params,
                         reported={e.work_order_no for e in r36.exceptions})
    mx_ok = not any(e.startswith("C02") for e in dup_errs)
    case36_ok = (
        bool(dup_exc) and "S1" in dup_exc[0].detail and "S2" in dup_exc[0].detail
        and mx_ok and not dup_errs and len(r36.assignments) == len(dup_orders)
    )
    print(f"[{'PASS' if case36_ok else 'FAIL'}] 跨套重复模具号：报 R010 且跨组串行不违反 C02"
          f"（报 {'已' if dup_exc else '未'}异常，排程 {len(r36.assignments)}/{len(dup_orders)}，"
          f"校验错误 {len(dup_errs)}）")
    results.append(case36_ok)

    # 37. V03 的另一半：交期早于计划起点时必须报 R010（且仍按最早可行时间排程）。
    #     此前只实现了「交期缺失」，早于计划起点会被 target_finish 静默钳到计划起点，
    #     既不报异常也不留痕，违反需求第八章「标记为异常数据并报告」。
    early_due = PLAN_START - timedelta(days=10)
    _, r37 = run_case("交期早于计划起点", [
        WorkOrder("WEARLY", "SO1", "P01", "X", D(60), "张", early_due)
    ], {"S1": s1}, presses, expect=("R010",))
    early_reported = [
        e for e in r37.exceptions if e.work_order_no == "WEARLY" and e.code == "R010"
    ]
    case37_ok = bool(early_reported) and len(r37.assignments) == 1
    print(f"[{'PASS' if case37_ok else 'FAIL'}] 交期早于计划起点报 R010 且仍排程"
          f"（{'已报' if early_reported else '未报'}，排程 {len(r37.assignments)} 张）")
    results.append(case37_ok)

    print()
    print(f"通过 {sum(results)}/{len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
