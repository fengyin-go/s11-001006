"""编译缓存测试：身份构成、LRU 淘汰、负缓存与版本隔离。"""
from __future__ import annotations

import unittest

from app.services.expression_cache import ExpressionCompileCache


class CompileCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = ExpressionCompileCache(capacity=3)

    def test_identity_includes_expression_lang_and_rule_version(self) -> None:
        key_a = self.cache.build_key("number(value) > 1", 1)
        key_b = self.cache.build_key("number(value) > 1", 2)
        key_c = self.cache.build_key("number(value) > 2", 1)
        self.assertEqual(len({key_a, key_b, key_c}), 3)
        self.assertEqual(key_a, self.cache.build_key("number(value) > 1", 1))

    def test_hot_run_reuses_compilation(self) -> None:
        first = self.cache.get_or_compile("number(value) > 1", 1)
        second = self.cache.get_or_compile("number(value) > 1", 1)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertIs(first.evaluator, second.evaluator)
        stats = self.cache.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)

    def test_rule_change_keeps_old_compilation_out_of_new_conclusion(self) -> None:
        v1 = self.cache.get_or_compile("number(value) > 1", rule_version=1)
        v2 = self.cache.get_or_compile("number(value) > 1", rule_version=2)
        self.assertFalse(v2.cache_hit)
        self.assertIsNot(v1.evaluator, v2.evaluator)
        self.assertEqual(v1.rule_version, 1)
        self.assertEqual(v2.rule_version, 2)
        # 两个版本的编译结果都在缓存里且身份不同，互不串用
        self.assertEqual(len(self.cache.inspect_keys()), 2)

    def test_lru_eviction_and_deterministic_recompile(self) -> None:
        expressions = [f"number(value) > {i}" for i in range(3)]
        for index, expression in enumerate(expressions):
            self.cache.get_or_compile(expression, 1)
        # 访问最早的一项把它变成最近使用
        self.cache.get_or_compile(expressions[0], 1)
        self.cache.get_or_compile("number(value) < 9", 1)  # 触发淘汰 expressions[1]
        stats = self.cache.stats()
        self.assertEqual(stats["evictions"], 1)
        self.assertEqual(stats["size"], 3)
        # 淘汰项重新编译：结论一致、身份一致
        key_before_evict = self.cache.build_key(expressions[1], 1)
        recompiled = self.cache.get_or_compile(expressions[1], 1)
        self.assertEqual(recompiled.key, key_before_evict)
        self.assertFalse(recompiled.cache_hit)
        sample = {"value": "5"}
        self.assertTrue(recompiled.evaluator.run({"value": "5", "field": "value", "sample": sample}))

    def test_invalid_expression_is_negative_cached(self) -> None:
        first = self.cache.get_or_compile("value >>> 1", 1)
        second = self.cache.get_or_compile("value >>> 1", 1)
        self.assertFalse(first.ok)
        self.assertFalse(second.ok)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.error_message, second.error_message)
        self.assertEqual(self.cache.stats()["negative_hits"], 1)
        # 负缓存条目同样不含任何样本结构
        for entry in self.cache.inspect_entries():
            self.assertIsNone(entry.evaluator)
            self.assertNotIn("sample", entry.__dict__)

    def test_cache_entries_never_contain_sample_data(self) -> None:
        secret = "SENSITIVE-A"
        result = self.cache.get_or_compile("number(sample['price']) > 1", 1)
        result.evaluator.run({
            "value": None,
            "field": "price",
            "sample": {"price": "12", "secret": secret},
        })
        for entry in self.cache.inspect_entries():
            rendered = repr(entry.__dict__)
            self.assertNotIn(secret, rendered)
            # 缓存条目只允许出现表达式/版本/编译产物这类身份信息
            self.assertEqual(set(entry.__dict__), {
                "evaluator", "error_message", "lang_version",
                "rule_version", "expression",
            })

    def test_lang_version_participates_in_identity(self) -> None:
        from app.services import expression_cache as cache_module

        key_v1 = self.cache.build_key("value > 1", 1)
        original = cache_module.RULE_LANG_VERSION
        try:
            cache_module.RULE_LANG_VERSION = original + 1
            key_v2 = self.cache.build_key("value > 1", 1)
        finally:
            cache_module.RULE_LANG_VERSION = original
        self.assertNotEqual(key_v1, key_v2)
        self.assertEqual(len(key_v1), 64)


if __name__ == "__main__":
    unittest.main()
