# -*- coding: utf-8 -*-
"""Word 文档状态扫描 + 三态谓词（L0 前置路由用）。

重架构（九对象/七状态机/决策表/Overlay/preflight/分片/事务层）
只在这里留**数据字段**，具体实现见 `module.py` 的占位函数。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from docx import Document
from lxml import etree


def _xml(doc) -> str:
    return etree.tostring(doc.element.body, encoding="unicode")


def file_sha256(path: Path) -> str:
    """文件内容指纹（检测"文件被改"）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def structure_sha256(state: dict) -> str:
    """结构指纹（段落数 / 表格 / 样式分布）。"""
    blob = json.dumps(
        {"p": state.get("paragraphs"), "t": state.get("tables"),
         "s": state.get("styles")},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def scan(path: Path) -> dict:
    """扫描 docx → DocumentState（最小字段，可扩展）。"""
    doc = Document(str(path))
    paragraphs = [p for p in doc.paragraphs if p.text.strip()]

    styles: dict[str, int] = {}
    for p in paragraphs:
        name = p.style.name if p.style is not None else "Normal"
        styles[name] = styles.get(name, 0) + 1

    tables = []
    for t in doc.tables:
        nrows, ncols = len(t.rows), len(t.columns)
        tcs = [t.cell(r, c)._tc for r in range(nrows) for c in range(ncols)]
        merged = len(tcs) - len({id(x) for x in tcs})
        tables.append({"rows": nrows, "cols": ncols, "merged": merged})

    xml = _xml(doc)
    result = {
        "path": str(path),
        "paragraphs": len(paragraphs),
        "styles": styles,
        "tables": tables,
        "inline_shapes": len(doc.inline_shapes),
        "has_revision": ("<w:ins " in xml) or ("<w:del " in xml),
        "has_control": "<w:sdt" in xml,
        "has_field": ("<w:fldSimple" in xml) or ("<w:instrText" in xml),
        "has_bookmark": "<w:bookmarkStart" in xml,
        "has_nested_table": xml.count("<w:tbl>") > len(doc.tables),
    }
    result["structure_sha256"] = structure_sha256(result)
    return result


def predicates(state: dict) -> dict:
    """三态谓词：true / false / unknown；unknown 保守处理（转确认或升级）。"""

    def tri(ok: bool, evidence: str) -> dict:
        return {"value": "true" if ok else "false", "evidence": evidence}

    return {
        "no_revision_in_target": tri(
            not state["has_revision"],
            "有修订标记" if state["has_revision"] else "无修订"),
        "no_content_control": tri(
            not state["has_control"],
            "有内容控件" if state["has_control"] else "无控件"),
        "no_field_in_range": tri(
            not state["has_field"],
            "有字段" if state["has_field"] else "无字段"),
        "no_bookmark_in_range": tri(
            not state["has_bookmark"],
            "有书签" if state["has_bookmark"] else "无书签"),
        "no_nested_table": tri(
            not state["has_nested_table"],
            "有嵌套表" if state["has_nested_table"] else "无嵌套表"),
        "undo_available": {
            "value": "unknown",
            "evidence": "python-docx 不暴露 undo 栈；靠写前备份兜底",
        },
    }


_STRUCTURAL_PREDICATES = (
    "no_revision_in_target",
    "no_content_control",
    "no_field_in_range",
    "no_bookmark_in_range",
    "no_nested_table",
)


def is_clean(preds: dict) -> bool:
    """结构类三态谓词全为 true 才算干净；undo_available 单独由备份兜底，不参与。"""
    return all(preds.get(k, {}).get("value") == "true" for k in _STRUCTURAL_PREDICATES)
