"""规模与性能验证：1 万工单、30 产品类型、数十机台、数百模具。"""

from __future__ import annotations

import random
import sys
import time
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
)
from engine.scheduler import Engine  # noqa: E402
from verify.constraint_checker import check_all, summarize  # noqa: E402

PLAN_START = date(2026, 1, 5)
SEED = 20260105

# §19 规模估算：「组数迭代最多 3 次，总时长 < 15 秒」。§21.3 把「全量排程限时」
# 列为验收项，本文件据此把它做成可判定的断言——只打印耗时的话，除约束失败外
# 恒 rc=0，退化到 14 秒也不会让任何流程失败。实测约 0.8s，留了约 18 倍余量。
FULL_RUN_LIMIT_SECONDS = 15.0


def build(
    n_orders: int = 10000,
    n_types: int = 30,
    due_range: tuple[int, int] = (40, 110),
    seed: int = SEED,
    ratio_pool: tuple[int, ...] = (40, 60, 80, 120),
    capacity_scale: float = 1.0,
):
    """默认参数即 §24 的规模测试数据集；其余参数供 tests/sensitivity.py 做扫描。

    ratio_pool 控制同一类型内各套配比的离散度——§10.1 选套策略的收益依赖它。
    capacity_scale 按比例缩放每班产能（取整，故各档之间不是严格等比）。
    """
    rng = random.Random(seed)  # 固定种子，保证可复现

    product_types = {
        f"P{i:02d}": ProductType(f"P{i:02d}", f"类型{i}", (f"A{i:02d}",))
        for i in range(1, n_types + 1)
    }

    mold_sets: dict[str, MoldSet] = {}
    set_seq = 0
    for i in range(1, n_types + 1):
        ptype = f"P{i:02d}"
        # 真实工厂：每类型约 10 副模具，每套 2–4 副（组内 2 台机可同时开工）
        n_sets = rng.randint(2, 4)
        for s in range(n_sets):
            set_seq += 1
            set_no = f"S{set_seq:04d}"
            molds = []
            for m in range(rng.randint(2, 4)):
                patterns = ["X", "Y"] if m % 2 == 0 else ["X"]
                molds.append(
                    Mold(
                        mold_no=f"M{set_seq:04d}{m}",
                        set_no=set_no,
                        mold_type=MoldType.MULTI if len(patterns) > 1 else MoldType.SINGLE,
                        priority=m + 1,
                        ratios={p: Decimal(rng.choice(ratio_pool)) for p in patterns},
                    )
                )
            mold_sets[set_no] = MoldSet(set_no, ptype, s + 1, molds)

    presses: dict[str, Press] = {}
    p_seq = 0
    for i in range(1, n_types + 1):
        ptype = f"P{i:02d}"
        for _ in range(rng.choice([2, 2, 4])):
            p_seq += 1
            base_cap = rng.choice([20, 24, 30])
            presses[f"R{p_seq:03d}"] = Press(
                f"R{p_seq:03d}", max(1, int(base_cap * capacity_scale)), ptype
            )

    calendar_days = {}
    cur = PLAN_START
    while cur <= PLAN_START + timedelta(days=400):
        if cur.weekday() < 6:
            calendar_days[cur] = CalendarDay(cur, shifts=2)
        cur += timedelta(days=1)

    # 一张销售订单只有一个交期，其下所有工单共用（K3 的实际展开方式）
    n_sales_orders = 1500
    sales_due = {
        i: PLAN_START + timedelta(days=rng.randint(*due_range))
        for i in range(1, n_sales_orders + 1)
    }

    orders = []
    for i in range(n_orders):
        ptype = f"P{rng.randint(1, n_types):02d}"
        so = rng.randint(1, n_sales_orders)
        orders.append(
            WorkOrder(
                work_order_no=f"WO{i:06d}",
                sales_order_no=f"SO{so:05d}",
                product_type_code=ptype,
                pattern_code=rng.choice(["X", "Y"]),
                qty=Decimal(rng.randint(20, 2000)),
                uom="张",
                due_date=sales_due[so],
            )
        )

    params = Params(plan_start=PLAN_START, group_size=2, pre_offset_days=15, changeover_hours=2)
    return params, product_types, mold_sets, presses, calendar_days, orders


def main() -> int:
    params, product_types, mold_sets, presses, calendar_days, orders = build()

    t0 = time.perf_counter()
    engine = Engine(params, product_types, mold_sets, presses, calendar_days, orders)
    result = engine.run()
    elapsed = time.perf_counter() - t0

    t1 = time.perf_counter()
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
    check_elapsed = time.perf_counter() - t1

    stats = summarize(
        result.assignments, result.batches, presses, result.calendar, params, orders
    )
    n_molds = sum(len(s.molds) for s in mold_sets.values())
    n_slots = len(result.calendar)
    delayed_orders = sum(1 for o in result.order_delivery if o.delayed)

    print(f"工单 {len(orders)}  类型 {len(product_types)}  机台 {len(presses)}  "
          f"模具 {n_molds}  套 {len(mold_sets)}  槽位 {n_slots}")
    print(f"排程 {len(result.assignments)}  批次 {len(result.batches)}  "
          f"异常 {len(result.exceptions)}")
    print(f"延期工单 {stats['delayed_work_orders']}（{stats['delayed_work_orders'] / len(orders):.1%}）"
          f"  总延期天数 {stats['total_delay_days']}")
    print(f"延期订单 {delayed_orders}（{delayed_orders / max(1, len(result.order_delivery)):.1%}）")
    print(f"设备利用率 {stats['press_utilization']:.1%}（分母为整个计划地平线 {n_slots} 槽，"
          f"由 §19 悲观外推，故偏低）")
    print(f"交期窗口内利用率 {stats['window_utilization']:.1%}"
          f"（分母为各类型交期窗口内的机台产能，判断产能是否富余看这个）")
    print(f"换模 {stats['changeover_count']} 次  超产 {stats['surplus_qty']}")
    print(f"计算耗时 {elapsed:.2f}s  校验耗时 {check_elapsed:.2f}s")

    checks: list[tuple[str, bool, str]] = []
    checks.append(("约束 C01–C15 全部通过", not errors, f"{len(errors)} 项失败"))
    checks.append((
        f"全量排程限时 ≤ {FULL_RUN_LIMIT_SECONDS:.0f}s（§19、§21.3）",
        elapsed <= FULL_RUN_LIMIT_SECONDS,
        f"{elapsed:.2f}s",
    ))
    checks.append((
        "未触发时间上限截断（truncated=false）",
        result.meta.get("truncated") is False,
        f"truncated={result.meta.get('truncated')}",
    ))
    # 本数据集只缺周日（build 里 weekday() < 6），因此 R011 是预期内的日历缺失报告；
    # 真正要断言的是「没有工单级异常」——即没有任何工单被跳过。
    order_level = [e for e in result.exceptions if e.work_order_no]
    checks.append((
        "全部工单排入，无工单级异常",
        len(result.assignments) == len(orders) and not order_level,
        f"排程 {len(result.assignments)}/{len(orders)}，"
        f"工单级异常 {len(order_level)}，日历缺失 {len(result.exceptions) - len(order_level)}",
    ))

    print()
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  （{detail}）" if detail else ""))
    failed = sum(1 for _, ok, _ in checks if not ok)
    print()
    print(f"通过 {len(checks) - failed}/{len(checks)}")

    if errors:
        print("约束失败：")
        for e in errors[:10]:
            print("  -", e)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
