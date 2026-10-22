"""领域模型：热压排程算法的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from functools import cached_property


class WorkOrderStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class MoldType(str, Enum):
    SINGLE = "SINGLE"
    MULTI = "MULTI"


class ResourceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    MAINTENANCE = "MAINTENANCE"


# §14.0 原因码严重度：异常 > 瓶颈 > 预警 > 信息
REASON_SEVERITY: dict[str, int] = {
    "R008": 3, "R009": 3, "R010": 3, "R011": 3,
    "R005": 2, "R006": 2, "R007": 2,
    "R002": 1, "R012": 1,
    "R001": 0, "R003": 0, "R004": 0,
}


def primary_reason(codes: list[str]) -> str:
    """§4.1 取最严重者；同级时按码值升序，保证确定性。"""
    if not codes:
        return ""
    return min(codes, key=lambda c: (-REASON_SEVERITY.get(c, 0), c))


@dataclass(frozen=True)
class ProductType:
    code: str
    name: str
    erp_category_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MoldPattern:
    mold_no: str
    pattern_code: str
    ratio: Decimal


@dataclass
class Mold:
    mold_no: str
    set_no: str
    mold_type: MoldType
    priority: int
    ratios: dict[str, Decimal] = field(default_factory=dict)
    status: ResourceStatus = ResourceStatus.AVAILABLE

    def covers(self, pattern_code: str) -> bool:
        return pattern_code in self.ratios

    def covers_all(self, patterns) -> bool:
        return all(p in self.ratios for p in patterns)

    @property
    def patterns(self) -> frozenset[str]:
        return frozenset(self.ratios)

    @property
    def has_valid_ratios(self) -> bool:
        """V07：配比缺失或非正的模具不可用（否则模数计算会除零或得出负模数）。"""
        return bool(self.ratios) and all(r > 0 for r in self.ratios.values())


@dataclass
class MoldSet:
    set_no: str
    product_type_code: str
    set_order: int
    molds: list[Mold] = field(default_factory=list)

    @cached_property
    def available_molds(self) -> list[Mold]:
        """套内可用模具，按 (优先级, 模具号) 升序，**模具号唯一**。

        同一模具号在套内出现两次是资料错误：模具号标识一副物理模具，而引擎处处以
        模具号为键（`Batch.cycles_by_mold`、`mold_tls`），重复项会让批次模数与工单
        模数取自不同的模具对象，进而索引越界。这里按序保留优先级最高的一条，重复项
        由 `Engine._validate_resources` 报 R010。
        """
        seen: set[str] = set()
        out: list[Mold] = []
        for m in sorted(
            (
                m
                for m in self.molds
                if m.status is ResourceStatus.AVAILABLE and m.has_valid_ratios
            ),
            key=lambda m: (m.priority, m.mold_no),
        ):
            if m.mold_no in seen:
                continue
            seen.add(m.mold_no)
            out.append(m)
        return out

    @property
    def mold_count(self) -> int:
        return len(self.available_molds)


@dataclass(frozen=True)
class Press:
    """一台热压机。现场 38 台可独立运行，不再按「两台一组」固定配对。

    `product_type_code = "*"` 表示通用机，可服务任意产品类型；给具体类型则表示
    专用机，只做该类型。机台时间轴按 `press_no` 全局共享，通用机可在不同时段做
    不同类型（A2 不再假设单归属）。
    """

    press_no: str
    capacity_per_shift: int
    product_type_code: str = "*"
    status: ResourceStatus = ResourceStatus.AVAILABLE

    @property
    def is_general(self) -> bool:
        return self.product_type_code in ("", "*")

    def serves(self, product_type_code: str) -> bool:
        return self.is_general or self.product_type_code == product_type_code


@dataclass
class WorkOrder:
    work_order_no: str
    sales_order_no: str
    product_type_code: str
    pattern_code: str
    qty: Decimal
    uom: str
    # 允许缺失：需求第八章要求「交期缺失」标记为异常数据并报告，非空字段无法表示该情形
    due_date: date | None = None
    status: WorkOrderStatus = WorkOrderStatus.NOT_STARTED
    remaining_qty: Decimal | None = None
    material_code: str = ""

    @property
    def plan_qty(self) -> Decimal:
        if self.status is WorkOrderStatus.IN_PROGRESS and self.remaining_qty is not None:
            return self.remaining_qty
        return self.qty

    @property
    def is_schedulable(self) -> bool:
        return (
            self.status not in (WorkOrderStatus.COMPLETED, WorkOrderStatus.CANCELLED)
            and self.plan_qty > 0
        )


def is_preprocessable(wo: WorkOrder, product_type_codes=None) -> bool:
    """§7 P0：工单能否进入排程。**这是该判据的单一来源。**

    `is_schedulable` 只看状态与数量，不够：引擎的 P0 还会排除缺交期、缺牌型、
    产品类型未定义的工单。校验器 C10 若只按 `is_schedulable` 判断「哪些工单应当
    出现在结果里」，就会把引擎正确地跳过的工单算成漏排，把合规结果判为无效。

    `product_type_codes` 为已定义的产品类型集合；传 None 表示调用方不掌握该信息，
    此时跳过这一项（其余字段仍按同一口径判断）。
    """
    return (
        wo.is_schedulable
        and bool(wo.pattern_code)
        and wo.due_date is not None
        and (product_type_codes is None or wo.product_type_code in product_type_codes)
    )


@dataclass(frozen=True)
class CalendarDay:
    day: date
    shifts: int
    is_holiday: bool = False

    @property
    def is_working(self) -> bool:
        return self.shifts > 0 and not self.is_holiday


@dataclass(frozen=True)
class Params:
    plan_start: date
    # 现场：38 台机器、19 人、一人两台，同时开 19 台。0 表示不限制（仅用于合成测试）。
    max_concurrent_presses: int = 19
    pre_offset_days: int = 15
    shift_hours: int = 12
    changeover_hours: int = 2
    horizon_months: int = 3
    # §20.2：全量计算的时间上限（秒）。0 表示不限制；超限则输出部分结果并置
    # run_meta.truncated=true，同时把未排工单逐条报为异常。
    time_limit_seconds: float = 0.0
    allow_cross_sales_order_batch: bool = False
    # §12.6：回填开关。true 时从「首个仍有剩余产能的槽位」起寻找连续空闲区间，
    # 允许填入前面遗留的空档；false 时从追加游标起单调追加。
    backfill_gaps: bool = True
    # §10.1 模具套启用策略。默认按「用该套排完本类型待排工单所需机台模数」升序：
    # 配比直接决定模数（模数 = ceil(数量/配比)），只看副数会选中副数多但配比差的套。
    # 可选 MAX_MOLDS_FIRST / SET_ORDER / COVERAGE_FIRST。
    set_activation_policy: str = "DEMAND_CYCLES_FIRST"


@dataclass
class Batch:
    batch_no: str
    sales_order_no: str
    group_no: int
    set_no: str
    mold_no: str
    press_no: str
    work_order_nos: list[str] = field(default_factory=list)
    patterns: set[str] = field(default_factory=set)
    cycles: int = 0
    cycles_by_mold: dict[str, int] = field(default_factory=dict)
    start_slot: int = -1
    end_slot: int = -1
    changeover_cycles: int = 0
    surplus: dict[str, Decimal] = field(default_factory=dict)
    # 以下为 §4.2 要求的派生字段，追加在末尾以免打乱既有按位置构造的调用方
    start_date: date | None = None
    start_shift: int = 0
    end_date: date | None = None
    end_shift: int = 0
    output_qty: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class Assignment:
    """单张工单的排程结果。"""

    work_order_no: str
    sales_order_no: str
    product_type_code: str
    pattern_code: str
    batch_no: str
    press_no: str
    group_no: int
    set_no: str
    mold_no: str
    plan_qty: Decimal
    cycles: int
    start_slot: int
    end_slot: int
    delayed: bool = False
    delay_days: int = 0
    reason_codes: list[str] = field(default_factory=list)
    bottleneck_resource: str = ""
    reason_text: str = ""
    # 以下为 §4.1 要求的派生字段，追加在末尾以免打乱既有按位置构造的调用方
    planned_start_date: date | None = None
    planned_start_shift: int = 0
    planned_end_date: date | None = None
    planned_end_shift: int = 0

    @property
    def reason_code(self) -> str:
        """§4.1 schedule_result.reason_code：完整列表中最严重的一个。"""
        return primary_reason(self.reason_codes)


@dataclass
class ScheduleReason:
    """§4.5 排程理由。"""

    work_order_no: str
    machine_reason: str = ""
    time_reason: str = ""
    batch_reason: str = ""
    mold_reason: str = ""
    sensitivity_note: str = ""


@dataclass
class ReleasePlan:
    """§4.4 可释放数量与预计释放时间，供下游工序排程使用。"""

    batch_no: str
    pattern_code: str
    release_qty: Decimal
    release_date: date
    release_shift: int
    downstream_order_no: str = ""


@dataclass
class OrderDelivery:
    """§4.3 订单齐套与延期。"""

    sales_order_no: str
    due_date: date
    expected_finish_date: date
    delayed: bool
    delay_days: int
    bottleneck_resource: str = ""
    reason_code: str = ""
    reason_text: str = ""


@dataclass
class TypeDiagnostics:
    """单个产品类型的排程诊断，供 §13.3 瓶颈归因使用。

    `max_groups` / `groups_needed` / `groups_used` 是历史字段名，现在表示**模具套数**
    （独立机台模型下不再有物理「组」）。`concurrency_cap` 是全局并行机台上限。
    """

    product_type_code: str
    mold_set_count: int = 0
    press_count: int = 0
    max_groups: int = 0
    groups_needed: int = 0
    groups_used: int = 0
    working_days: int = 0
    latest_target: date | None = None
    total_cycles: int = 0
    daily_capacity: int = 0
    concurrency_cap: int = 0


@dataclass
class ExceptionRecord:
    work_order_no: str
    code: str
    detail: str
    suggestion: str = ""
