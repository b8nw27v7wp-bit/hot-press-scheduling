"""子进程探针：跑黄金数据集，把结果指纹打到 stdout。

供 `stability.py` 以不同 `PYTHONHASHSEED` 调用。同进程重复调用测不出
`set`/`dict` 遍历顺序导致的不确定性——同一进程里哈希种子是固定的，
两次运行必然一致，无论引擎是否依赖遍历顺序。只有换进程 + 换种子才能测出来。

用法： py tests/hash_probe.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.scheduler import Engine  # noqa: E402
from tests.golden import build  # noqa: E402
from verify.constraint_checker import fingerprint  # noqa: E402


def main() -> int:
    params, product_types, mold_sets, presses, calendar_days, orders = build()
    result = Engine(params, product_types, mold_sets, presses, calendar_days, orders).run()
    print(
        fingerprint(
            result.assignments, result.batches, result.order_delivery, result.release_plan
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
