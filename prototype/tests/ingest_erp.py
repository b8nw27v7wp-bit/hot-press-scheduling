"""ERP 取数层回归：字段映射、状态判定、后道剔除、时间窗与输入校验。

数据全部现场构造，不依赖真实 .xls；真实文件的体检用 `tests/real_data.py`。
"""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import WorkOrderStatus  # noqa: E402
from ingest.erp import (  # noqa: E402
    COLUMNS,
    import_work_orders,
    is_secondary,
    parse_date,
    product_type_of,
)

PLAN_START = dt.date(2026, 10, 22)
WINDOW_END = dt.date(2027, 1, 22)
RULE = "=" * 78


def row(**overrides):
    base = {
        COLUMNS["work_order_no"]: "WORK000001",
        COLUMNS["workshop"]: "热压车间",
        COLUMNS["doc_status"]: "下达",
        COLUMNS["exec_status"]: "下达",
        COLUMNS["sales_order_no"]: "SO1",
        COLUMNS["material_code"]: "2.2.11.P00001",
        COLUMNS["material_name"]: "5208白色无钉点数骨牌（已抛光）",
        COLUMNS["qty"]: 100.0,
        COLUMNS["inbound_qty"]: 0.0,
        COLUMNS["uom"]: "pcs",
        COLUMNS["planned_start"]: "2026/11/03",
        COLUMNS["planned_finish"]: "2026/11/03",
    }
    base.update(overrides)
    return base


def run(rows, **kwargs):
    kwargs.setdefault("plan_start", PLAN_START)
    kwargs.setdefault("window_end", WINDOW_END)
    return import_work_orders(rows=rows, **kwargs)


def report(items, results):
    for ok, text in items:
        print(f"  [{'ok' if ok else 'FAIL'}] {text}")
        results.append(ok)


def main() -> int:
    results: list[bool] = []
    print("ERP 取数层回归")
    print(RULE)

    print("产品类型与牌型")
    report([
        (product_type_of("2.2.11.P00001") == "2.2.11", "2.2.11.P00001 → 2.2.11"),
        (product_type_of("2.2.16.P00469-01") == "2.2.16", "2.2.16.P00469-01 → 2.2.16"),
        (product_type_of("H.2.2.99.P00279") == "H.2.2.99", "H.2.2.99.P00279 → H.2.2.99"),
        (product_type_of("H.2.2.30.D00103") == "H.2.2.30", "H.2.2.30.D00103 → H.2.2.30"),
    ], results)

    print("日期解析")
    report([
        (parse_date("2026/11/03") == dt.date(2026, 11, 3), "2026/11/03"),
        (parse_date("2026-11-03") == dt.date(2026, 11, 3), "2026-11-03"),
        (parse_date(46329) == dt.date(2026, 11, 3), "Excel 序列号 46329"),
        (parse_date("") is None, "空串 → None"),
    ], results)

    print("后道工单识别")
    report([
        (is_secondary("香蕉牌字母 A 滚牌"), "滚牌"),
        (is_secondary("30MM白色色白胚 摆牌"), "摆牌"),
        (is_secondary("40MM三角牌（黑点）铜钉"), "铜钉"),
        (not is_secondary("30MM粉色双色美国麻将 1饼（已点漆）"), "已点漆不算后道"),
        (not is_secondary("5208白色无钉点数骨牌（已抛光）"), "普通主工单"),
    ], results)

    # 主用例：一行非热压、一行后道、一行结案、一行部分入库、一行主工单
    rows = [
        row(**{COLUMNS["work_order_no"]: "W0", COLUMNS["workshop"]: "点漆车间"}),
        row(**{COLUMNS["work_order_no"]: "W1", COLUMNS["material_name"]: "香蕉牌字母 A 滚牌"}),
        row(**{COLUMNS["work_order_no"]: "W2", COLUMNS["doc_status"]: "结案"}),
        row(**{
            COLUMNS["work_order_no"]: "W3",
            COLUMNS["inbound_qty"]: 40.0,
            COLUMNS["planned_finish"]: "2026/11/10",
        }),
        row(**{COLUMNS["work_order_no"]: "W4"}),
    ]
    result = run(rows)
    by_no = {wo.work_order_no: wo for wo in result.work_orders}
    report([
        (len(result.work_orders) == 2, f"入排程 2 张（实际 {len(result.work_orders)}）"),
        ("W1" not in by_no, "后道工单默认剔除"),
        ("W2" not in by_no, "结案工单剔除"),
        (by_no["W3"].status is WorkOrderStatus.IN_PROGRESS, "部分入库 → IN_PROGRESS"),
        (by_no["W3"].remaining_qty == Decimal("60.0"), "剩余量 = 100 - 40"),
        (by_no["W3"].plan_qty == Decimal("60.0"), "plan_qty 取剩余量"),
        (by_no["W4"].status is WorkOrderStatus.NOT_STARTED, "无入库 → NOT_STARTED"),
        (by_no["W4"].pattern_code == "2.2.11.P00001", "牌型取物料长代码"),
        (by_no["W4"].product_type_code == "2.2.11", "产品类型取代码前缀"),
        (result.stats["secondary_excluded"] == 1, "后道计数"),
        (result.stats["completed"] == 1, "已完成计数"),
    ], results)

    with_secondary = run(rows, include_secondary=True)
    report([
        ("W1" in {wo.work_order_no for wo in with_secondary.work_orders},
         "include_secondary=True 时保留后道工单"),
    ], results)

    print("时间窗")
    rows = [
        row(**{COLUMNS["work_order_no"]: "PAST", COLUMNS["planned_finish"]: "2026/09/01"}),
        row(**{COLUMNS["work_order_no"]: "NOW", COLUMNS["planned_finish"]: "2026/11/03"}),
        row(**{COLUMNS["work_order_no"]: "FAR", COLUMNS["planned_finish"]: "2027/03/01"}),
    ]
    default = run(rows)
    overdue = run(rows, include_overdue=True)
    report([
        ([wo.work_order_no for wo in default.work_orders] == ["NOW"],
         "默认剔除过期与地平线外"),
        (overdue.stats["overdue_excluded"] == 1
         and [wo.work_order_no for wo in overdue.work_orders] == ["PAST", "NOW"],
         "include_overdue=True 时保留过期工单"),
        (default.stats["beyond_window"] == 1, "地平线外计数"),
    ], results)

    print("输入校验")
    rows = [
        row(**{COLUMNS["work_order_no"]: "ZERO", COLUMNS["qty"]: 0.0}),
        row(**{COLUMNS["work_order_no"]: "NODUE", COLUMNS["planned_finish"]: ""}),
        row(**{COLUMNS["work_order_no"]: "DUP"}),
        row(**{COLUMNS["work_order_no"]: "DUP"}),
    ]
    result = run(rows)
    codes = sorted((issue.code, issue.work_order_no) for issue in result.issues)
    report([
        (("V02", "ZERO") in codes, "数量非正 → V02"),
        (("V03", "NODUE") in codes, "交期缺失 → V03"),
        (("V01", "DUP") in codes and len(result.work_orders) == 1, "重复工单 → V01 且只保留一张"),
    ], results)

    print()
    print(RULE)
    print(f"通过 {sum(results)}/{len(results)}")
    print(RULE)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
