"""离线样本试验：冷启动 vs 热运行的可复现计数对比演示。

运行（在 backend 目录下）：
    .venv/bin/python -m scripts.benchmark_trial

计数口径完全确定：同一 (表达式, 规则版本) 第一次试验必然 miss 并编译，
之后 N-1 次必然命中；清空缓存后重新开始，计数可逐次复现。
"""
from __future__ import annotations

import time

from app.services.expression_cache import compile_cache
from app.services.trial import TrialRequest, trial_service

REPEAT = 20
SAMPLE = {"price": "10.6", "batch": "bench"}
EXPRESSION = "number(value) >= 10 and len(sample['batch']) > 0"


def _run_round(label: str) -> tuple[int, int]:
    compile_cache.clear()
    hits_before = compile_cache.stats()["hits"]
    misses_before = compile_cache.stats()["misses"]
    started = time.perf_counter()
    for _ in range(REPEAT):
        trial_service.run_trial(TrialRequest(
            rule_id=None,
            sample=dict(SAMPLE),
            expression=EXPRESSION,
            rule_version=1,
        ))
    elapsed_ms = (time.perf_counter() - started) * 1000
    stats = compile_cache.stats()
    hits = stats["hits"] - hits_before
    misses = stats["misses"] - misses_before
    print(
        f"{label}: 试验 {REPEAT} 次 | 编译未命中 {misses} 次 | 编译命中 {hits} 次 | "
        f"淘汰 {stats['evictions']} 次 | 耗时 {elapsed_ms:.2f} ms"
    )
    return misses, hits


def main() -> None:
    print(f"表达式：{EXPRESSION}")
    cold_misses, cold_hits = _run_round("冷启动（清空缓存后首轮）")
    hot_misses, hot_hits = _run_round("再清缓存复现一轮    ")
    hot_misses_2, hot_hits_2 = _run_round("第三轮复现          ")
    assert (cold_misses, cold_hits) == (1, REPEAT - 1)
    assert (hot_misses, hot_hits) == (hot_misses_2, hot_hits_2) == (1, REPEAT - 1)
    print("结论：每轮计数固定为 1 次编译 + 19 次缓存命中，冷/热结果可复现。")


if __name__ == "__main__":
    main()
