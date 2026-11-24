# -*- coding: utf-8 -*-
"""打磨：批注/控件/字段只读 + 纠正可忘记；两套记忆不合并。"""
from __future__ import annotations

from pathlib import Path

import config
import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from agent_core.local_choice_forget import match as forget_match
from agent_core.local_word_read import match as word_read_match
from db import choice_memory
from engine.dispatcher import IntentDispatcher
from pipeline.word_office import WordModule


def _run_with_text(text: str):
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    return r


def _docx_with_sdt(path: Path, *, alias: str, tag: str, text: str) -> None:
    """造一份带内容控件的 docx（python-docx 无高层 API，直接塞 w:sdt）。"""
    d = Document()
    p = d.add_paragraph(text)
    r = p.runs[0]._r
    p._p.remove(r)
    sdt = OxmlElement("w:sdt")
    sdt_pr = OxmlElement("w:sdtPr")
    a = OxmlElement("w:alias")
    a.set(qn("w:val"), alias)
    t = OxmlElement("w:tag")
    t.set(qn("w:val"), tag)
    plain = OxmlElement("w:text")
    sdt_pr.append(a)
    sdt_pr.append(t)
    sdt_pr.append(plain)
    content = OxmlElement("w:sdtContent")
    content.append(r)
    sdt.append(sdt_pr)
    sdt.append(content)
    p._p.append(sdt)
    d.save(str(path))


def _docx_with_fields(path: Path) -> None:
    """简单域 PAGE + 复杂域 REF（交叉引用）。"""
    d = Document()
    p = d.add_paragraph()
    # fldSimple: PAGE
    simple = OxmlElement("w:fldSimple")
    simple.set(qn("w:instr"), " PAGE ")
    simple.append(_run_with_text("1"))
    p._p.append(simple)
    # complex: REF bookmark
    p2 = d.add_paragraph()
    for ftype in ("begin",):
        r = OxmlElement("w:r")
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), ftype)
        r.append(fc)
        p2._p.append(r)
    r_instr = OxmlElement("w:r")
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = ' REF sec1 \\h '
    r_instr.append(it)
    p2._p.append(r_instr)
    for ftype in ("separate",):
        r = OxmlElement("w:r")
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), ftype)
        r.append(fc)
        p2._p.append(r)
    p2._p.append(_run_with_text("第一章"))
    for ftype in ("end",):
        r = OxmlElement("w:r")
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), ftype)
        r.append(fc)
        p2._p.append(r)
    d.save(str(path))


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "")


def test_forget_match_requires_explicit():
    assert forget_match("忘记这次洗表选择").status == "hit"
    assert forget_match("清除纠正记忆").status == "hit"
    assert forget_match("忘记").status == "miss"
    assert forget_match("别记住").status == "miss"
    assert forget_match("按类型整理").status == "miss"


def test_forget_revokes_choice(tmp_path, monkeypatch):
    xlsx = tmp_path / "a.xlsx"
    xlsx.write_bytes(b"PK")  # 占位，只要存在/后缀对
    scope = str(xlsx)
    choice_memory.forget_scope("table_wash", scope)
    choice_memory.record(
        "table_wash", scope, "t1",
        {"action": "skip", "file_hash": "h1", "columns": []},
    )
    assert choice_memory.lookup("table_wash", scope, "t1", file_hash="h1")
    out = IntentDispatcher(None).dispatch(
        "忘记这次洗表选择", tmp_path, file_paths=[scope],
    )
    assert out.get("tool") == "choice_forget"
    assert int(out.get("revoked") or 0) >= 1
    assert choice_memory.lookup("table_wash", scope, "t1", file_hash="h1") is None


def test_word_read_match():
    assert word_read_match("看看批注").op == "list_comments"
    assert word_read_match("看看控件").op == "list_controls"
    assert word_read_match("列出内容控件").op == "list_controls"
    assert word_read_match("看看字段").op == "list_fields"
    assert word_read_match("看看交叉引用").op == "list_fields"
    assert word_read_match("检查结构").op == "inspect"
    assert word_read_match("把甲方改成乙方").status == "miss"


def test_list_comments_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    p = tmp_path / "c.docx"
    d = Document()
    para = d.add_paragraph("甲方是A型机。")
    d.add_comment(para.runs[0], text="请核对金额", author="审", initials="S")
    d.save(str(p))
    before = p.read_bytes()
    r = WordModule.handle({
        "user_input": "看看批注",
        "selected_files": [str(p)],
    })
    assert r["status"] == "done"
    assert r["reason"] == "list_comments"
    assert "请核对金额" in (r.get("message") or "")
    assert p.read_bytes() == before


def test_dispatch_no_key_list_comments(tmp_path):
    p = tmp_path / "c.docx"
    d = Document()
    para = d.add_paragraph("正文")
    d.add_comment(para.runs[0], text="批注甲", author="A", initials="A")
    d.save(str(p))
    out = IntentDispatcher(None).dispatch(
        "看看批注", tmp_path, file_paths=[str(p)])
    assert out.get("tool") == "word_office"
    assert "批注甲" in (out.get("message") or "")
    assert out.get("status") == "done"


def test_list_controls_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    p = tmp_path / "ctrl.docx"
    _docx_with_sdt(p, alias="甲方名称", tag="party_a", text="某某公司")
    before = p.read_bytes()
    r = WordModule.handle({
        "user_input": "看看控件",
        "selected_files": [str(p)],
    })
    assert r["status"] == "done"
    assert r["reason"] == "list_controls"
    msg = r.get("message") or ""
    assert "甲方名称" in msg
    assert "某某公司" in msg
    assert p.read_bytes() == before
    assert (r.get("controls") or [{}])[0].get("type") == "text"


def test_dispatch_no_key_list_controls(tmp_path):
    p = tmp_path / "ctrl.docx"
    _docx_with_sdt(p, alias="投标有效期", tag="validity", text="90天")
    out = IntentDispatcher(None).dispatch(
        "看看控件", tmp_path, file_paths=[str(p)])
    assert out.get("tool") == "word_office"
    assert "投标有效期" in (out.get("message") or "")
    assert "90天" in (out.get("message") or "")
    assert out.get("status") == "done"


def test_inspect_mentions_control_count(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    p = tmp_path / "ctrl.docx"
    _docx_with_sdt(p, alias="金额", tag="amt", text="100")
    r = WordModule.handle({
        "user_input": "检查结构",
        "selected_files": [str(p)],
    })
    assert r["status"] == "done"
    assert "控件 1 个" in (r.get("message") or "")


def test_write_still_blocked_when_has_control(tmp_path, monkeypatch):
    """有控件时：只读放行，A 级写仍要确认（不静默）。"""
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    p = tmp_path / "ctrl.docx"
    _docx_with_sdt(p, alias="甲方", tag="a", text="旧名")
    before = p.read_bytes()
    r = WordModule.handle({
        "user_input": "把旧名改成新名",
        "selected_files": [str(p)],
    })
    assert r["status"] == "failed"
    assert "结构风险" in (r.get("message") or "")
    assert p.read_bytes() == before


def test_list_fields_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    p = tmp_path / "fld.docx"
    _docx_with_fields(p)
    before = p.read_bytes()
    r = WordModule.handle({
        "user_input": "看看字段",
        "selected_files": [str(p)],
    })
    assert r["status"] == "done"
    assert r["reason"] == "list_fields"
    msg = r.get("message") or ""
    assert "PAGE" in msg
    assert "REF" in msg
    assert "第一章" in msg
    assert "交叉引用类" in msg
    assert p.read_bytes() == before
    kinds = {f.get("kind") for f in (r.get("fields") or [])}
    assert "PAGE" in kinds
    assert "REF" in kinds


def test_dispatch_no_key_list_fields(tmp_path):
    p = tmp_path / "fld.docx"
    _docx_with_fields(p)
    out = IntentDispatcher(None).dispatch(
        "看看交叉引用", tmp_path, file_paths=[str(p)])
    assert out.get("tool") == "word_office"
    assert "REF" in (out.get("message") or "")
    assert out.get("status") == "done"


def test_inspect_mentions_field_count(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    p = tmp_path / "fld.docx"
    _docx_with_fields(p)
    r = WordModule.handle({
        "user_input": "检查结构",
        "selected_files": [str(p)],
    })
    assert r["status"] == "done"
    assert "字段 2 个" in (r.get("message") or "")


def test_write_still_blocked_when_has_field(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    p = tmp_path / "fld.docx"
    _docx_with_fields(p)
    # 给正文加可替换字，避免 not_found；有字段 → 结构风险应拒写
    d = Document(str(p))
    d.add_paragraph("旧名在此")
    d.save(str(p))
    before = p.read_bytes()
    r = WordModule.handle({
        "user_input": "把旧名改成新名",
        "selected_files": [str(p)],
    })
    assert r["status"] == "failed"
    assert "结构风险" in (r.get("message") or "")
    assert p.read_bytes() == before
