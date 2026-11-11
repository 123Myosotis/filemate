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


def test_tool_registry_dispatch(docx, monkeypatch):
    """ToolRegistry：dispatch 表能把 op 转到执行器。"""
    from pipeline.word_office.objects import ToolRegistry
    assert ToolRegistry().tools["insert"] == "_exec_insert"
    assert ToolRegistry().tools["tracked_change"] == "_exec_tracked_change"


def test_patch_set_in_result(docx, monkeypatch):
    """PatchSet：执行结果带 patch_set（ops/backup/applied）。"""
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "把 A型机 替换成 C型机",
                           "selected_files": [str(docx)]})
    assert r["status"] == "done"
    assert "patch_set" in r
    assert r["patch_set"]["applied"] >= 1


def test_overlay_dangerous(docx, monkeypatch):
    """ConfirmationOverlay：delete → modal（needs_confirmation）。"""
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "删除 甲方是A型机",
                           "selected_files": [str(docx)]})
    assert r["status"] == "needs_confirmation"


def test_capability_manifest():
    """CapabilityManifest：能力清单包含 7 个 op。"""
    from pipeline.word_office.objects import CapabilityManifest
    ops = set(CapabilityManifest().op_types)
    assert {"replace", "insert", "delete", "format", "move", "tracked_change"} <= ops


def test_redaction_policy():
    """RedactionPolicy：api_key 落盘前被抹掉。"""
    from pipeline.word_office.evidence import _redact
    out = _redact({"api_key": "sk-xxx", "user_input": "hi", "authorization": "Bearer y"})
    assert out["api_key"] == "<redacted>"
    assert out["authorization"] == "<redacted>"
    assert out["user_input"] == "hi"


def test_intermediate_artifact():
    """IntermediateArtifact：画像返回结构体（kind/ref/payload）。"""
    import config
    from docx import Document
    from pipeline.word_office.module import _portrait
    from pipeline.word_office.state import scan
    monkeypatch = None
    config.get_api_key = lambda: "k"
    p = BASE / "art.docx"
    p.parent.mkdir(parents=True, exist_ok=True)
    d = Document()
    d.add_paragraph("甲方是A型机。")
    d.save(str(p))
    art = _portrait(p, scan(p))
    assert art.kind == "portrait"
    assert art.payload["text"].startswith("段落数")


def test_preflight():
    """preflight：没有执行器的 op → 拒。"""
    from pipeline.word_office.module import _preflight
    assert _preflight({"operations": [{"op": "insert"}]})["ok"] is True
    bad = _preflight({"operations": [{"op": "nope"}]})
    assert bad["ok"] is False
    assert "no_executor" in bad["reason"]


def test_format_color(docx, monkeypatch):
    """format 颜色：设为红色。"""
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "把 甲方是A型机 设为红色",
                           "selected_files": [str(docx)]})
    assert r["status"] == "done"
    assert r["reason"] == "format"


def test_dry_run(docx, monkeypatch):
    """dry-run：执行后恢复备份，**文件不变**。"""
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    before = docx.read_bytes()
    r = WordModule.handle({"user_input": "把 A型机 替换成 C型机",
                           "selected_files": [str(docx)], "dry_run": True})
    assert r["status"] == "done"
    assert r.get("dry_run") is True
    assert r.get("applied") == 0
    assert docx.read_bytes() == before


def test_rollback(docx, monkeypatch):
    """rollback：执行后恢复备份。"""
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    before = docx.read_bytes()
    r = WordModule.handle({"user_input": "把 A型机 替换成 C型机",
                           "selected_files": [str(docx)], "rollback": True})
    assert r["status"] == "done"
    assert r.get("rollback") is True
    assert docx.read_bytes() == before


def test_outline():
    """outline：标题降级（Heading 1 → Heading 2）。"""
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    base = BASE / "outline"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "o.docx"
    d = Document()
    d.add_heading("一、总览", level=1)
    d.save(str(p))
    config.get_api_key = lambda: "k"
    r = WordModule.handle({"user_input": "把 一、总览 设为二级标题",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert r["reason"] == "outline"
    assert Document(str(p)).paragraphs[0].style.name == "Heading 2"


def test_move_keeps_punctuation(docx, monkeypatch):
    """move：run 级移动时**带紧邻标点**。"""
    import config
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    r = WordModule.handle({"user_input": "把 乙方是B型机 移到 甲方是A型机 前面",
                           "selected_files": [str(docx)]})
    assert r["status"] == "done"


def test_outline_warns_numbering():
    """outline：带 1.2 编号时，提示「编号可能需手工核对」。"""
    import config
    from docx import Document
    from pipeline.word_office import WordModule

    base = BASE / "outline2"
    base.mkdir(parents=True, exist_ok=True)
    p = base / "o2.docx"
    d = Document()
    d.add_heading("1.2 背景", level=2)
    d.save(str(p))
    config.get_api_key = lambda: "k"
    r = WordModule.handle({"user_input": "把 1.2 背景 设为一级标题",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert "编号可能需手工核对" in r["message"]


def test_independent_backup():
    """独立备份：storage/backups/word 下有文件。"""
    from pathlib import Path
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule
    import config

    config.get_api_key = lambda: "k"
    base = BASE / "bk"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "b.docx"
    d = Document(); d.add_paragraph("甲方是A型机。"); d.save(str(p))
    WordModule.handle({"user_input": "把 A型机 替换成 C型机",
                       "selected_files": [str(p)]})
    root = Path(__file__).resolve().parents[1] / "storage" / "backups" / "word"
    assert root.exists()
    assert list(root.glob("*_b.docx")), "独立备份没生成"


def test_table_to_text():
    """跨结构（表→叙述）：表格转成段落，表格消失。"""
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    config.get_api_key = lambda: "k"
    base = BASE / "t2t"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "t.docx"
    d = Document()
    d.add_paragraph("前文")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "名称"
    t.cell(0, 1).text = "数量"
    t.cell(1, 0).text = "A型机"
    t.cell(1, 1).text = "3"
    d.save(str(p))
    r = WordModule.handle({"user_input": "把 设备表 表格转成文字",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert r["reason"] == "table_to_text"
    d2 = Document(str(p))
    assert len(d2.tables) == 0
    texts = [q.text for q in d2.paragraphs]
    assert "名称：数量" in texts
    assert "A型机：3" in texts


def test_table_to_text_by_caption():
    """跨结构（表→叙述）：`find` 是表标题（不在格子里）也能精确定位。"""
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    config.get_api_key = lambda: "k"
    base = BASE / "t2t_cap"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "c.docx"
    d = Document()
    d.add_paragraph("设备表")
    d.add_paragraph("（隔一段的说明）")   # caption 与表之间隔一段
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "名称"
    t.cell(0, 1).text = "数量"
    t.cell(1, 0).text = "A型机"
    t.cell(1, 1).text = "3"
    d.add_paragraph("后文")
    d.save(str(p))
    r = WordModule.handle({"user_input": "把 设备表 表格转成文字",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert "按表标题" in r["message"]
    d2 = Document(str(p))
    assert len(d2.tables) == 0
    texts = [q.text for q in d2.paragraphs]
    assert "名称：数量" in texts
    assert "后文" in texts


def test_text_to_table():
    """跨结构（叙述→表）：`键：值` 段落转成 2 列表格。"""
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    config.get_api_key = lambda: "k"
    base = BASE / "t2t2"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "t2.docx"
    d = Document()
    d.add_paragraph("设备概况")
    d.add_paragraph("名称：A型机")
    d.add_paragraph("数量：3")
    d.save(str(p))
    r = WordModule.handle({"user_input": "把 设备概况 转成表格",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert r["reason"] == "text_to_table"
    tables = Document(str(p)).tables
    assert len(tables) == 1
    assert [c.text for c in tables[0].rows[0].cells] == ["名称", "A型机"]


def test_text_to_table_skips_blank_and_equals():
    """跨结构（叙述→表）：跳过空行、支持 `=` 分隔。"""
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    config.get_api_key = lambda: "k"
    base = BASE / "t2t3"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "t3.docx"
    d = Document()
    d.add_paragraph("设备概况")
    d.add_paragraph("名称=A型机")
    d.add_paragraph("")           # 空行
    d.add_paragraph("数量=3")
    d.add_paragraph("（结束）")    # 非键值对 → 停止
    d.save(str(p))
    r = WordModule.handle({"user_input": "把 设备概况 转成表格",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert r["reason"] == "text_to_table"
    tables = Document(str(p)).tables
    assert len(tables) == 1
    rows = [[c.text for c in row.cells] for row in tables[0].rows]
    assert rows == [["名称", "A型机"], ["数量", "3"]]
    # 结束标记不被吞
    assert "（结束）" in [q.text for q in Document(str(p)).paragraphs]


def test_table_to_outline():
    """跨结构（表→大纲）：表格首列 → 标题，其余列 → 正文。"""
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    config.get_api_key = lambda: "k"
    base = BASE / "t2o"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "t.docx"
    d = Document()
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "一、总览"
    t.cell(0, 1).text = "说明"
    t.cell(1, 0).text = "二、明细"
    t.cell(1, 1).text = "说明"
    d.save(str(p))
    r = WordModule.handle({"user_input": "把 设备表 表格转成大纲",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert r["reason"] == "table_to_outline"
    d2 = Document(str(p))
    assert len(d2.tables) == 0
    texts = [q.text for q in d2.paragraphs]
    assert "一、总览" in texts
    assert "说明" in texts      # 其余列保留为正文
    assert "二、明细" in texts


def test_outline_to_table():
    """跨结构（大纲→表）：标题 → 2 列表格（标题 + 数字层级），**默认保留原标题**。"""
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    config.get_api_key = lambda: "k"
    base = BASE / "o2t"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "o.docx"
    d = Document()
    d.add_paragraph("大纲")
    d.add_heading("一、总览", level=1)
    d.add_heading("二、明细", level=2)
    d.save(str(p))
    r = WordModule.handle({"user_input": "把 大纲 转成表格",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert r["reason"] == "outline_to_table"
    d2 = Document(str(p))
    tables = d2.tables
    assert len(tables) == 1
    assert [c.text for c in tables[0].rows[0].cells] == ["一、总览", "1"]
    # 默认保留原标题（非破坏性）
    heads = [q.text for q in d2.paragraphs if (q.style.name or "").startswith("Heading")]
    assert heads == ["一、总览", "二、明细"]


def test_outline_to_table_remove_source():
    """跨结构（大纲→表）：显式「并删除原标题」→ 标题被搬走。"""
    import config
    import shutil
    from docx import Document
    from pipeline.word_office import WordModule

    config.get_api_key = lambda: "k"
    base = BASE / "o2t_del"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "o.docx"
    d = Document()
    d.add_paragraph("大纲")
    d.add_heading("一、总览", level=1)
    d.add_heading("二、明细", level=2)
    d.save(str(p))
    r = WordModule.handle({"user_input": "把 大纲 转成表格并删除原标题",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert r["reason"] == "outline_to_table"
    d2 = Document(str(p))
    assert len(d2.tables) == 1
    heads = [q.text for q in d2.paragraphs if (q.style.name or "").startswith("Heading")]
    assert heads == []
