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
        # 订单级的 due/finish 取「最早交期 / 最晚完成」，与任何一张工单自身的延期
        # 都未必同源：最晚完成的那张工单可能自己并不延期，于是 apply_attribution
        # 没给它写归因文本。只按工单级 delay_days 挑「最重」的那张，就会挑到一张
        # 文本为空的工单，令订单行 delayed=True 而 reason_text 为空——§4.3 的
        # 「原因说明」形同虚设。故：优先取真正延期最重的工单；一张都没有时，退到
        # 决定订单完成日的那张（它才是这一行延期的直接原因）。
        delayed_items = [p for p in items if p[1].delayed]
        source = (
            max(delayed_items, key=lambda p: (p[1].delay_days, p[0].work_order_no))
            if delayed_items
            else max(items, key=lambda p: (p[1].end_slot, p[0].work_order_no))
        )
        reason_text = source[1].reason_text
        if delay > 0 and not reason_text:
            reason_text = (
                f"本订单应交日期取各工单最早交期 {due}，而最晚完成工单 "
                f"{source[0].work_order_no} 于 {finish} 完成，晚 {delay} 天"
            )
        out.append(
            OrderDelivery(
                sales_order_no=sales_no,
                due_date=due,
                expected_finish_date=finish,
                delayed=delay > 0,
                delay_days=delay,
                bottleneck_resource=source[1].bottleneck_resource,
                reason_code=primary_reason([c for _, a in items for c in a.reason_codes]),
                reason_text=reason_text,
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

    判定顺序为**日历 → 资源上限 → 兜底**。日历排在资源之前：生产日或班次不够不是
    加机台、加模具能解决的，先报出来才有指向性；反之若日历够用而组数已开满，
    那才是资源数量的问题。

    资源分支以「已开满组数上限」为判据，**不用** §9.3 的 `groups_needed`：后者是为
    §9.4 组数迭代算的下界估算（偏乐观），拿它当诊断判据会把「已开满、再加一组确实
    能缓解」的延期误判成非资源原因。§9.4 只在上一组数仍有延期时才加组，因此走到
    「开满且仍延期」即说明组数就是限制。

    禁止输出「产能不足」这类笼统描述，原因必须落到套数、机台数或日历上。
    """
    # 窗口内一个生产日都没有，是本分支里最强的日历情形：加机台、加模具都不能改变它。
    # 必须排在 daily_capacity 判断之前——working_days == 0 时 daily_capacity 按定义
    # 就是 0（它是「总产能 ÷ 生产日数」，分母为 0 时取 0），原判据写成
    # `daily_capacity > 0 and working_days > 0` 才进入，恰好把这一情形排除掉，
    # 于是落到资源分支去报「模具套数不足」。
    if diag.total_cycles > 0 and diag.working_days == 0:
        return "R007", (
            f"产品类型 {wo.product_type_code} 的交期窗口内没有任何生产日"
            f"（需求 {diag.total_cycles} 模），日历生产日/班次不足"
        )

    if diag.daily_capacity > 0:
        needed_days = -(-diag.total_cycles // diag.daily_capacity)
        if needed_days > diag.working_days:
            return "R007", (
                f"产品类型 {wo.product_type_code} 在 {diag.working_days} 个生产日内最多完成 "
                f"{diag.daily_capacity * diag.working_days} 模，需求 {diag.total_cycles} 模，"
                f"日历生产日/班次不足"
            )

    sets_cap = diag.mold_set_count
    # 与 _max_groups 的 `max(1, …)` 下限同口径（§9.2）：A < group_size 时按 §8.2「余数
    # 单独成组」仍可开 1 组，此时 floor(A/group_size) 为 0，拿它比较会让 R006 永不命中，
    # 归因落到兜底行、输出与事实相反的「未开满」（它其实正卡在组数上限上）。
    press_cap = max(1, diag.press_count // group_size)
    saturated = diag.groups_used >= diag.max_groups > 0

    if saturated and diag.max_groups == sets_cap:
        return "R005", (
            f"产品类型 {wo.product_type_code} 仅配 {sets_cap} 套模具，最多开动 "
            f"{diag.max_groups} 组且已开满，模具套数不足"
        )
    if saturated and diag.max_groups == press_cap:
        return "R006", (
            f"产品类型 {wo.product_type_code} 适用机台 {diag.press_count} 台，按每组 "
            f"{group_size} 台最多开动 {diag.max_groups} 组且已开满，设备数量不足"
        )
    return "R005", (
        f"产品类型 {wo.product_type_code} 已开动 {diag.groups_used} 组（上限 "
        f"{diag.max_groups} 组）未开满，工单 {wo.work_order_no} 于 {finish_day} 完成，"
        f"晚于交期 {wo.due_date}，受排程顺序限制"
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
