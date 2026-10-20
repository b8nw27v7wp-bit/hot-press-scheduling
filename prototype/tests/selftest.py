"""自检脚本：构造小数据集，跑通全流程并校验硬约束。"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import (  # noqa: E402
    CalendarDay,
    Mold,
    MoldPattern,
    MoldSet,
    MoldType,
    Params,
    Press,
    ProductType,
    WorkOrder,
    WorkOrderStatus,
)
from engine.scheduler import Engine  # noqa: E402
from verify.constraint_checker import (  # noqa: E402
    check_all,
    check_determinism,
    fingerprint,
    summarize,
)


def build_dataset():
    plan_start = date(2026, 1, 5)
    params = Params(
        plan_start=plan_start,
        group_size=2,
        pre_offset_days=15,
        shift_hours=12,
        changeover_hours=2,
    )

    product_types = {
        "P01": ProductType("P01", "三角牌", ("A01",)),
        "P02": ProductType("P02", "多米诺骨牌", ("A02",)),
    }

    def mold(no, set_no, kind, prio, ratios):
        return Mold(
            mold_no=no,
            set_no=set_no,
            mold_type=kind,
            priority=prio,
            ratios={p: Decimal(str(r)) for p, r in ratios.items()},
        )

    s1 = MoldSet(
        set_no="S1",
        product_type_code="P01",
        set_order=1,
        molds=[
            mold("M1", "S1", MoldType.MULTI, 1, {"X": 2, "Y": 2}),
            mold("M2", "S1", MoldType.SINGLE, 2, {"X": 4}),
        ],
    )
    s2 = MoldSet(
        set_no="S2",
        product_type_code="P01",
        set_order=2,
        molds=[
            mold("M3", "S2", MoldType.MULTI, 1, {"X": 2, "Y": 2}),
            mold("M4", "S2", MoldType.SINGLE, 2, {"Y": 4}),
        ],
    )
    s3 = MoldSet(
        set_no="S3",
        product_type_code="P02",
        set_order=1,
        molds=[mold("M5", "S3", MoldType.SINGLE, 1, {"Z": 5})],
    )

    presses = {
        "R1": Press("R1", 20, "P01"),
        "R2": Press("R2", 20, "P01"),
        "R3": Press("R3", 20, "P01"),
        "R4": Press("R4", 20, "P01"),
        "R5": Press("R5", 25, "P02"),
        "R6": Press("R6", 25, "P02"),
    }

    calendar_days = {}
    cur = plan_start
    end = plan_start + timedelta(days=120)
    while cur <= end:
        if cur.weekday() < 5:
            calendar_days[cur] = CalendarDay(cur, shifts=2, is_holiday=False)
        cur += timedelta(days=1)

    orders = []
    seq = 1

    def add(ptype, pattern, qty, sales, due, status=WorkOrderStatus.NOT_STARTED, remaining=None):
        nonlocal seq
        orders.append(
            WorkOrder(
                work_order_no=f"WO{seq:04d}",
                sales_order_no=sales,
                product_type_code=ptype,
                pattern_code=pattern,
                qty=Decimal(str(qty)),
                uom="张",
                due_date=due,
                status=status,
                remaining_qty=None if remaining is None else Decimal(str(remaining)),
            )
        )
        seq += 1

    due1 = date(2026, 2, 10)
    add("P01", "X", 120, "SO-01", due1)
    add("P01", "Y", 90, "SO-01", due1)
    add("P01", "X", 200, "SO-01", due1)
    add("P01", "Y", 60, "SO-02", date(2026, 2, 12))
    add("P01", "X", 80, "SO-02", date(2026, 2, 12))
    add("P01", "X", 45, "SO-03", date(2026, 2, 20), WorkOrderStatus.IN_PROGRESS, 30)
    add("P01", "Y", 55, "SO-03", date(2026, 2, 20))
    add("P01", "X", 100, "SO-04", date(2026, 3, 2))
    add("P02", "Z", 500, "SO-05", date(2026, 2, 25))
    add("P02", "Z", 300, "SO-05", date(2026, 2, 25))
    add("P02", "Z", 250, "SO-06", date(2026, 3, 5))
    # 已完成：不应进入排程
    add("P01", "X", 100, "SO-01", due1, WorkOrderStatus.COMPLETED)

    return params, product_types, {"S1": s1, "S2": s2, "S3": s3}, presses, calendar_days, orders


def main():
    params, product_types, mold_sets, presses, calendar_days, orders = build_dataset()
    engine = Engine(params, product_types, mold_sets, presses, calendar_days, orders)
    result = engine.run()

    errors = check_all(
        result.assignments,
        result.batches,
        {w.work_order_no: w for w in orders},
        presses,
        result.calendar,
        mold_sets,
        params,
        reported={e.work_order_no for e in result.exceptions},
    )

    # C14 可复现：同输入再跑一次，指纹必须一致
    second = Engine(params, product_types, mold_sets, presses, calendar_days, orders).run()
    errors += check_determinism(
        fingerprint(
            result.assignments, result.batches, result.order_delivery, result.release_plan
        ),
        fingerprint(
            second.assignments, second.batches, second.order_delivery, second.release_plan
        ),
    )

    stats = summarize(result.assignments, result.batches, presses, result.calendar)

    print(f"排程工单数: {len(result.assignments)}")
    print(f"批次数:     {len(result.batches)}")
    print(f"异常数:     {len(result.exceptions)}")
    print(f"延期工单:   {sum(1 for a in result.assignments if a.delayed)}")
    print(f"总延期天数: {sum(a.delay_days for a in result.assignments)}")
    print()
    print(f"{'工单':<8}{'牌型':<6}{'机台':<6}{'模具':<6}{'批次':<9}{'模数':>5}  {'开始':<12}{'结束':<12}{'延期':>5}")
    for a in sorted(result.assignments, key=lambda x: (x.press_no, x.start_slot)):
        start = result.calendar.slot(a.start_slot)
        end = result.calendar.slot(a.end_slot)
        print(
            f"{a.work_order_no:<8}{a.pattern_code:<6}{a.press_no:<6}{a.mold_no:<6}{a.batch_no:<9}"
            f"{a.cycles:>5}  {str(start.day)+'/'+str(start.shift):<12}"
            f"{str(end.day)+'/'+str(end.shift):<12}{a.delay_days:>5}"
        )
    print()
    print(
        f"利用率: {stats['press_utilization']:.1%}"
        f"（{stats['used_cycles']}/{stats['available_cycles']} 模）"
        f"  换模 {stats['changeover_count']} 次  超产 {stats['surplus_qty']}"
    )
    print()
    print("订单齐套与延期 (§4.3):")
    for o in result.order_delivery:
        flag = f"延期 {o.delay_days} 天" if o.delayed else "按期"
        print(f"  {o.sales_order_no:<8} 应交 {o.due_date}  预计完成 {o.expected_finish_date}  {flag}")
    print()
    print("可释放数量 (§4.4):")
    for rp in result.release_plan[:5]:
        print(f"  {rp.batch_no}  {rp.pattern_code}  {rp.release_qty}  "
              f"{rp.release_date} 第 {rp.release_shift} 班")
    if len(result.release_plan) > 5:
        print(f"  … 共 {len(result.release_plan)} 条")
    print()
    sample = result.reasons[0]
    print(f"排程理由样例 (§4.5, 工单 {sample.work_order_no}):")
    for label, text in (
        ("机台", sample.machine_reason),
        ("时间", sample.time_reason),
        ("批次", sample.batch_reason),
        ("模具", sample.mold_reason),
    ):
        print(f"  {label}: {text}")
    print()
    if errors:
        print("约束校验失败：")
        for e in errors:
            print("  -", e)
        return 1
    print("约束校验全部通过 (C01-C15)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
