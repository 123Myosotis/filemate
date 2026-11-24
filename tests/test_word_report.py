# -*- coding: utf-8 -*-
"""Word D/E Report：严闸门——A/B/C 与只读不出；D/E 写成功才出。"""
from __future__ import annotations

from pathlib import Path

import config
import pytest
from docx import Document

from pipeline.word_office import WordModule
from pipeline.word_office.module import _decide_tier
from pipeline.word_office.report import should_emit_report


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(config, "get_api_key", lambda: "k")


@pytest.fixture()
def docx(tmp_path):
    p = tmp_path / "t.docx"
    d = Document()
    d.add_paragraph("甲方是旧名。")
    d.save(str(p))
    return p


def test_decide_tier_strict_d():
    assert _decide_tier("生成标书并填充技术部分", {}, {}) == "D"
    assert _decide_tier("起草合同初稿", {}, {}) == "D"
    # 单字/口语不升 D
    assert _decide_tier("这份合同里把甲方改成乙方", {}, {}) == "A"
    assert _decide_tier("润色这份合同", {}, {}) == "B"
    assert _decide_tier("检查结构", {}, {}) == "A"


def test_decide_tier_strict_e():
    assert _decide_tier("按章节分块处理", {}, {}) == "E"
    assert _decide_tier("大文档需要分块", {}, {}) == "A"  # route 提示语不升 E


def test_should_emit_rejects_abc():
    plan = {"operations": [{"op": "find_replace", "find": "a", "replace": "b"}]}
    result = {"status": "done", "applied": 1, "txn": {"phase": "committed"}}
    for tier in ("A", "B", "C"):
        assert should_emit_report(
            tier=tier, result=result, plan=plan, request={}) is False


def test_should_emit_rejects_readonly_even_on_d():
    plan = {"operations": [{"op": "list_fields"}]}
    result = {"status": "done", "applied": 0, "txn": {"phase": "dry"}}
    assert should_emit_report(
        tier="D", result=result, plan=plan, request={}) is False


def test_should_emit_e_needs_batch_flag():
    plan = {"operations": [{"op": "find_replace", "find": "a", "replace": "b"}]}
    result = {"status": "done", "applied": 1, "txn": {"phase": "committed"}}
    assert should_emit_report(
        tier="E", result=result, plan=plan, request={}) is False
    assert should_emit_report(
        tier="E", result=result, plan=plan,
        request={"emit_report": True}) is True


def test_should_emit_rejects_zero_applied_even_if_committed():
    """空跑 committed 也不出——防刷报告。"""
    plan = {"operations": [{"op": "find_replace", "find": "a", "replace": "b"}]}
    result = {"status": "done", "applied": 0, "txn": {"phase": "committed"}}
    assert should_emit_report(
        tier="D", result=result, plan=plan, request={}) is False


def test_a_replace_no_report(docx):
    before = list(docx.parent.glob("*_编辑报告_*.docx"))
    r = WordModule.handle({
        "user_input": "把旧名改成新名",
        "selected_files": [str(docx)],
    })
    assert r["status"] == "done"
    assert not r.get("report_path")
    after = list(docx.parent.glob("*_编辑报告_*.docx"))
    assert after == before


def test_d_write_emits_report(docx):
    r = WordModule.handle({
        "user_input": "把旧名改成新名",
        "selected_files": [str(docx)],
        "tier_override": "D",  # 模拟已严判为 D；口令侧另有 decision_rules
    })
    assert r["status"] == "done"
    path = r.get("report_path")
    assert path
    assert Path(path).is_file()
    assert "编辑报告_D_" in Path(path).name
    assert "已生成 D/E 编辑报告" in (r.get("message") or "")


def test_d_inspect_no_report(docx):
    r = WordModule.handle({
        "user_input": "检查结构",
        "selected_files": [str(docx)],
        "tier_override": "D",
    })
    assert r["status"] == "done"
    assert not r.get("report_path")
    assert r.get("report_skipped") is True
    assert list(docx.parent.glob("*_编辑报告_*.docx")) == []


def test_dry_run_no_report(docx):
    r = WordModule.handle({
        "user_input": "把旧名改成新名",
        "selected_files": [str(docx)],
        "tier_override": "D",
        "dry_run": True,
    })
    assert r.get("dry_run") or r["status"] == "done"
    assert not r.get("report_path")
    assert list(docx.parent.glob("*_编辑报告_*.docx")) == []
