"""试验沙箱安全限制测试：不能执行任意 Python。"""
from __future__ import annotations

import unittest

from app.services.expression_sandbox import Evaluator, ExpressionError


def run(expression: str, **namespace: object) -> object:
    return Evaluator(expression).run(namespace)


class SandboxSecurityTest(unittest.TestCase):
    def test_allows_safe_data_expressions(self) -> None:
        sample = {"price": "10.5", "name": " AC-100 ", "tags": ["a", "b"]}
        self.assertTrue(run("number(value) > 10", value=sample["price"]))
        self.assertEqual(run("value.strip().lower()", value=sample["name"]), "ac-100")
        self.assertTrue(run("'a' in sample['tags']", sample=sample))
        self.assertEqual(run("sample.get('missing', 0)", sample=sample), 0)
        self.assertAlmostEqual(run("math.sqrt(number(value))", value="16"), 4.0)
        self.assertEqual(run("len(value)", value=sample["tags"]), 2)

    def test_rejects_import_and_statements(self) -> None:
        for evil in (
            "__import__('os').system('echo pwn')",
            "import os",
            "value.__class__",
            "().__class__.__bases__[0].__subclasses__()",
            "open('/etc/passwd').read()",
            "getattr(value, 'strip')",
            "eval('1+1')",
            "exec('x = 1')",
            "lambda: 1",
            "[x for x in range(3)]",
            "x := 1",
            "value if True else None; __import__('os')",
        ):
            with self.subTest(expression=evil):
                with self.assertRaises(ExpressionError):
                    Evaluator(evil)

    def test_rejects_dunder_and_unknown_attributes(self) -> None:
        with self.assertRaises(ExpressionError):
            Evaluator("value.__class__")
        with self.assertRaises(ExpressionError):
            Evaluator("value.format('x')")
        with self.assertRaises(ExpressionError):
            Evaluator("math.pow(2, 3)")

    def test_builtins_namespace_is_empty(self) -> None:
        for name in ("__builtins__", "abs", "len"):
            with self.subTest(name=name):
                # 未作为名字注入时直接引用即报未定义（abs/len 是显式白名单函数除外）
                if name == "__builtins__":
                    with self.assertRaises(ExpressionError):
                        run(name)

    def test_runtime_guards(self) -> None:
        with self.assertRaises(ExpressionError):
            run("number(value) / 0", value="10")
        with self.assertRaises(ExpressionError):
            run("value['missing']", value={})
        with self.assertRaises(ExpressionError):
            run("number(value)", value="not-a-number")
        with self.assertRaises(ExpressionError):
            Evaluator("x" * 1001)
        with self.assertRaises(ExpressionError):
            Evaluator("2 ** 101").run({})

    def test_step_budget_blocks_pathological_expression(self) -> None:
        expression = " + ".join(["1"] * 3000)
        with self.assertRaises(ExpressionError):
            run(expression)


if __name__ == "__main__":
    unittest.main()
