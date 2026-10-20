"""§21.2 稳定性测试：对输入做小幅扰动，统计结果变化的工单比例。

设计目标（§16.2）是「输入小幅变化时，仅受影响的工单/批次可能移动；不因单个
工单变化重排全部」。本文件量化这一性质，并把「哪一类变化」分开统计：

- **资源与交期层面**（换模具套、换机台、延期与否翻转、组数决策）：这些是计划
  的业务含义所在，必须稳定，本文件对其设断言。
- **开工槽位层面**：串行排产下天然存在「传送带效应」——同机台上某一批变大，
  其后的批次整体顺移。设计文档 §16.2 已指出，抑制它需要把上一次结果作为
  tie-break 末位，而那会让结果依赖历史、与「全量重算」原则冲突，故默认关闭。
  因此槽位联动只作报告，不作断言。

用法： py tests/stability.py
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.scheduler import Engine  # noqa: E402
from tests.golden import build  # noqa: E402
from verify.constraint_checker import check_all, fingerprint, summarize  # noqa: E402

# 业务含义层面的变化比例上限
MAX_LATE_FLIP_RATIO = 0.10   # 延期与否翻转的工单比例
MAX_SET_CHANGE_RATIO = 0.50  # 换模具套的工单比例
MAX_PRESS_CHANGE_RATIO = 0.60  # 换机台的工单比例


def run(params, product_types, mold_sets, presses, calendar_days, orders):
    return Engine(params, product_types, mold_sets, presses, calendar_days, orders).run()


def compare(base, other) -> dict:
    """逐项统计结果变化，分母为两次都排入的工单数。"""
    b = {a.work_order_no: a for a in base.assignments}
    o = {a.work_order_no: a for a in other.assignments}
    common = sorted(set(b) & set(o))
    n = len(common) or 1
    return {
        "press": sum(1 for k in common if b[k].press_no != o[k].press_no) / n,
        "start": sum(1 for k in common if b[k].start_slot != o[k].start_slot) / n,
        "set": sum(1 for k in common if b[k].set_no != o[k].set_no) / n,
        "delay": sum(1 for k in common if b[k].delay_days != o[k].delay_days) / n,
        "late_flip": sum(1 for k in common if b[k].delayed != o[k].delayed) / n,
        "slot_shift": (
            sum(abs(b[k].start_slot - o[k].start_slot) for k in common) / n
        ),
    }


def groups_signature(result) -> dict:
    """实际开动组数（clamp 之后的决策值）。

    不断言 groups_needed：它只是 §9.3 的估算输入，会被 clamp 到 max_groups，
    估算值的小幅浮动不改变实际决策。真正要稳定的是 clamp 后的组数。
    """
    return {
        t: (d.groups_used, d.max_groups)
        for t, d in sorted(result.diagnostics.items())
    }


def groups_estimate(result) -> dict:
    return {t: d.groups_needed for t, d in sorted(result.diagnostics.items())}


def hash_across_processes(seeds: tuple[str, ...]) -> dict[str, str]:
    """在各自独立的子进程里、用不同的 PYTHONHASHSEED 跑一遍，收集结果指纹。

    同进程重复调用时哈希种子固定，`set`/`dict` 的遍历顺序两次必然相同，
    因此那种写法根本测不出「引擎是否依赖哈希顺序」。§16.1 要求「所有集合
    遍历前排序」，这条断言才是它的可执行证据。
    """
    probe = Path(__file__).resolve().parent / "hash_probe.py"
    out: dict[str, str] = {}
    for seed in seeds:
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [sys.executable, str(probe)],
            capture_output=True, text=True, env=env, check=False,
        )
        out[seed] = proc.stdout.strip() if proc.returncode == 0 else f"<失败 rc={proc.returncode}>"
    return out


def main() -> int:
    params, product_types, mold_sets, presses, calendar_days, orders = build()
    base = run(params, product_types, mold_sets, presses, calendar_days, orders)

    cases: list[tuple[str, list]] = []
    for factor in (Decimal("1.05"), Decimal("0.95")):
        cases.append(
            (f"全部工单数量 ×{factor}",
             [replace(w, qty=w.qty * factor) for w in orders])
        )
    rng = random.Random(20260105)
    jittered = [
        replace(w, qty=w.qty * (Decimal("1") + Decimal(rng.randint(-5, 5)) / Decimal("100")))
        for w in orders
    ]
    cases.append(("逐单随机扰动 ±5%", jittered))

    checks: list[tuple[str, bool, str]] = []
    print(f"基线下：排程 {len(base.assignments)}  批次 {len(base.batches)}  "
          f"延期 {summarize(base.assignments, base.batches, presses, base.calendar)['delayed_work_orders']}")
    print()
    print(f"{'扰动':<22}{'换机台':>8}{'换模具套':>10}{'延期翻转':>10}{'槽位偏移':>10}")
    for name, perturbed in cases:
        other = run(params, product_types, mold_sets, presses, calendar_days, perturbed)
        m = compare(base, other)
        print(f"{name:<22}{m['press']:>7.1%}{m['set']:>10.1%}"
              f"{m['late_flip']:>10.1%}{m['slot_shift']:>10.1f}")

        checks.append((
            f"{name}：延期与否基本不翻转（≤{MAX_LATE_FLIP_RATIO:.0%}）",
            m["late_flip"] <= MAX_LATE_FLIP_RATIO,
            f"{m['late_flip']:.1%}",
        ))
        checks.append((
            f"{name}：模具套基本稳定（≤{MAX_SET_CHANGE_RATIO:.0%}）",
            m["set"] <= MAX_SET_CHANGE_RATIO,
            f"{m['set']:.1%}",
        ))
        checks.append((
            f"{name}：机台分配基本稳定（≤{MAX_PRESS_CHANGE_RATIO:.0%}）",
            m["press"] <= MAX_PRESS_CHANGE_RATIO,
            f"{m['press']:.1%}",
        ))
        # 组数决策（§16.2 要求步长固定、不跳变）
        checks.append((
            f"{name}：实际开动组数不变",
            groups_signature(base) == groups_signature(other),
            f"实际 {groups_signature(other)}，估算 need {groups_estimate(other)}",
        ))
        # 扰动后仍须满足全部硬约束
        errs = check_all(
            other.assignments, other.batches,
            {w.work_order_no: w for w in perturbed},
            presses, other.calendar, mold_sets, params,
            reported={e.work_order_no for e in other.exceptions},
        )
        checks.append((f"{name}：扰动后约束 C01–C15 仍全部通过", not errs, f"{len(errs)} 项失败"))

    # 可复现：同一扰动输入跑两次结果一致
    other = run(params, product_types, mold_sets, presses, calendar_days, jittered)
    again = run(params, product_types, mold_sets, presses, calendar_days, jittered)
    checks.append((
        "同一扰动输入两次运行结果一致",
        fingerprint(other.assignments, other.batches, other.order_delivery,
                    other.release_plan)
        == fingerprint(again.assignments, again.batches, again.order_delivery,
                       again.release_plan),
        "",
    ))

    # §16.1 确定性：跨进程 + 跨 PYTHONHASHSEED 的指纹必须一致。
    # 上面那条「两次运行一致」是在同一个进程里比的，哈希种子相同，
    # 对「遍历顺序是否影响结果」没有证明力。
    seeds = ("0", "1", "12345")
    hashes = hash_across_processes(seeds)
    distinct = set(hashes.values())
    checks.append((
        f"跨进程 + 不同 PYTHONHASHSEED（{len(seeds)} 个）结果指纹一致",
        len(distinct) == 1 and "<失败" not in next(iter(distinct)),
        " / ".join(f"{s}:{h[:12]}" for s, h in hashes.items()),
    ))

    print()
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  （{detail}）" if detail else ""))
    failed = sum(1 for _, ok, _ in checks if not ok)
    print()
    print(f"通过 {len(checks) - failed}/{len(checks)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

