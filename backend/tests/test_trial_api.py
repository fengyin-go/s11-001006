"""接口级测试：试验接口、解析器送样、版本修订与缓存观测的端到端链路。"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.services.expression_cache import compile_cache
from app.services.trial import trial_service
from app.store import store


class TrialApiTest(unittest.TestCase):
    def setUp(self) -> None:
        compile_cache.clear()
        trial_service.clear_history()
        self.client = TestClient(app)
        rows = store.rows("validate")
        self.rule_id = max((int(row["id"]) for row in rows), default=0) + 1
        rows.append({
            "id": self.rule_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "规则编号": f"VALI-API-{self.rule_id}",
            "校验目标字段": "price",
            "校验表达式": "number(value) >= 10",
            "期望取值": "10",
            "容差范围": "1",
            "规则版本": 1,
            "修订记录": [],
        })

    def test_trial_cold_then_hot_cache_counts(self) -> None:
        payload = {"rule_id": self.rule_id, "sample": {"price": "10.5"}}
        first = self.client.post("/api/validate/trials", json=payload)
        second = self.client.post("/api/validate/trials", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["cache_hit"])
        self.assertTrue(second.json()["cache_hit"])
        stats = self.client.get("/api/validate/cache").json()
        self.assertEqual(stats["misses"], 1)
        self.assertGreaterEqual(stats["hits"], 1)

    def test_parser_handoff_feeds_same_sample_into_trial(self) -> None:
        parser_rows = store.rows("parser")
        template_id = max((int(row["id"]) for row in parser_rows), default=0) + 1
        parser_rows.append({
            "id": template_id,
            "status": "已启用",
            "pending": False,
            "abnormal": False,
            "模板编号": "PARS-API-1",
            "目标字段名": "price",
            "XPath表达式": "//price",
        })
        sample = {"price": "10.2", "trace": "api-handoff"}
        handoff = self.client.post("/api/parser/sample-handoff", json={
            "sample": sample,
            "parser_template_id": template_id,
        })
        self.assertEqual(handoff.status_code, 200)
        self.assertEqual(handoff.json()["sample"], sample)

        trial = self.client.post("/api/validate/trials", json={
            "rule_id": self.rule_id,
            "sample": sample,
            "sample_source": "parser",
            "parser_template_id": template_id,
        }).json()
        self.assertTrue(trial["passed"])
        self.assertEqual(trial["sample"]["trace"], "api-handoff")
        self.assertIn("PARS-API-1", trial["source_note"])

    def test_revision_bumps_version_and_old_compilation_not_reused(self) -> None:
        old = self.client.post("/api/validate/trials", json={
            "rule_id": self.rule_id, "sample": {"price": "5"},
        }).json()
        self.assertFalse(old["passed"])

        revised = self.client.post(f"/api/validate/{self.rule_id}/revision", json={
            "values": {"校验表达式": "number(value) >= 1"},
        })
        self.assertTrue(revised.json()["ok"])
        self.assertEqual(revised.json()["entry"]["规则版本"], 2)

        new_trial = self.client.post("/api/validate/trials", json={
            "rule_id": self.rule_id, "sample": {"price": "5"},
        }).json()
        self.assertEqual(new_trial["规则版本"], 2)
        self.assertTrue(new_trial["passed"])
        self.assertFalse(new_trial["cache_hit"])
        self.assertNotEqual(new_trial["cache_key"], old["cache_key"])

    def test_invalid_expression_returns_field_failure_not_500(self) -> None:
        response = self.client.post("/api/validate/trials", json={
            "rule_id": self.rule_id,
            "sample": {"price": "10"},
            "expression": "value -> 1",
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["compile_ok"])
        self.assertTrue(body["failure_reasons"])

    def test_clear_cache_keeps_conclusions_reproducible(self) -> None:
        payload = {"rule_id": self.rule_id, "sample": {"price": "20"}}
        before = self.client.post("/api/validate/trials", json=payload).json()
        self.client.delete("/api/validate/cache")
        after = self.client.post("/api/validate/trials", json=payload).json()
        self.assertFalse(after["cache_hit"])
        self.assertEqual(before["passed"], after["passed"])
        self.assertEqual(
            [item["reason"] for item in before["field_results"]],
            [item["reason"] for item in after["field_results"]],
        )

    def test_history_endpoint_records_trials_but_not_records(self) -> None:
        self.client.post("/api/validate/trials", json={
            "rule_id": self.rule_id, "sample": {"price": "10"},
        })
        history = self.client.get("/api/validate/trials/history").json()
        self.assertEqual(history["total"], 1)
        entry = self.client.get(f"/api/validate/{self.rule_id}").json()
        self.assertNotIn("trial_id", entry)


if __name__ == "__main__":
    unittest.main()
