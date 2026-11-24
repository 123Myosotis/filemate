# -*- coding: utf-8 -*-
"""P2B：模板填槽另存（不瞎填、不仿写）。"""
from __future__ import annotations

from pathlib import Path

import config
from docx import Document

from agent_core.local_file_intent import (
    match,
    wants_batch_content_new_docs,
    wants_structure_split,
    wants_template_fill,
)
from engine.dispatcher import IntentDispatcher
from pipeline.content_split import run_template_fill
from pipeline.stage1_parser.file_parser import parse_file


def _write_template(path: Path) -> None:
    doc = Document()
    doc.add_paragraph("协议书")
    doc.add_paragraph("甲方：{{甲方}}")
    doc.add_paragraph("乙方：【乙方】")
    doc.add_paragraph("金额：｛金额｝")
    doc.save(str(path))


def test_wants_template_priority():
    u = "按模板填槽成稿，甲方=张三"
    assert wants_template_fill(u)
    assert not wants_structure_split(u)
    assert not wants_batch_content_new_docs(u)
    assert match(u).status == "veto"


def test_fill_ok_from_utterance(tmp_path: Path):
    _write_template(tmp_path / "合同模板.docx")
    out = run_template_fill(
        "按模板套写 甲方=甲司 乙方=乙司 金额=10万",
        tmp_path,
    )
    assert out.get("ok") is True, out
    assert out.get("reason") == "ok"
    assert (tmp_path / "合同模板.docx").is_file()
    created = list((tmp_path / "模板成稿").glob("合同模板_*.docx"))
    assert created, out
    text = parse_file(created[0]).get("text") or ""
    assert "甲司" in text and "乙司" in text and "10万" in text
    assert "{{甲方}}" not in text
    assert (tmp_path / "模板成稿" / "成稿清单.docx").is_file()
    assert not (tmp_path / "模板成稿" / "_成稿清单.md").exists()


def test_fill_manifest_accumulates_across_runs(tmp_path: Path):
    """累计行进 storage，docx 只展示；第二轮不得覆盖成 1 行。"""
    from docx import Document as DocxDocument

    from pipeline.content_split import state as content_state

    _write_template(tmp_path / "合同模板.docx")
    out1 = run_template_fill(
        "按模板套写 甲方=甲1 乙方=乙1 金额=1万",
        tmp_path,
    )
    assert out1.get("ok") is True, out1
    out2 = run_template_fill(
        "按模板套写 甲方=甲2 乙方=乙2 金额=2万",
        tmp_path,
    )
    assert out2.get("ok") is True, out2
    root = tmp_path / "模板成稿"
    rows_path = content_state.cache_path(root, "fill_rows.json")
    assert rows_path.is_file()
    import json

    data = json.loads(rows_path.read_text(encoding="utf-8"))
    assert len(data.get("rows") or []) == 2
    doc = DocxDocument(str(root / "成稿清单.docx"))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    body = "\n".join(parts)
    assert "累计成稿：2" in body
    # 行存的是模板/槽位/路径，不是槽值；两轮路径应都在表里
    assert body.count("合同模板.docx") >= 2
    assert "_1.docx" in body or body.count("模板成稿\\") >= 2 or body.count("模板成稿/") >= 2
    assert "成稿清单.docx" in (out2.get("message") or "")


def test_missing_slots_fail_closed(tmp_path: Path):
    _write_template(tmp_path / "合同模板.docx")
    out = run_template_fill("按模板填槽 甲方=甲司", tmp_path)
    assert out.get("ok") is False
    assert out.get("reason") == "missing_slots"
    assert "乙方" in (out.get("message") or "")
    assert not list((tmp_path / "模板成稿").glob("*.docx"))


def test_dispatch_template_with_material_file(tmp_path: Path, monkeypatch):
    _write_template(tmp_path / "通知模板.docx")
    (tmp_path / "填槽材料.txt").write_text(
        "甲方=甲\n乙方=乙\n金额=1元\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    out = IntentDispatcher(None).dispatch("用模板生成成稿", tmp_path)
    assert out.get("tool") == "template_fill"
    assert out.get("reason") == "ok"


def test_fill_preserves_table_and_paragraph_style(tmp_path: Path):
    """成稿应复制模板：表格槽位可读可填，段落样式名保留。"""
    tpl = tmp_path / "带表模板.docx"
    doc = Document()
    p = doc.add_paragraph("标题{{标题}}")
    try:
        p.style = "Heading 1"
    except KeyError:
        p.style = doc.styles["Normal"]
    style_name = p.style.name if p.style else "Normal"
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "甲方：{{甲方}}"
    table.cell(0, 1).text = "乙方：【乙方】"
    doc.add_paragraph("金额：｛金额｝")
    doc.save(str(tpl))

    out = run_template_fill(
        "按模板套写 标题=协议 甲方=甲司 乙方=乙司 金额=10万",
        tmp_path,
    )
    assert out.get("ok") is True, out
    created = list((tmp_path / "模板成稿").glob("带表模板_*.docx"))
    assert created
    out_doc = Document(str(created[0]))
    # 表格仍在且已填
    assert out_doc.tables, "成稿应保留表格"
    cell0 = out_doc.tables[0].cell(0, 0).text
    cell1 = out_doc.tables[0].cell(0, 1).text
    assert "甲司" in cell0 and "{{甲方}}" not in cell0
    assert "乙司" in cell1 and "【乙方】" not in cell1
    # 首段样式名应与模板一致（复制就地填，非从零重建）
    assert out_doc.paragraphs[0].style.name == style_name
    body = "\n".join(p.text for p in out_doc.paragraphs)
    assert "协议" in body and "10万" in body
