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


# ---- 阶段 3：指纹 + 证据 ----

def test_fingerprint_stable(docx):
    from pipeline.word_office.state import scan

    s1 = scan(docx)
    s2 = scan(docx)
    assert s1["structure_sha256"] == s2["structure_sha256"]
    assert s1["structure_sha256"]


def test_evidence_written(docx, monkeypatch):
    import config
    from pipeline.word_office import evidence

    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    before = len(list(evidence.DIR.glob("*.json.gz"))) if evidence.DIR.exists() else 0
    r = WordModule.handle({"user_input": "把 A型机 替换成 C型机",
                           "selected_files": [str(docx)]})
    assert r["status"] == "done"
    after = len(list(evidence.DIR.glob("*.json.gz")))
    assert after >= before + 1


# ---- 阶段 4.2：handle 后写记忆 ----

def test_memory_written(docx, monkeypatch):
    import config
    from pipeline.word_office import memory
    from pipeline.word_office.state import file_sha256

    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    WordModule.handle({"user_input": "检查结构", "selected_files": [str(docx)]})
    m = memory.get_file_memory(file_sha256(docx))
    assert m is not None
    assert m["summary"].startswith("段落")
    assert m["path"]


# ---- 阶段 6.1：insert / delete / format ----

def test_insert(docx, monkeypatch):
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "在 标题 后面插入 新章节",
                           "selected_files": [str(docx)]})
    assert r["status"] == "done"
    assert r["reason"] == "insert"


def test_delete_requires_confirmation(docx, monkeypatch):
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "删除 甲方是A型机",
                           "selected_files": [str(docx)]})
    assert r["status"] == "needs_confirmation"
    assert r["reason"] == "dangerous_op"


def test_delete(docx, monkeypatch):
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "删除 甲方是A型机",
                           "selected_files": [str(docx)], "confirmed": True})
    assert r["status"] == "done"
    assert r["reason"] == "delete"


def test_format_bold(docx, monkeypatch):
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "把 乙方是B型机 加粗",
                           "selected_files": [str(docx)]})
    assert r["status"] == "done"
    assert r["reason"] == "format"


def test_move():
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    base = BASE / "move"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "m.docx"
    d = Document()
    d.add_paragraph("第一段：甲方是A型机。")
    d.add_paragraph("第二段：乙方是B型机。")
    d.save(str(p))
    config.get_api_key = lambda: "k"
    r = WordModule.handle({"user_input": "把 甲方是A型机 移到 乙方是B型机 后面",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert r["reason"] == "move"


def test_move_not_found(docx, monkeypatch):
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "把 不存在 移到 乙方是B型机 后面",
                           "selected_files": [str(docx)]})
    assert r["status"] == "failed"
    assert r["reason"] == "not_found"


def test_tracked_change(docx, monkeypatch):
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "修订：把 甲方是A型机 改成 甲方是X型机",
                           "selected_files": [str(docx)]})
    assert r["status"] == "done"
    assert r["reason"] == "tracked_change"


def test_tracked_change_not_found(docx, monkeypatch):
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "修订：把 不存在 改成 也不存在",
                           "selected_files": [str(docx)]})
    assert r["status"] == "failed"
    assert r["reason"] == "not_found"


def test_tracked_change_run_level(docx, monkeypatch):
    """run 级：`w:delText` 只含 find，不含整段。"""
    import config
    import re
    import zipfile
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "修订：把 甲方是A型机 改成 甲方是X型机",
                           "selected_files": [str(docx)]})
    assert r["status"] == "done"
    xml = zipfile.ZipFile(str(docx)).read("word/document.xml").decode("utf-8")
    texts = re.findall(r"<w:delText[^>]*>([^<]*)</w:delText>", xml)
    assert texts == ["甲方是A型机"]


def test_delete_preserves_format():
    """delete 只删目标文字，**其余 run 的格式保留**。"""
    import config
    import shutil
    from docx import Document
    from docx.shared import Pt
    from pipeline.word_office import WordModule

    base = BASE / "fmt"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "f.docx"
    d = Document()
    para = d.add_paragraph()
    r1 = para.add_run("甲方是A型机，")
    r1.bold = True
    r1.font.size = Pt(14)
    r2 = para.add_run("乙方是B型机。")
    r2.italic = True
    d.save(str(p))
    config.get_api_key = lambda: "k"
    r = WordModule.handle({"user_input": "删除 甲方是A型机",
                           "selected_files": [str(p)], "confirmed": True})
    assert r["status"] == "done"
    d2 = Document(str(p))
    texts = {run.text: run for q in d2.paragraphs for run in q.runs if run.text}
    assert "，" in texts
    assert texts["，"].bold is True
    assert texts["，"].font.size == Pt(14)
    assert texts["乙方是B型机。"].italic is True


def test_insert_inline():
    """insert 在非标题段落里是**内联**（不新增段落）。"""
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    base = BASE / "ins"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "i.docx"
    d = Document()
    d.add_paragraph("甲方是A型机，乙方是B型机。")
    d.save(str(p))
    config.get_api_key = lambda: "k"
    r = WordModule.handle({"user_input": "在 甲方是A型机 后面插入（已确认）",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    d2 = Document(str(p))
    assert len(d2.paragraphs) == 1
    assert "（已确认）" in d2.paragraphs[0].text


def test_format_cross_run():
    """format 跨 run：目标文字被拆在两个 run 里也能加粗。"""
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    base = BASE / "fmtrun"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "f.docx"
    d = Document()
    para = d.add_paragraph()
    para.add_run("甲方是")
    para.add_run("A型机，乙方是B型机。")
    d.save(str(p))
    config.get_api_key = lambda: "k"
    r = WordModule.handle({"user_input": "把 A型机 加粗",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    d2 = Document(str(p))
    bolded = "".join(run.text for run in d2.paragraphs[0].runs if run.bold)
    assert "A型机" in bolded
