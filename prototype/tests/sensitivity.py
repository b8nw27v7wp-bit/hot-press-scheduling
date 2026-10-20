"""参数敏感性扫描：量化 §18.2 待明确事项取值对结果的影响。

不依赖正式模具清单。扫描的是参数的**结构**敏感度，用途有二：
（a）帮现场判断待定参数大致落在什么量级；
（b）真实取值到位后可直接代入已知点，不必重新摸索。

每个扫描点都跑约束校验——任何参数取值都不得让结果违反 C01–C15。

用法： py tests/sensitivity.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark  # noqa: E402
from engine.scheduler import Engine  # noqa: E402
from verify.constraint_checker import check_all, summarize  # noqa: E402

# 5 个固定种子取均值：单种子易受数据巧合影响，见 §24.1 第 4 条的口径说明
SEEDS = (20260105, 7, 99, 1234, 55555)

METRICS = ("延期工单", "总延期天数", "延期订单", "换模次数", "窗口内利用率")


def measure(build_kw: dict | None = None, param_kw: dict | None = None) -> dict:
    """在全部种子上跑一遍，返回各指标的均值与约束失败数。"""
    build_kw = build_kw or {}
    param_kw = param_kw or {}
    acc: dict[str, float] = defaultdict(float)
    failures = 0
    for seed in SEEDS:
        params, ptypes, sets, presses, cal, orders = benchmark.build(seed=seed, **build_kw)
        if param_kw:
            params = replace(params, **param_kw)
        result = Engine(params, ptypes, sets, presses, cal, orders).run()
        failures += len(
            check_all(
                result.assignments,
                result.batches,
                {w.work_order_no: w for w in orders},
                presses,
                result.calendar,
                sets,
                params,
                reported={e.work_order_no for e in result.exceptions},
            )
        )
        s = summarize(
            result.assignments, result.batches, presses, result.calendar, params, orders
        )
        acc["延期工单"] += s["delayed_work_orders"]
        acc["总延期天数"] += s["total_delay_days"]
        acc["延期订单"] += sum(1 for o in result.order_delivery if o.delayed)
        acc["换模次数"] += s["changeover_count"]
        acc["窗口内利用率"] += s["window_utilization"]
    out = {k: acc[k] / len(SEEDS) for k in METRICS}
    out["约束失败"] = failures
    return out


def row(label: str, m: dict) -> str:
    warn = "" if m["约束失败"] == 0 else f" ⚠{m['约束失败']}"
    return (
        f"| {label}{warn} | {m['延期工单']:.0f} | {m['总延期天数']:.0f} | "
        f"{m['延期订单']:.0f} | {m['换模次数']:.0f} | {m['窗口内利用率']:.1%} |"
    )


def table(values, build_of, param_of, fmt=str) -> None:
    print("| 取值 | " + " | ".join(METRICS) + " |")
    print("| --- | " + " | ".join(["---"] * len(METRICS)) + " |")
    for v in values:
        print(row(fmt(v), measure(build_of(v), param_of(v))))


def main() -> int:
    print(f"数据集：1 万工单 / 30 产品类型 / 80 机台（§24 规模测试口径）")
    print(f"种子：{', '.join(map(str, SEEDS))}，下表为 {len(SEEDS)} 个种子的均值")

    print("\n## 1. 前置偏移天数（§18.2 第 5 项；当前取值 15）\n")
    print("> 实测不影响排程与延期指标，只影响 R001/R002 预警判定；")
    table((0, 7, 15, 20, 30, 45), lambda v: {}, lambda v: {"pre_offset_days": v},
          lambda v: f"{v} 天")

    print("\n## 2. 换模时间（§18.2 第 4 项；单位未定）\n")
    print("> 对结果最敏感的单一参数之一，建议优先确认数值与单位（§18.1 A5）。")
    table((0, 1, 2, 4, 8), lambda v: {}, lambda v: {"changeover_hours": v},
          lambda v: f"{v} 小时")

    print("\n## 3. 每班产能（§18.2 第 3 项；绝对取值未定）\n")
    print("> 按比例缩放全部机台产能，各档之间非严格等比（产能取整）。")
    table((0.75, 1.0, 1.25, 1.5), lambda v: {"capacity_scale": v}, lambda v: {},
          lambda v: f"×{v:g}")

    print("\n## 4. 配比离散度 × 套启用策略（§18.2 第 1、2 项）\n")
    print("> 同一类型内各套配比越接近，`DEMAND_CYCLES_FIRST` 相对 `MAX_MOLDS_FIRST`")
    print("> 的优势越小；配比完全一致时两者等价。正式模具清单到位后应对照本表定位。")
    pools = (
        ("窄（80/85/90，±6%）", (80, 85, 90)),
        ("中（60/80/100，±25%）", (60, 80, 100)),
        ("宽（40/60/80/120，3 倍）", (40, 60, 80, 120)),
    )
    print("\n| 配比离散度 | 策略 | " + " | ".join(METRICS) + " |")
    print("| --- | --- | " + " | ".join(["---"] * len(METRICS)) + " |")
    for label, pool in pools:
        for policy in ("MAX_MOLDS_FIRST", "DEMAND_CYCLES_FIRST"):
            m = measure(
                {"ratio_pool": pool},
                {"set_activation_policy": policy},
            )
            print(row(policy, m).replace("| " + policy, f"| {label} | {policy}", 1))

    print("\n> 注：以上均在合成数据上测得。参数取值的**结构**敏感度可迁移，")
    print("> 但绝对数值需用正式模具清单与机台产能表复测（§18.2）。")
    print("> 完整解读见《热压排程参数敏感性分析.md》。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
