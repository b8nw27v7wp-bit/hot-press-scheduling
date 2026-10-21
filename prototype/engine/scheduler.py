"""主引擎：把各阶段串起来。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from time import perf_counter

from domain.models import (
    Assignment,
    Batch,
    CalendarDay,
    ExceptionRecord,
    Mold,
    MoldSet,
    OrderDelivery,
    Params,
    Press,
    ProductType,
    ReleasePlan,
    ScheduleReason,
    TypeDiagnostics,
    WorkOrder,
    WorkOrderStatus,
)
from engine import capacity, delay, explain, molding
from engine.calendar import Calendar
from engine.scheduling import MoldTimeline, PressTimeline, Scheduler

ALGORITHM_VERSION = "0.1.0-prototype"

# 排程地平线在「最晚交期」之后最多外推的天数，防止悲观估算溢出 date 范围
MAX_HORIZON_DAYS = 3650


def validate_params(params: Params) -> None:
    """§20.2「输入结构错误」：参数取值非法时立即失败，不输出半成品结果。

    这些取值不会自然出现，但一旦出现，静默继续比明确失败更危险：
    `group_size` 或 `shift_hours` 为 0 会在 `_max_groups` / `changeover_cycles`
    里 ZeroDivisionError，为负数则不崩、却算出一份看似正常而实际高估产能的排程
    （组宽为负会让分组切片错乱）；`changeover_hours` 取负值会被静默当成「不换模」，
    `pre_offset_days` 取负值会让目标完成日落到交期之后——两者都是往乐观方向偏。
    """
    problems = []
    if params.group_size < 1:
        problems.append(f"group_size={params.group_size}（每组机台数须 ≥ 1）")
    if params.shift_hours < 1:
        problems.append(f"shift_hours={params.shift_hours}（单班时长须 ≥ 1 小时）")
    if params.changeover_hours < 0:
        problems.append(f"changeover_hours={params.changeover_hours}（换模时长不可为负）")
    if params.pre_offset_days < 0:
        problems.append(f"pre_offset_days={params.pre_offset_days}（前置偏移天数不可为负）")
    if params.time_limit_seconds < 0:
        problems.append(f"time_limit_seconds={params.time_limit_seconds}（时间上限不可为负）")
    if problems:
        raise ValueError("参数取值非法：" + "；".join(problems))


@dataclass
class RunResult:
    assignments: list[Assignment]
    batches: list[Batch]
    order_delivery: list[OrderDelivery]
    release_plan: list[ReleasePlan]
    reasons: list[ScheduleReason]
    exceptions: list[ExceptionRecord]
    diagnostics: dict[str, TypeDiagnostics]
    calendar: Calendar
    meta: dict


class Engine:
    def __init__(
        self,
        params: Params,
        product_types: dict[str, ProductType],
        mold_sets: dict[str, MoldSet],
        presses: dict[str, Press],
        calendar_days: dict[date, CalendarDay],
        work_orders: list[WorkOrder],
    ):
        self.params = params
        validate_params(params)
        self.product_types = product_types
        self.mold_sets = mold_sets
        self.presses = presses
        self.work_orders = work_orders
        self.calendar_days = calendar_days

        self.horizon_end = self._compute_horizon()
        self.calendar = Calendar(calendar_days, params, self.horizon_end)
        self.exceptions: list[ExceptionRecord] = []
        self._reasons: list[ScheduleReason] = []
        # 批号唯一计数源。曾出现「内层按创建的批次推进、外层按落位的批次推进」
        # 两套计数，一旦有批次排不进去，后面的类型就会复用已用过的批号。
        self._batch_seq = 1
        # §20.2 的时间预算：由 run() 设置，_schedule_type 在排程过程中读取
        self._t0: float = perf_counter()
        self._truncated = False

    def _compute_horizon(self) -> date:
        """日历必须覆盖到最晚可能完成日，否则延期工单会被误判为不可排。

        取「最晚交期 + 前置偏移」与「按工作量估算的天数」两者的较大值。

        **这个估算不是单侧的悲观界，而是两个反向偏差互相抵消后的启发式**：
        机台侧取「只有一台最慢机台、每天 1 班」（悲观，约等于把真实产能除以
        机台数×每日班次），配比侧取 `max(ratios)`（乐观，按最好的那副模具算模数）。
        两者相除后误差约为「配比离散度 ÷ (机台数 × 每日班次)」：配比离散度在这个
        倍数以内时估算仍是上界，超出时地平线偏短，超出得多的工单会以
        「排程超出日历范围」报 R007——而真正卡住它们的是下面那个十年封顶。
        改成 `min(ratios)` 会让估算变成单侧上界，代价是地平线（在输入日历足够长时）
        拉长到十年，内存与利用率分母随之变大，故保持现状、只把性质写明。
        """
        latest = max(
            (w.due_date for w in self.work_orders if w.due_date), default=self.params.plan_start
        )
        base = latest + timedelta(days=self.params.pre_offset_days + 30)

        total_cycles = 0
        for w in self.work_orders:
            if not w.is_schedulable:
                continue
            ratios = [
                m.ratios[w.pattern_code]
                for s in self.mold_sets.values()
                if s.product_type_code == w.product_type_code
                for m in s.available_molds
                if m.covers(w.pattern_code)
            ]
            if ratios:
                total_cycles += capacity.cycles_for(w.plan_qty, max(ratios))

        caps = [
            p.capacity_per_shift
            for p in self.presses.values()
            if p.status.name == "AVAILABLE"
        ]
        if total_cycles and caps:
            # 机台侧取最悲观：只有一台最慢机台、每天 1 班。
            # 必须封顶：工作量极大时这个外推会溢出 date 范围（OverflowError），
            # 而超过十年仍排不完的计划本身已经没有意义，按 R005/R007 报瓶颈即可。
            pessimistic_days = min(total_cycles // max(1, min(caps)), MAX_HORIZON_DAYS)
            return base + timedelta(days=pessimistic_days + 30)
        return base

    # ---------------- 主流程 ----------------

    def run(self) -> RunResult:
        t0 = perf_counter()
        # §20.2 的时间上限必须在排程过程中生效，不能只在产品类型边界检查：
        # 单个产品类型的工单量可以占满整个预算（实测单类型 3000 单会把 0.05s
        # 的上限跑到 0.4s 以上而 truncated 仍为 False）。
        self._t0 = t0
        self._truncated = False
        # 以下三项在排程过程中只增不减。同一个 Engine 被调用两次（复现校验、参数扫描
        # 里的复用）时必须得到同一结果，所以每次进入都先清空——否则第二次调用的批号
        # 会从 B00002 起，违反 §11.6「从 1 起连续」，异常表与理由表也会叠加。
        self.exceptions = []
        self._reasons = []
        self._batch_seq = 1
        self._validate_resources()
        valid = self._preprocess()

        sets_by_type: dict[str, list[MoldSet]] = defaultdict(list)
        for s in self.mold_sets.values():
            # 无可用模具的套（全在保养、或缺配比）不参与启用顺序。SET_ORDER 会把它排在
            # 首位，而它一旦被选中就占掉一个组名额，却覆盖不到任何牌型 —— 整个类型
            # 的工单会落 R008，即便后面的套完全够用。全部套都不可用时列表为空，
            # 由下面的 `if not sets` 按「无可用模具」报 R008。
            if s.available_molds:
                sets_by_type[s.product_type_code].append(s)
        for lst in sets_by_type.values():
            lst.sort(key=lambda s: (s.set_order, s.set_no))

        presses_by_type: dict[str, list[Press]] = defaultdict(list)
        for p in self.presses.values():
            if p.status.name == "AVAILABLE":
                presses_by_type[p.product_type_code].append(p)
        for lst in presses_by_type.values():
            lst.sort(key=lambda p: p.press_no)

        orders_by_type: dict[str, list[WorkOrder]] = defaultdict(list)
        for w in valid:
            orders_by_type[w.product_type_code].append(w)
        for lst in orders_by_type.values():
            lst.sort(key=lambda w: capacity.order_key(w, self.params))

        assignments: list[Assignment] = []
        batches: list[Batch] = []
        diagnostics: dict[str, TypeDiagnostics] = {}

        # 模具时间轴按**模具号全局**建一条，跨产品类型共享。§5.6 的假设是「一副模具
        # 同一时刻只能装在一台热压机上」，模具号标识的正是那副物理模具，所以它必须
        # 跨类型串行：同一模具号可以出现在两个类型的套里（V10），按类型各建一条会让
        # 两个类型同时占用它，C02 必失败。正常情况下每副模具只属于一个类型的套，
        # 因此与按类型建完全等价。
        mold_tls: dict[str, MoldTimeline] = {
            m.mold_no: MoldTimeline(mold_no=m.mold_no)
            for s in self.mold_sets.values()
            for m in s.available_molds
        }

        for ptype in sorted(orders_by_type):
            orders = orders_by_type[ptype]
            sets = sets_by_type.get(ptype, [])
            presses = presses_by_type.get(ptype, [])

            if not sets:
                for w in orders:
                    self.exceptions.append(
                        ExceptionRecord(w.work_order_no, "R008", f"产品类型 {ptype} 无可用模具")
                    )
                continue
            if not presses:
                for w in orders:
                    self.exceptions.append(
                        ExceptionRecord(w.work_order_no, "R009", f"产品类型 {ptype} 无适用机台")
                    )
                continue

            max_groups = self._max_groups(sets, presses)
            # 启用顺序只依赖 (sets, orders)，与组数无关，故按类型算一次，
            # 组数迭代里只做切片，避免重复评分。
            ordered_sets = self._order_sets(sets, orders)
            g_needed = self._estimate_groups(orders, ordered_sets[0], presses)

            if self._time_exceeded(t0):
                self._truncated = True
                break

            result = None
            g_values = list(range(max(1, min(g_needed, max_groups)), max_groups + 1))
            for idx, g in enumerate(g_values):
                seq_before = self._batch_seq
                # 组数迭代会重跑本类型。模具时间轴是跨类型共享的，重跑前必须把本次
                # 尝试占用的槽位还原，否则下一次尝试会看到并不存在的占用（进而不必要
                # 地判为放不下）。机台时间轴是每次调用新建的，不需要还原。
                mold_snapshot = {no: tl.busy[:] for no, tl in mold_tls.items()}
                result = self._schedule_type(
                    ptype, orders, sets, presses, g, ordered_sets, g_needed, max_groups,
                    mold_tls,
                )
                # 已超时：不再加组重试，采纳本次的部分结果
                if self._truncated:
                    break
                # 只有「还会再试一次」时才回收编号；最后一次尝试的结果是要采纳的，
                # 回收它的编号会让下一个产品类型撞号。
                last_attempt = idx == len(g_values) - 1
                if not any(a.delayed for a in result[0]) or last_attempt:
                    break
                self._batch_seq = seq_before
                for no, busy in mold_snapshot.items():
                    mold_tls[no].busy = busy
            if result is None:
                continue
            type_assignments, type_batches, diag, type_exceptions, type_reasons = result
            assignments.extend(type_assignments)
            batches.extend(type_batches)
            self.exceptions.extend(type_exceptions)
            self._reasons.extend(type_reasons)
            diagnostics[ptype] = diag

        # 超时截断：所有未排入的有效工单必须逐单报异常，不得静默丢弃（§20.2）。
        # 截断可能发生在某个产品类型**内部**，因此不能只按「未排的类型」报——
        # 那样已开排但没排完的类型里的剩余工单会被静默丢掉。
        if self._truncated:
            scheduled = {a.work_order_no for a in assignments}
            # 只跳过**已报过 R007** 的工单（批次落位失败时已逐条报过），不能按
            # 「有任何异常」跳过：交期早于计划起点这类工单本身带一条 R010，若一并
            # 跳过就永远拿不到「未排入」的结论，那条 R010 的文案（「已按最早可行
            # 时间排程」）也会变成假的——它根本没被排程。
            already_unscheduled = {
                e.work_order_no for e in self.exceptions if e.code == "R007"
            }
            for ptype in sorted(orders_by_type):
                for w in orders_by_type[ptype]:
                    if w.work_order_no in scheduled or w.work_order_no in already_unscheduled:
                        continue
                    self.exceptions.append(
                        ExceptionRecord(
                            w.work_order_no,
                            "R007",
                            "计算超出时间上限，该工单未排入（本次为部分结果）",
                            "放宽时间上限或缩小计划范围后重算",
                        )
                    )

        # 无有效工单即无可排计划，不报日历缺失（§15 8.8 空输入返回空结果）
        latest_need = max(
            (capacity.target_finish(w.due_date, self.params) for w in valid), default=None
        )
        for start, end in (self.calendar.missing_ranges(latest_need) if latest_need else []):
            span = (end - start).days + 1
            self.exceptions.append(
                ExceptionRecord(
                    "",
                    "R011",
                    f"{start} 至 {end} 日历缺失，按不生产处理，可用班次减少 {span} 天",
                    "补录该区间班次后重算",
                )
            )

        by_no = {w.work_order_no: w for w in valid}
        delay.apply_attribution(
            assignments, by_no, diagnostics, self.calendar, self.params.group_size
        )
        order_delivery = delay.build_order_delivery(assignments, by_no, self.calendar)
        # 模具的身份是 (套, 模具号)，不是模具号：同一模具号可以出现在两个套里
        # （V10，引擎按模具号跨组串行）。只按模具号建索引会让后写的套覆盖先写的，
        # 于是 release_qty 取到另一副模具的配比，与同一份输出的 output_qty、
        # surplus 自相矛盾。校验器早就是这个双键口径（见 constraint_checker._Context）。
        molds_by_key = {
            (s.set_no, m.mold_no): m for s in self.mold_sets.values() for m in s.molds
        }
        release_plan = explain.build_release_plan(
            assignments, by_no, molds_by_key, self.calendar, batches
        )

        from verify.constraint_checker import fingerprint  # 避免模块级循环依赖

        elapsed = round(perf_counter() - t0, 3)

        return RunResult(
            assignments=assignments,
            batches=batches,
            order_delivery=order_delivery,
            release_plan=release_plan,
            reasons=self._reasons,
            exceptions=self.exceptions,
            diagnostics=diagnostics,
            calendar=self.calendar,
            meta={
                "algorithm_version": ALGORITHM_VERSION,
                "params": {
                    k: (v.isoformat() if isinstance(v, date) else v)
                    for k, v in asdict(self.params).items()
                },
                "horizon_end": self.horizon_end.isoformat(),
                "input_counts": {
                    "work_orders": len(self.work_orders),
                    "schedulable": len(valid),
                    "presses": len(self.presses),
                    "mold_sets": len(self.mold_sets),
                    "product_types": len(self.product_types),
                    "calendar_days": len(self.calendar_days),
                },
                "result_hash": fingerprint(
                    assignments, batches, order_delivery, release_plan
                ),
                "elapsed_seconds": elapsed,
                "truncated": self._truncated,
                "performance_note": self._performance_note(
                    valid, assignments, elapsed, self._truncated
                ),
            },
        )

    def _performance_note(
        self,
        valid: list[WorkOrder],
        assignments: list[Assignment],
        elapsed: float,
        truncated: bool,
    ) -> str:
        """§15 8.10 性能说明：规模显著超预期或超时截断时给出可读结论。"""
        n_slots = len(self.calendar)
        n_molds = sum(len(s.molds) for s in self.mold_sets.values())
        head = (
            f"输入规模：工单 {len(self.work_orders)} 张（有效 {len(valid)}）、"
            f"产品类型 {len(self.product_types)} 个、机台 {len(self.presses)} 台、"
            f"模具 {n_molds} 副、生产槽位 {n_slots} 个；计算用时 {elapsed}s。"
        )
        if truncated:
            limit = self.params.time_limit_seconds
            done = len({a.product_type_code for a in assignments})
            return (
                head
                + f"已超过时间上限 {limit}s，本次只排完 {done} 个产品类型，"
                f"其余工单未排入并已逐条报为异常（R007）。"
                f"建议放宽时间上限、缩小计划范围或增加机台/模具后重算。"
            )
        return head + "未触发时间上限，本次为完整结果。"

    def _expand_coverage(
        self,
        orders: list[WorkOrder],
        presses: list[Press],
        selected: list[MoldSet],
        ordered_all: list[MoldSet],
        max_groups: int,
    ) -> list[MoldSet]:
        """§10.2：按需启用更多模具套，直到覆盖全部待排牌型或触到组数上限。

        启用顺序沿用 §10.1 的策略排序，由调用方传入以保证与首次选套同源；
        每新增一套都需要一组空闲机台。
        """
        needed = {w.pattern_code for w in orders}
        chosen = list(selected)
        chosen_nos = {s.set_no for s in chosen}

        for cand in ordered_all:
            if not needed - {p for s in chosen for m in s.available_molds for p in m.patterns}:
                break
            if cand.set_no in chosen_nos:
                continue
            if not ({p for m in cand.available_molds for p in m.patterns} & needed):
                continue
            # 新增一组需要 group_size 台空闲机台，且组数不得超过上限
            if len(chosen) >= max_groups:
                break
            if (len(chosen) + 1) * self.params.group_size > len(presses):
                break
            chosen.append(cand)
            chosen_nos.add(cand.set_no)
        return chosen

    def _diagnostics(
        self,
        ptype: str,
        orders: list[WorkOrder],
        sets: list[MoldSet],
        presses: list[Press],
        g_needed: int,
        g_used: int,
        selected: list[MoldSet],
    ) -> TypeDiagnostics:
        """汇总单个产品类型的诊断量，供 §13.3 归因与 §14 理由生成使用。

        统计口径必须落在**实际启用的套**上：这些量决定 R005/R006/R007 的归因，
        取错资源会让「无法按期完成的原因」指向错误的瓶颈。
        """
        latest, days, slots = self._window_stats(orders)
        cap = min((p.capacity_per_shift for p in presses), default=0)
        # 组按 group_size 顺序切分机台，末组可能不足 group_size 台，故逐组按实际机台数算
        gs = self.params.group_size
        parallel = sum(
            self._effective_parallel(s, len(presses[i * gs : (i + 1) * gs]))
            for i, s in enumerate(selected)
        )
        return TypeDiagnostics(
            product_type_code=ptype,
            mold_set_count=len(sets),
            press_count=len(presses),
            max_groups=self._max_groups(sets, presses),
            groups_needed=g_needed,
            groups_used=g_used,
            working_days=days,
            latest_target=latest,
            # §9.1 的工作量下界取该类型全部套的模具：只取一套并不构成下界
            total_cycles=self._work_estimate(
                orders, [m for s in sets for m in s.available_molds]
            ),
            daily_capacity=(parallel * cap * slots) // days if days else 0,
        )

    # ---------------- P0 ----------------

    def _time_exceeded(self, t0: float) -> bool:
        limit = self.params.time_limit_seconds
        return limit > 0 and (perf_counter() - t0) > limit

    def _validate_resources(self) -> None:
        """V06/V07/V09：机台、模具套、模具层面的输入校验。

        这些对象不是「某张工单」的问题，而是资料本身有问题，因此异常记录的
        工单号为空，靠 detail 指明具体对象。
        """
        # V06：机台的适用产品类型未定义 → 该机台不参与分配
        for p in sorted(self.presses.values(), key=lambda x: x.press_no):
            if p.product_type_code not in self.product_types:
                self.exceptions.append(
                    ExceptionRecord(
                        "",
                        "R009",
                        f"机台 {p.press_no} 的适用产品类型 {p.product_type_code} 未定义，"
                        f"该机台不参与分配",
                        "补充产品类型映射或修正机台资料",
                    )
                )

        for s in sorted(self.mold_sets.values(), key=lambda x: x.set_no):
            # V09：模具套挂在一个未定义的产品类型上
            if s.product_type_code not in self.product_types:
                self.exceptions.append(
                    ExceptionRecord(
                        "",
                        "R010",
                        f"模具套 {s.set_no} 的产品类型 {s.product_type_code} 未定义，"
                        f"该套模具不可用",
                        "补充产品类型映射",
                    )
                )
            for m in sorted(s.molds, key=lambda x: x.mold_no):
                # V09：模具记录的所属套与它实际所在的套不一致
                if m.set_no != s.set_no:
                    self.exceptions.append(
                        ExceptionRecord(
                            "",
                            "R010",
                            f"模具 {m.mold_no} 记录的模具套 {m.set_no} 与所在套 {s.set_no} 不符",
                            "核对模具资料",
                        )
                    )
                # V07：配比缺失或非正
                if not m.ratios:
                    self.exceptions.append(
                        ExceptionRecord(
                            "",
                            "R010",
                            f"模具 {m.mold_no} 无产出配比，不可用",
                            "补齐该模具的产出配比",
                        )
                    )
                else:
                    bad = sorted(p for p, r in m.ratios.items() if r <= 0)
                    if bad:
                        self.exceptions.append(
                            ExceptionRecord(
                                "",
                                "R010",
                                f"模具 {m.mold_no} 的配比非正：{'、'.join(bad)}，该模具不可用",
                                "修正产出配比",
                            )
                        )

            # 套内重复模具号：模具号标识一副物理模具，套内出现两次会让 cycles_by_mold
            # 与 mold_tls 的键冲突——批次模数取自一副模具、工单模数取自另一副，
            # 选中的配比更低时索引会越过槽位数组末尾。MoldSet.available_molds 已按序
            # 保留优先级最高的一条，这里把资料问题报出来。
            by_no: dict[str, list[Mold]] = defaultdict(list)
            for m in s.molds:
                by_no[m.mold_no].append(m)
            for mold_no, dups in sorted(by_no.items()):
                if len(dups) > 1:
                    self.exceptions.append(
                        ExceptionRecord(
                            "",
                            "R010",
                            f"模具套 {s.set_no} 内模具号 {mold_no} 重复 {len(dups)} 次，"
                            f"已保留优先级最高的一条，其余不参与排程",
                            "核对模具资料：模具号应唯一标识一副物理模具",
                        )
                    )

        # V10：同一模具号出现在多个模具套里。模具号标识一副物理模具，而 §5.6 假设
        # 「一副模具同一时刻只能装在一台热压机上」，所以引擎按模具号全局串行（见
        # _schedule_type 里 mold_tls 的建法），结果不会违反 C02。
        # 仍然报出来：模具资料把同一副模具挂在两个套上，多半是主数据要核对的情形，
        # 客户需要知道产能因此被两个组共享。
        by_mold_no: dict[str, list[str]] = defaultdict(list)
        for s in sorted(self.mold_sets.values(), key=lambda x: x.set_no):
            for m in sorted(s.molds, key=lambda x: x.mold_no):
                by_mold_no[m.mold_no].append(s.set_no)
        for mold_no, set_nos in sorted(by_mold_no.items()):
            if len(set_nos) > 1:
                self.exceptions.append(
                    ExceptionRecord(
                        "",
                        "R010",
                        f"模具 {mold_no} 同时出现在模具套 {'、'.join(set_nos)}，"
                        f"已按「一副模具同一时刻只在一台机台」跨组串行，"
                        f"其产能由这些组共享",
                        "核对模具资料；若该模具确实归属多个套，请确认产能共享符合现场",
                    )
                )

    def _preprocess(self) -> list[WorkOrder]:
        """P0 校验与预处理（§7）。

        注意不能先用 is_schedulable 过滤：它要求 plan_qty > 0，会让「数量非正」
        的工单在到达 R010 分支之前就被静默丢弃，违反需求第八章「必须报告，不得静默跳过」。
        """
        valid: list[WorkOrder] = []
        seen: set[str] = set()
        for w in self.work_orders:
            if w.status in (WorkOrderStatus.COMPLETED, WorkOrderStatus.CANCELLED):
                continue
            if w.work_order_no in seen:
                self.exceptions.append(
                    ExceptionRecord(
                        w.work_order_no, "R010", "工单号重复，保留首条，本条不排程", "核对工单来源"
                    )
                )
                continue
            seen.add(w.work_order_no)
            if not w.pattern_code:
                self.exceptions.append(
                    ExceptionRecord(
                        w.work_order_no, "R010", "牌型缺失，无法匹配模具", "补齐牌型后重算"
                    )
                )
                continue
            if w.due_date is None:
                self.exceptions.append(
                    ExceptionRecord(
                        w.work_order_no, "R010", "交期缺失，不排程", "补齐客户交期后重算"
                    )
                )
                continue
            # V03 的另一半：交期早于计划起点。仍按最早可行时间排程（§5.5），
            # 但必须报出来——需求第八章要求这类数据「标记为异常数据并报告，
            # 不得擅自以默认值替代」，静默 clamp 到计划起点正属该禁止之列。
            if w.due_date < self.params.plan_start:
                self.exceptions.append(
                    ExceptionRecord(
                        w.work_order_no,
                        "R010",
                        f"交期 {w.due_date} 早于计划起点 {self.params.plan_start}，"
                        f"已按最早可行时间排程，该工单为高风险延期",
                        "核对客户交期或计划起点后重算",
                    )
                )
            if w.plan_qty <= 0:
                self.exceptions.append(
                    ExceptionRecord(
                        w.work_order_no,
                        "R010",
                        f"数量非正（排程数量 {w.plan_qty}），不排程",
                        "核对工单数量或剩余数量",
                    )
                )
                continue
            if w.product_type_code not in self.product_types:
                self.exceptions.append(
                    ExceptionRecord(
                        w.work_order_no,
                        "R010",
                        f"产品类型 {w.product_type_code} 未定义，无法映射到模具与机台",
                        "补充产品类型映射",
                    )
                )
                continue
            valid.append(w)
        return valid

    # ---------------- P2 ----------------

    def _max_groups(self, sets: list[MoldSet], presses: list[Press]) -> int:
        """最多可同时开动的组数 = min(可用套数, 机台数 / 每组机台数)。"""
        if not sets or not presses:
            return 0
        return max(1, min(len(sets), len(presses) // self.params.group_size))

    def _effective_parallel(self, mold_set: MoldSet, group_press_count: int) -> int:
        """§5.6 有效并行度 = min(组内**实际**机台数, 套内可用模具副数)。

        必须是实际机台数而不是配置的 group_size：组按 group_size 顺序切分机台，
        末组可能不足 group_size 台（例如 1 台机配 group_size=2 时该组只有 1 台机）。
        用配置值会按两倍产能估算，进而把「机台不够」误归到模具套数上，并把 R007
        文案里的可完成模数写成实际能力的两倍。
        """
        return max(1, min(group_press_count, mold_set.mold_count))

    def _work_estimate(self, orders: list[WorkOrder], molds: list[Mold]) -> int:
        """§9.1 工作量下界：按销售订单分别取「单牌型总模数」的最大值再求和。

        多点模具一次可同时推进多个牌型，机台模数的下界由单牌型总量的最大值决定。
        """
        per_order: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for w in orders:
            best_ratio = max(
                (m.ratios[w.pattern_code] for m in molds if m.covers(w.pattern_code)),
                default=None,
            )
            if best_ratio is None:
                continue
            per_order[w.sales_order_no][w.pattern_code] += capacity.cycles_for(
                w.plan_qty, best_ratio
            )
        return sum(max(v.values()) for v in per_order.values() if v)

    def _window_stats(self, orders: list[WorkOrder]) -> tuple[date | None, int, int]:
        """计划窗口统计：最晚目标完成日、窗口内生产日数、窗口内槽位数。

        §9.3 的 H 是「plan_start 到该类型最晚 target_finish 的生产日数」，
        不是整个推算地平线——后者会把需求组数压到 1，人为放大延期。

        返回槽位数而非「平均每日班次」，是为了让下游全部用整数运算（§16.1
        禁止浮点参与决策）。
        """
        latest = max(
            (capacity.target_finish(w.due_date, self.params) for w in orders), default=None
        )
        window = [s for s in self.calendar.slots if latest is None or s.day <= latest]
        return latest, len({s.day for s in window}), len(window)

    def _estimate_groups(
        self, orders: list[WorkOrder], mold_set: MoldSet, presses: list[Press]
    ) -> int:
        """§9.3 需求组数（未按 max_groups 截断）。

        截断只发生在选取实际开动组数时；这里保留原始需求值，否则
        「需求组数 > 上限」这一瓶颈永远无法被 §13.3 识别出来。
        """
        molds = mold_set.available_molds
        if not molds:
            return 1
        work = self._work_estimate(orders, molds)

        _, days, slots = self._window_stats(orders)
        if days == 0 or slots == 0:
            return 1
        cap = min(p.capacity_per_shift for p in presses)
        # 一组最多拿到 min(group_size, 机台总数) 台机；再受套内副数限制（§5.6）
        parallel = self._effective_parallel(
            mold_set, min(self.params.group_size, len(presses))
        )
        # 一组在窗口内的总产能 = 组内并行度 × 机台班产能 × 窗口槽位数（全整数）
        per_group = max(1, parallel * cap * slots)
        return max(1, -(-work // per_group))

    # ---------------- P3–P5 ----------------

    def _order_sets(self, sets: list[MoldSet], orders: list[WorkOrder]) -> list[MoldSet]:
        """给出本类型全部套的启用顺序（§10.1）。

        返回完整顺序而非前 g 个：调用方按 `[:g]` 取用，同一顺序复用于 §10.2 的
        按需补套，两处不会因各自重算而分叉。

        策略名与其实际排序键严格对应，便于按 §18.1 A3 切换与追溯。
        """
        policy = self.params.set_activation_policy
        if policy == "DEMAND_CYCLES_FIRST":
            scored = [(self._set_score(s, orders), s) for s in sets]
            scored.sort(
                key=lambda x: (
                    x[0] is None,
                    x[0] if x[0] is not None else (0, Fraction(0)),
                    -x[1].mold_count,
                    x[1].set_order,
                    x[1].set_no,
                )
            )
            return [s for _, s in scored]
        if policy == "MAX_MOLDS_FIRST":
            return sorted(sets, key=lambda s: (-s.mold_count, s.set_order, s.set_no))
        if policy == "SET_ORDER":
            return sorted(sets, key=lambda s: (s.set_order, s.set_no))
        if policy == "COVERAGE_FIRST":
            return sorted(
                sets,
                key=lambda s: (-len({p for m in s.available_molds for p in m.patterns}),
                               -s.mold_count, s.set_order, s.set_no),
            )
        raise ValueError(
            f"未知的模具套启用策略 {policy!r}，可选 DEMAND_CYCLES_FIRST / "
            f"MAX_MOLDS_FIRST / SET_ORDER / COVERAGE_FIRST"
        )

    def _set_score(
        self, s: MoldSet, orders: list[WorkOrder]
    ) -> tuple[int, Fraction] | None:
        """§10.1 DEMAND_CYCLES_FIRST 的评分：(未覆盖的待排工单数, 实需机台模数/有效并行度)。

        两个维度按字典序比较，都越小越该优先启用。套内无可用模具时返回 None。

        - **未覆盖工单数优先**：组数上限会让 §10.2 的按需补套失效，此时本套覆盖不到的
          工单会直接落 R008。若按「别的套会承接」来计价，窄覆盖的高配比套会被高估
          ——实测在 2 台机（只能开 1 组）的类型上，这会选中一个让半数工单排不了的套。
        - **除以 min(组内机台数, 套内可用副数)**：一副模具同一时刻只能装一台机，单副
          模具的套实际只有 1 台机在跑，不折算会高估其产能（§5.6 有效并行度）。
        """
        molds = s.available_molds
        if not molds:
            return None
        best: dict[str, Decimal] = {}
        for m in molds:
            for p, r in m.ratios.items():
                if r > best.get(p, Decimal(0)):
                    best[p] = r

        uncovered = 0
        total = 0
        for w in orders:
            ratio = best.get(w.pattern_code)
            if ratio is None:
                uncovered += 1
                continue
            total += capacity.cycles_for(w.plan_qty, ratio)
        return uncovered, Fraction(total, min(self.params.group_size, len(molds)))

    def _schedule_type(
        self,
        ptype: str,
        orders: list[WorkOrder],
        sets: list[MoldSet],
        presses: list[Press],
        g: int,
        ordered_sets: list[MoldSet],
        g_needed: int,
        max_groups: int,
        mold_tls: dict[str, MoldTimeline],
    ) -> tuple[
        list[Assignment], list[Batch], TypeDiagnostics,
        list[ExceptionRecord], list[ScheduleReason],
    ]:
        # 组数迭代会多次调用本方法，异常与理由只能由最终采纳的那一次提交
        local_exceptions: list[ExceptionRecord] = []
        scheduler = Scheduler(self.calendar, self.params)

        selected_sets = ordered_sets[:g]
        # §10.2：已选中的套若覆盖不到某些牌型，而还有未启用且能覆盖的套、
        # 且组数未达上限，则启用它。少了这一步，本可排产的工单会被误报 R008。
        selected_sets = self._expand_coverage(
            orders, presses, selected_sets, ordered_sets, max_groups
        )

        group_presses: list[list[Press]] = []
        for i in range(len(selected_sets)):
            chunk = presses[i * self.params.group_size : (i + 1) * self.params.group_size]
            if chunk:
                group_presses.append(chunk)
        # 机台不足时组数按实际可组数收敛
        g = len(group_presses)

        timelines: dict[int, list[PressTimeline]] = {}
        for gi, plist in enumerate(group_presses):
            timelines[gi] = [PressTimeline(press=p) for p in plist]

        # mold_tls 由 run() 按模具号全局建一条并跨产品类型共享（见那里的说明），
        # 这里只使用，不再按本类型的 selected_sets 重建。

        # 工单分配：按排序依次放入「预计完成时间最早」的组。
        # 组内并行度受套内模具副数限制（一副模具同一时刻只能装一台机），
        # 因此负荷需按有效并行度折算，否则模具少的组会占满工单却只开一台机。
        group_orders: dict[int, list[WorkOrder]] = defaultdict(list)
        group_load: dict[int, int] = defaultdict(int)
        group_parallel: dict[int, int] = {}
        for gi in range(len(group_presses)):
            mold_set = selected_sets[gi]
            group_parallel[gi] = self._effective_parallel(
                mold_set, len(group_presses[gi])
            )

        for w in orders:
            best_g = None
            for gi in group_parallel:
                mold_set = selected_sets[gi]
                if not any(m.covers(w.pattern_code) for m in mold_set.available_molds):
                    continue
                ratio = max(
                    (m.ratios[w.pattern_code] for m in mold_set.available_molds
                     if m.covers(w.pattern_code)),
                    default=1,
                )
                add = capacity.cycles_for(w.plan_qty, ratio)
                # 折算后的负荷：用 Fraction 而非除法，避免浮点参与决策（§16.1）
                projected = Fraction(group_load[gi] + add, group_parallel[gi])
                key = (projected, gi)
                if best_g is None or key < best_g[0]:
                    best_g = (key, gi, add)
            if best_g is None:
                local_exceptions.append(
                    ExceptionRecord(w.work_order_no, "R008", f"牌型 {w.pattern_code} 无模具覆盖")
                )
                continue
            _, gi, add = best_g
            group_orders[gi].append(w)
            group_load[gi] += add

        assignments: list[Assignment] = []
        all_batches: list[Batch] = []
        all_reasons: list[ScheduleReason] = []

        diag = self._diagnostics(
            ptype, orders, sets, presses, g_needed, len(group_presses), selected_sets
        )

        for gi, g_orders in sorted(group_orders.items()):
            mold_set = selected_sets[gi]
            # 这里的批号只是组内的临时序号（用于组内稳定排序）；
            # 真正的全局批号在落位成功时分配，使未落位的批次不占用编号。
            batch_list = molding.build_batches(
                g_orders,
                mold_set,
                group_no=gi,
                allow_cross_sales_order=self.params.allow_cross_sales_order_batch,
                batch_seq_start=1,
            )
            by_no = {w.work_order_no: w for w in g_orders}
            b_due = {
                b.batch_no: min(by_no[n].due_date for n in b.work_order_nos)
                for b in batch_list
            }

            plist = timelines[gi]
            placed_batches: list[Batch] = []
            for b in batch_list:
                # §20.2：时间上限在批次粒度上生效。未落位的工单由 run() 统一报 R007。
                if self._time_exceeded(self._t0):
                    self._truncated = True
                    break
                batch_due = b_due[b.batch_no]  # 必须在重新分配批号之前取出
                batch_target_day = capacity.target_finish(batch_due, self.params)
                decision = scheduler.place_batch(b, plist, mold_set.available_molds, mold_tls)
                if decision is None:
                    for no in b.work_order_nos:
                        local_exceptions.append(
                            ExceptionRecord(
                                no,
                                "R007",
                                "排程超出日历范围（产能严重不足），该批次未排入",
                                "延长日历或增加机台/模具后重算",
                            )
                        )
                    continue
                # 落位成功才分配全局批号：编号从 1 起连续，未落位的批次不占号（§11.6）
                b.batch_no = f"B{self._batch_seq:05d}"
                self._batch_seq += 1
                press, mold, placement = decision.press, decision.mold, decision.placement
                batch_cycles, switched = decision.cycles, decision.switched
                slot_per_unit = scheduler.commit(
                    press, mold_tls[mold.mold_no], placement, batch_cycles
                )
                b.mold_no = mold.mold_no
                b.cycles = batch_cycles
                b.press_no = press.press.press_no
                b.start_slot = placement.start
                b.end_slot = placement.end
                b.changeover_cycles = placement.changeover
                s_slot = self.calendar.slot(placement.start)
                e_slot = self.calendar.slot(placement.end)
                b.start_date, b.start_shift = s_slot.day, s_slot.shift
                b.end_date, b.end_shift = e_slot.day, e_slot.shift
                # §12.3：R001/R002 判的是批次是否落在前置窗口内，判据必须落到**日**。
                # 目标完成日是「交期 − 前置偏移」的日期差，完成在同一目标日的第 2 班
                # 并不晚于它。此前拿槽位下标比「目标日的首个槽位」，会把这种批次判成
                # R002，理由文本随之写出与事实相反的「晚于该窗口截止 <目标日>」。
                within_window = e_slot.day <= batch_target_day
                placed_batches.append(b)

                for no in b.work_order_nos:
                    wo = next(x for x in g_orders if x.work_order_no == no)
                    cyc = capacity.cycles_for(wo.plan_qty, mold.ratios[wo.pattern_code])
                    end = slot_per_unit[placement.changeover + cyc - 1]
                    finish_day = self.calendar.day_of(end)
                    d = capacity.delay_days(finish_day, wo.due_date)
                    produced = Decimal(batch_cycles) * mold.ratios[wo.pattern_code]
                    surplus = produced - wo.plan_qty
                    # §4.2 按牌型记录产出与超产（每个牌型在批次内至多一张工单）
                    b.output_qty[wo.pattern_code] = produced
                    b.surplus[wo.pattern_code] = surplus
                    own = self.calendar.slot(end)

                    # R001/R002 判的是「是否落在前置窗口内」，与是否延期是两回事（§12.3）
                    codes = ["R001"] if within_window else ["R002"]
                    if switched:
                        codes.append("R003")
                    if cyc > press.press.capacity_per_shift:
                        codes.append("R004")
                    if surplus > 0:
                        codes.append("R012")
                    assignment = Assignment(
                        work_order_no=no,
                        sales_order_no=wo.sales_order_no,
                        product_type_code=wo.product_type_code,
                        pattern_code=wo.pattern_code,
                        batch_no=b.batch_no,
                        press_no=press.press.press_no,
                        group_no=gi,
                        set_no=mold_set.set_no,
                        mold_no=mold.mold_no,
                        plan_qty=wo.plan_qty,
                        cycles=cyc,
                        start_slot=placement.start,
                        end_slot=end,
                        planned_start_date=s_slot.day,
                        planned_start_shift=s_slot.shift,
                        planned_end_date=own.day,
                        planned_end_shift=own.shift,
                        delayed=d > 0,
                        delay_days=d,
                        reason_codes=codes,
                    )
                    assignments.append(assignment)
                    all_reasons.append(
                        explain.build_reason(
                            assignment=assignment,
                            wo=wo,
                            batch=b,
                            mold=mold,
                            mold_set=mold_set,
                            all_sets=sets,
                            decision=decision,
                            group_presses=group_presses[gi],
                            calendar=self.calendar,
                            params=self.params,
                            target_day=batch_target_day,
                            batch_due=batch_due,
                            within_window=within_window,
                            finish_slot=end,
                            batch_orders=[by_no[n] for n in b.work_order_nos],
                            diag=diag,
                        )
                    )
            # 未落位的批次不进 batch_result：它们没有起止时间，其工单已由 R007 异常报告
            all_batches.extend(placed_batches)
            if self._truncated:
                # 已超时：本类型其余组不再开排，保留已落位的部分结果
                break

        return assignments, all_batches, diag, local_exceptions, all_reasons
