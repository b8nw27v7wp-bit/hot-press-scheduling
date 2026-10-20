"""约束校验器：独立于求解器，逐条检查硬约束（C01–C14）。

刻意不复用 engine.capacity 的计算函数：校验器若与求解器共用同一份公式，
公式本身写错时两边会一起错，校验就失去意义。此处按设计文档 §17 重新实现。
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING

from domain.models import (
    Assignment,
    Batch,
    Mold,
    MoldSet,
    Params,
    Press,
    ResourceStatus,
    WorkOrder,
    WorkOrderStatus,
)
from engine.calendar import Calendar


def _ceil_cycles(qty: Decimal, ratio: Decimal) -> int:
    """工单模数 = ceil(qty / ratio)。"""
    return int((qty / ratio).to_integral_value(rounding=ROUND_CEILING))


def _configured_changeover(params: Params, capacity_per_shift: int) -> int:
    return math.ceil(params.changeover_hours / params.shift_hours * capacity_per_shift)


class _Context:
    """一次校验所需的全部索引。"""

    def __init__(
        self,
        assignments: list[Assignment],
        batches: list[Batch],
        orders: dict[str, WorkOrder],
        presses: dict[str, Press],
        calendar: Calendar,
        mold_sets: dict[str, MoldSet],
        params: Params,
    ):
        self.assignments = assignments
        self.batches = batches
        self.orders = orders
        self.presses = presses
        self.calendar = calendar
        self.mold_sets = mold_sets
        self.params = params
        self.mold_index: dict[str, tuple[MoldSet, Mold]] = {
            m.mold_no: (s, m) for s in mold_sets.values() for m in s.molds
        }


def check_all(
    assignments: list[Assignment],
    batches: list[Batch],
    orders: dict[str, WorkOrder],
    presses: dict[str, Press],
    calendar: Calendar,
    mold_sets: dict[str, MoldSet],
    params: Params,
    reported: set[str] | None = None,
) -> list[str]:
    """reported 为已明确报异常的工单号，不计入「漏排」。"""
    ctx = _Context(assignments, batches, orders, presses, calendar, mold_sets, params)
    errors: list[str] = []
    errors += _check_press_no_overlap(ctx)          # C01
    errors += _check_mold_serialization(ctx)        # C02
    errors += _check_within_calendar(ctx)           # C03
    errors += _check_press_product_type(ctx)        # C04
    errors += _check_mold_belongs_to_type(ctx)      # C05
    errors += _check_set_single_group(ctx)          # C06
    errors += _check_batch_cycles(ctx)              # C07
    errors += _check_changeover(ctx)                # C08
    errors += _check_no_split(ctx, reported or set())  # C09 / C10
    errors += _check_terminal_orders_absent(ctx)    # C11
    errors += _check_batch_single_sales_order(ctx)  # C12
    errors += _check_quantity(ctx)                  # C13
    errors += _check_tables_consistent(ctx)         # C15
    return errors


# ---------------- C01 ----------------

def _check_press_no_overlap(ctx: _Context) -> list[str]:
    """同一机台上的批次区间不得重叠（同批次内多工单共享区间是正常的）。"""
    by_press = defaultdict(list)
    for b in ctx.batches:
        if b.start_slot < 0:
            continue
        by_press[b.press_no].append((b.start_slot, b.end_slot, b.batch_no))
    errs = []
    for press_no, items in by_press.items():
        items.sort()
        for (s1, e1, b1), (s2, e2, b2) in zip(items, items[1:]):
            if s2 <= e1:
                errs.append(f"C01 机台 {press_no} 批次重叠：{b1}({s1}-{e1}) 与 {b2}({s2}-{e2})")
    return errs


# ---------------- C02 ----------------

def _check_mold_serialization(ctx: _Context) -> list[str]:
    by_mold = defaultdict(list)
    for b in ctx.batches:
        if b.start_slot < 0:
            continue
        by_mold[b.mold_no].append((b.start_slot, b.end_slot, b.batch_no))
    errs = []
    for mold_no, items in by_mold.items():
        items.sort()
        for (s1, e1, b1), (s2, e2, b2) in zip(items, items[1:]):
            if s2 <= e1:
                errs.append(f"C02 模具 {mold_no} 时间重叠：{b1}({s1}-{e1}) 与 {b2}({s2}-{e2})")
    return errs


# ---------------- C03 ----------------

def _check_within_calendar(ctx: _Context) -> list[str]:
    errs = []
    n = len(ctx.calendar)
    for a in ctx.assignments:
        if a.start_slot < 0 or a.end_slot >= n or a.start_slot > a.end_slot:
            errs.append(f"C03 工单 {a.work_order_no} 越出日历范围 {a.start_slot}-{a.end_slot}")
    return errs


# ---------------- C04 ----------------

def _check_press_product_type(ctx: _Context) -> list[str]:
    errs = []
    for a in ctx.assignments:
        p = ctx.presses.get(a.press_no)
        if p is None:
            errs.append(f"C04 工单 {a.work_order_no} 引用了不存在的机台 {a.press_no}")
        elif p.status is not ResourceStatus.AVAILABLE:
            errs.append(f"C04 工单 {a.work_order_no} 排到非可用机台 {a.press_no}（{p.status.value}）")
        elif p.product_type_code != a.product_type_code:
            errs.append(
                f"C04 工单 {a.work_order_no} 排到不适用的机台 {a.press_no}"
                f"（{p.product_type_code} != {a.product_type_code}）"
            )
    return errs


# ---------------- C05 ----------------

def _check_mold_belongs_to_type(ctx: _Context) -> list[str]:
    """模具必须存在、可用，且属于该工单产品类型下的模具套。"""
    errs = []
    for a in ctx.assignments:
        entry = ctx.mold_index.get(a.mold_no)
        if entry is None:
            errs.append(f"C05 工单 {a.work_order_no} 引用了不存在的模具 {a.mold_no}")
            continue
        mold_set, mold = entry
        if mold.status is not ResourceStatus.AVAILABLE:
            errs.append(f"C05 工单 {a.work_order_no} 使用了非可用模具 {a.mold_no}（{mold.status.value}）")
        if mold_set.set_no != a.set_no:
            errs.append(
                f"C05 工单 {a.work_order_no} 的模具 {a.mold_no} 不属于模具套 {a.set_no}"
            )
        if mold_set.product_type_code != a.product_type_code:
            errs.append(
                f"C05 工单 {a.work_order_no} 使用了产品类型 {mold_set.product_type_code} 的模具套 "
                f"{a.set_no}，与该工单类型 {a.product_type_code} 不符"
            )
        if not mold.covers(a.pattern_code):
            errs.append(
                f"C05 模具 {a.mold_no} 不覆盖牌型 {a.pattern_code}（工单 {a.work_order_no}）"
            )
    return errs


# ---------------- C06 ----------------

def _check_set_single_group(ctx: _Context) -> list[str]:
    """同一套模具同一时刻只能被一组使用。"""
    by_set = defaultdict(list)
    for b in ctx.batches:
        if b.start_slot < 0:
            continue
        by_set[b.set_no].append((b.start_slot, b.end_slot, b.group_no, b.batch_no))
    errs = []
    for set_no, items in by_set.items():
        items.sort()
        for (s1, e1, g1, b1), (s2, e2, g2, b2) in zip(items, items[1:]):
            if g1 != g2 and s2 <= e1:
                errs.append(
                    f"C06 模具套 {set_no} 同时被组 {g1}（{b1}）与组 {g2}（{b2}）占用"
                    f"：{s1}-{e1} 与 {s2}-{e2}"
                )
    return errs


# ---------------- C07 ----------------

def _check_batch_cycles(ctx: _Context) -> list[str]:
    """以完整模具生产：批次模数 = max 工单模数。"""
    errs = []
    for b in ctx.batches:
        entry = ctx.mold_index.get(b.mold_no)
        if entry is None:
            errs.append(f"C07 批次 {b.batch_no} 引用了不存在的模具 {b.mold_no}")
            continue
        _, mold = entry
        expected = 0
        for no in b.work_order_nos:
            wo = ctx.orders.get(no)
            if wo is None:
                continue
            ratio = mold.ratios.get(wo.pattern_code)
            if ratio is None:
                errs.append(
                    f"C07 批次 {b.batch_no} 的模具 {b.mold_no} 不覆盖工单 {no} 的牌型 "
                    f"{wo.pattern_code}"
                )
                continue
            expected = max(expected, _ceil_cycles(wo.plan_qty, ratio))
        if b.cycles != expected:
            errs.append(
                f"C07 批次 {b.batch_no} 模数 {b.cycles} 与工单最大模数 {expected} 不符"
            )
    return errs


# ---------------- C08 ----------------

def _check_changeover(ctx: _Context) -> list[str]:
    """不同模具之间换模消耗 ≥ 配置值；同模具连续生产不换模。"""
    errs = []
    by_press = defaultdict(list)
    for b in ctx.batches:
        if b.start_slot < 0:
            continue
        by_press[b.press_no].append(b)
    for press_no, items in by_press.items():
        items.sort(key=lambda b: b.start_slot)
        press = ctx.presses.get(press_no)
        if press is None:
            continue
        required = _configured_changeover(ctx.params, press.capacity_per_shift)
        prev: Batch | None = None
        for b in items:
            if prev is None:
                # 机台在计划期内首次装模不是「更换」，不应计换模（§12.5）
                if b.changeover_cycles != 0:
                    errs.append(
                        f"C08 机台 {press_no} 的首个批次 {b.batch_no} 无前序模具，"
                        f"却计了 {b.changeover_cycles} 模换模"
                    )
            elif prev.mold_no == b.mold_no:
                if b.changeover_cycles != 0:
                    errs.append(
                        f"C08 机台 {press_no} 批次 {prev.batch_no}→{b.batch_no} 使用同一模具 "
                        f"{b.mold_no}，却计了 {b.changeover_cycles} 模换模"
                    )
            elif b.changeover_cycles < required:
                errs.append(
                    f"C08 机台 {press_no} 批次 {prev.batch_no}→{b.batch_no} 换模"
                    f"（{prev.mold_no}→{b.mold_no}）消耗 {b.changeover_cycles} 模，"
                    f"少于配置值 {required} 模"
                )
            prev = b
    return errs


# ---------------- C09 / C10 ----------------

def _check_no_split(ctx: _Context, reported: set[str]) -> list[str]:
    seen = defaultdict(int)
    for a in ctx.assignments:
        seen[a.work_order_no] += 1
    errs = [f"C09 工单 {no} 被拆分为 {c} 条记录" for no, c in seen.items() if c > 1]
    scheduled = set(seen)
    expected = {no for no, w in ctx.orders.items() if w.is_schedulable}
    for no in sorted(expected - scheduled - reported):
        errs.append(f"C10 有效工单 {no} 未出现在结果中")
    for no in sorted(scheduled - expected):
        errs.append(f"C10 结果包含不应排程的工单 {no}")
    return errs


# ---------------- C11 ----------------

def _check_terminal_orders_absent(ctx: _Context) -> list[str]:
    errs = []
    terminal = (WorkOrderStatus.COMPLETED, WorkOrderStatus.CANCELLED)
    for a in ctx.assignments:
        wo = ctx.orders.get(a.work_order_no)
        if wo is not None and wo.status in terminal:
            errs.append(f"C11 工单 {a.work_order_no} 状态为 {wo.status.value}，不应出现在结果中")
    for b in ctx.batches:
        for no in b.work_order_nos:
            wo = ctx.orders.get(no)
            if wo is not None and wo.status in terminal:
                errs.append(f"C11 批次 {b.batch_no} 含 {wo.status.value} 工单 {no}")
    return errs


# ---------------- C12 ----------------

def _check_batch_single_sales_order(ctx: _Context) -> list[str]:
    if ctx.params.allow_cross_sales_order_batch:
        return []
    errs = []
    for b in ctx.batches:
        sales = {ctx.orders[no].sales_order_no for no in b.work_order_nos if no in ctx.orders}
        if len(sales) > 1:
            errs.append(f"C12 批次 {b.batch_no} 跨销售订单：{sorted(sales)}")
        elif sales and b.sales_order_no not in sales:
            errs.append(
                f"C12 批次 {b.batch_no} 记录的销售订单 {b.sales_order_no} 与工单不符 {sorted(sales)}"
            )
    return errs


# ---------------- C13 ----------------

def _check_quantity(ctx: _Context) -> list[str]:
    """每张工单 cycles × ratio ≥ qty，且不超过 qty + ratio（即向上取整的紧界）。"""
    errs = []
    for a in ctx.assignments:
        entry = ctx.mold_index.get(a.mold_no)
        if entry is None:
            continue  # C05 已报
        _, mold = entry
        ratio = mold.ratios.get(a.pattern_code)
        if ratio is None:
            continue  # C05 已报
        produced = Decimal(a.cycles) * ratio
        if produced < a.plan_qty:
            errs.append(
                f"C13 工单 {a.work_order_no} 模数不足：{a.cycles} 模 × {ratio} = {produced} "
                f"< 需求 {a.plan_qty}"
            )
        elif produced - a.plan_qty >= ratio:
            errs.append(
                f"C13 工单 {a.work_order_no} 模数过剩：{a.cycles} 模 × {ratio} = {produced}，"
                f"需求 {a.plan_qty}，多出 {produced - a.plan_qty} ≥ 单模产量 {ratio}"
            )
    return errs


# ---------------- C15 ----------------

def _check_tables_consistent(ctx: _Context) -> list[str]:
    """schedule_result 与 batch_result 必须互相引用得上。

    C01/C02/C06/C07/C08 全部只遍历 batches：若批次表被整体丢失或 batch_no 被写坏，
    这些检查会全部空跑，结果被判为有效。因此必须显式校验两张表的交叉一致性。
    """
    errs: list[str] = []
    # 批号必须全局唯一（§11.6）。两套计数错位会静默产生重复批号，
    # 若不显式检查，只会以「工单清单与排程结果不符」这种费解的形式暴露出来。
    seen_no: dict[str, int] = defaultdict(int)
    for b in ctx.batches:
        seen_no[b.batch_no] += 1
    for no, cnt in sorted(seen_no.items()):
        if cnt > 1:
            errs.append(f"C15 批号 {no} 重复出现 {cnt} 次，批号必须全局唯一")

    batch_nos = {b.batch_no for b in ctx.batches}
    scheduled: set[str] = set()
    assigned: dict[str, list[str]] = defaultdict(list)
    for a in ctx.assignments:
        scheduled.add(a.work_order_no)
        assigned[a.batch_no].append(a.work_order_no)
        if a.batch_no not in batch_nos:
            errs.append(
                f"C15 工单 {a.work_order_no} 引用了不存在的批次 {a.batch_no}"
            )
    if ctx.assignments and not ctx.batches:
        errs.append(
            f"C15 有 {len(ctx.assignments)} 条排程结果但批次表为空，两张输出表不一致"
        )
    for b in ctx.batches:
        if not b.work_order_nos:
            errs.append(f"C15 批次 {b.batch_no} 未包含任何工单")
        for no in b.work_order_nos:
            if no not in scheduled:
                errs.append(f"C15 批次 {b.batch_no} 的工单 {no} 未出现在排程结果中")
        listed = set(b.work_order_nos)
        actual = set(assigned.get(b.batch_no, []))
        if listed != actual:
            errs.append(
                f"C15 批次 {b.batch_no} 的工单清单与排程结果不符："
                f"批次记录 {sorted(listed)}，结果记录 {sorted(actual)}"
            )
    return errs


# ---------------- C14 ----------------

def fingerprint(
    assignments: list[Assignment],
    batches: list[Batch],
    order_delivery: list | None = None,
    release_plan: list | None = None,
) -> str:
    """结果指纹，用于「相同输入两次运行输出一致」的复现校验（C14）。

    覆盖排程决策与由其派生的两张输出表；理由文本是前者的纯函数，不重复计入。
    """
    h = hashlib.sha256()
    for a in sorted(assignments, key=lambda x: x.work_order_no):
        h.update(
            f"{a.work_order_no}|{a.press_no}|{a.mold_no}|{a.batch_no}|"
            f"{a.cycles}|{a.start_slot}|{a.end_slot}|{a.delay_days}\n".encode()
        )
    for b in sorted(batches, key=lambda x: x.batch_no):
        h.update(
            f"{b.batch_no}|{b.press_no}|{b.mold_no}|{b.start_slot}|{b.end_slot}|"
            f"{b.cycles}|{','.join(sorted(b.work_order_nos))}\n".encode()
        )
    for o in sorted(order_delivery or [], key=lambda x: x.sales_order_no):
        h.update(
            f"{o.sales_order_no}|{o.due_date}|{o.expected_finish_date}|"
            f"{o.delay_days}|{o.reason_code}\n".encode()
        )
    for r in sorted(release_plan or [], key=lambda x: (x.batch_no, x.pattern_code)):
        h.update(
            f"{r.batch_no}|{r.pattern_code}|{r.release_qty}|"
            f"{r.release_date}|{r.release_shift}\n".encode()
        )
    return h.hexdigest()


def check_determinism(first: str, second: str) -> list[str]:
    if first != second:
        return [f"C14 两次运行结果不一致：{first[:16]}… != {second[:16]}…"]
    return []


# ---------------- 统计 ----------------

def summarize(
    assignments: list[Assignment],
    batches: list[Batch],
    presses: dict[str, Press],
    calendar: Calendar,
) -> dict:
    """§17 要求的统计口径。订单级延期由 §4.3 order_delivery 单独给出。"""
    n_slots = len(calendar)
    available = sum(
        p.capacity_per_shift * n_slots
        for p in presses.values()
        if p.status is ResourceStatus.AVAILABLE
    )
    used = sum(b.cycles + b.changeover_cycles for b in batches)
    changeovers = sum(1 for b in batches if b.changeover_cycles > 0)
    surplus = sum((v for b in batches for v in b.surplus.values()), Decimal(0))
    return {
        "delayed_work_orders": sum(1 for a in assignments if a.delayed),
        "total_delay_days": sum(a.delay_days for a in assignments),
        "press_utilization": (used / available) if available else 0.0,
        "used_cycles": used,
        "available_cycles": available,
        "changeover_count": changeovers,
        "surplus_qty": surplus,
        "scheduled_work_orders": len(assignments),
        "batch_count": len(batches),
    }
