"""产能计算：模数、换模折算、工单优先级键。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_CEILING

from domain.models import Params, WorkOrder


def cycles_for(qty: Decimal, ratio: Decimal) -> int:
    """工单模数 = ceil(qty / ratio)，向上取整。"""
    if ratio <= 0:
        raise ValueError("ratio must be positive")
    return int((qty / ratio).to_integral_value(rounding=ROUND_CEILING))


def changeover_cycles(params: Params, capacity_per_shift: int) -> int:
    """换模时长折算为消耗模数。

    用整数运算而非 `hours / shift_hours * capacity`：后者是 int/int 除法、结果为 float，
    会让浮点误差进入排程决策（§16.1 禁止）。
    """
    if params.changeover_hours <= 0:
        return 0
    return -(-(params.changeover_hours * capacity_per_shift) // params.shift_hours)


def target_finish(due: date, params: Params) -> date:
    """热压计划目标完成日 = 交期 - 前置偏移天数。"""
    from datetime import timedelta

    t = due - timedelta(days=params.pre_offset_days)
    return max(t, params.plan_start)


def order_key(wo: WorkOrder, params: Params) -> tuple:
    """全局排序键：交期优先，全部 tie-break 确定性。"""
    return (
        target_finish(wo.due_date, params),
        wo.due_date,
        wo.sales_order_no,
        wo.work_order_no,
    )


def delay_days(finish: date, due: date) -> int:
    return max(0, (finish - due).days)
