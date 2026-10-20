"""批次形成：在同一销售订单内，用同一副模具覆盖多个牌型。"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.models import Batch, Mold, MoldSet, WorkOrder
from engine.capacity import cycles_for


@dataclass
class _OpenBatch:
    sales_order_no: str
    orders: list[WorkOrder] = field(default_factory=list)
    patterns: set[str] = field(default_factory=set)


def _best_mold_for(patterns: set[str], molds: list[Mold]) -> Mold | None:
    """在给定模具列表中找覆盖全部牌型、优先级最小的模具。"""
    for m in molds:  # 已按 (priority, mold_no) 排序
        if m.covers_all(patterns):
            return m
    return None


def build_batches(
    orders: list[WorkOrder],
    mold_set: MoldSet,
    group_no: int,
    allow_cross_sales_order: bool,
    batch_seq_start: int = 1,
) -> list[Batch]:
    """把分配给同一组的工单切分为批次。

    orders 必须已按全局优先级排序；本函数保持该顺序。
    """
    molds = mold_set.available_molds
    open_batches: list[_OpenBatch] = []
    batches: list[Batch] = []
    seq = batch_seq_start

    for wo in orders:
        target: _OpenBatch | None = None
        for ob in open_batches:
            if not allow_cross_sales_order and ob.sales_order_no != wo.sales_order_no:
                continue
            if wo.pattern_code in ob.patterns:
                continue
            if _best_mold_for(ob.patterns | {wo.pattern_code}, molds) is None:
                continue
            target = ob
            break

        if target is None:
            target = _OpenBatch(sales_order_no=wo.sales_order_no)
            open_batches.append(target)
        target.orders.append(wo)
        target.patterns.add(wo.pattern_code)

    for ob in open_batches:
        covering = [m for m in molds if m.covers_all(ob.patterns)]
        if not covering:
            continue
        cycles_by_mold = {
            m.mold_no: max(
                cycles_for(w.plan_qty, m.ratios[w.pattern_code]) for w in ob.orders
            )
            for m in covering
        }
        best = covering[0]  # 已按 (priority, mold_no) 排序
        batches.append(
            Batch(
                batch_no=f"B{seq:05d}",
                sales_order_no=ob.sales_order_no,
                group_no=group_no,
                set_no=mold_set.set_no,
                mold_no=best.mold_no,
                press_no="",  # 排布阶段填写
                work_order_nos=[w.work_order_no for w in ob.orders],
                patterns=set(ob.patterns),
                cycles=cycles_by_mold[best.mold_no],
                cycles_by_mold=cycles_by_mold,
            )
        )
        seq += 1

    # 批次按交期顺序排入时间轴
    order_index = {w.work_order_no: i for i, w in enumerate(orders)}
    batches.sort(
        key=lambda b: (
            min(order_index[n] for n in b.work_order_nos),
            b.sales_order_no,
            b.batch_no,
        )
    )
    return batches
