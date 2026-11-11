"""离线样本试验的受限表达式引擎。

设计约束（对应需求）：

1. 只允许表达式（``ast.Expression``），不允许语句，因此不可能出现赋值、导入、
   ``def``/``class``、``while`` 等任意 Python 能力；
2. 白名单 AST 节点 + 白名单安全函数/类型，任何越界节点直接判定表达式不合法；
3. 不向命名空间注入任何内置项（``eval(codename, {"__builtins__": {}}, ns)``），
   无法访问 ``__import__``、``open``、``getattr`` 等；
4. 显式拒绝 dunder 名称（``__class__`` 等），切断逃逸到对象模型的路径；
5. 求值预算限制（节点步数、字符串长度、序列展开长度），表达式只能做数据判断，
   不能拖垮进程；
6. 解析/编译失败统一抛出 :class:`ExpressionError`，由上层做「历史不合法表达式」
   兼容（不污染正式记录，只在试验结论里给出字段级解释）。
"""
from __future__ import annotations

import ast
import math
from typing import Any

# 当前规则语言版本。语义发生不兼容变化时递增；缓存身份包含该版本，
# 因此旧版本编译结果永远不会被新版本试验复用。
RULE_LANG_VERSION = 1

MAX_EXPRESSION_CHARS = 1000
MAX_SAMPLE_CHARS = 200_000
MAX_EVAL_STEPS = 20_000
MAX_ITER_EXPAND = 2_000

# 允许出现的 AST 节点白名单。任何不在表内的节点（Import/Assign/Call 之外的
# 语句等）都会被判为不合法。
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    # 字面量与名字
    ast.Constant,
    ast.Name,
    ast.Load,
    # 运算
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    # 容器
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Set,
    # 下标与属性（属性名再做 dunder/白名单校验）
    ast.Subscript,
    ast.Attribute,
    ast.Slice,
    # 仅允许调用白名单函数
    ast.Call,
    ast.keyword,
    # 运算符
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
)

# 表达式里允许访问的属性（全部是纯数据方法/属性，无反射能力）。
_ALLOWED_ATTRS = frozenset({
    "get", "keys", "values", "items",
    "lower", "upper", "strip", "startswith", "endswith",
    "split", "join", "replace", "find", "count",
    "isdigit", "isalpha", "isalnum",
    "abs", "round", "min", "max", "sum", "len",
})

# 仅暴露确定性、无副作用的纯函数；不暴露 type/int 等带反射能力的内建。
MATH_NS: dict[str, Any] = {
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
    "fabs": math.fabs,
    "pi": math.pi,
    "e": math.e,
}
def _to_number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ExpressionError("空字符串无法转为数值")
        try:
            return float(text)
        except ValueError as exc:
            raise ExpressionError(f"取值「{_preview(text)}」不是合法数值") from exc
    raise ExpressionError(f"类型 {type(value).__name__} 无法转为数值")


def _safe_len(value: Any) -> int:
    try:
        length = len(value)
    except TypeError as exc:
        raise ExpressionError(f"类型 {type(value).__name__} 没有长度") from exc
    if length > MAX_ITER_EXPAND:
        raise ExpressionError("序列长度超过试验安全上限")
    return length


def _safe_min(*args: Any) -> Any:
    if len(args) == 1:
        args = tuple(args[0])
    _check_expand(len(args))
    return min(args)


def _safe_max(*args: Any) -> Any:
    if len(args) == 1:
        args = tuple(args[0])
    _check_expand(len(args))
    return max(args)


def _safe_sum(values: Any) -> float:
    seq = list(values)
    _check_expand(len(seq))
    return sum(_to_number(item) for item in seq)


def _safe_round(value: Any, ndigits: Any = 0) -> float:
    return round(_to_number(value), int(ndigits))


def _safe_split(value: Any, sep: Any = None) -> list[str]:
    if not isinstance(value, str):
        raise ExpressionError("split 只能作用于字符串")
    result = value.split(sep) if sep is not None else value.split()
    _check_expand(len(result))
    return result


def _safe_join(sep: Any, values: Any) -> str:
    if not isinstance(sep, str):
        raise ExpressionError("join 的分隔符必须是字符串")
    seq = list(values)
    _check_expand(len(seq))
    return sep.join(str(item) for item in seq)


def _safe_replace(value: Any, old: Any, new: Any) -> str:
    if not isinstance(value, str):
        raise ExpressionError("replace 只能作用于字符串")
    return value.replace(str(old), str(new))


def _safe_find(value: Any, sub: Any) -> int:
    if not isinstance(value, str):
        raise ExpressionError("find 只能作用于字符串")
    return value.find(str(sub))


def _safe_str_count(value: Any, sub: Any) -> int:
    if isinstance(value, str):
        return value.count(str(sub))
    seq = list(value)
    _check_expand(len(seq))
    return seq.count(sub)


def _safe_startswith(value: Any, prefix: Any) -> bool:
    return isinstance(value, str) and value.startswith(str(prefix))


def _safe_endswith(value: Any, suffix: Any) -> bool:
    return isinstance(value, str) and value.endswith(str(suffix))


def _str_predicate(name: str) -> Any:
    def predicate(value: Any) -> bool:
        return isinstance(value, str) and getattr(value, name)()
    return predicate


SAFE_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    "True": True,
    "False": False,
    "None": None,
    "abs": lambda value: abs(_to_number(value)),
    "round": _safe_round,
    "min": _safe_min,
    "max": _safe_max,
    "sum": _safe_sum,
    "len": _safe_len,
    "number": _to_number,
    "is_none": lambda value: value is None,
    "is_blank": lambda value: value is None or (isinstance(value, str) and not value.strip()),
    "math": MATH_NS,
}


class ExpressionError(Exception):
    """表达式不合法或越过安全边界时抛出；信息直接用于字段级失败解释。"""


class StepBudget:
    def __init__(self, limit: int = MAX_EVAL_STEPS) -> None:
        self.remaining = limit

    def consume(self, amount: int = 1) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise ExpressionError("表达式求值步数超过安全上限，疑似复杂循环式运算")


def _check_expand(size: int) -> None:
    if size > MAX_ITER_EXPAND:
        raise ExpressionError("序列展开长度超过试验安全上限")


def _preview(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _validate_node(node: ast.AST) -> None:
    allowed_call_names = {
        name for name, value in SAFE_GLOBALS.items()
        if callable(value)
    }
    allowed_names = {"value", "field", "sample"} | set(SAFE_GLOBALS)
    for child in ast.walk(node):
        if not isinstance(child, _ALLOWED_NODES):
            raise ExpressionError(
                f"表达式包含不被允许的语法：{type(child).__name__}；试验仅支持数据判断表达式"
            )
        if isinstance(child, ast.Name):
            if child.id.startswith("__"):
                raise ExpressionError("表达式禁止使用双下划线名称")
            if child.id not in allowed_names:
                raise ExpressionError(f"名称「{child.id}」不在试验允许范围内")
        if isinstance(child, ast.Attribute):
            if child.attr.startswith("__"):
                raise ExpressionError("表达式禁止访问双下划线属性")
            # math 命名空间的成员单独白名单；其余属性必须是数据方法白名单成员。
            if (
                isinstance(child.value, ast.Name)
                and child.value.id == "math"
            ):
                if child.attr not in MATH_NS:
                    raise ExpressionError(f"math.{child.attr} 不在试验允许范围内")
            elif child.attr not in _ALLOWED_ATTRS:
                raise ExpressionError(f"属性「{child.attr}」不在试验允许范围内")
        if isinstance(child, ast.Call):
            if not isinstance(child.func, (ast.Name, ast.Attribute)):
                raise ExpressionError("只允许调用白名单函数，禁止动态调用")
            if isinstance(child.func, ast.Name) and child.func.id not in allowed_call_names:
                raise ExpressionError(f"函数「{child.func.id}」不在试验白名单内，禁止调用")


def _truthy(value: Any) -> bool:
    return bool(value)


def _apply_binop(op: type[ast.AST], left: Any, right: Any) -> Any:
    if op is ast.Add:
        if isinstance(left, str) or isinstance(right, str):
            return str(left) + str(right)
        return _to_number(left) + _to_number(right)
    if op is ast.Sub:
        return _to_number(left) - _to_number(right)
    if op is ast.Mult:
        return _to_number(left) * _to_number(right)
    if op is ast.Div:
        denominator = _to_number(right)
        if denominator == 0:
            raise ExpressionError("除数为零")
        return _to_number(left) / denominator
    if op is ast.FloorDiv:
        denominator = _to_number(right)
        if denominator == 0:
            raise ExpressionError("除数为零")
        return _to_number(left) // denominator
    if op is ast.Mod:
        if isinstance(left, str):
            raise ExpressionError("试验环境禁止字符串百分号格式化")
        denominator = _to_number(right)
        if denominator == 0:
            raise ExpressionError("除数为零")
        return _to_number(left) % denominator
    if op is ast.Pow:
        base = _to_number(left)
        exponent = _to_number(right)
        if abs(exponent) > 100:
            raise ExpressionError("幂运算指数超过安全上限 100")
        return base ** exponent
    raise ExpressionError(f"不支持的运算符：{op.__name__}")


def _apply_compare(node: ast.Compare, guard: Any) -> bool:
    left = guard(node.left)
    for op, comparator_node in zip(node.ops, node.comparators):
        right = guard(comparator_node)
        if isinstance(op, (ast.In, ast.NotIn)):
            try:
                contained = left in right
            except TypeError as exc:
                raise ExpressionError("in 运算两侧类型不兼容") from exc
            if isinstance(op, ast.NotIn) == contained:
                return False
        elif isinstance(op, (ast.Is, ast.IsNot)):
            same = left is right
            if isinstance(op, ast.IsNot) == same:
                return False
        else:
            ordered = (
                ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE
            )
            if not isinstance(op, ordered):  # pragma: no cover - 白名单已限定
                raise ExpressionError(f"不支持的比较：{type(op).__name__}")
            try:
                if isinstance(op, ast.Eq):
                    ok = left == right
                elif isinstance(op, ast.NotEq):
                    ok = left != right
                elif isinstance(op, ast.Lt):
                    ok = left < right
                elif isinstance(op, ast.LtE):
                    ok = left <= right
                elif isinstance(op, ast.Gt):
                    ok = left > right
                else:
                    ok = left >= right
            except TypeError as exc:
                raise ExpressionError("比较两侧类型不兼容，无法判断大小") from exc
            if not ok:
                return False
        left = right
    return True


def _apply_subscript(target: Any, key: Any) -> Any:
    if isinstance(target, dict):
        if isinstance(key, str) and key.startswith("__"):
            raise ExpressionError("禁止访问双下划线键")
        if key not in target:
            raise ExpressionError(f"样本中不存在字段「{key}」")
        return target[key]
    if isinstance(target, (list, tuple, str)):
        if isinstance(key, slice):
            return target[key]
        index = int(key)
        if not -len(target) <= index < len(target):
            raise ExpressionError(f"下标 {index} 越界")
        return target[index]
    raise ExpressionError(f"类型 {type(target).__name__} 不支持下标访问")


def _apply_attribute(target: Any, attr: str) -> Any:
    if target is MATH_NS:
        if attr not in MATH_NS:
            raise ExpressionError(f"math.{attr} 不在试验允许范围内")
        return MATH_NS[attr]
    if attr not in _ALLOWED_ATTRS:
        raise ExpressionError(f"属性「{attr}」不在试验允许范围内")
    if isinstance(target, dict):
        if attr == "get":
            return target.get
        raise ExpressionError(f"字典不支持属性「{attr}」")
    if isinstance(target, str):
        string_methods = {
            "lower", "upper", "strip", "startswith", "endswith",
            "split", "replace", "find", "count", "isdigit", "isalpha", "isalnum",
        }
        if attr in string_methods:
            return getattr(target, attr)
        raise ExpressionError(f"字符串不支持属性「{attr}」")
    if isinstance(target, (list, tuple, set)):
        if attr == "count":
            return target.count
        raise ExpressionError(f"序列不支持属性「{attr}」")
    if isinstance(target, dict) or target is None:
        raise ExpressionError("该取值上没有可用属性")
    raise ExpressionError(f"类型 {type(target).__name__} 不支持属性访问")


def _apply_call(node: ast.Call, guard: Any) -> Any:
    kwargs = {kw.arg: guard(kw.value) for kw in node.keywords if kw.arg is not None}
    if kwargs:
        raise ExpressionError("试验表达式不支持关键字参数调用")
    args = [guard(arg) for arg in node.args]
    if isinstance(node.func, ast.Name):
        func = SAFE_GLOBALS.get(node.func.id)
        if func is None or node.func.id in ("True", "False", "None"):
            raise ExpressionError(f"函数「{node.func.id}」不在白名单内")
        return func(*args)
    # ast.Attribute 调用
    target = guard(node.func.value)
    attr = node.func.attr
    if target is MATH_NS:
        if attr not in MATH_NS or not callable(MATH_NS[attr]):
            raise ExpressionError(f"math.{attr} 不是可调用的白名单成员")
        return MATH_NS[attr](*args)
    if isinstance(target, dict) and attr == "get":
        if len(args) == 1:
            return target.get(args[0])
        if len(args) == 2:
            return target.get(args[0], args[1])
        raise ExpressionError("dict.get 只接受 1 到 2 个参数")
    if isinstance(target, str):
        handlers = {
            "lower": str.lower,
            "upper": str.upper,
            "strip": str.strip,
            "startswith": _safe_startswith,
            "endswith": _safe_endswith,
            "split": _safe_split,
            "join": _safe_join,
            "replace": _safe_replace,
            "find": _safe_find,
            "count": _safe_str_count,
            "isdigit": _str_predicate("isdigit"),
            "isalpha": _str_predicate("isalpha"),
            "isalnum": _str_predicate("isalnum"),
        }
        handler = handlers.get(attr)
        if handler is None:
            raise ExpressionError(f"字符串方法「{attr}」不在白名单内")
        return handler(target, *args)
    if isinstance(target, (list, tuple, set)) and attr == "count":
        _check_expand(len(target))
        return target.count(*args)
    if isinstance(target, dict) and attr == "keys":
        return list(target.keys())
    if isinstance(target, dict) and attr == "values":
        return list(target.values())
    if isinstance(target, dict) and attr == "items":
        return [list(item) for item in target.items()]
    if attr in ("len", "abs", "round", "min", "max", "sum", "count"):
        raise ExpressionError(f"请把 {attr} 作为顶层函数调用，而不是方法调用")
    raise ExpressionError(f"方法「{attr}」不在试验允许范围内")


class Evaluator:
    """保留 AST 的受限求值器：编译一次、可对不同样本反复执行。"""

    def __init__(self, expression: str) -> None:
        if not isinstance(expression, str) or not expression.strip():
            raise ExpressionError("校验表达式为空")
        if len(expression) > MAX_EXPRESSION_CHARS:
            raise ExpressionError(f"表达式长度超过 {MAX_EXPRESSION_CHARS} 字符上限")
        try:
            self._tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"表达式语法不合法：{exc.msg}") from exc
        _validate_node(self._tree)
        self.source = expression
        self.lang_version = RULE_LANG_VERSION
        self.code = compile(self._tree, "<validate-expression>", "eval")

    def run(self, namespace: dict[str, Any]) -> Any:
        budget = StepBudget()

        def guard(node: ast.AST) -> Any:
            budget.consume()
            if isinstance(node, ast.Expression):
                return guard(node.body)
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                if node.id in namespace:
                    return namespace[node.id]
                if node.id in SAFE_GLOBALS:
                    return SAFE_GLOBALS[node.id]
                raise ExpressionError(
                    f"名称「{node.id}」未定义，可用变量只有 value、field、sample"
                )
            if isinstance(node, ast.BinOp):
                return _apply_binop(type(node.op), guard(node.left), guard(node.right))
            if isinstance(node, ast.UnaryOp):
                operand = guard(node.operand)
                if isinstance(node.op, ast.USub):
                    return -_to_number(operand)
                if isinstance(node.op, ast.UAdd):
                    return _to_number(operand)
                return not _truthy(operand)
            if isinstance(node, ast.BoolOp):
                result: Any = True
                for value_node in node.values:
                    result = guard(value_node)
                    if isinstance(node.op, ast.And) and not _truthy(result):
                        return result
                    if isinstance(node.op, ast.Or) and _truthy(result):
                        return result
                return result
            if isinstance(node, ast.Compare):
                return _apply_compare(node, guard)
            if isinstance(node, ast.IfExp):
                return guard(node.body) if _truthy(guard(node.test)) else guard(node.orelse)
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                values = [guard(item) for item in node.elts]
                _check_expand(len(values))
                if isinstance(node, ast.Tuple):
                    return tuple(values)
                if isinstance(node, ast.Set):
                    return set(values)
                return values
            if isinstance(node, ast.Dict):
                _check_expand(len(node.keys))
                return {guard(key): guard(value) for key, value in zip(node.keys, node.values)}
            if isinstance(node, ast.Subscript):
                return _apply_subscript(guard(node.value), guard(node.slice))
            if isinstance(node, ast.Slice):
                return slice(
                    guard(node.lower) if node.lower is not None else None,
                    guard(node.upper) if node.upper is not None else None,
                    guard(node.step) if node.step is not None else None,
                )
            if isinstance(node, ast.Attribute):
                return _apply_attribute(guard(node.value), node.attr)
            if isinstance(node, ast.Call):
                return _apply_call(node, guard)
            raise ExpressionError(f"未支持的语法节点：{type(node).__name__}")

        return guard(self._tree)


def validate_sample_size(sample: dict[str, Any]) -> None:
    """样本进入求值前的体量约束，防止超大输入拖慢试验。"""
    rendered = repr(sample)
    if len(rendered) > MAX_SAMPLE_CHARS:
        raise ExpressionError(f"样本体量超过 {MAX_SAMPLE_CHARS} 字符上限，请裁剪后再试验")
