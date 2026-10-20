"""§14 排程理由生成：每张工单四段结构化理由。

要求「每个决策都能落到一条规则与一组数据上」，因此文本里出现的每一个数字
都取自本次运行的实际输入与结果，不做概括性描述。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from domain.models import (
    Assignment,
    Batch,
    Mold,
    MoldSet,
    Params,
    Press,
    ReleasePlan,
    ScheduleReason,
    TypeDiagnostics,
    WorkOrder,
)
from engine.calendar import Calendar
from engine.capacity import target_finish
from engine.scheduling import PlacementDecision


def _slot_text(calendar: Calendar, index: int) -> str:
    if index < 0 or index >= len(calendar):
        # 日历未覆盖到该位置。绝不能让它抛 IndexError——理由生成崩溃会掀翻整个运行。
        return f"日历范围外的槽位 {index}"
    slot = calendar.slot(index)
    return f"{slot.day} 第 {slot.shift} 班"


def machine_reason(
    wo: WorkOrder,
    decision: PlacementDecision,
    group_no: int,
    group_presses: list[Press],
    calendar: Calendar,
) -> str:
    """§14.1 机台理由：适用类型匹配 + 同组绑定 + 组内最早可用。"""
    peers = "、".join(p.press_no for p in sorted(group_presses, key=lambda p: p.press_no))
    chosen = decision.press.press.press_no
    others = [
        f"{no} 最早可于 {_slot_text(calendar, start)} 开工"
        for no, start in decision.press_options
        if no != chosen
    ]
    detail = f"；组内其他机台：{'；'.join(others)}" if others else "（组内仅此一台机）"
    return (
        f"本工单属于产品类型 {wo.product_type_code}，可用机台为 {peers}（同属第 {group_no + 1} 组，"
        f"共用模具套 {decision.mold.set_no}）。按「开始槽位最早」比较，{chosen} 于 "
        f"{_slot_text(calendar, decision.placement.start)} 最早可用，故分配 {chosen}{detail}。"
    )


def time_reason(
    wo: WorkOrder,
    decision: PlacementDecision,
    batch_no: str,
    batch_due: date,
    own_cycles: int,
    own_finish_slot: int,
    params: Params,
    calendar: Calendar,
    target_day: date,
    within_window: bool,
) -> str:
    """§14.2 时间理由：交期倒推、扣换模、按班次。

    窗口判据用的是批次最早交期（R001/R002 按批次判），本工单自身的目标完成日
    可能与之不同，因此两者都要写明，否则同一句里会出现两个互相矛盾的日期。
    窗口用日期而非槽位表达——目标日可能落在日历之外，槽位下标并不总存在。
    """
    cap = decision.press.press.capacity_per_shift
    chg = decision.placement.changeover
    chg_text = (
        f"，其中换模占用 {chg} 模"
        f"（{params.changeover_hours} 小时 ÷ {params.shift_hours} 小时 × {cap} 模/班 向上取整）"
        if chg
        else "，机台未装模或沿用同模具，不计换模"
    )
    window = (
        f"落在该窗口内（截止 {target_day}）"
        if within_window
        else f"晚于该窗口截止 {target_day}，记 R002 预警，但不空等"
    )
    batch_note = (
        f"（批次 {batch_no} 合计 {decision.cycles} 模，至 "
        f"{_slot_text(calendar, decision.placement.end)} 结束）"
        if own_cycles != decision.cycles
        else ""
    )
    return (
        f"客户交期 {wo.due_date}，前置偏移 {params.pre_offset_days} 天，本工单目标完成日 "
        f"{target_finish(wo.due_date, params)}；本工单 {own_cycles} 模，"
        f"{decision.press.press.press_no} 每班 {cap} 模{chg_text}；"
        f"批次 {batch_no} 按批次最早交期 {batch_due} 定的前置窗口，"
        f"计划自 {_slot_text(calendar, decision.placement.start)} 起排入，"
        f"本工单于 {_slot_text(calendar, own_finish_slot)} 完成{batch_note}，{window}。"
    )


def batch_reason(
    wo: WorkOrder,
    batch: Batch,
    mold: Mold,
    batch_orders: list[WorkOrder],
    calendar: Calendar,
    finish_slot: int,
) -> str:
    """§14.3 批次理由：为什么与这些工单同批。

    batch_orders 必须只含本批次内的工单——列出同组其它工单会让人误判合并范围。
    """
    if len(batch.work_order_nos) == 1:
        # 单工单批次的成因可能是牌型冲突、不同销售订单或该组首张工单，
        # 此处不臆测原因，只陈述可核实的事实。
        return (
            f"本工单单独构成批次 {batch.batch_no}，批次共 {batch.cycles} 模，"
            f"本工单 {wo.pattern_code} 于 {_slot_text(calendar, finish_slot)} 完成。"
        )
    others = "、".join(
        f"{w.work_order_no}（{w.pattern_code}）"
        for w in batch_orders
        if w.work_order_no != wo.work_order_no
    )
    ratios = "，".join(
        f"{p} 配比 {mold.ratios[p]}" for p in sorted(batch.patterns)
    )
    surplus = batch.surplus.get(wo.pattern_code, Decimal(0))
    tail = f"本工单完成后该牌型超产 {surplus}。" if surplus > 0 else "本工单完成时该牌型无超产。"
    # 开启 allow_cross_sales_order_batch 后批次可跨销售订单，此时「同属销售订单」
    # 与事实相反，且同一批次的各工单会各自声称批次属于自己那个订单。
    sales = sorted({w.sales_order_no for w in batch_orders})
    same_order = (
        f"同属销售订单 {wo.sales_order_no}"
        if len(sales) == 1
        else f"分属销售订单 {'、'.join(sales)}（已开启跨销售订单合批）"
    )
    return (
        f"与 {others} {same_order}，牌型互不冲突且可由同一副模具 "
        f"{mold.mold_no} 一次成型（{ratios}），故合并为批次 {batch.batch_no}。"
        f"批次按各工单模数最大值 {batch.cycles} 模运行，本工单 {wo.pattern_code} 于 "
        f"{_slot_text(calendar, finish_slot)} 完成。{tail}"
    )


def mold_reason(
    mold: Mold,
    mold_set: MoldSet,
    diag: TypeDiagnostics | None,
    all_sets: list[MoldSet],
    switched: bool,
    batch_patterns: set[str],
) -> str:
    """§14.4 模具理由：为什么用这套/这副模具。

    优先级是在「能覆盖本批次牌型的模具」中比较的：套内优先级更高的模具可能
    根本不覆盖本批次的牌型，此时用它作基准会得出错误的理由（并误报 R003）。
    """
    enabled = diag.groups_used if diag else 1
    covers = "、".join(sorted(mold.patterns))
    others = sorted(
        (m for m in mold_set.available_molds if m.mold_no != mold.mold_no),
        key=lambda m: (m.priority, m.mold_no),
    )
    alt = (
        "；套内其余可用模具：" + "、".join(f"{m.mold_no}(优先级{m.priority})" for m in others)
        if others
        else "；套内无其他可用模具"
    )
    covering = [m for m in mold_set.available_molds if m.covers_all(batch_patterns)]
    top = covering[0] if covering else mold
    why = (
        f"覆盖本批次牌型的最高优先级模具 {top.mold_no}（优先级 {top.priority}）已被其他机台占用，"
        f"改用次优先模具以保证不空转（R003）"
        if switched
        else f"在覆盖本批次牌型（{'、'.join(sorted(batch_patterns))}）的模具中，"
        f"{mold.mold_no} 优先级 {mold.priority} 最高，故优先使用"
    )
    return (
        f"产品类型 {mold_set.product_type_code} 共 {len(all_sets)} 套模具，本次启用 {enabled} 套，"
        f"本组绑定 {mold_set.set_no}。选用 {mold.mold_no}（{'多点' if len(mold.patterns) > 1 else '单点'}，"
        f"覆盖 {covers}）：{why}{alt}。"
    )


def sensitivity_note(
    wo: WorkOrder,
    assignment: Assignment,
    diag: TypeDiagnostics | None,
    params: Params,
    calendar: Calendar,
) -> str:
    """§14.5 敏感性说明：给出可加资源的方向与量级（不做重跑，只陈述瓶颈事实）。"""
    if not assignment.delayed or diag is None:
        return ""
    finish_day = calendar.day_of(assignment.end_slot)
    parts = [f"本工单延期 {assignment.delay_days} 天（完成 {finish_day}，交期 {wo.due_date}）"]
    if diag.groups_needed > diag.max_groups:
        parts.append(
            f"需求 {diag.groups_needed} 组而当前上限 {diag.max_groups} 组"
            f"（{diag.mold_set_count} 套模具 / {diag.press_count} 台机台）"
        )
    # 只陈述算式与当前取值。不能写「减少前置偏移就提前」——目标完成日 = 交期 -
    # 前置偏移，减少它只会把目标完成日**推后**，且在早于计划起点时被钳到计划起点
    # 而根本不变。方向性的因果一律交给末句的「重跑一次」。
    parts.append(
        f"前置偏移当前 {params.pre_offset_days} 天，本工单目标完成日 "
        f"{target_finish(wo.due_date, params)}（= 交期 - 前置偏移，早于计划起点时取计划起点）"
    )
    return "；".join(parts) + "。精确影响需以调整后的参数重跑一次。"


def build_reason(
    assignment: Assignment,
    wo: WorkOrder,
    batch: Batch,
    mold: Mold,
    mold_set: MoldSet,
    all_sets: list[MoldSet],
    decision: PlacementDecision,
    group_presses: list[Press],
    calendar: Calendar,
    params: Params,
    target_day: date,
    batch_due: date,
    within_window: bool,
    finish_slot: int,
    batch_orders: list[WorkOrder],
    diag: TypeDiagnostics | None,
) -> ScheduleReason:
    return ScheduleReason(
        work_order_no=wo.work_order_no,
        machine_reason=machine_reason(wo, decision, assignment.group_no, group_presses, calendar),
        time_reason=time_reason(
            wo,
            decision,
            batch.batch_no,
            batch_due,
            assignment.cycles,
            finish_slot,
            params,
            calendar,
            target_day,
            within_window,
        ),
        batch_reason=batch_reason(wo, batch, mold, batch_orders, calendar, finish_slot),
        mold_reason=mold_reason(
            mold, mold_set, diag, all_sets, decision.switched, batch.patterns
        ),
        sensitivity_note=sensitivity_note(wo, assignment, diag, params, calendar),
    )


def build_release_plan(
    assignments: list[Assignment],
    orders: dict[str, WorkOrder],
    molds: dict[tuple[str, str], Mold],
    calendar: Calendar,
    batches: list[Batch] | None = None,
) -> list[ReleasePlan]:
    """§4.4 每批热压完成后可交付下游的数量与时间。

    口径取**批次**而非工单：多点模具每一模同时产出全部牌型，整批跑完
    K(B) 模后该牌型的实际产出是 `K(B) × 配比`（§11.4 的 `produced(w)`），
    完成时点即批次结束槽位。若改用该工单自身的 `K(w)`，同一份输出里
    `schedule_result.output_qty`（记 K(B)×配比）与 `batch_result.surplus`
    （报 K(B)×配比 - 需求量）会与释放量互相矛盾，超产部分等于凭空消失，
    下游按释放计划排产就会少拿这一段物料。

    传入 batches 时按批次口径；未传则回落到工单口径（保持旧行为，
    仅用于不掌握批次表的调用方）。

    `molds` 以 **(套, 模具号)** 为键：同一模具号可以出现在两个套里，两处的配比
    未必相同，只按模具号查会取到另一副模具的配比。
    """
    batch_index = {b.batch_no: b for b in (batches or [])}
    out: list[ReleasePlan] = []
    for a in sorted(assignments, key=lambda x: (x.batch_no, x.pattern_code, x.work_order_no)):
        wo = orders.get(a.work_order_no)
        mold = molds.get((a.set_no, a.mold_no))
        if wo is None or mold is None:
            continue
        ratio = mold.ratios.get(a.pattern_code)
        if ratio is None:
            continue
        b = batch_index.get(a.batch_no)
        cycles = b.cycles if b is not None else a.cycles
        slot = calendar.slot(b.end_slot if b is not None else a.end_slot)
        out.append(
            ReleasePlan(
                batch_no=a.batch_no,
                pattern_code=a.pattern_code,
                release_qty=Decimal(cycles) * ratio,
                release_date=slot.day,
                release_shift=slot.shift,
            )
        )
    return out
