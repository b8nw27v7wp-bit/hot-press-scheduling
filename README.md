# 热压排程（hot-press scheduling）

多品种小尺寸成型产品的热压工单排程：把客户订单拆成热压工单，按机台、模具、人工与前置期约束排出可执行班次计划，并给出约束校验与「为什么这样排」的解释。

本项目只排**第一道热压**工序。进桶/滚筒、摆牌、点漆不在范围内，但输出会带**可释放数量与预计释放时间**，作为工序间接口。

- 需求原文：`热压排程算法说明.md`（客户书面口径，含流程图与验收标准）
- 算法设计：`热压排程算法设计.md`
- 现场口径与真实数据：`热压排程项目背景与已知知识.md`

## 现场口径（2026-10-22 确认）

| 项 | 值 |
|---|---|
| 热压机 | 38 台 |
| 操作工 | 19 人，一人两台，同时开 19 台 |
| 前置期 | 15 天 |
| 排程范围 | 仅热压，下游工序不排 |
| 工单来源 | 金蝶 K3《生产任务单序时簿》，全厂 43,967 行 |

口径优先级：**客户书面需求 > 现场确认 > 真实数据 > 设计稿/参考实现**，冲突按此顺序裁决并在背景文档留痕。

## 目录

```
prototype/
  domain/models.py            # 领域模型：工单、机台、模具、班次
  engine/scheduler.py         # 排程主流程（1045 行）
  engine/scheduling.py        # 择优与贪心填充
  engine/capacity.py          # 产能核算
  engine/molding.py           # 模具与换模
  engine/calendar.py          # 班次日历
  engine/delay.py             # 延期与前置期
  engine/explain.py           # 排程解释（每个决定给出依据）
  ingest/erp.py               # 金蝶 K3 .xls 导出解析（可选依赖 xlrd）
  verify/constraint_checker.py# 约束校验器 C01-C16（739 行，独立于引擎）
  tests/                      # 场景/负例/稳定性/金样/单测
```

核心引擎零第三方依赖；仅读取 ERP 导出的 `.xls`（BIFF 二进制）需要可选依赖。

## 环境

Python ≥ 3.14（`pyproject.toml` 的 `requires-python`）。

```bash
pip install -e ".[erp]"     # 只有需要读 .xls 工单时才装可选依赖
```

## 跑测试

全部入口在 `prototype/` 下逐个执行，退出码 0 即通过：

```bash
cd prototype
python tests/selftest.py        # 约束 C01-C16 校验（含真实换模的 C08），由 verify/constraint_checker.py 判定
python tests/scenarios.py       # 场景用例，输出「通过 64/64」
python tests/negative_check.py  # 负例，输出「通过 27/27」
python tests/stability.py       # 输入扰动稳定性，输出「通过 17/17」
python tests/golden.py          # 金样回归，输出「通过 22/22」
python tests/units.py           # 单元测试，输出「通过 5/5」
```

另有三个探针脚本（产出数据而非断言）：`benchmark.py`（性能）、`sensitivity.py`（参数敏感性）、`hash_probe.py`（哈希稳定性）、`demo_cases.py`、`real_data.py`、`ingest_erp.py`（真实工单导入演练）。

## 文档索引

| 文档 | 内容 |
|---|---|
| `热压排程算法说明.md` | 客户需求原文与验收标准 |
| `热压排程算法设计.md` | 算法设计与实现落点（最长的一份） |
| `热压排程项目背景与已知知识.md` | 企业背景、现场口径、真实工单事实 |
| `热压排程输入输出字段字典.md` | 字段级契约 |
| `热压排程资源数据口径缺口清单.md` | 缺失数据与假设 |
| `热压排程结构性假设影响评估.md` | 假设对结论的影响 |
| `热压排程参数敏感性分析.md` | 参数扰动与结论稳健性 |
| `热压排程算法自身问题清单.md` | 已知缺陷与待办 |
| `热压排程算法落地计划.md` | 阶段计划 |
| `热压排程代码审核记录.md` | 代码审核结论与修复记录 |

## 已知限制

- 下游工序不排，只给「可释放数量 / 预计释放时间」，下游是否吃得下需另行评估。
- 资源数据存在口径缺口，见 `热压排程资源数据口径缺口清单.md`；排程结论建立在文中列明的假设之上。
- 结构性假设的影响范围见 `热压排程结构性假设影响评估.md`，改动假设后必须重跑 `tests/golden.py`。
