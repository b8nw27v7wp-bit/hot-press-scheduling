"""负向校验：逐条构造违反 C01–C15 的最小数据集，确认每条检查真的会报警。

校验器在合法数据上通过等于没有验证——它的全部价值在于能抓出非法数据。
因此本文件对每条检查都造一份「恰好违反该条」的数据，断言确实报警。

只读引擎输出并做副本突变，或手工构造最小对象，不改动任何被测代码。
用法： py tests/negative_check.py
"""

from __future__ import annotations

import copy
import sys
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import (  # noqa: E402
    Assignment,
    Batch,
    CalendarDay,
    Mold,
    MoldSet,
    MoldType,
    Params,
    Press,
    ProductType,
    WorkOrder,
    WorkOrderStatus,
)
from engine.calendar import Calendar  # noqa: E402
from engine.scheduler import Engine  # noqa: E402
from verify.constraint_checker import (  # noqa: E402
    check_all,
    check_determinism,
    fingerprint,
)

PLAN_START = date(2026, 1, 5)
D = Decimal
PASS: list[str] = []
FAIL: list[str] = []


def cal_days(n: int = 60, shifts: int = 2):
    out = {}
    cur = PLAN_START
    for _ in range(n):
        if cur.weekday() < 5:
            out[cur] = CalendarDay(cur, shifts=shifts)
        cur += timedelta(days=1)
    return out


def mk_cal(params: Params, n: int = 60):
    return Calendar(cal_days(n), params, PLAN_START + timedelta(days=n))


def report(code: str, fired: bool, note: str):
    tag = "PASS" if fired else "FAIL"
    (PASS if fired else FAIL).append(code)
    print(f"[{tag}] {code}: {note}")


# ----------------------------------------------------------------------------
# 基准场景：单机台 R1，套 S1 = {M1 覆盖 X, M2 覆盖 Y}，制造真实换模
# ----------------------------------------------------------------------------

def baseline():
    params = Params(plan_start=PLAN_START, changeover_hours=2, shift_hours=12)
    M1 = Mold("M1", "S1", MoldType.SINGLE, 1, {"X": D(2)})
    M2 = Mold("M2", "S1", MoldType.SINGLE, 2, {"Y": D(2)})
    S1 = MoldSet("S1", "P01", 1, [M1, M2])
    presses = {"R1": Press("R1", 10, "P01")}
    ptypes = {"P01": ProductType("P01", "三角牌", ("A01",))}
    orders = [
        WorkOrder("W1", "SO1", "P01", "X", D(100), "张", date(2026, 2, 10)),
        WorkOrder("W2", "SO2", "P01", "Y", D(100), "张", date(2026, 2, 12)),
    ]
    eng = Engine(params, ptypes, {"S1": S1}, presses, cal_days(), orders)
    res = eng.run()
    odict = {w.work_order_no: w for w in orders}
    return params, {"S1": S1}, presses, odict, res, eng


def run(ctx_assign, ctx_batch, orders, presses, sets, params, days=60, reported=None):
    cal = mk_cal(params, days)
    return check_all(ctx_assign, ctx_batch, orders, presses, cal, sets, params, reported)


def main():
    params, sets, presses, orders, res, eng = baseline()
    print("== 基准 ==")
    base_errs = run(res.assignments, res.batches, orders, presses, sets, params)
    print(f"基准 check_all -> {base_errs}")
    assert base_errs == [], "基准必须通过"
    print(f"基准批次: {[(b.batch_no, b.mold_no, b.press_no, b.start_slot, b.end_slot, b.changeover_cycles) for b in res.batches]}")
    print()

    # ---------------- C01 机台重叠 ----------------
    A = Assignment("W1", "SO1", "P01", "X", "BA", "R1", 0, "S1", "M1", D(100), 50, 0, 5)
    Bb = Assignment("W2", "SO2", "P01", "Y", "BB", "R1", 0, "S1", "M2", D(100), 50, 3, 8)
    bA = Batch("BA", "SO1", 0, "S1", "M1", "R1", ["W1"], {"X"}, 0, {}, 0, 5, 0)
    bB = Batch("BB", "SO2", 0, "S1", "M2", "R1", ["W2"], {"Y"}, 0, {}, 3, 8, 2)
    e = run([], [bA, bB], {}, presses, sets, params)
    report("C01", any(x.startswith("C01") for x in e), f"同机台 R1 两批次 0-5 与 3-8 重叠 -> {e}")

    # ---------------- C02 模具时间重叠 ----------------
    bC = Batch("BC", "SO1", 0, "S1", "M1", "R2", ["W1"], {"X"}, 0, {}, 0, 5, 0)
    bD = Batch("BD", "SO1", 0, "S1", "M1", "R1", ["W1"], {"X"}, 0, {}, 3, 8, 0)
    presses2 = {"R1": presses["R1"], "R2": Press("R2", 10, "P01")}
    e = run([], [bC, bD], {}, presses2, sets, params)
    report("C02", any(x.startswith("C02") for x in e), f"同模具 M1 两机台时间重叠 -> {e}")

    # ---------------- C03 越界 ----------------
    a = copy.deepcopy(res.assignments)
    a[0].end_slot = len(mk_cal(params))  # >= len(calendar)
    e = run(a, res.batches, orders, presses, sets, params)
    report("C03", any(x.startswith("C03") for x in e), f"工单 {a[0].work_order_no} end_slot 越界 -> {e}")

    # ---------------- C04 机台适用性 ----------------
    a = copy.deepcopy(res.assignments)
    a[0].press_no = "GHOST"
    e = run(a, res.batches, orders, presses, sets, params)
    report("C04", any(x.startswith("C04") for x in e), f"引用不存在机台 GHOST -> {e}")

    # ---------------- C05 模具归属 ----------------
    a = copy.deepcopy(res.assignments)
    a[0].set_no = "WRONG"
    e = run(a, res.batches, orders, presses, sets, params)
    report("C05", any(x.startswith("C05") for x in e), f"工单 set_no 与模具所属套不符 -> {e}")

    # ---------------- C06 同套单组 ----------------
    # 同一套 S1 被组 0 与组 1 同时占用（不同模具，避开 C02）
    bE = Batch("BE", "SO1", 0, "S1", "M1", "R1", [], set(), 0, {}, 0, 5, 0)
    bF = Batch("BF", "SO1", 1, "S1", "M2", "R2", [], set(), 0, {}, 3, 8, 0)
    presses3 = {"R1": presses["R1"], "R2": Press("R2", 10, "P01")}
    e = run([], [bE, bF], {}, presses3, sets, params)
    report("C06", any(x.startswith("C06") for x in e), f"套 S1 被组0/组1 同时占用 -> {e}")

    # ---------------- C07 批次模数 ----------------
    b = copy.deepcopy(res.batches)
    b[0].cycles += 1
    e = run(res.assignments, b, orders, presses, sets, params)
    report("C07", any(x.startswith("C07") for x in e), f"批次 {b[0].batch_no} 模数+1 -> {e}")

    # ---------------- C08 换模 ----------------
    # (a) 不同模具换模不足
    b = copy.deepcopy(res.batches)
    # 找到按开始槽位排序后、换模分支的那一批（第 2 批，M1->M2）
    b[1].changeover_cycles = 0
    e = run(res.assignments, b, orders, presses, sets, params)
    report("C08a", any(x.startswith("C08") for x in e), f"不同模具换模计 0（<配置值）-> {e}")

    # (b) 同模具却计了换模：另造同模两批次
    M3 = Mold("M3", "S1", MoldType.SINGLE, 1, {"X": D(2)})
    S3 = MoldSet("S1", "P01", 1, [M3])
    bG = Batch("BG", "SO1", 0, "S1", "M3", "R1", [], set(), 0, {}, 0, 3, 0)
    bH = Batch("BH", "SO2", 0, "S1", "M3", "R1", [], set(), 0, {}, 5, 8, 2)
    e = run([], [bG, bH], {}, presses, {"S1": S3}, params)
    report("C08b", any(x.startswith("C08") for x in e), f"同模具 M3 却计换模 2 -> {e}")

    # ---------------- C09 拆分 ----------------
    a = copy.deepcopy(res.assignments) + [copy.deepcopy(res.assignments[0])]
    e = run(a, res.batches, orders, presses, sets, params)
    report("C09", any(x.startswith("C09") for x in e), f"同一工单出现 2 条记录 -> {e}")

    # ---------------- C10 漏排 / 多排 ----------------
    a = copy.deepcopy(res.assignments)[:1]  # 丢掉 W2 的排程
    e = run(a, res.batches, orders, presses, sets, params)
    report("C10-miss", any("未出现在结果中" in x for x in e), f"可排工单 W2 未出现 -> {e}")

    # 多排：为不可排（qty=0）且非终态工单造记录，避开 C11
    o_zero = WorkOrder("WZ", "SOZ", "P01", "X", D(0), "张", date(2026, 2, 10))
    a = copy.deepcopy(res.assignments) + [
        Assignment("WZ", "SOZ", "P01", "X", "BA", "R1", 0, "S1", "M1", D(0), 0, 0, 0)
    ]
    e = run(a, res.batches, {**orders, "WZ": o_zero}, presses, sets, params)
    report("C10-extra", any("不应排程" in x for x in e), f"结果含不可排工单 WZ -> {e}")

    # ---------------- C11 终态工单 ----------------
    # 用批次途径隔离：把已完成工单塞进某批次，qty 小且牌型被覆盖、同销售订单
    o_done = WorkOrder("WD", "SO1", "P01", "X", D(10), "张", date(2026, 2, 10),
                       WorkOrderStatus.COMPLETED)
    b = copy.deepcopy(res.batches)
    b[0].work_order_nos = b[0].work_order_nos + ["WD"]
    e = run(res.assignments, b, {**orders, "WD": o_done}, presses, sets, params)
    report("C11", any(x.startswith("C11") for x in e), f"批次含 COMPLETED 工单 WD -> {e}")

    # ---------------- C12 跨销售订单 ----------------
    bX = Batch("BX", "SO1", 0, "S1", "M1", "R1", ["W1", "W2"], {"X", "Y"}, 0, {}, 0, 3, 0)
    e = run([], [bX], orders, presses, sets, params)
    report("C12", any(x.startswith("C12") for x in e), f"批次含 SO1 与 SO2 两订单 -> {e}")

    # C12 开关：allow_cross=True 时直接返回 []
    p_cross = Params(plan_start=PLAN_START, allow_cross_sales_order_batch=True)
    e = run([], [bX], orders, presses, sets, p_cross)
    report("C12-switch", not any(x.startswith("C12") for x in e),
           f"allow_cross_sales_order_batch=True 时 C12 被跳过 -> {e}")

    # ---------------- C13 数量 ----------------
    a = copy.deepcopy(res.assignments)
    a[0].cycles = 1  # 1*2=2 < 100
    e = run(a, res.batches, orders, presses, sets, params)
    report("C13-low", any("模数不足" in x for x in e), f"模数 1 产量 2 < 需求 100 -> {e}")

    a = copy.deepcopy(res.assignments)
    a[0].cycles = 200  # 200*2=400, 400-100=300 >= ratio 2
    e = run(a, res.batches, orders, presses, sets, params)
    report("C13-high", any("模数过剩" in x for x in e), f"模数 200 产量 400 远超需求 -> {e}")

    # ---------------- C14 确定性（不在 check_all 内） ----------------
    f1 = fingerprint(res.assignments, res.batches)
    res2 = Engine(params, {"P01": ProductType("P01", "三角牌", ("A01",))},
                  sets, presses, cal_days(), list(orders.values())).run()
    f2 = fingerprint(res2.assignments, res2.batches)
    same = check_determinism(f1, f2) == []
    mut = copy.deepcopy(res.assignments)
    mut[0].start_slot += 1
    diff = check_determinism(f1, fingerprint(mut, res.batches)) != []
    report("C14", same and diff, f"同输入指纹一致={same}；改一槽位后不一致={diff}")

    # ---------------- 空 / 退化输入 ----------------
    e = run([], [], {}, presses, sets, params)
    report("EMPTY-truly", e == [], f"完全空输入 check_all -> {e}（是否静默通过）")

    # 有可排工单但结果为空
    e = run([], [], orders, presses, sets, params)
    report("EMPTY-orders", any("未出现在结果中" in x for x in e),
           f"有可排工单但无结果 -> {e}")

    # 有 assignments 但 batches 为空（批次整体失败）——C15 必须抓到
    e = run(res.assignments, [], orders, presses, sets, params)
    report("NOBATCH", any(x.startswith("C15") for x in e),
           f"assignments 非空但 batches 为空 -> {e}")

    # assignment 引用不存在的批次——C15 必须抓到
    a = copy.deepcopy(res.assignments)
    a[0].batch_no = "NOPE"
    e = run(a, res.batches, orders, presses, sets, params)
    report("BATCHREF", any(x.startswith("C15") for x in e),
           f"assignment.batch_no 指向不存在批次 -> {e}")

    print()
    print("== 可达性 / 排序 / 退化 深度分析 ==")
    reachability_and_ordering()
    adversarial_c08()
    adversarial_ordering()

    print()
    print(f"通过 {len(PASS)}/{len(PASS) + len(FAIL)}")
    if FAIL:
        print("未触发：", FAIL)
    return 0


def _big_run():
    """中等规模真实引擎输出，用于可达性/排序扫描。"""
    import random
    rng = random.Random(7)
    params = Params(plan_start=PLAN_START, changeover_hours=2, shift_hours=12,
                    group_size=2, allow_cross_sales_order_batch=False)
    ptypes = {f"P{i:02d}": ProductType(f"P{i:02d}", f"类型{i}", (f"A{i:02d}",)) for i in range(1, 6)}
    mold_sets = {}
    seq = 0
    for i in range(1, 6):
        for s in range(rng.randint(2, 3)):
            seq += 1
            molds = []
            for m in range(rng.randint(2, 3)):
                pats = ["X", "Y"] if m % 2 == 0 else ["X"]
                molds.append(Mold(f"M{seq:03d}{m}", f"S{seq:03d}", MoldType.MULTI, m + 1,
                                  {p: D(rng.choice([2, 3, 4])) for p in pats}))
            mold_sets[f"S{seq:03d}"] = MoldSet(f"S{seq:03d}", f"P{i:02d}", s + 1, molds)
    presses = {}
    ps = 0
    for i in range(1, 6):
        for _ in range(rng.choice([2, 4])):
            ps += 1
            presses[f"R{ps:03d}"] = Press(f"R{ps:03d}", rng.choice([10, 20]), f"P{i:02d}")
    orders = []
    for i in range(400):
        pt = f"P{rng.randint(1, 5):02d}"
        orders.append(WorkOrder(f"WO{i:04d}", f"SO{rng.randint(1, 80):04d}", pt,
                                rng.choice(["X", "Y"]), D(rng.randint(20, 400)), "张",
                                PLAN_START + timedelta(days=rng.randint(40, 110))))
    eng = Engine(params, ptypes, mold_sets, presses, cal_days(200), orders)
    res = eng.run()
    return params, mold_sets, presses, {w.work_order_no: w for w in orders}, res


def reachability_and_ordering():
    params, mold_sets, presses, orders, res = _big_run()
    errs = check_all(res.assignments, res.batches, orders, presses, res.calendar, mold_sets,
                     params, reported={e.work_order_no for e in res.exceptions})
    print(f"规模场景 check_all -> {len(errs)} 项错误 {errs[:3]}")

    # 1) C06 可达性：是否存在「同套不同组」的批次
    set_to_groups = {}
    for b in res.batches:
        set_to_groups.setdefault(b.set_no, set()).add(b.group_no)
    multi = {s: g for s, g in set_to_groups.items() if len(g) > 1}
    print(f"C06: 同套出现多组的套 -> {multi}（空=引擎结构上不可达）")

    # 2) 排序：每台机台 start_slot 是否随 batch_no（落位顺序）单调（只看已排批次）
    bad = []
    for press_no in {b.press_no for b in res.batches if b.start_slot >= 0}:
        bs = sorted((b for b in res.batches if b.press_no == press_no and b.start_slot >= 0),
                    key=lambda x: x.batch_no)
        starts = [b.start_slot for b in bs]
        if starts != sorted(starts) or len(starts) != len(set(starts)):
            bad.append((press_no, [(b.batch_no, b.start_slot) for b in bs]))
    print(f"C08: 落位顺序 vs 开始槽位顺序不一致的机台 -> {bad}（空=排序无隐患）")

    # 2b) C08 正确性：真实数据上逐对断言换模值正确
    mism = []
    for press_no in {b.press_no for b in res.batches if b.start_slot >= 0}:
        p = presses[press_no]
        req = -(-params.changeover_hours * p.capacity_per_shift // params.shift_hours)
        bs = sorted((b for b in res.batches if b.press_no == press_no and b.start_slot >= 0),
                    key=lambda x: x.start_slot)
        for prev, b in zip(bs, bs[1:]):
            want = 0 if prev.mold_no == b.mold_no else req
            if b.changeover_cycles != want:
                mism.append((press_no, prev.batch_no, b.batch_no, b.changeover_cycles, want))
    print(f"C08: 真实数据中换模值不匹配的对 -> {mism}（空=C08 无假阳/假阴）")

    # 3) C13 上界可达性：是否存在 produced - qty >= ratio
    over = []
    for a in res.assignments:
        entry = None
        for s in mold_sets.values():
            for m in s.molds:
                if m.mold_no == a.mold_no:
                    entry = m
        r = entry.ratios.get(a.pattern_code)
        if r is not None and D(a.cycles) * r - a.plan_qty >= r:
            over.append(a.work_order_no)
    print(f"C13: 上界（produced-qty>=ratio）触发的工单 -> {over}（空=上界不可达）")

    # 4) C07 不变量：b.cycles == max ceil(qty/ratio)
    bad7 = []
    for b in res.batches:
        entry = next(m for s in mold_sets.values() for m in s.molds if m.mold_no == b.mold_no)
        exp = 0
        for no in b.work_order_nos:
            wo = orders.get(no)
            if wo is None:
                continue
            r = entry.ratios.get(wo.pattern_code)
            if r:
                exp = max(exp, int((wo.plan_qty / r).to_integral_value(rounding=ROUND_CEILING)))
        if b.cycles != exp:
            bad7.append((b.batch_no, b.cycles, exp))
    print(f"C07: 不变量被破坏的批次 -> {bad7}（空=恒成立）")

    # 5) C12 可达性：引擎输出中是否有跨销售订单批次
    cross = [b.batch_no for b in res.batches
             if len({orders[n].sales_order_no for n in b.work_order_nos if n in orders}) > 1]
    print(f"C12: 引擎输出中的跨订单批次 -> {cross}（空=引擎不可达）")

    # 6) 批次引用的机台是否存在
    ghost = [b.batch_no for b in res.batches if b.press_no not in presses]
    print(f"批次引用不存在机台 -> {ghost}")


def adversarial_c08():
    """C08 逻辑隐患：落位顺序 ≠ 开始槽位顺序时的错配。"""
    params = Params(plan_start=PLAN_START, changeover_hours=2, shift_hours=12)
    M1 = Mold("M1", "S1", MoldType.SINGLE, 1, {"X": D(2)})
    M2 = Mold("M2", "S1", MoldType.SINGLE, 2, {"X": D(2)})
    S1 = MoldSet("S1", "P01", 1, [M1, M2])
    presses = {"R1": Press("R1", 10, "P01")}
    # 落位顺序 P(先, mold M1, changeover 0) → Q(后, mold M1, changeover 0)，
    # 但 Q 因回填落到更早的槽位：C08 按 start 排序会把 Q 当 prev，
    # 于是 Q(M1)->P(M1) 同模，若 P 计了换模就误报。
    P = Batch("P", "SO1", 0, "S1", "M1", "R1", [], set(), 0, {}, 10, 12, 0)
    Q = Batch("Q", "SO1", 0, "S1", "M1", "R1", [], set(), 0, {}, 0, 5, 0)
    e = run([], [P, Q], {}, presses, {"S1": S1}, params)
    print(f"C08 错配探针: 同模两批次、start 逆序 -> {e}")

    # 假阴性探针：机台第一序批次（无 prev）计了换模，C08 不查
    R = Batch("R", "SO1", 0, "S1", "M1", "R1", [], set(), 0, {}, 0, 5, 7)
    e = run([], [R], {}, presses, {"S1": S1}, params)
    print(f"C08 假阴性探针: 首批次 changeover=7 无 prev -> {e}（空=漏检）")

    # 假阳性探针：同机台区间重叠（C01 已破）时，按 start 排序的 prev 不是
    # 时间上真正的前序，C08 会误报。
    # Q: M1 0-10（长，真正前序，10 结束）；P: M2 2-4（短，start 更大）；
    # b: M1 12-14，相对 Q 同模应为 0 换模，但按 start 排序 prev=P(M2)。
    Q = Batch("Q", "SO1", 0, "S1", "M1", "R1", [], set(), 0, {}, 0, 10, 0)
    P = Batch("P", "SO1", 0, "S1", "M2", "R1", [], set(), 0, {}, 2, 4, 2)
    b2 = Batch("b2", "SO1", 0, "S1", "M1", "R1", [], set(), 0, {}, 12, 14, 0)
    e = run([], [Q, P, b2], {}, presses, {"S1": S1}, params)
    print(f"C08 假阳性探针（区间重叠）-> {e}")


def adversarial_ordering():
    """C08 依赖 start_slot 单调性；构造落位顺序与 start 逆序的真实风格数据。"""
    # 见 _big_run 的排序扫描；这里再直接验证一个回填+多机台场景
    params = Params(plan_start=PLAN_START, changeover_hours=2, shift_hours=12, group_size=2)
    M1 = Mold("M1", "S1", MoldType.SINGLE, 1, {"X": D(2)})
    M2 = Mold("M2", "S1", MoldType.SINGLE, 2, {"X": D(2)})
    S1 = MoldSet("S1", "P01", 1, [M1, M2])
    presses = {"R1": Press("R1", 10, "P01"), "R2": Press("R2", 10, "P01")}
    orders = [WorkOrder(f"W{i:02d}", f"SO{i}", "P01", "X", D(200), "张",
                        PLAN_START + timedelta(days=40 + i)) for i in range(1, 9)]
    eng = Engine(params, {"P01": ProductType("P01", "t", ())}, {"S1": S1}, presses,
                 cal_days(120), orders)
    res = eng.run()
    bad = []
    for press_no in {b.press_no for b in res.batches}:
        bs = sorted((b for b in res.batches if b.press_no == press_no), key=lambda x: x.batch_no)
        starts = [b.start_slot for b in bs]
        if starts != sorted(starts):
            bad.append((press_no, [(b.batch_no, b.start_slot) for b in bs]))
    print(f"回填+多机台: 落位顺序逆于 start 的机台 -> {bad}（空=无隐患）")


if __name__ == "__main__":
    raise SystemExit(main())
