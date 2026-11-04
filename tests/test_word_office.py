# -*- coding: utf-8 -*-
"""Word 模块 L0：结构检查 + 确定性替换（workspace fixture，沙箱/本地都能跑）。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from docx import Document

from pipeline.word_office import WordModule

BASE = Path(__file__).resolve().parents[1] / "构建" / "smoke" / "_word_test"


@pytest.fixture()
def docx():
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True, exist_ok=True)
    p = BASE / "t.docx"
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
    yield p
    shutil.rmtree(BASE, ignore_errors=True)


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


def test_cross_run_replace(docx):
    # 追加一段：两个 run 拼成 "A型机"，run 级匹配不到，应走段落级
    d = Document(str(docx))
    p = d.add_paragraph()
    p.add_run("A型")
    p.add_run("机")
    d.save(str(docx))
    r = WordModule.handle({"user_input": "把 A型机 替换成 C型机", "selected_files": [str(docx)]})
    assert r["status"] == "done"
    text = "\n".join(p.text for p in Document(str(docx)).paragraphs)
    assert "A型机" not in text


def test_not_found(docx):
    r = WordModule.handle({"user_input": "把 不存在 替换成 X", "selected_files": [str(docx)]})
    assert r["status"] == "failed"
    assert r["reason"] == "not_found"


def test_no_docx():
    r = WordModule.handle({"user_input": "检查结构", "selected_files": ["a.xlsx"]})
    assert r["status"] == "failed"
    assert r["reason"] == "no_docx"


# ---- L1：AI 建议 + 本地执行 + 校验（mock 掉 LLM） ----

def test_l1_replace(docx, monkeypatch):
    import config
    import pipeline.word_office.ai as ai

    monkeypatch.setattr(ai, "call_l1", lambda *a, **k: {
        "summary": "统一命名",
        "ops": [{"find": "A型机", "replace": "C型机"}]})
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "帮我润色一下", "selected_files": [str(docx)]})
    assert r["status"] == "done"
    assert "已按 AI 建议修改" in r["message"]
    text = "\n".join(p.text for p in Document(str(docx)).paragraphs)
    assert "A型机" not in text
    assert "C型机" in text


def test_l1_suggest(docx, monkeypatch):
    import config
    import pipeline.word_office.ai as ai

    monkeypatch.setattr(ai, "call_l1", lambda *a, **k: {
        "summary": "这是一份设备台账。", "ops": []})
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "给个摘要", "selected_files": [str(docx)]})
    assert r["status"] == "done"
    assert "💡" in r["message"]


def test_l1_not_found(docx, monkeypatch):
    import config
    import pipeline.word_office.ai as ai

    monkeypatch.setattr(ai, "call_l1", lambda *a, **k: {
        "summary": "x", "ops": [{"find": "不存在的文本", "replace": "X"}]})
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "润色", "selected_files": [str(docx)]})
    assert r["status"] == "failed"
    assert r["reason"] == "not_found"
