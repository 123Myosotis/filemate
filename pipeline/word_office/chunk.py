# -*- coding: utf-8 -*-
"""E 分块（阶段 6）：按**标题层级**把 docx 切成块（不按字数）。"""
from __future__ import annotations

from pathlib import Path

from docx import Document

_HEADING_STYLES = ("Heading 1", "Heading 2", "Heading 3", "Title")


def split_by_heading(path: Path) -> list[dict]:
    """按 Heading 1/2/3 切块；无标题则整篇一块。"""
    doc = Document(str(path))
    chunks: list[dict] = []
    current = {"title": "", "style": "", "paras": []}
    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = p.style.name if p.style is not None else "Normal"
        if style in _HEADING_STYLES:
            if current["paras"] or current["title"]:
                chunks.append(current)
            current = {"title": text, "style": style, "paras": []}
        else:
            current["paras"].append(text)
    if current["paras"] or current["title"]:
        chunks.append(current)
    if not chunks:
        return [{"title": "", "style": "", "n_paras": 0, "text": ""}]
    return [{"title": c["title"], "style": c["style"],
             "n_paras": len(c["paras"]),
             "text": "\n".join(c["paras"])[:2000]} for c in chunks]
