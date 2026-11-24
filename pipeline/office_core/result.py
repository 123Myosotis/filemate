# -*- coding: utf-8 -*-
"""公共结果结构：状态机 / 失败原因映射 / 统一返回。

从 `word_office.module` 抽出，行为保持一致；text/pdf 共用这一份，
Word 模块尚未迁移，迁移前两套状态机并存。
"""
from __future__ import annotations

# 七状态机 + 待确认
STATUSES = (
    "done",
    "failed",
    "partially_completed",
    "plan_invalidated",
    "failed_postcheck",
    "compensation_failed",
    "rollback_failed",
    "needs_confirmation",
)

_REASON_STATUS = {
    "pre_check": "plan_invalidated",
    "plan_invalidated": "plan_invalidated",
    "verify_failed": "failed_postcheck",
    "failed_postcheck": "failed_postcheck",
    "compensation_failed": "compensation_failed",
    "rollback_failed": "rollback_failed",
}


def fail(reason: str, message: str) -> dict:
    """失败：按 reason 映射到状态机（默认 failed）。"""
    return {
        "status": _REASON_STATUS.get(reason, "failed"),
        "reason": reason,
        "message": message,
        "plan_ref": None,
        "audit_ref": None,
        "user_actions": [],
    }


def finish(result: dict, stages: list = None, warnings: list = None) -> dict:
    """统一补齐返回字段，避免调用方拿到缺键的 dict。"""
    result.setdefault("stages", stages or [])
    result.setdefault("fail_locus", "")
    result.setdefault("tokens", {})
    result.setdefault("warnings", warnings or [])
    if result.get("status") not in STATUSES:
        result["status"] = "failed"
    return result
