"""P6 延期判定与瓶颈归因（§13）。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from domain.models import (
    Assignment,
    OrderDelivery,
    TypeDiagnostics,
    WorkOrder,
    primary_reason,
)


def work_order_delay(finish_day: date, due_date: date) -> int:
    """§13.1 延期天数按自然日。"""
    return max(0, (finish_day - due_date).days)


def build_order_delivery(
    assignments: list[Assignment],
    orders: dict[str, WorkOrder],
    calendar,
) -> list[OrderDelivery]:
    """§13.2 订单齐套与延期。

    expected_finish = 该订单最晚工单完成日；应交日期取该订单下所有工单交期的最早值
    （正常输入下一张销售订单只有一个交期，此时即该交期）。

    delay_days 严格按 §13.2 的公式由本行的 expected_finish 与 due_date 相减得出，
    不能改用工单延期天数的最大值——否则同一行里三个字段互相矛盾，客户按表内
    交期与完成日算出来的天数会与 delay_days 不符。
    """
    by_order: dict[str, list[tuple[WorkOrder, Assignment]]] = defaultdict(list)
    for a in assignments:
        wo = orders.get(a.work_order_no)
        if wo is not None:
            by_order[a.sales_order_no].append((wo, a))

    out: list[OrderDelivery] = []
    for sales_no in sorted(by_order):
        items = by_order[sales_no]
        due = min(wo.due_date for wo, _ in items)
        finish = max(calendar.day_of(a.end_slot) for _, a in items)
        delay = work_order_delay(finish, due)
        worst = max(items, key=lambda p: (p[1].delay_days, p[0].work_order_no))
        out.append(
            OrderDelivery(
                sales_order_no=sales_no,
                due_date=due,
                expected_finish_date=finish,
                delayed=delay > 0,
                delay_days=delay,
                bottleneck_resource=worst[1].bottleneck_resource,
                reason_code=primary_reason([c for _, a in items for c in a.reason_codes]),
            )
        )
    return out


def attribute(
    wo: WorkOrder,
    diag: TypeDiagnostics,
    group_size: int,
    finish_day: date,
) -> tuple[str, str]:
    """§13.3 瓶颈归因，取首个命中项。返回 (原因码, 具体原因文本)。

    禁止输出「产能不足」这类笼统描述，原因必须落到套数、机台数或日历上。
    """
    sets_cap = diag.mold_set_count
    press_cap = diag.press_count // group_size
    saturated = diag.groups_used >= diag.max_groups > 0
    short_of_groups = diag.groups_needed > diag.max_groups

    if saturated and short_of_groups and diag.max_groups == sets_cap:
        return "R005", (
            f"产品类型 {wo.product_type_code} 仅有 {sets_cap} 套模具，最多开动 "
            f"{diag.max_groups} 组，需求需 {diag.groups_needed} 组，模具套数不足"
        )
    if saturated and short_of_groups and diag.max_groups == press_cap:
        return "R006", (
            f"产品类型 {wo.product_type_code} 适用机台 {diag.press_count} 台，按每组 "
            f"{group_size} 台最多开动 {diag.max_groups} 组，需求需 {diag.groups_needed} 组，"
            f"设备数量不足"
        )
    if diag.daily_capacity > 0 and diag.working_days > 0:
        needed_days = -(-diag.total_cycles // diag.daily_capacity)
        if needed_days > diag.working_days:
            return "R007", (
                f"产品类型 {wo.product_type_code} 在 {diag.working_days} 个生产日内最多完成 "
                f"{diag.daily_capacity * diag.working_days} 模，需求 {diag.total_cycles} 模，"
                f"日历生产日/班次不足"
            )
    return "R005", (
        f"产品类型 {wo.product_type_code} 已开满 {diag.groups_used} 组（上限 "
        f"{diag.max_groups} 组），工单 {wo.work_order_no} 于 {finish_day} 完成，"
        f"晚于交期 {wo.due_date}，受模具产能限制"
    )


def apply_attribution(
    assignments: list[Assignment],
    orders: dict[str, WorkOrder],
    diagnostics: dict[str, TypeDiagnostics],
    calendar,
    group_size: int,
) -> None:
    """就地写入延期工单的瓶颈归因。"""
    for a in assignments:
        if not a.delayed:
            continue
        wo = orders.get(a.work_order_no)
        diag = diagnostics.get(a.product_type_code)
        if wo is None or diag is None:
            continue
        code, text = attribute(wo, diag, group_size, calendar.day_of(a.end_slot))
        a.reason_codes.append(code)
        a.bottleneck_resource = text
        a.reason_text = text
