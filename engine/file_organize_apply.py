# -*- coding: utf-8 -*-
"""A 级整理：契约规则 → FileRuleEngine。按钮与聊天共用映射，这里只给按钮预览/执行。

不读用户原话、不调模型。聊天路径仍走 dispatcher 编译，避免两套编译器。
"""
from __future__ import annotations

from pathlib import Path

from engine.executor import FileRuleEngine
from engine.rules import DateRule, ExtensionRule, KeywordRule


def contract_engine() -> FileRuleEngine:
    from agent_core.local_file_intent import date_format, default_ext_map, keyword_map

    engine = FileRuleEngine(".")
    ext_map = default_ext_map()
    if ext_map:
        engine.add_rule(ExtensionRule(ext_map))
    kw = keyword_map()
    if kw:
        engine.add_rule(KeywordRule(kw))
    engine.add_rule(DateRule(date_format()))
    return engine


def only_files(paths) -> list:
    out = []
    for raw in paths or []:
        p = Path(raw)
        if p.is_file():
            out.append(p)
    return out


def plan_moves(paths) -> dict:
    """dry_run：原文件不动。"""
    files = only_files(paths)
    if not files:
        return {"moved": [], "errors": [], "files": []}
    result = contract_engine().apply_rules(files, dry_run=True)
    return {"moved": result["moved"], "errors": result["errors"], "files": files}


def apply_moves(paths) -> dict:
    files = only_files(paths)
    if not files:
        return {"moved": [], "errors": [], "files": []}
    result = contract_engine().apply_rules(files, dry_run=False)
    return {"moved": result["moved"], "errors": result["errors"], "files": files}


def preview_text(plan: dict, limit: int = 20) -> str:
    moved = list(plan.get("moved") or [])
    if not moved:
        return "选中的文件套不上契约规则（扩展名 / 合同关键词 / 日期），不会移动。"
    lines = ["将按契约规则移动 %s 个文件（扩展名 → 关键词 → 日期兜底）：" % len(moved), ""]
    for src, dst in moved[:limit]:
        src, dst = Path(src), Path(dst)
        try:
            folder = str(dst.parent.relative_to(src.parent))
        except ValueError:
            folder = dst.parent.name
        lines.append("· %s → %s/%s" % (src.name, folder, dst.name))
    if len(moved) > limit:
        lines.append("…还有 %s 个未列出" % (len(moved) - limit))
    lines.append("")
    lines.append("同名不覆盖。确定后才移动；可在日志里撤回。")
    return "\n".join(lines)
