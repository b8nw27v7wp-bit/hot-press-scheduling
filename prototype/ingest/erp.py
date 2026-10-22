"""ERP「生产任务单序时簿」→ 热压工单。

数据来源是 ERP 导出的 .xls（单表，列见 `COLUMNS`）。本模块只负责**取数与字段映射**，
不做任何排程决策；映射口径与《热压排程输入输出字段字典.md》§1.1 对齐。

三条口径在这里定死，均可通过参数覆盖：

1. **牌型取物料长代码**（A1-b 的落地形态）。ERP 工单没有独立的牌型字段，物料长代码
   在同一产品类型内唯一标识一个牌型，且跨销售订单复用——正合「牌型」的语义。
2. **`计划完工日期` 当交期，前置偏移保持 15 天**（2026-10-22 确认「热压须在交期前
   15 天完成」）。注意：按此口径，未来 3 个月的 711 张工单中有 549 张的目标完成日
   （交期 − 15 天）已经早于计划起点，排程会集中报延期。
3. **后道工单默认剔除**：物料名称以「滚牌 / 摆牌 / 铜钉」结尾的工单是热压之后的后道
   工序，与主工单数量成对（实测 5 252/5 436 组逐组相等）。2026-10-22 确认**只做热压
   排程**，保持默认剔除；`include_secondary=True` 仅用于对照。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Mapping, Sequence

from domain.models import WorkOrder, WorkOrderStatus

HOT_PRESS_WORKSHOP = "热压车间"
SECONDARY_MARKERS = ("滚牌", "摆牌", "铜钉")

COLUMNS = {
    "work_order_no": "编号",
    "workshop": "生产车间",
    "doc_status": "单据状态",
    "exec_status": "执行状态",
    "sales_order_no": "销售订单号",
    "material_code": "物料长代码",
    "material_name": "物料名称",
    "qty": "计划生产数量",
    "inbound_qty": "合格品入库数量",
    "uom": "单位",
    "planned_start": "计划开工日期",
    "planned_finish": "计划完工日期",
}

DONE_DOC_STATUS = "结案"
DONE_EXEC_STATUS = "完全入库"


@dataclass(frozen=True)
class ImportIssue:
    """输入侧问题。V 码沿用 §7；这里只报，不改写数据。"""

    code: str
    work_order_no: str
    detail: str


@dataclass
class ImportResult:
    work_orders: list[WorkOrder] = field(default_factory=list)
    issues: list[ImportIssue] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)


def read_xls_rows(path: str) -> list[dict[str, object]]:
    """读第一张表为「列名 → 值」的字典列表。xlrd 是可选依赖（`.xls` 为 BIFF 二进制）。"""
    try:
        import xlrd  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise ImportError(
            "读取 .xls 需要 xlrd：pip install 'rypc[erp]'（或先另存为 .xlsx/.csv）"
        ) from exc

    book = xlrd.open_workbook(path, on_demand=True)
    sheet = book.sheet_by_index(0)
    header = [str(sheet.cell_value(0, c)).strip() for c in range(sheet.ncols)]
    rows = []
    for r in range(1, sheet.nrows):
        rows.append(
            {header[c]: sheet.cell_value(r, c) for c in range(sheet.ncols)}
        )
    book.release_resources()
    return rows


def parse_date(value: object) -> dt.date | None:
    """接受 `YYYY/MM/DD`、`YYYY-MM-DD`、`date/datetime` 与 Excel 序列号。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Excel 1900 日期系统的序列号以 1899-12-30 为原点
        if value <= 0:
            return None
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(value))
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _decimal(value: object) -> Decimal:
    """一律 `Decimal(str(x))`，禁止经 float 中转（§16.1）。"""
    if value is None or str(value).strip() == "":
        return Decimal(0)
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"无法解析为数值：{value!r}") from exc


def is_secondary(name: str) -> bool:
    """后道工序工单：物料名称以 滚牌 / 摆牌 / 铜钉 结尾。"""
    text = str(name).strip()
    return any(text.endswith(marker) for marker in SECONDARY_MARKERS)


def product_type_of(material_code: str) -> str:
    """物料长代码 → 产品类型。

    2.2.xx 的三段前缀即为产品系列（如 2.2.11 = 5208 骨牌、2.2.16 = 三角牌）；
    H.2.2 的第三段只是大类，真正区分系列的是第四段（H.2.2.99 象牙麻将、
    H.2.2.30 美国麻将、H.2.2.18 数字方块）。
    """
    code = str(material_code).strip()
    segments = code.split(".")
    if code.startswith("H.2.2."):
        return ".".join(segments[:4])
    return ".".join(segments[:3]) if len(segments) >= 3 else code


def _row_status(doc_status: str, exec_status: str, qty: Decimal, inbound: Decimal):
    if doc_status == DONE_DOC_STATUS or exec_status == DONE_EXEC_STATUS:
        return WorkOrderStatus.COMPLETED, qty
    if inbound > 0 and inbound >= qty:
        return WorkOrderStatus.COMPLETED, qty
    if inbound > 0:
        return WorkOrderStatus.IN_PROGRESS, qty - inbound
    return WorkOrderStatus.NOT_STARTED, qty


def import_work_orders(
    path: str | None = None,
    *,
    plan_start: dt.date,
    window_start: dt.date | None = None,
    window_end: dt.date | None = None,
    workshop: str = HOT_PRESS_WORKSHOP,
    include_secondary: bool = False,
    include_overdue: bool = False,
    product_type_of_code: Callable[[str], str] = product_type_of,
    rows: Sequence[Mapping[str, object]] | None = None,
) -> ImportResult:
    """把 ERP 导出的序时簿转成引擎可直接消费的 `WorkOrder` 列表。

    过滤顺序：车间 → 后道工单 → 已完成 → 数量/交期校验 → 时间窗。
    已完成工单不计入 `work_orders`，但计入 `stats`，便于核对取数范围。
    """
    if rows is None:
        if path is None:
            raise ValueError("必须提供 path 或 rows 之一")
        raw_rows = read_xls_rows(path)
    else:
        raw_rows = list(rows)
    window_start = plan_start if window_start is None else window_start

    result = ImportResult()
    seen: dict[str, int] = {}
    by_type: dict[str, dict[str, object]] = {}
    sales_orders: set[str] = set()
    patterns: set[str] = set()
    counts = {
        "rows": len(raw_rows),
        "hot_press_rows": 0,
        "secondary_excluded": 0,
        "completed": 0,
        "overdue_excluded": 0,
        "beyond_window": 0,
        "missing_due": 0,
        "non_positive_qty": 0,
        "duplicates": 0,
        "imported": 0,
    }

    for index, row in enumerate(raw_rows, start=1):
        if str(row.get(COLUMNS["workshop"], "")).strip() != workshop:
            continue
        counts["hot_press_rows"] += 1

        work_order_no = str(row.get(COLUMNS["work_order_no"], "")).strip()
        material_code = str(row.get(COLUMNS["material_code"], "")).strip()
        material_name = str(row.get(COLUMNS["material_name"], "")).strip()

        if not include_secondary and is_secondary(material_name):
            counts["secondary_excluded"] += 1
            continue

        qty = _decimal(row.get(COLUMNS["qty"]))
        inbound = _decimal(row.get(COLUMNS["inbound_qty"]))
        status, remaining = _row_status(
            str(row.get(COLUMNS["doc_status"], "")).strip(),
            str(row.get(COLUMNS["exec_status"], "")).strip(),
            qty,
            inbound,
        )
        if status is WorkOrderStatus.COMPLETED:
            counts["completed"] += 1
            continue

        if qty <= 0:
            counts["non_positive_qty"] += 1
            result.issues.append(
                ImportIssue("V02", work_order_no, f"计划生产数量非正：{qty}")
            )
            continue

        due = parse_date(row.get(COLUMNS["planned_finish"]))
        if due is None:
            counts["missing_due"] += 1
            result.issues.append(
                ImportIssue("V03", work_order_no, "计划完工日期缺失或无法解析")
            )
            continue

        if due < window_start:
            counts["overdue_excluded"] += 1
            if not include_overdue:
                continue
        elif window_end is not None and due > window_end:
            counts["beyond_window"] += 1
            continue

        if work_order_no in seen:
            counts["duplicates"] += 1
            result.issues.append(
                ImportIssue(
                    "V01",
                    work_order_no,
                    f"工单号重复（第 {index} 行与第 {seen[work_order_no]} 行）",
                )
            )
            continue
        seen[work_order_no] = index

        if not material_code:
            result.issues.append(
                ImportIssue("V05", work_order_no, "物料长代码为空，无法确定牌型")
            )
            continue

        product_type = product_type_of_code(material_code)
        work_order = WorkOrder(
            work_order_no=work_order_no,
            sales_order_no=str(row.get(COLUMNS["sales_order_no"], "")).strip(),
            product_type_code=product_type,
            pattern_code=material_code,
            qty=qty,
            uom=str(row.get(COLUMNS["uom"], "")).strip(),
            due_date=due,
            status=status,
            remaining_qty=remaining if status is WorkOrderStatus.IN_PROGRESS else None,
            material_code=material_code,
        )
        result.work_orders.append(work_order)

        bucket = by_type.setdefault(
            product_type,
            {"orders": 0, "qty": Decimal(0), "patterns": set(), "sales_orders": set()},
        )
        bucket["orders"] += 1
        bucket["qty"] += work_order.plan_qty
        bucket["patterns"].add(material_code)
        bucket["sales_orders"].add(work_order.sales_order_no)
        sales_orders.add(work_order.sales_order_no)
        patterns.add(material_code)

    counts["imported"] = len(result.work_orders)
    result.stats = {
        **counts,
        "product_types": len(by_type),
        "sales_orders": len(sales_orders),
        "patterns": len(patterns),
        "qty": sum((w.plan_qty for w in result.work_orders), Decimal(0)),
        "by_product_type": {
            code: {
                "orders": bucket["orders"],
                "qty": bucket["qty"],
                "patterns": len(bucket["patterns"]),
                "sales_orders": len(bucket["sales_orders"]),
            }
            for code, bucket in sorted(by_type.items())
        },
    }
    return result
