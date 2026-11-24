# -*- coding: utf-8 -*-
"""P2A：按标题/章节结构拆成多份新文档。"""
from __future__ import annotations

from pathlib import Path

import config
from docx import Document

from agent_core.local_file_intent import (
    wants_batch_content_new_docs,
    wants_structure_split,
    wants_template_fill,
)
from engine.dispatcher import IntentDispatcher
from pipeline.content_split import run_structure_split


def _doc_with_headings(path: Path) -> None:
    doc = Document()
    doc.add_heading("第一章 总则", level=1)
    doc.add_paragraph("总则正文甲。")
    doc.add_paragraph("总则正文乙。")
    doc.add_heading("第二章 细则", level=1)
    doc.add_paragraph("细则正文。")
    doc.save(str(path))


def test_wants_structure_not_p1_or_template():
    u = "把这份word按标题拆成新文档"
    assert wants_structure_split(u)
    assert not wants_batch_content_new_docs(u)
    assert not wants_template_fill(u)


def test_run_structure_split_writes_sections(tmp_path: Path):
    src = tmp_path / "规章.docx"
    _doc_with_headings(src)
    out = run_structure_split("按标题拆成新文档", tmp_path)
    assert out.get("ok") is True, out
    assert out.get("reason") == "ok"
    assert (tmp_path / "规章.docx").is_file()
    root = tmp_path / "内容拆分" / "规章"
    assert (root / "第一章 总则.docx").is_file()
    assert (root / "第二章 细则.docx").is_file()
    assert (tmp_path / "内容拆分" / "拆分清单.docx").is_file()
    assert not (tmp_path / "内容拆分" / "_拆分清单.md").exists()


def test_no_structure_honest_fail(tmp_path: Path):
    doc = Document()
    doc.add_paragraph("只有一段流水账没有标题。再补一点字数保证能打开。")
    doc.save(str(tmp_path / "平.docx"))
    out = run_structure_split("按章节拆成新文档", tmp_path)
    assert out.get("ok") is False
    assert out.get("reason") == "no_structure"
    assert "未检出" in (out.get("message") or "")


def test_dispatch_structure_no_key(tmp_path: Path, monkeypatch):
    _doc_with_headings(tmp_path / "a.docx")
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    out = IntentDispatcher(None).dispatch("按标题拆成多份新文档", tmp_path)
    assert out.get("tool") == "structure_split"
    assert out.get("reason") == "ok"
    assert "选中一个" not in (out.get("message") or "")
