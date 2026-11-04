# -*- coding: utf-8 -*-
"""Word 模块 L0：结构检查 + 确定性替换（mock 文档，不依赖真实文件）。"""
from __future__ import annotations

import pytest
from docx import Document

from pipeline.word_office import WordModule


@pytest.fixture()
def docx(tmp_path):
    p = tmp_path / "t.docx"
    d = Document()
    d.add_heading("标题", level=1)
    d.add_paragraph("甲方是A型机，乙方是B型机。")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "设备"
    t.rows[0].cells[1].text = "状态"
    row = t.add_row().cells
    row[0].text = "A型机"
    row[1].text = "运行"
    d.save(str(p))
    return p


def test_inspect(docx):
    r = WordModule.handle({"user_input": "检查结构", "selected_files": [str(docx)]})
    assert r["status"] == "done"
    assert "结构检查" in r["message"]


def test_find_replace(docx):
    r = WordModule.handle({"user_input": "把 A型机 替换成 C型机", "selected_files": [str(docx)]})
    assert r["status"] == "done"
    assert "已替换" in r["message"]
    assert (docx.with_suffix(docx.suffix + ".bak")).is_file()
    text = "\n".join(p.text for p in Document(str(docx)).paragraphs)
    assert "A型机" not in text
    assert "C型机" in text


def test_not_found(docx):
    r = WordModule.handle({"user_input": "把 不存在 替换成 X", "selected_files": [str(docx)]})
    assert r["status"] == "failed"
    assert r["reason"] == "not_found"


def test_no_docx():
    r = WordModule.handle({"user_input": "检查结构", "selected_files": ["a.xlsx"]})
    assert r["status"] == "failed"
    assert r["reason"] == "no_docx"
