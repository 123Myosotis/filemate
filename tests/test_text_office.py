# -*- coding: utf-8 -*-
"""文本文件模块：编码识别、替换、行操作、JSON 安全写。"""
from __future__ import annotations

import json

import pytest

from pipeline.text_office import TextModule


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    """备份与证据写临时目录，不污染项目 storage。"""
    import pipeline.office_core.evidence as evidence
    import pipeline.office_core.txn as txn

    monkeypatch.setattr(txn, "ROOT", tmp_path / "root")
    monkeypatch.setattr(evidence, "ROOT", tmp_path / "root")


@pytest.fixture()
def txt(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("甲方是A型机\nDEBUG 日志一行\n乙方是B型机\n", encoding="utf-8")
    return p


def test_inspect(txt):
    r = TextModule.handle({"user_input": "检查编码和行数",
                           "selected_files": [str(txt)]})
    assert r["status"] == "done"
    assert "编码" in r["message"]
    assert "3 行" in r["message"]


def test_find_replace(txt):
    r = TextModule.handle({"user_input": "把 A型机 替换成 C型机",
                           "selected_files": [str(txt)]})
    assert r["status"] == "done"
    assert r["applied"] == 1
    text = txt.read_text(encoding="utf-8")
    assert "A型机" not in text
    assert "C型机" in text


def test_replace_not_found_keeps_file(txt):
    before = txt.read_text(encoding="utf-8")
    r = TextModule.handle({"user_input": "把 不存在 替换成 X",
                           "selected_files": [str(txt)]})
    assert r["status"] == "failed"
    assert txt.read_text(encoding="utf-8") == before


def test_delete_line_needs_confirmation(txt):
    r = TextModule.handle({"user_input": "删除包含 DEBUG 的行",
                           "selected_files": [str(txt)]})
    assert r["status"] == "needs_confirmation"
    assert "DEBUG" in txt.read_text(encoding="utf-8")

    r = TextModule.handle({"user_input": "删除包含 DEBUG 的行",
                           "selected_files": [str(txt)], "confirmed": True})
    assert r["status"] == "done"
    assert "DEBUG" not in txt.read_text(encoding="utf-8")


def test_insert_line(txt):
    r = TextModule.handle({"user_input": "在第 1 行后插入 新增说明",
                           "selected_files": [str(txt)]})
    assert r["status"] == "done"
    lines = txt.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "新增说明"


def test_json_format_invalid_not_written(tmp_path):
    p = tmp_path / "bad.json"
    bad = '{"a": 1,}'
    p.write_text(bad, encoding="utf-8")
    r = TextModule.handle({"user_input": "格式化 JSON", "selected_files": [str(p)]})
    assert r["status"] == "failed"
    assert p.read_text(encoding="utf-8") == bad


def test_json_format_valid(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text('{"b":2,"a":1}', encoding="utf-8")
    r = TextModule.handle({"user_input": "格式化 JSON", "selected_files": [str(p)]})
    assert r["status"] == "done"
    text = p.read_text(encoding="utf-8")
    assert "\n" in text
    assert json.loads(text) == {"a": 1, "b": 2}


def test_json_validate_reports_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"a": }', encoding="utf-8")
    r = TextModule.handle({"user_input": "校验 JSON 是否合法",
                           "selected_files": [str(p)]})
    assert r["status"] == "failed"
    assert "JSON" in r["message"]


def test_gbk_encoding_preserved(tmp_path):
    p = tmp_path / "gbk.txt"
    p.write_bytes("旧名".encode("gb18030"))
    r = TextModule.handle({"user_input": "把 旧名 替换成 新名",
                           "selected_files": [str(p)]})
    assert r["status"] == "done"
    assert p.read_bytes().decode("gb18030") == "新名"


def test_multiple_candidates_asks(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("x", encoding="utf-8")
    b.write_text("y", encoding="utf-8")
    r = TextModule.handle({"user_input": "把 x 替换成 z",
                           "selected_files": [str(a), str(b)]})
    assert r["status"] == "needs_confirmation"
    assert r["reason"] == "multiple_candidates"


def test_no_target(tmp_path):
    p = tmp_path / "a.xlsx"
    p.write_bytes(b"not a text file")
    r = TextModule.handle({"user_input": "检查编码", "selected_files": [str(p)]})
    assert r["status"] == "failed"
    assert r["reason"] == "no_target"


def test_dispatcher_routes_to_text_office(txt, monkeypatch):
    """接入层冒烟：模型判定 text_office 后，dispatcher 能把结果原样透出。"""
    import config
    from agent_core import intent_router
    from engine.dispatcher import IntentDispatcher

    monkeypatch.setattr(config, "get_api_key", lambda: "k")
    monkeypatch.setattr(intent_router, "route", lambda *a, **k: {
        "tool": "text_office", "mode": "execute", "subdir": "",
        "actions": [], "plan": "",
    })

    out = IntentDispatcher(None).dispatch(
        "把 A型机 替换成 C型机", txt.parent, file_paths=[str(txt)])

    assert out["tool"] == "text_office"
    assert out["handler"] == "TextOffice"
    assert out["status"] == "done"
    assert "C型机" in txt.read_text(encoding="utf-8")

