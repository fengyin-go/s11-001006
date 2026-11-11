"""校验表达式编译缓存。

缓存身份（沿现有「校验规则只读数据流」确定）：

    key = sha256(语言版本 | 规则版本 | 表达式原文)

- 语言版本：受限引擎语义版本（``RULE_LANG_VERSION``），引擎升级后旧 key 自然失配；
- 规则版本：登记记录上的「规则版本」，规则修改换版本后，即使表达式文本相同也会
  得到不同 key，旧编译结果不可能混入新结论；
- 表达式原文：逐字符参与哈希，任何改动都产生新身份。

容量淘汰：有界 LRU（OrderedDict），超容量淘汰最久未使用项；淘汰后再次试验会
重新编译并得出相同结论（编译是确定性纯函数）。

隔离红线：缓存值只保存编译产物（AST/code）或编译错误信息，**绝不保存样本数据**；
样本只存在于单次试验调用的局部命名空间里，并发试验之间互不共享。
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from app.services.expression_sandbox import RULE_LANG_VERSION, Evaluator, ExpressionError

DEFAULT_CACHE_CAPACITY = 64


@dataclass(frozen=True)
class CompileResult:
    """一次编译查询的结果；ok=False 时 evaluator 为 None，错误被负缓存。"""

    key: str
    ok: bool
    evaluator: Evaluator | None
    error_message: str | None
    cache_hit: bool
    lang_version: int
    rule_version: int


@dataclass
class _Entry:
    evaluator: Evaluator | None
    error_message: str | None
    lang_version: int
    rule_version: int
    expression: str


class ExpressionCompileCache:
    """线程安全的有界 LRU 编译缓存。"""

    def __init__(self, capacity: int = DEFAULT_CACHE_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("缓存容量必须为正整数")
        self._capacity = capacity
        self._items: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._negative_hits = 0

    @staticmethod
    def build_key(expression: str, rule_version: int) -> str:
        identity = f"lang-v{RULE_LANG_VERSION}|rule-v{int(rule_version)}|{expression}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def get_or_compile(self, expression: str, rule_version: int) -> CompileResult:
        """命中（含负缓存命中）直接复用；未命中则编译并按 LRU 放入。"""
        key = self.build_key(expression, rule_version)
        with self._lock:
            entry = self._items.get(key)
            if entry is not None:
                self._items.move_to_end(key)
                self._hits += 1
                if entry.error_message is not None:
                    self._negative_hits += 1
                return CompileResult(
                    key=key,
                    ok=entry.error_message is None,
                    evaluator=entry.evaluator,
                    error_message=entry.error_message,
                    cache_hit=True,
                    lang_version=entry.lang_version,
                    rule_version=entry.rule_version,
                )

            self._misses += 1
            try:
                evaluator = Evaluator(expression)
                cached = _Entry(
                    evaluator=evaluator,
                    error_message=None,
                    lang_version=RULE_LANG_VERSION,
                    rule_version=int(rule_version),
                    expression=expression,
                )
                ok = True
                error_message = None
            except ExpressionError as exc:
                # 历史不合法表达式：负缓存，避免反复解析；它永远不会产出试验结论。
                cached = _Entry(
                    evaluator=None,
                    error_message=str(exc),
                    lang_version=RULE_LANG_VERSION,
                    rule_version=int(rule_version),
                    expression=expression,
                )
                ok = False
                error_message = str(exc)
                evaluator = None

            self._put(key, cached)
            return CompileResult(
                key=key,
                ok=ok,
                evaluator=evaluator,
                error_message=error_message,
                cache_hit=False,
                lang_version=RULE_LANG_VERSION,
                rule_version=int(rule_version),
            )

    def _put(self, key: str, entry: _Entry) -> None:
        if key in self._items:
            self._items.move_to_end(key)
            return
        self._items[key] = entry
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
            self._evictions += 1

    def evict(self, expression: str, rule_version: int) -> bool:
        key = self.build_key(expression, rule_version)
        with self._lock:
            return self._items.pop(key, None) is not None

    def clear(self) -> int:
        with self._lock:
            removed = len(self._items)
            self._items.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0
            self._negative_hits = 0
            return removed

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "capacity": self._capacity,
                "size": len(self._items),
                "hits": self._hits,
                "misses": self._misses,
                "negative_hits": self._negative_hits,
                "evictions": self._evictions,
                "lang_version": RULE_LANG_VERSION,
                "hit_rate": round(self._hits / (self._hits + self._misses), 4)
                if (self._hits + self._misses)
                else 0.0,
            }

    def inspect_keys(self) -> list[str]:
        """仅供校验/测试：列出当前缓存身份。"""
        with self._lock:
            return list(self._items.keys())

    def inspect_entries(self) -> tuple[_Entry, ...]:
        """仅供校验/测试：确认缓存条目里没有样本数据。"""
        with self._lock:
            return tuple(self._items.values())


# 进程级单例：沿校验模块的只读数据流共享，FastAPI 线程池并发访问由锁保护。
compile_cache = ExpressionCompileCache()
