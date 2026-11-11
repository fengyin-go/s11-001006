"""采集校验业务规则：状态流转、字段校验、筛选口径与规则版本化都收在这里。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "validate"
REQUIRED_FIELDS = ["规则编号", "校验目标字段", "校验表达式"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用规则": "已启用", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]
INITIAL_RULE_VERSION = 1
REVISION_HISTORY_LIMIT = 20


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ValidateService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._with_version(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self._with_version(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 登记时允许一并保存期望取值、容差范围等可选字段
        for optional in ("期望取值", "容差范围", "采样比例", "阻断级别", "告警触发"):
            if optional in values:
                entry[optional] = values.get(optional)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["规则版本"] = INITIAL_RULE_VERSION
        entry["修订记录"] = [{
            "version": INITIAL_RULE_VERSION,
            "expression": entry.get("校验表达式"),
            "revised_at": _now(),
            "note": "首次登记",
        }]
        rows.append(entry)
        return self._with_version(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"校验规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集校验可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self._with_version(entry), f"校验规则已{action}"

    def revise_expression(
        self, entry_id: int, expression: str, *, note: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """修改正式校验表达式并递增规则版本。

        历史表达式保留在「修订记录」里（有界），试验缓存按新版本重新编译，
        旧版本编译结果永远不会服务新版本的结论。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"校验规则 {entry_id} 不存在或已归档"
        text = (expression or "").strip()
        if not text:
            return None, "新的校验表达式不能为空"
        self._ensure_version_fields(entry)
        if text == str(entry.get("校验表达式", "")):
            return self._with_version(entry), "表达式未发生变化，规则版本保持不变"
        entry["校验表达式"] = text
        entry["规则版本"] = int(entry["规则版本"]) + 1
        history = entry.setdefault("修订记录", [])
        history.append({
            "version": entry["规则版本"],
            "expression": text,
            "revised_at": _now(),
            "note": note or "表达式修订",
        })
        del history[:-REVISION_HISTORY_LIMIT]
        return self._with_version(entry), f"规则版本已升至 v{entry['规则版本']}"

    def _ensure_version_fields(self, entry: dict[str, Any]) -> None:
        # 兼容改造前已存在的历史记录：首次访问时补齐版本字段，不改业务字段。
        if entry.get("规则版本") is None:
            entry["规则版本"] = INITIAL_RULE_VERSION
        if "修订记录" not in entry:
            entry["修订记录"] = [{
                "version": INITIAL_RULE_VERSION,
                "expression": entry.get("校验表达式"),
                "revised_at": _now(),
                "note": "历史数据补录",
            }]

    def _with_version(self, entry: dict[str, Any]) -> dict[str, Any]:
        self._ensure_version_fields(entry)
        return entry
