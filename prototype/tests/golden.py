"""§21.2 黄金数据集：3 个产品类型、10 台机台、20 副模具、500 张工单。

与 scenarios.py 的差别：scenarios 用最小数据集逐条验证规则，本文件用一个接近
真实规模的固定数据集跑通全流程，并对若干「人工可核对」的关键工单做硬断言。
数据集完全确定（固定种子），因此结果可复现、可回归。

用法： py tests/golden.py
"""

from __future__ import annotations

import random
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
)
from engine.capacity import cycles_for  # noqa: E402
from engine.scheduler import Engine  # noqa: E402
from verify.constraint_checker import (  # noqa: E402
    check_all,
    check_determinism,
    fingerprint,
    summarize,
)

PLAN_START = date(2026, 1, 5)
SEED = 20260105
D = Decimal

# 每个产品类型的（机台数, 模具套数, 每套副数）
LAYOUT = {
    "P01": (4, 3, 2),
    "P02": (4, 3, 2),
    "P03": (2, 4, 2),
}
# P03 的模具全部为配比 2 的多点模具，使关键工单的模数可人工手算
DETERMINISTIC_TYPE = "P03"
KEY_RATIO = Decimal(2)
PRESS_CAPACITY = 20


def build():
    rng = random.Random(SEED)

    product_types = {
        code: ProductType(code, f"类型{code[1:]}", (f"A{code[1:]}",)) for code in LAYOUT
    }

    mold_sets: dict[str, MoldSet] = {}
    presses: dict[str, Press] = {}
    seq = 0
    pseq = 0
    for code, (n_press, n_sets, molds_per_set) in LAYOUT.items():
        for _ in range(n_press):
            pseq += 1
            presses[f"R{pseq:02d}"] = Press(f"R{pseq:02d}", PRESS_CAPACITY, code)
        for s in range(n_sets):
            seq += 1
            set_no = f"S{seq:02d}"
            molds = []
            for m in range(molds_per_set):
                if code == DETERMINISTIC_TYPE:
                    # 全部为配比 2 的多点模具 → 关键工单模数确定可算
                    ratios = {"X": KEY_RATIO, "Y": KEY_RATIO}
                    kind = MoldType.MULTI
                elif m == 0:
                    ratios = {"X": D(2), "Y": D(2)}
                    kind = MoldType.MULTI
                else:
                    ratios = {"X": D(4)} if m % 2 else {"Y": D(4)}
                    kind = MoldType.SINGLE
                molds.append(
                    Mold(
                        mold_no=f"M{seq:02d}{m + 1}",
                        set_no=set_no,
                        mold_type=kind,
                        priority=m + 1,
                        ratios=ratios,
                    )
                )
            mold_sets[set_no] = MoldSet(set_no, code, s + 1, molds)

    # 日历补齐每一天：周末记为 0 班（非生产），而不是整条缺失——
    # 缺失会被报成 R011，而真实输入里日历是覆盖全期的。
    calendar_days = {}
    cur = PLAN_START
    while cur <= PLAN_START + timedelta(days=400):
        weekend = cur.weekday() >= 5
        calendar_days[cur] = CalendarDay(cur, shifts=0 if weekend else 2, is_holiday=weekend)
        cur += timedelta(days=1)

    orders: list[WorkOrder] = []

    def add(no, code, pattern, qty, sales, due, **kw):
        orders.append(
            WorkOrder(
                work_order_no=no,
                sales_order_no=sales,
                product_type_code=code,
                pattern_code=pattern,
                qty=D(qty),
                uom="张",
                due_date=due,
                **kw,
            )
        )

    # ---- 人工可核对的关键工单（放在配比恒为 2 的 P03 上）----
    # K1: 40 张 / 配比 2 = 20 模，机台每班 20 模 → 恰好占 1 个槽位
    add("K1", DETERMINISTIC_TYPE, "X", 40, "SO-K1", date(2026, 4, 6))
    # K2: 41 张 / 配比 2 = 21 模 → 超出单班产能，跨 2 个槽位
    add("K2", DETERMINISTIC_TYPE, "X", 41, "SO-K2", date(2026, 4, 6))
    # K3/K4: 同一销售订单、牌型 X 与 Y，可由多点模具一次成型 → 应合并为一个批次
    add("K3", DETERMINISTIC_TYPE, "X", 40, "SO-K3", date(2026, 4, 6))
    add("K4", DETERMINISTIC_TYPE, "Y", 40, "SO-K3", date(2026, 4, 6))

    # ---- 随机填充到 500 张 ----
    for i in range(len(orders), 500):
        code = rng.choice(list(LAYOUT))
        add(
            f"WO{i:04d}",
            code,
            rng.choice(["X", "Y"]),
            rng.randint(20, 300),
            f"SO{rng.randint(1, 120):04d}",
            PLAN_START + timedelta(days=rng.randint(60, 220)),
        )

    params = Params(
        plan_start=PLAN_START,
        group_size=2,
        pre_offset_days=15,
        shift_hours=12,
        changeover_hours=2,
    )
    return params, product_types, mold_sets, presses, calendar_days, orders


def main() -> int:
    params, product_types, mold_sets, presses, calendar_days, orders = build()
    engine = Engine(params, product_types, mold_sets, presses, calendar_days, orders)
    result = engine.run()

    by_no = {w.work_order_no: w for w in orders}
    errors = check_all(
        result.assignments,
        result.batches,
        by_no,
        presses,
        result.calendar,
        mold_sets,
        params,
        reported={e.work_order_no for e in result.exceptions},
    )
    second = Engine(params, product_types, mold_sets, presses, calendar_days, orders).run()
    errors += check_determinism(
        fingerprint(result.assignments, result.batches, result.order_delivery,
                    result.release_plan),
        fingerprint(second.assignments, second.batches, second.order_delivery,
                    second.release_plan),
    )

    stats = summarize(result.assignments, result.batches, presses, result.calendar)
    n_molds = sum(len(s.molds) for s in mold_sets.values())
    print(f"工单 {len(orders)}  产品类型 {len(product_types)}  机台 {len(presses)}  "
          f"模具 {n_molds}  套 {len(mold_sets)}  槽位 {len(result.calendar)}")
    print(f"排程 {len(result.assignments)}  批次 {len(result.batches)}  "
          f"异常 {len(result.exceptions)}")
    print(f"延期工单 {stats['delayed_work_orders']}  总延期 {stats['total_delay_days']} 天  "
          f"利用率 {stats['press_utilization']:.1%}  换模 {stats['changeover_count']} 次")
    print()

    checks: list[tuple[str, bool, str]] = []

    # 1. 规模符合 §21.2 的约定
    checks.append((
        "规模符合 §21.2（3 类型 / 10 机台 / 20 副模具 / 500 工单）",
        len(product_types) == 3 and len(presses) == 10 and n_molds == 20 and len(orders) == 500,
        f"{len(product_types)}/{len(presses)}/{n_molds}/{len(orders)}",
    ))

    # 2. 全部有效工单都被排入，且无异常
    checks.append((
        "500 张工单全部排入且无异常",
        len(result.assignments) == 500 and not result.exceptions,
        f"排程 {len(result.assignments)}，异常 {len(result.exceptions)}",
    ))

    # 3. K1：40 张 / 配比 2 = 20 模，恰好等于单班产能 → 只占 1 个槽位
    a1 = next(a for a in result.assignments if a.work_order_no == "K1")
    checks.append((
        "K1 20 模 = 单班产能，占 1 个槽位",
        a1.cycles == 20 and a1.start_slot == a1.end_slot,
        f"模数 {a1.cycles}，槽位 {a1.start_slot}-{a1.end_slot}",
    ))

    # 4. K2：41 张 / 配比 2 = 21 模，超单班产能 → 跨 2 个槽位（不拆分）
    a2 = next(a for a in result.assignments if a.work_order_no == "K2")
    checks.append((
        "K2 21 模超单班产能，跨 2 个槽位且不拆分",
        a2.cycles == 21 and a2.end_slot == a2.start_slot + 1,
        f"模数 {a2.cycles}，槽位 {a2.start_slot}-{a2.end_slot}",
    ))

    # 5. K3/K4：同订单、牌型 X 与 Y 合并为同一批次
    a3 = next(a for a in result.assignments if a.work_order_no == "K3")
    a4 = next(a for a in result.assignments if a.work_order_no == "K4")
    batch3 = next(b for b in result.batches if b.batch_no == a3.batch_no)
    checks.append((
        "K3/K4 同订单 X+Y 合并为同一批次",
        a3.batch_no == a4.batch_no and set(batch3.patterns) == {"X", "Y"},
        f"批次 {a3.batch_no}，牌型 {sorted(batch3.patterns)}",
    ))

    # 6. 批次模数 = 批次内工单模数最大值
    ok_cycles = all(
        b.cycles == max(
            cycles_for(by_no[n].plan_qty,
                       next(m for s in mold_sets.values() for m in s.molds
                            if m.mold_no == b.mold_no).ratios[by_no[n].pattern_code])
            for n in b.work_order_nos
        )
        for b in result.batches
    )
    checks.append(("每个批次模数 = 批次内工单模数最大值", ok_cycles, ""))

    # 7. 输出表交叉一致
    bnos = {b.batch_no for b in result.batches}
    checks.append((
        "每条排程结果都指向存在的批次",
        all(a.batch_no in bnos for a in result.assignments),
        "",
    ))

    # 8. 约束与可复现
    checks.append(("约束 C01–C15 全部通过", not errors, f"{len(errors)} 项失败"))
    checks.append(("同输入两次运行结果一致", not check_determinism(
        fingerprint(result.assignments, result.batches, result.order_delivery,
                    result.release_plan),
        fingerprint(second.assignments, second.batches, second.order_delivery,
                    second.release_plan),
    ), ""))

    # 9. 每张工单都能给出四段理由
    reasons = {r.work_order_no: r for r in result.reasons}
    complete = all(
        reasons.get(a.work_order_no)
        and reasons[a.work_order_no].machine_reason
        and reasons[a.work_order_no].time_reason
        and reasons[a.work_order_no].batch_reason
        and reasons[a.work_order_no].mold_reason
        for a in result.assignments
    )
    checks.append(("每张工单都有完整四段排程理由（§4.5）", complete, ""))

    # 10. 可释放数量与工单一一对应
    checks.append((
        "可释放数量每条排程恰好一行（§4.4）",
        len(result.release_plan) == len(result.assignments),
        f"{len(result.release_plan)} vs {len(result.assignments)}",
    ))

    for name, ok, detail in checks:
        suffix = f"  （{detail}）" if detail else ""
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{suffix}")

    failed = sum(1 for _, ok, _ in checks if not ok)
    print()
    print(f"通过 {len(checks) - failed}/{len(checks)}")
    if errors:
        print("约束失败：")
        for e in errors[:10]:
            print("  -", e)
    return 1 if failed or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
