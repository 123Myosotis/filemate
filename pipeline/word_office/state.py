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
from docx.oxml.ns import qn
from lxml import etree


def _xml(doc) -> str:
    return etree.tostring(doc.element.body, encoding="unicode")


# w:sdtPr 里常见类型节点（缺省当 richText）
_SDT_TYPE_TAGS = (
    "text", "richText", "comboBox", "dropDownList", "date", "checkbox",
    "docPartObj", "citation", "group", "picture", "repeatingSection",
)


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
    comments = list_comments(path, doc=doc)
    controls = list_controls(path, doc=doc)
    fields = list_fields(path, doc=doc)
    result = {
        "path": str(path),
        "paragraphs": len(paragraphs),
        "styles": styles,
        "tables": tables,
        "inline_shapes": len(doc.inline_shapes),
        "comment_count": len(comments),
        "control_count": len(controls),
        "field_count": len(fields),
        "has_revision": ("<w:ins " in xml) or ("<w:del " in xml),
        "has_control": "<w:sdt" in xml,
        "has_field": ("<w:fldSimple" in xml) or ("<w:instrText" in xml),
        "has_bookmark": "<w:bookmarkStart" in xml,
        "has_nested_table": xml.count("<w:tbl>") > len(doc.tables),
    }
    result["structure_sha256"] = structure_sha256(result)
    return result


def list_comments(path: Path, *, doc=None, limit: int = 50) -> list[dict]:
    """只读批注。不写文件。写批注仍拒。"""
    if doc is None:
        doc = Document(str(path))
    out = []
    try:
        bag = getattr(doc, "comments", None)
        items = list(bag) if bag is not None else []
    except Exception:  # noqa: BLE001
        return []
    for c in items[: max(0, int(limit))]:
        out.append({
            "author": str(getattr(c, "author", "") or ""),
            "text": str(getattr(c, "text", "") or "").strip(),
        })
    return out


def list_controls(path: Path, *, doc=None, limit: int = 50) -> list[dict]:
    """只读内容控件（w:sdt）。不写文件；写控件仍拒。

    只扫正文 body（不含页眉页脚），与批注只读同一「能读多少算多少、读不出明示」原则。
    """
    if doc is None:
        doc = Document(str(path))
    out: list[dict] = []
    try:
        body = doc.element.body
    except Exception:  # noqa: BLE001
        return []
    cap = max(0, int(limit))
    for sdt in body.iter(qn("w:sdt")):
        if len(out) >= cap:
            break
        pr = sdt.find(qn("w:sdtPr"))
        alias = ""
        tag = ""
        ctype = "richText"
        if pr is not None:
            a = pr.find(qn("w:alias"))
            if a is not None:
                alias = str(a.get(qn("w:val")) or "")
            t = pr.find(qn("w:tag"))
            if t is not None:
                tag = str(t.get(qn("w:val")) or "")
            for name in _SDT_TYPE_TAGS:
                if pr.find(qn("w:%s" % name)) is not None:
                    ctype = name
                    break
        content = sdt.find(qn("w:sdtContent"))
        parts: list[str] = []
        if content is not None:
            for node in content.iter(qn("w:t")):
                if node.text:
                    parts.append(node.text)
        text = "".join(parts).strip()
        out.append({
            "alias": alias,
            "tag": tag,
            "type": ctype,
            "text": text,
        })
    return out


def _field_kind(instr: str) -> str:
    """指令首词：REF / PAGEREF / PAGE / DATE / HYPERLINK …"""
    s = str(instr or "").strip()
    if not s:
        return "unknown"
    token = s.split(None, 1)[0].upper().lstrip("\\")
    return token or "unknown"


def list_fields(path: Path, *, doc=None, limit: int = 50) -> list[dict]:
    """只读域/字段（含交叉引用 REF/PAGEREF）。不写文件；不更新域结果。

    覆盖：`w:fldSimple` + 复杂域（fldChar begin/separate/end + instrText）。
    只扫正文；嵌套域扁平列出（能读多少算多少）。
    """
    if doc is None:
        doc = Document(str(path))
    out: list[dict] = []
    try:
        body = doc.element.body
    except Exception:  # noqa: BLE001
        return []
    cap = max(0, int(limit))

    for el in body.iter(qn("w:fldSimple")):
        if len(out) >= cap:
            return out
        instr = str(el.get(qn("w:instr")) or "").strip()
        parts = [t.text for t in el.iter(qn("w:t")) if t.text]
        text = "".join(parts).strip()
        out.append({
            "kind": _field_kind(instr),
            "instr": instr,
            "text": text,
            "form": "simple",
        })

    mode = None  # None | instr | result
    instr_parts: list[str] = []
    result_parts: list[str] = []
    for el in body.iter():
        if len(out) >= cap:
            break
        tag = el.tag
        if tag == qn("w:fldChar"):
            ftype = el.get(qn("w:fldCharType"))
            if ftype == "begin":
                mode = "instr"
                instr_parts = []
                result_parts = []
            elif ftype == "separate" and mode == "instr":
                mode = "result"
            elif ftype == "end" and mode is not None:
                instr = "".join(instr_parts).strip()
                text = "".join(result_parts).strip()
                if instr or text:
                    out.append({
                        "kind": _field_kind(instr),
                        "instr": instr,
                        "text": text,
                        "form": "complex",
                    })
                mode = None
                instr_parts = []
                result_parts = []
        elif mode == "instr" and tag == qn("w:instrText"):
            if el.text:
                instr_parts.append(el.text)
            if el.tail:
                instr_parts.append(el.tail)
        elif mode == "result" and tag == qn("w:t"):
            if el.text:
                result_parts.append(el.text)
    return out[:cap]


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
