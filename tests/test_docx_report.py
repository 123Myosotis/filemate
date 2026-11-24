# -*- coding: utf-8 -*-
"""通用表格报告：500 行阈值与占用另存。"""
from __future__ import annotations

import os
from pathlib import Path

from docx import Document
from openpyxl import load_workbook

from export.docx_report import write_table_report


def test_under_500_only_docx(tmp_path: Path):
    headers = ["a", "b"]
    rows = [[str(i), "x"] for i in range(10)]
    paths = write_table_report(
        tmp_path / "分类清单",
        "测试清单",
        ["meta"],
        headers,
        rows,
        "note",
        max_rows=500,
    )
    assert len(paths) == 1
    assert paths[0].name == "分类清单.docx"
    assert not (tmp_path / "分类清单.xlsx").exists()
    doc = Document(str(paths[0]))
    assert any("测试清单" in p.text for p in doc.paragraphs)


def test_over_500_docx_and_xlsx(tmp_path: Path):
    headers = ["原文件", "状态", "格式", "判别类型", "得分", "来源", "新文档", "跳过"]
    rows = [[f"f{i}", "已分类", "docx", "其他", "-", "rule", f"d{i}", ""] for i in range(501)]
    paths = write_table_report(
        tmp_path / "分类清单",
        "内容分类清单",
        ["meta"],
        headers,
        rows,
        "note",
        max_rows=500,
    )
    names = {p.name for p in paths}
    assert "分类清单.docx" in names
    assert "分类清单.xlsx" in names
    wb = load_workbook(str(tmp_path / "分类清单.xlsx"))
    # 表头 + 501 行
    assert wb.active.max_row == 502
    doc = Document(str(tmp_path / "分类清单.docx"))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "完整" in text or "xlsx" in text.lower() or "501" in text


def test_locked_target_writes_timestamp(tmp_path: Path, monkeypatch):
    headers = ["a"]
    rows = [["1"]]
    real_replace = os.replace

    def _fail_once(src, dst):
        if str(dst).endswith("分类清单.docx"):
            raise OSError(13, "locked")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_once)
    paths = write_table_report(
        tmp_path / "分类清单",
        "T",
        [],
        headers,
        rows,
        "",
        max_rows=500,
    )
    assert len(paths) == 1
    assert paths[0].name.startswith("分类清单_")
    assert paths[0].suffix == ".docx"
    assert paths[0].is_file()
