"""采集校验接口：维护校验规则，覆盖提交校验、启用规则、停用规则与离线样本试验。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult, TrialPayload
from app.services.trial import TrialRequest, trial_service
from app.services.validate import ValidateService

router = APIRouter(prefix="/api/validate", tags=["采集校验"])

service = ValidateService()

LIST_FIELDS = ["规则编号", "校验目标字段", "校验表达式", "期望取值", "容差范围", "采样比例", "阻断级别", "告警触发"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤采集校验列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/trials", response_model=dict)
def run_trial(payload: TrialPayload) -> dict[str, Any]:
    """对离线样本反复运行校验表达式试验。

    只读正式记录与解析模板，不写回任何登记数据；编译结果按
    「语言版本 + 规则版本 + 表达式原文」复用，样本仅存在于本次调用。
    """
    request = TrialRequest(
        rule_id=payload.rule_id,
        sample=payload.sample,
        expression=payload.expression,
        rule_version=payload.rule_version,
        expected=payload.expected,
        tolerance=payload.tolerance,
        sample_source=payload.sample_source,
        parser_template_id=payload.parser_template_id,
    )
    try:
        return trial_service.run_trial(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/trials/history", response_model=dict)
def trial_history() -> dict[str, Any]:
    """读取本进程内最近的离线试验记录（有界，独立于正式校验记录）。"""
    items = trial_service.list_history()
    return {"total": len(items), "items": items}


@router.delete("/trials/history", response_model=ActionResult)
def clear_trial_history() -> ActionResult:
    removed = trial_service.clear_history()
    return ActionResult(ok=True, message=f"已清空 {removed} 条离线试验记录")


@router.get("/cache", response_model=dict)
def cache_stats() -> dict[str, Any]:
    """查看表达式编译缓存容量、命中/未命中/淘汰计数。"""
    return trial_service.cache_stats()


@router.delete("/cache", response_model=dict)
def clear_cache() -> dict[str, Any]:
    """清空编译缓存；清空后再次试验会重新编译，结论保持一致。"""
    return trial_service.clear_cache()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集校验清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "validate", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条校验规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"校验规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条校验规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="校验规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条校验规则执行提交校验、启用规则、停用规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/revision", response_model=ActionResult)
def revise_expression(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修订正式表达式并递增规则版本；旧版本编译结果不得混入新结论。"""
    expression = str(payload.values.get("校验表达式") or "").strip()
    note = payload.remark
    entry, message = service.revise_expression(entry_id, expression, note=note)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
