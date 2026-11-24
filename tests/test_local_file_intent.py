# -*- coding: utf-8 -*-
"""A 级 file_organize：契约规则编译、veto、无 Key 可整理、不猜查表/Word。"""
from __future__ import annotations

import re
from pathlib import Path

import config
from agent_core.local_file_intent import match
from engine.dispatcher import IntentDispatcher
from engine.executor import FileRuleEngine
from engine.rules import ExtensionRule


def _fo():
    from pipeline.synth_contract.registry import load_contract
    return load_contract().data.get("file_organize") or {}


def test_file_organize_rules_compile_and_unique():
    fo = _fo()
    rules = list(fo.get("intent_rules") or []) + list(fo.get("veto_patterns") or [])
    assert rules
    ids, prios = [], []
    for r in rules:
        rid = r.get("id")
        pat = str(r.get("pattern") or "")
        assert pat, "规则 %s 缺 pattern" % rid
        re.compile(pat)
        ids.append(rid)
        prios.append(int(r.get("priority") or 0))
    assert len(ids) == len(set(ids)), "规则 id 重复"
    assert len(prios) == len(set(prios)), "规则 priority 重复（会互相抢）"


def test_hit_by_ext_and_contract():
    m = match("按类型整理")
    assert m.status == "hit"
    assert m.rule_id == "R_BY_EXT"
    assert m.intent["actions"][0]["op"] == "organize_by_ext"
    m2 = match("把合同归档")
    assert m2.status == "hit"
    assert m2.rule_id == "R_KEYWORD_CONTRACT"
    assert m2.intent["actions"][0]["params"]["keyword"] == "合同"


def test_veto_table_word_consult_content():
    assert match("把这几张表联查一下").status == "veto"
    assert match("把这几张表联查一下").kind == "table"
    assert match("润色这一段").status == "veto"
    assert match("润色这一段").kind == "word"
    assert match("怎么整理这些文件").status == "veto"
    assert match("怎么整理这些文件").kind == "consult"
    assert match("整理内容并生成报告").status == "veto"
    assert match("整理内容并生成报告").kind == "content"
    assert match("这些表格里的数据筛出来").status == "veto"
    assert match("整理这个 Word").status == "veto"
    assert match("整理这个 Word").kind == "word"


def test_veto_content_recognize_then_split_docs():
    """「内容识别后分类进新文档」≠ 按扩展名搬家；禁止 A 级。

    用例说明：docs/test_cases/content_recognize_not_a_organize.md
    """
    cases_veto = (
        "帮我把现在桌面上的word文档内容识别后，分类整理进各自新文档里",
        "按内容分类整理",
        "把这些文档内容识别后分类进新文档",
    )
    for u in cases_veto:
        m = match(u)
        assert m.status == "veto", (u, m)
        assert m.kind in ("content", "word"), (u, m)
    # 纯位置整理仍可走 A
    assert match("分类整理").status == "hit"
    assert match("分类整理").rule_id == "R_DEFAULT_ORGANIZE"
    assert match("按类型整理").status == "hit"
    assert match("按类型整理").rule_id == "R_BY_EXT"


def test_dispatch_content_recognize_does_not_move(tmp_path, monkeypatch):
    """有/无 Key：内容识别句都不得触发 A 级移动（含 .lnk 扫尾）。"""
    (tmp_path / "说明.docx").write_bytes(b"PK")
    (tmp_path / "微信开发者工具.lnk").write_bytes(b"x")
    (tmp_path / "~$WRL2715.tmp").write_bytes(b"y")
    for key in ("", "sk-test"):
        monkeypatch.setattr(config, "get_api_key", lambda k=key: k)
        if key:
            # 有 Key 时若未 veto 会进模型；这里 stub 掉，只断言未搬家
            monkeypatch.setattr(
                "agent_core.intent_router.route",
                lambda *a, **k: {
                    "tool": "chat", "mode": "chat", "subdir": "",
                    "actions": [], "plan": "stub",
                },
            )
        out = IntentDispatcher(None).dispatch(
            "帮我把现在桌面上的word文档内容识别后，分类整理进各自新文档里",
            tmp_path,
        )
        assert (tmp_path / "说明.docx").is_file(), key
        assert (tmp_path / "微信开发者工具.lnk").is_file(), key
        assert (tmp_path / "~$WRL2715.tmp").is_file(), key
        assert not (tmp_path / "文档").exists() or not any(
            (tmp_path / "文档").iterdir()
        ), (key, out)


def test_dispatch_batch_content_not_word_office_select_gate(tmp_path, monkeypatch):
    """模型误选 word_office 时，不得落成「请先选中一个 .docx」；走内容分类。

    注意：句中若含「桌面」会解析到真实桌面；本测用工作区路径，不加「桌面」。
    """
    from agent_core.local_file_intent import wants_batch_content_new_docs
    from docx import Document

    u = "帮我把word文档内容识别后，分类整理进各自新文档里，只动word文档。"
    assert wants_batch_content_new_docs(u)
    doc = Document()
    doc.add_paragraph(
        "本合同由甲方与乙方签署。第一条 条款与违约责任。第二条 付款。"
        "第三条 争议解决。双方签署盖章后生效。附件为本合同组成部分。"
        "未尽事宜由甲乙双方另行协商并书面确认。"
    )
    doc.save(str(tmp_path / "a.docx"))
    # 无 Key：走规则高置信合同类，避免软标签打网
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    monkeypatch.setattr(
        "agent_core.intent_router.route",
        lambda *a, **k: {
            "tool": "word_office", "mode": "execute", "subdir": "",
            "actions": [], "plan": "stub-wrong-tool",
        },
    )
    out = IntentDispatcher(None).dispatch(u, tmp_path)
    msg = out.get("message") or ""
    assert "选中一个" not in msg
    assert out.get("tool") == "content_split"
    assert out.get("reason") == "ok"
    assert (tmp_path / "a.docx").is_file()
    assert (tmp_path / "内容分类" / "合同协议" / "a.docx").is_file()


def test_bare_hetong_is_not_keyword_rule():
    """单字「合同」不得当整理指令（宁可 miss，也不误搬）。"""
    m = match("合同")
    assert m.status != "hit" or m.rule_id != "R_KEYWORD_CONTRACT"


def test_dispatch_no_key_organizes_by_ext(tmp_path, monkeypatch):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "b.jpg").write_bytes(b"xx")
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    out = IntentDispatcher(None).dispatch("按类型整理", tmp_path)
    assert (tmp_path / "文档" / "a.pdf").is_file()
    assert (tmp_path / "图片" / "b.jpg").is_file()
    assert out.get("operations")


def test_dispatch_no_key_contract_keyword(tmp_path, monkeypatch):
    (tmp_path / "合同A.pdf").write_bytes(b"x")
    (tmp_path / "其它.pdf").write_bytes(b"y")
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    IntentDispatcher(None).dispatch("把合同归档", tmp_path)
    assert (tmp_path / "合同类" / "合同A.pdf").is_file()
    assert (tmp_path / "其它.pdf").is_file()


def test_dispatch_no_key_veto_table(tmp_path, monkeypatch):
    (tmp_path / "a.pdf").write_bytes(b"x")
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    out = IntentDispatcher(None).dispatch("把这几张表联查一下", tmp_path)
    assert (tmp_path / "a.pdf").is_file()
    assert "API Key" in (out.get("message") or "")
    assert not out.get("operations")


def test_dispatch_no_key_consult_does_not_move(tmp_path, monkeypatch):
    (tmp_path / "a.pdf").write_bytes(b"x")
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    out = IntentDispatcher(None).dispatch("怎么整理这些文件", tmp_path)
    assert (tmp_path / "a.pdf").is_file()
    assert "问方案" in (out.get("message") or "")
    assert not out.get("operations")


def test_dispatch_a_level_skips_planner_and_review_ai(tmp_path, monkeypatch):
    """有 Key 也不能为 A 级整理去调规划/复核模型。"""
    (tmp_path / "a.pdf").write_bytes(b"x")
    monkeypatch.setattr(config, "get_api_key", lambda: "secret-key")

    def boom_plan(*_a, **_k):
        raise AssertionError("A 级整理不得调用 parse_and_plan")

    def boom_review(*_a, **_k):
        raise AssertionError("A 级整理不得调用 review_execution")

    monkeypatch.setattr("agent_core.task_planner.parse_and_plan", boom_plan)
    monkeypatch.setattr("agent_core.reviewer.review_execution", boom_review)
    monkeypatch.setattr("ai.client.ask_llm", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("A 级整理不得 ask_llm")))
    out = IntentDispatcher(None).dispatch("按类型整理", tmp_path)
    assert (tmp_path / "文档" / "a.pdf").is_file()
    assert out.get("operations")


def test_rule_engine_does_not_overwrite(tmp_path):
    folder = tmp_path / "文档"
    folder.mkdir()
    existing = folder / "a.pdf"
    existing.write_bytes(b"old")
    src = tmp_path / "a.pdf"
    src.write_bytes(b"new")
    engine = FileRuleEngine(tmp_path)
    engine.add_rule(ExtensionRule({".pdf": "文档"}))
    result = engine.apply_rules([src])
    assert existing.read_bytes() == b"old"
    moved_dst = Path(result["moved"][0][1])
    assert moved_dst.name == "a_1.pdf"
    assert moved_dst.read_bytes() == b"new"
