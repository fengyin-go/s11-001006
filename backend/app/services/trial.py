"""离线样本试验服务。

边界约定：

- **只读正式数据**：只通过 ``store.find`` 读取校验规则与解析模板，绝不写入、修改
  解析模板或正式校验记录；试验历史保存在独立的有界内存结构里；
- **样本不出调用边界**：样本只存在于单次试验的局部命名空间，编译缓存条目里
  不允许出现任何样本内容；
- **并发隔离**：每次试验独立构造命名空间，共享的只有不可变编译产物与缓存计数器；
- **历史不合法表达式兼容**：编译失败被负缓存并转成字段级失败解释，不抛 500、
  不动正式记录；
- **版本隔离**：规则版本与语言版本都进入缓存身份，改规则/换版本后旧编译结果
  不可能服务新结论。
"""
from __future__ import annotations

import threading
import uuid
from collections import deque
from datetime import datetime
from typing import Any

from app.services.expression_cache import CompileResult, compile_cache
from app.services.expression_sandbox import ExpressionError, validate_sample_size
from app.store import store

VALIDATE_MODULE = "validate"
PARSER_MODULE = "parser"
HISTORY_LIMIT = 50
DEFAULT_RULE_VERSION = 1


class TrialRequest:
    """轻量请求载体（路由层用 pydantic 模型转换后传入）。"""

    def __init__(
        self,
        *,
        rule_id: int | None = None,
        sample: dict[str, Any] | None = None,
        expression: str | None = None,
        rule_version: int | None = None,
        expected: Any = None,
        tolerance: str | None = None,
        sample_source: str = "validate",
        parser_template_id: int | None = None,
    ) -> None:
        self.rule_id = rule_id
        self.sample = sample or {}
        self.expression = expression
        self.rule_version = rule_version
        self.expected = expected
        self.tolerance = tolerance
        self.sample_source = sample_source
        self.parser_template_id = parser_template_id


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def parse_tolerance(raw: Any) -> tuple[float | None, str, str]:
    """把登记文本里的容差范围解析成 (数值, 种类, 说明)。

    支持：空（精确匹配）、绝对数值（含 ``±0.5``）、百分比（``5%``，相对期望值）。
    解析不了时回退为精确匹配并保留说明，不阻断试验。
    """
    if raw is None or not str(raw).strip():
        return None, "exact", "未配置容差范围，按精确匹配判断"
    text = str(raw).strip()
    try:
        if text.endswith("%"):
            percent = float(text[:-1].strip().lstrip("±"))
            return percent / 100.0, "relative", f"相对容差 {text}（按期望值比例）"
        return float(text.lstrip("±")), "absolute", f"绝对容差 ±{text.lstrip('±')}"
    except ValueError:
        return None, "exact", f"容差范围「{text}」无法解析为数值，回退精确匹配"


class TrialService:
    def __init__(self) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)
        self._history_lock = threading.Lock()

    # ------------------------------------------------------------------ 对外能力
    def run_trial(self, request: TrialRequest) -> dict[str, Any]:
        rule = self._load_rule(request.rule_id)
        expression = (request.expression or "").strip() or str(rule.get("校验表达式", "") if rule else "")
        target_field = str(rule.get("校验目标字段", "")) if rule else ""
        rule_version = self._resolve_rule_version(request, rule)
        expected = request.expected if request.expected is not None else (
            rule.get("期望取值") if rule else None
        )
        tolerance_raw = request.tolerance if request.tolerance is not None else (
            rule.get("容差范围") if rule else None
        )
        sample = self._validate_sample(request.sample)
        source_note = self._resolve_source(request)

        result: dict[str, Any] = {
            "trial_id": uuid.uuid4().hex,
            "created_at": _now(),
            "rule_id": request.rule_id,
            "规则编号": rule.get("规则编号") if rule else None,
            "规则版本": rule_version,
            "lang_version": compile_cache.stats()["lang_version"],
            "expression": expression,
            "target_field": target_field,
            "expected": expected,
            "tolerance_raw": tolerance_raw,
            "sample_source": request.sample_source,
            "source_note": source_note,
            "sample": sample,
            "compile_ok": False,
            "cache_hit": False,
            "cache_key": None,
            "passed": False,
            "actual": None,
            "field_results": [],
            "failure_reasons": [],
        }

        compiled = compile_cache.get_or_compile(expression, rule_version)
        result["cache_hit"] = compiled.cache_hit
        result["cache_key"] = compiled.key
        if not compiled.ok:
            # 历史不合法表达式：负缓存命中也算命中，结论固定为编译失败。
            self._add_field_failure(
                result, target_field or "(表达式)", "编译",
                f"表达式不合法，未进入求值：{compiled.error_message}",
            )
            return self._finish(result, compiled)

        result["compile_ok"] = True
        field_results = self._check_field_presence(target_field, sample)
        result["field_results"].extend(field_results)

        namespace = self._build_namespace(target_field, sample)
        try:
            actual = compiled.evaluator.run(namespace)  # type: ignore[union-attr]
        except ExpressionError as exc:
            self._add_field_failure(result, target_field or "(表达式)", "求值", str(exc))
            return self._finish(result, compiled)
        result["actual"] = self._json_safe(actual)

        passed, reason = self._judge(actual, expected, tolerance_raw)
        result["field_results"].append({
            "field": target_field or "(表达式结果)",
            "stage": "判断",
            "passed": passed,
            "actual": result["actual"],
            "expected": self._json_safe(expected),
            "reason": reason,
        })
        result["passed"] = passed and all(item["passed"] for item in result["field_results"])
        if not result["passed"]:
            result["failure_reasons"] = [
                item["reason"] for item in result["field_results"] if not item["passed"]
            ]
        return self._finish(result, compiled)

    def list_history(self) -> list[dict[str, Any]]:
        with self._history_lock:
            return list(self._history)

    def clear_history(self) -> int:
        with self._history_lock:
            removed = len(self._history)
            self._history.clear()
            return removed

    def cache_stats(self) -> dict[str, Any]:
        return compile_cache.stats()

    def clear_cache(self) -> dict[str, Any]:
        removed = compile_cache.clear()
        return {"cleared": removed, **compile_cache.stats()}

    # ------------------------------------------------------------------ 内部细节
    def _load_rule(self, rule_id: int | None) -> dict[str, Any] | None:
        if rule_id is None:
            return None
        return store.find(VALIDATE_MODULE, rule_id)

    def _resolve_rule_version(
        self, request: TrialRequest, rule: dict[str, Any] | None
    ) -> int:
        if request.rule_version is not None:
            return int(request.rule_version)
        if rule and rule.get("规则版本") is not None:
            return int(rule["规则版本"])
        return DEFAULT_RULE_VERSION

    def _resolve_source(self, request: TrialRequest) -> str:
        if request.sample_source != "parser":
            return "校验页离线样本"
        template = None
        if request.parser_template_id is not None:
            template = store.find(PARSER_MODULE, request.parser_template_id)
        if template is None:
            return "内容解析器页送样（未匹配到模板，仅记录来源）"
        return f"内容解析器页送样，来源模板：{template.get('模板编号', request.parser_template_id)}"

    def _validate_sample(self, sample: Any) -> dict[str, Any]:
        if not isinstance(sample, dict):
            raise ValueError("试验样本必须是字段名到取值的 JSON 对象")
        validate_sample_size(sample)
        return dict(sample)

    def _build_namespace(self, target_field: str, sample: dict[str, Any]) -> dict[str, Any]:
        # 每次试验独立构造；三个只读变量，样本引用不共享给任何缓存结构。
        return {
            "value": sample.get(target_field) if target_field else None,
            "field": target_field,
            "sample": sample,
        }

    def _check_field_presence(self, target_field: str, sample: dict[str, Any]) -> list[dict[str, Any]]:
        if not target_field:
            return [{
                "field": "(表达式)",
                "stage": "字段",
                "passed": True,
                "reason": "未指定校验目标字段，直接对样本求值",
            }]
        present = target_field in sample
        return [{
            "field": target_field,
            "stage": "字段",
            "passed": present,
            "actual": self._json_safe(sample.get(target_field)),
            "expected": "字段存在",
            "reason": "样本包含目标字段" if present else f"样本缺少校验目标字段「{target_field}」",
        }]

    def _judge(self, actual: Any, expected: Any, tolerance_raw: Any) -> tuple[bool, str]:
        if isinstance(actual, bool):
            return actual, f"表达式返回布尔值：{actual}"
        tolerance, kind, tolerance_note = parse_tolerance(tolerance_raw)
        actual_num = _coerce_number(actual)
        expected_num = _coerce_number(expected)
        if actual_num is not None and expected_num is not None:
            if kind == "relative":
                tolerance_value = abs(tolerance or 0) * abs(expected_num)
            else:
                tolerance_value = tolerance or 0.0
            delta = abs(actual_num - expected_num)
            passed = delta <= tolerance_value
            reason = (
                f"数值比对：实际 {actual_num} vs 期望 {expected_num}，"
                f"偏差 {delta:.6g}，容差 {tolerance_value:.6g}（{tolerance_note}）"
            )
            return passed, reason
        passed = actual == expected
        return passed, (
            f"精确比对：实际 {self._json_safe(actual)!r} vs 期望 {self._json_safe(expected)!r}；{tolerance_note}"
        )

    def _add_field_failure(
        self, result: dict[str, Any], field: str, stage: str, reason: str
    ) -> None:
        result["field_results"].append({
            "field": field,
            "stage": stage,
            "passed": False,
            "reason": reason,
        })
        result["failure_reasons"].append(reason)
        result["passed"] = False

    def _finish(self, result: dict[str, Any], compiled: CompileResult) -> dict[str, Any]:
        # 以字段级结果为准统一汇总失败原因，保证「字段缺失」等前置失败也可解释。
        result["failure_reasons"] = [
            item["reason"] for item in result["field_results"] if not item["passed"]
        ]
        result["passed"] = bool(result["field_results"]) and all(
            item["passed"] for item in result["field_results"]
        )
        result["cache"] = compile_cache.stats()
        with self._history_lock:
            self._history.append(dict(result))
        return result

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(key): TrialService._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [TrialService._json_safe(item) for item in value]
        return repr(value)


trial_service = TrialService()
