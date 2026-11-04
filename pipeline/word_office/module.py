# -*- coding: utf-8 -*-
"""Word 编辑模块：L0 最小实现（结构检查 + 确定性替换）+ 重架构接口占位。

重架构（九对象/七状态机/决策表/Overlay/preflight/分片/事务层/五角色/审计）
在本文件里全部以**占位函数**存在，后续按需替换实现（不改调用点）。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from docx import Document

from .state import is_clean, predicates, scan

# ---------------------------------------------------------------
# 重架构占位（后续接；现在不改行为）
# ---------------------------------------------------------------


def _decide_tier(text: str, state: dict, preds: dict) -> str:
    """决策表占位：现在只判 L0（确定性），后续换规则表。"""
    return "L0"


def _apply_overlay(plan: dict) -> dict:
    """ConfirmationOverlay 占位：现在不改 requires_confirmation。"""
    return plan


def _preflight(plan: dict) -> dict:
    """capability_preflight 占位：现在放行（后续加能力/代价/分片）。"""
    return {"ok": True, "reason": ""}


def _maybe_split(state: dict) -> list:
    """分片占位：现在不分片。"""
    return [state]


def _authorize(request: dict) -> bool:
    """五角色/双人确认占位：单用户，直接放行。"""
    return True


def _audit(event: dict) -> None:
    """审计占位：现在不落盘（后续接双通道审计）。"""
    return None


# ---------------------------------------------------------------
# L0 工具（确定性，不调模型）
# ---------------------------------------------------------------


def _count_runs(path: Path, needle: str) -> int:
    if not needle:
        return 0
    doc = Document(str(path))
    n = 0
    for p in doc.paragraphs:
        for r in p.runs:
            n += r.text.count(needle)
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    for r in p.runs:
                        n += r.text.count(needle)
    return n


def _replace_runs(doc, find: str, repl: str) -> int:
    """在 run 级替换（保留格式）；跨 run 的字符串替换不到，如实报告。"""
    n = 0

    def _do(runs):
        nonlocal n
        for r in runs:
            if find in r.text:
                r.text = r.text.replace(find, repl)
                n += 1

    for p in doc.paragraphs:
        _do(p.runs)
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    _do(p.runs)
    return n


def _inspect_msg(state: dict, preds: dict) -> str:
    styles = "，".join(f"{k}×{v}" for k, v in sorted(
        state["styles"].items(), key=lambda kv: -kv[1])[:5]) or "（无样式信息）"
    tables = "；".join(
        f"{t['rows']}×{t['cols']}（合并 {t['merged']} 处）" for t in state["tables"]
    ) or "（无表格）"
    dirty = [k for k in (
        "no_revision_in_target", "no_content_control", "no_field_in_range",
        "no_bookmark_in_range", "no_nested_table")
        if preds.get(k, {}).get("value") != "true"]
    return (
        "📄 结构检查：段落 %d 个；样式 %s；表格 %s；图片 %d 张。\n"
        "结构风险：%s"
        % (state["paragraphs"], styles, tables, state["inline_shapes"],
           "无" if not dirty else "、".join(dirty))
    )


def _plan_ops(text: str) -> list:
    m = re.search(r"(?:把|将)\s*(.+?)\s*(?:替换成|改成|改为|换成)\s*(.+?)(?:[。，,；;]|$)", text)
    if m:
        return [{"op": "find_replace", "find": m.group(1).strip(), "replace": m.group(2).strip()}]
    return [{"op": "inspect"}]


# ---------------------------------------------------------------
# 唯一对外入口
# ---------------------------------------------------------------


class WordModule:
    @staticmethod
    def handle(request: dict) -> dict:
        if not _authorize(request):
            return _fail("unauthorized", "当前无权执行 Word 操作。")

        text = str(request.get("user_input") or "")
        selected = [str(p) for p in (request.get("selected_files") or []) if str(p).strip()]
        docxs = [p for p in selected if p.lower().endswith(".docx")]
        if not docxs:
            return _fail("no_docx", "请先在文件管理器里选中一个 .docx 文件，我才能处理。")
        target = Path(docxs[0])
        if not target.is_file():
            return _fail("not_found", "选中的文件不存在：%s" % target.name)

        state = scan(target)
        preds = predicates(state)
        tier = _decide_tier(text, state, preds)
        if not is_clean(preds) and tier == "L0":
            # 谓词不干净（有修订/控件/字段/书签/嵌套表）：L0 不静默执行，转确认
            dirty = [k for k in (
                "no_revision_in_target", "no_content_control", "no_field_in_range",
                "no_bookmark_in_range", "no_nested_table")
                if preds.get(k, {}).get("value") != "true"]
            return _fail(
                "needs_confirmation",
                "这份文档有结构风险（%s），L0 不静默执行。" % "、".join(dirty))

        plan = {"tier": tier, "operations": _plan_ops(text)}
        plan = _apply_overlay(plan)
        pre = _preflight(plan)
        if not pre.get("ok"):
            return _fail("preflight", str(pre.get("reason") or "预检未通过"))

        result = _execute(target, state, preds, plan)
        _audit({"path": str(target), "tier": tier, "ops": len(plan["operations"]),
                "status": result.get("status")})
        return result


def _fail(reason: str, message: str) -> dict:
    return {"status": "failed", "reason": reason, "message": message,
            "plan_ref": None, "audit_ref": None, "user_actions": []}


def _execute(target: Path, state: dict, preds: dict, plan: dict) -> dict:
    op = (plan["operations"] or [{"op": "inspect"}])[0]
    if op["op"] == "inspect":
        return {"status": "done", "reason": "inspect",
                "message": _inspect_msg(state, preds),
                "plan_ref": None, "audit_ref": None, "user_actions": []}

    find, repl = op.get("find", ""), op.get("replace", "")
    before = _count_runs(target, find)
    if before == 0:
        return _fail("not_found", "文档里没找到「%s」（可能跨 run 断开了）。" % find)

    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    doc = Document(str(target))
    changed = _replace_runs(doc, find, repl)
    doc.save(str(target))

    after_find = _count_runs(target, find)
    if after_find != 0:
        shutil.copy2(backup, target)
        return _fail("verify_failed", "替换后仍有残留，已恢复原文件。")
    return {"status": "done", "reason": "find_replace",
            "message": "✅ 已替换 %d 处「%s」→「%s」（写前备份：%s）。"
                       % (changed, find, repl, backup.name),
            "plan_ref": None, "audit_ref": None, "user_actions": []}
