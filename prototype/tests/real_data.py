"""真实 ERP 工单取数体检：只读文件、只打印，不跑排程。

用法：
    python prototype/tests/real_data.py 工单.xls
    python prototype/tests/real_data.py 工单.xls --plan-start 2026-10-22 --months 3
    python prototype/tests/real_data.py 工单.xls --csv out/

默认口径：取热压车间、剔除「滚牌/摆牌/铜钉」后道工单、剔除已完成、
只保留 [计划起点, 计划起点 + N 月] 内到期的工单。过去的未结工单单独计数。

资源口径（2026-10-22 确认）：38 台热压机、19 人、一人两台，**同时开 19 台**；
交期沿用 ERP 计划完工日期，热压须提前 15 天完成。
"""

from __future__ import annotations

import argparse
import calendar
import collections
import csv
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.erp import COLUMNS, ImportResult, import_work_orders  # noqa: E402


def _add_months(day: dt.date, months: int) -> dt.date:
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _print(
    result: ImportResult,
    plan_start: dt.date,
    window_end: dt.date,
    *,
    pre_offset_days: int,
    concurrent_presses: int,
    shifts_per_day: int,
) -> None:
    stats = result.stats
    print(f"计划起点 {plan_start}  地平线止 {window_end}")
    print()
    print("取数漏斗")
    print(f"  序时簿总行数            {stats['rows']:>8}")
    print(f"  热压车间                {stats['hot_press_rows']:>8}")
    print(f"  剔除后道工单            {stats['secondary_excluded']:>8}")
    print(f"  剔除已完成              {stats['completed']:>8}")
    print(f"  剔除交期早于计划起点    {stats['overdue_excluded']:>8}")
    print(f"  剔除超出地平线          {stats['beyond_window']:>8}")
    print(f"  交期缺失                {stats['missing_due']:>8}")
    print(f"  数量非正                {stats['non_positive_qty']:>8}")
    print(f"  工单号重复              {stats['duplicates']:>8}")
    print(f"  ────────────────────────────────")
    print(f"  入排程                  {stats['imported']:>8}")

    print()
    print(
        f"入排程规模：{stats['sales_orders']} 个销售订单、"
        f"{stats['product_types']} 个产品类型、{stats['patterns']} 个牌型、"
        f"{stats['qty']:,.0f} pcs"
    )

    print()
    header = f"  {'产品类型':<10}{'工单':>7}{'牌型':>7}{'销售订单':>9}{'数量(pcs)':>15}"
    print(header)
    for code, bucket in stats["by_product_type"].items():
        print(
            f"  {code:<10}{bucket['orders']:>7}{bucket['patterns']:>7}"
            f"{bucket['sales_orders']:>9}{bucket['qty']:>15,.0f}"
        )

    due = collections.Counter(w.due_date for w in result.work_orders)
    print()
    print("到期日分布")
    for day, count in sorted(due.items()):
        print(f"  {day}  {count:>5} 张")

    target_past = [
        w for w in result.work_orders
        if w.due_date - dt.timedelta(days=pre_offset_days) < plan_start
    ]
    print()
    print(f"前置偏移 {pre_offset_days} 天：目标完成日已早于计划起点 "
          f"{len(target_past)} 张 / {sum((w.plan_qty for w in target_past), 0):,.0f} pcs")
    if len(target_past) > len(result.work_orders) // 2:
        print("  超过一半的目标日已在起点之前——若 ERP 日期本就是热压完成日，"
              "应把前置偏移改为 0；否则这是真实的逾期积压。")

    if result.work_orders:
        span_days = (max(w.due_date for w in result.work_orders) - plan_start).days + 1
        press_shifts = max(1, span_days) * shifts_per_day * max(1, concurrent_presses)
        total = sum(w.plan_qty for w in result.work_orders)
        print()
        print(f"产能口径：38 台机器、19 人、一人两台 → **同时开 {concurrent_presses} 台**；"
              f"{span_days} 天 × {shifts_per_day} 班 × {concurrent_presses} 台 = "
              f"{press_shifts} 个机台班")
        print(f"  按此并行度，平均每机台班需产出 {total / press_shifts:,.0f} pcs")
        for ratio in (10, 20, 50, 100):
            print(f"    配比 {ratio:>3} pcs/模 → 每班 {total / press_shifts / ratio:,.0f} 模")

    if result.issues:
        print()
        print(f"输入问题 {len(result.issues)} 条")
        for issue in result.issues[:20]:
            print(f"  {issue.code}  {issue.work_order_no}  {issue.detail}")
        if len(result.issues) > 20:
            print(f"  …… 另有 {len(result.issues) - 20} 条")


def _export(result: ImportResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "work_order.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                COLUMNS["work_order_no"],
                COLUMNS["sales_order_no"],
                "product_type_code",
                "pattern_code",
                COLUMNS["material_code"],
                COLUMNS["qty"],
                COLUMNS["uom"],
                COLUMNS["planned_finish"],
                "status",
                "remaining_qty",
            ]
        )
        for wo in result.work_orders:
            writer.writerow(
                [
                    wo.work_order_no,
                    wo.sales_order_no,
                    wo.product_type_code,
                    wo.pattern_code,
                    wo.material_code,
                    wo.qty,
                    wo.uom,
                    wo.due_date,
                    wo.status.value,
                    "" if wo.remaining_qty is None else wo.remaining_qty,
                ]
            )
    print()
    print(f"已导出 {path}（{len(result.work_orders)} 行）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="ERP 导出的 .xls")
    parser.add_argument("--plan-start", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--months", type=int, default=3)
    parser.add_argument("--include-secondary", action="store_true")
    parser.add_argument("--include-overdue", action="store_true")
    parser.add_argument("--pre-offset", type=int, default=15, help="热压须提前交期完成的天数")
    parser.add_argument("--concurrent-presses", type=int, default=19,
                        help="同时开动的机台数（38 台、19 人、一人两台）")
    parser.add_argument("--shifts", type=int, default=2, help="每天班次")
    parser.add_argument("--csv", type=Path, default=None, help="导出标准化 work_order.csv 的目录")
    args = parser.parse_args(argv)

    window_end = _add_months(args.plan_start, args.months)
    result = import_work_orders(
        args.path,
        plan_start=args.plan_start,
        window_end=window_end,
        include_secondary=args.include_secondary,
        include_overdue=args.include_overdue,
    )
    _print(
        result,
        args.plan_start,
        window_end,
        pre_offset_days=args.pre_offset,
        concurrent_presses=args.concurrent_presses,
        shifts_per_day=args.shifts,
    )
    if args.csv:
        _export(result, args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
