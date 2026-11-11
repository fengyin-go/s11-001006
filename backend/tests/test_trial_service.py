"""试验服务测试：字段级失败解释、版本切换、解析样本交替、并发隔离与淘汰一致性。"""
from __future__ import annotations

import concurrent.futures
import unittest

from app.services.expression_cache import compile_cache
from app.services.trial import TrialRequest, trial_service
from app.store import store


class TrialServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        compile_cache.clear()
        trial_service.clear_history()
        # 准备一条可控的正式校验规则（写的是内存仓库，测试间随进程重建）
        rows = store.rows("validate")
        self.rule_id = max((int(row["id"]) for row in rows), default=0) + 1
        rows.append({
            "id": self.rule_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "规则编号": f"VALI-TEST-{self.rule_id}",
            "校验目标字段": "price",
            "校验表达式": "number(value) >= 10",
            "期望取值": "10",
            "容差范围": "1",
            "规则版本": 1,
            "修订记录": [{"version": 1, "expression": "number(value) >= 10"}],
        })
        parser_rows = store.rows("parser")
        self.template_id = max((int(row["id"]) for row in parser_rows), default=0) + 1
        parser_rows.append({
            "id": self.template_id,
            "status": "已启用",
            "pending": False,
            "abnormal": False,
            "模板编号": "PARS-TEST-1",
            "目标字段名": "price",
            "XPathExpression": "//price",
        })

    def _request(self, sample: dict, **kwargs: object) -> TrialRequest:
        params = {"rule_id": self.rule_id, "sample": sample}
        params.update(kwargs)
        return TrialRequest(**params)  # type: ignore[arg-type]

    def test_passing_trial_keeps_field_level_explanation_and_rule_version(self) -> None:
        result = trial_service.run_trial(self._request({"price": "10.5"}))
        self.assertTrue(result["passed"])
        self.assertTrue(result["compile_ok"])
        self.assertEqual(result["规则版本"], 1)
        stages = {item["stage"] for item in result["field_results"]}
        self.assertEqual(stages, {"字段", "判断"})
        self.assertEqual(result["failure_reasons"], [])

    def test_missing_field_and_tolerance_failure_explained_per_field(self) -> None:
        missing = trial_service.run_trial(self._request({"other": "1"}))
        self.assertFalse(missing["passed"])
        self.assertTrue(any("缺少校验目标字段" in r for r in missing["failure_reasons"]))

        out_of_tolerance = trial_service.run_trial(self._request(
            {"price": "20"}, expression="number(value)", rule_version=1
        ))
        self.assertFalse(out_of_tolerance["passed"])
        reason = out_of_tolerance["field_results"][-1]["reason"]
        self.assertIn("数值比对", reason)
        self.assertIn("容差", reason)

    def test_relative_and_unparseable_tolerance(self) -> None:
        numeric_expr = "number(value)"
        ok = trial_service.run_trial(
            self._request({"price": "10.4"}, expression=numeric_expr,
                          expected="10", tolerance="5%", rule_version=1)
        )
        self.assertTrue(ok["passed"])
        fallback = trial_service.run_trial(
            self._request({"price": "10"}, expression=numeric_expr,
                          expected="10", tolerance="大约一点", rule_version=1)
        )
        self.assertTrue(fallback["passed"])
        self.assertIn("回退精确匹配", fallback["field_results"][-1]["reason"])

    def test_invalid_expression_is_compatible_without_touching_records(self) -> None:
        before = dict(store.find("validate", self.rule_id))  # type: ignore[arg-type]
        result = trial_service.run_trial(
            self._request({"price": "10"}, expression="value >>> 1", rule_version=1)
        )
        self.assertFalse(result["compile_ok"])
        self.assertFalse(result["passed"])
        self.assertTrue(any("表达式不合法" in r for r in result["failure_reasons"]))
        after = store.find("validate", self.rule_id)
        self.assertEqual(before, after)  # 正式记录未被修改
        # 负缓存命中：重复试验复用编译结论
        again = trial_service.run_trial(
            self._request({"price": "10"}, expression="value >>> 1", rule_version=1)
        )
        self.assertTrue(again["cache_hit"])

    def test_two_parser_samples_alternating_never_cross_use(self) -> None:
        sample_a = {"price": "12", "batch": "A"}
        sample_b = {"price": "8", "batch": "B"}
        results = []
        for round_index in range(3):
            sample = sample_a if round_index % 2 == 0 else sample_b
            results.append(trial_service.run_trial(self._request(
                sample,
                sample_source="parser",
                parser_template_id=self.template_id,
            )))
        for index, result in enumerate(results):
            expected_batch = "A" if index % 2 == 0 else "B"
            self.assertEqual(result["sample"]["batch"], expected_batch)
            self.assertEqual(result["sample_source"], "parser")
            self.assertIn("PARS-TEST-1", result["source_note"])
        # A 样本通过、B 样本失败，交替后结论仍各自正确，没有串样本
        self.assertTrue(results[0]["passed"])
        self.assertFalse(results[1]["passed"])
        self.assertTrue(results[2]["passed"])

    def test_version_switch_separates_compilations_and_conclusions(self) -> None:
        v1 = trial_service.run_trial(
            self._request({"price": "5"}, expression="number(value) >= 10", rule_version=1)
        )
        v2 = trial_service.run_trial(
            self._request({"price": "5"}, expression="number(value) >= 1", rule_version=2)
        )
        self.assertFalse(v1["passed"])
        self.assertTrue(v2["passed"])
        self.assertNotEqual(v1["cache_key"], v2["cache_key"])
        # 回到 v1：命中旧编译，结论仍为失败，旧结果没有被 v2 污染
        v1_again = trial_service.run_trial(
            self._request({"price": "5"}, expression="number(value) >= 10", rule_version=1)
        )
        self.assertTrue(v1_again["cache_hit"])
        self.assertEqual(v1_again["cache_key"], v1["cache_key"])
        self.assertFalse(v1_again["passed"])

    def test_concurrent_trials_isolate_samples(self) -> None:
        secret_a = "SECRET-A"
        secret_b = "SECRET-B"

        def trial(batch: str) -> tuple[str, str, bool]:
            sample = {"price": "10.5", "batch": batch, "marker": batch}
            result = trial_service.run_trial(self._request(sample))
            return result["sample"]["marker"], result["sample"]["batch"], result["passed"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(trial, secret_a if index % 2 == 0 else secret_b)
                for index in range(40)
            ]
            markers = [future.result() for future in futures]
        for marker, batch, passed in markers:
            self.assertEqual(marker, batch)
            self.assertTrue(passed)
        # 并发结束后缓存条目里仍找不到任何样本内容
        for entry in compile_cache.inspect_entries():
            self.assertNotIn(secret_a, repr(entry.__dict__))
            self.assertNotIn(secret_b, repr(entry.__dict__))

    def test_eviction_then_retry_yields_same_conclusion(self) -> None:
        # 用数值表达式让容差判断生效（谓词表达式只返回布尔，不参与容差）
        numeric_expr = "number(value)"
        first = trial_service.run_trial(self._request(
            {"price": "20"}, expression=numeric_expr, rule_version=1
        ))
        self.assertFalse(first["passed"])
        # 用其它表达式把该编译结果挤出 LRU
        for index in range(70):
            trial_service.run_trial(self._request(
                {"price": "10"}, expression=f"number(value) >= {index}", rule_version=100 + index
            ))
        self.assertGreaterEqual(compile_cache.stats()["evictions"], 1)
        retried = trial_service.run_trial(self._request(
            {"price": "20"}, expression=numeric_expr, rule_version=1
        ))
        self.assertFalse(retried["passed"])
        self.assertEqual(
            [item["reason"] for item in first["field_results"]],
            [item["reason"] for item in retried["field_results"]],
        )
        self.assertEqual(first["cache_key"], retried["cache_key"])

    def test_trial_history_is_bounded_and_independent_from_records(self) -> None:
        for index in range(80):
            trial_service.run_trial(self._request({"price": str(index)}))
        history = trial_service.list_history()
        self.assertLessEqual(len(history), 50)
        self.assertTrue(all("sample" in item for item in history))


if __name__ == "__main__":
    unittest.main()
