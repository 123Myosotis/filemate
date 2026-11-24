# -*- coding: utf-8 -*-
"""A 级 Word **只读**：结构 / 批注 / 控件 / 字段（交叉引用）。0 token，不写文件。

禁止用本模块猜改写/润色。写批注 / 写控件 / 写字段仍在 CapabilityManifest.rejected。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class WordReadMatch:
    status: str  # hit / miss
    op: str = ""
    reason: str = ""


_LIST_COMMENTS = re.compile(
    r"(?:看看|查看|列出|有哪些|有什么)?\s*批注|(?:批注)\s*(?:列表|有哪些|多少)"
)
_LIST_CONTROLS = re.compile(
    r"(?:看看|查看|列出|有哪些|有什么)?\s*(?:内容)?控件|"
    r"(?:内容控件|内容控制|表单域)\s*(?:列表|有哪些|多少)"
)
_LIST_FIELDS = re.compile(
    r"(?:看看|查看|列出|有哪些|有什么)?\s*(?:字段|域|交叉引用)|"
    r"(?:交叉引用|字段|域)\s*(?:列表|有哪些|多少)"
)
_INSPECT = re.compile(
    r"(?:检查结构|看看结构|结构检查|文档结构|这份.*结构)"
)

# 调度器用：命中后换成高置信口令，避免低置信弹窗
CANONICAL_PROMPT = {
    "list_comments": "看看批注",
    "list_controls": "看看控件",
    "list_fields": "看看字段",
    "inspect": "检查结构",
}


def match(user_input: str) -> WordReadMatch:
    text = str(user_input or "").strip()
    if not text:
        return WordReadMatch("miss", reason="空指令")
    if _LIST_COMMENTS.search(text):
        return WordReadMatch("hit", op="list_comments", reason="A 级只读批注")
    if _LIST_CONTROLS.search(text):
        return WordReadMatch("hit", op="list_controls", reason="A 级只读控件")
    if _LIST_FIELDS.search(text):
        return WordReadMatch("hit", op="list_fields", reason="A 级只读字段")
    if _INSPECT.search(text):
        return WordReadMatch("hit", op="inspect", reason="A 级结构检查")
    return WordReadMatch("miss", reason="非只读 Word 指令")
