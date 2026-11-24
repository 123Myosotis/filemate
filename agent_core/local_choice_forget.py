# -*- coding: utf-8 -*-
"""纠正记忆「忘记」：用户明确说才 revoke。不碰业务文件、不自动忘。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ForgetMatch:
    status: str  # hit / miss
    reason: str = ""


# 必须带「洗表/表头/纠正」或「选择/记忆/记录」，禁止光说「忘记」就命中
_FORGET = re.compile(
    r"(?:忘记|忘掉|撤销|取消)\s*(?:这次|这条|这个)?\s*"
    r"(?:(?:洗表|表头|纠正)\s*(?:选择|记忆|记录)?|(?:纠正)?(?:选择|记忆|记录))"
    r"|(?:别记住|不要记住)\s*(?:这次|这条|这个)?\s*(?:洗表|表头|纠正)(?:选择|记忆|记录)?"
    r"|(?:忘记纠正|清除纠正记忆)"
)


def match(user_input: str) -> ForgetMatch:
    text = str(user_input or "").strip()
    if not text:
        return ForgetMatch("miss", "空")
    if _FORGET.search(text):
        return ForgetMatch("hit", "用户明确要求忘记纠正")
    return ForgetMatch("miss", "非忘记指令")


def resolve_scopes(target_dir, selected_paths=None, pending=None) -> list[str]:
    """优先：pending 路径 → 选中的表格文件 → 当前目录顶层 xlsx/csv（最多 8）。"""
    out = []
    if pending and pending.get("file"):
        out.append(str(pending.get("file")))
    for raw in selected_paths or []:
        p = Path(str(raw))
        if p.suffix.lower() not in (".xlsx", ".xls", ".csv"):
            continue
        # 选中路径即使文件已挪走也要能忘（记忆键是 path 字符串）
        out.append(str(p.resolve()) if p.exists() else str(p))
    if out:
        return list(dict.fromkeys(out))
    root = Path(target_dir)
    if not root.is_dir():
        return []
    try:
        for p in sorted(root.iterdir()):
            if p.is_file() and p.suffix.lower() in (".xlsx", ".xls", ".csv"):
                out.append(str(p.resolve()))
            if len(out) >= 8:
                break
    except OSError:
        pass
    return out


def apply_forget(scopes: list[str]) -> dict:
    from db import choice_memory

    n = 0
    for scope in scopes or []:
        n += int(choice_memory.forget_scope("table_wash", scope) or 0)
    return {"revoked": n, "scopes": list(scopes or [])}
