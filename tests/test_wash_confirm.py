# -*- coding: utf-8 -*-
"""二次清洗"先问 → 表头/恢复/不用"路径回归（mock 重依赖，不写库、不读文件）。"""
from __future__ import annotations

import pytest

import agent_core.capabilities as cap
from engine.dispatcher import IntentDispatcher


def _parsed():
    return {
        "table": {"name": "t", "columns": ["1", "2"],
                  "rows": [{"1": "a", "2": "b"}, {"1": "c", "2": "d"}]},
        "filename": "t.xlsx", "path": "/tmp/t.xlsx", "mtime": 0, "hash": "h",
    }


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(cap.cache_db, "init_cache_tables", lambda: None)
    monkeypatch.setattr(cap.cache_db, "get_table_profile", lambda *a: {"x": 1})
    monkeypatch.setattr(cap.db_writer, "write_document", lambda u: None)
    monkeypatch.setattr(cap.normalizer, "normalize", lambda *a, **k: {
        "document_info": {"failed_chunks": 0, "type": "table"},
        "structured_content": {"tables": [], "global_entities": [], "facts": []},
    })
    monkeypatch.setattr("pipeline.stage1_parser.file_parser.parse_file",
                        lambda p: _parsed())
    cap.clear_pending_wash()
    yield
    cap.clear_pending_wash()


def _ask(monkeypatch):
    monkeypatch.setattr(cap.table_normalizer, "normalize",
                        lambda t: {"recognized": False})
    ctx = {"data": {}, "operations": [], "api_key": "", "user_input": "整理"}
    cap._finish_table(ctx, _parsed())
    return ctx, cap.read_pending_wash()


def test_unrecognized_with_data_asks(monkeypatch):
    ctx, pending = _ask(monkeypatch)
    assert ctx["data"].get("table_needs_user")
    assert pending is not None
    assert pending.get("table") == "t"


def test_empty_table_not_asked(monkeypatch):
    monkeypatch.setattr(cap.table_normalizer, "normalize",
                        lambda t: {"recognized": False})
    parsed = _parsed()
    parsed["table"]["rows"] = []
    ctx = {"data": {}, "operations": [], "api_key": "", "user_input": "整理"}
    cap._finish_table(ctx, parsed)
    assert not ctx["data"].get("table_needs_user")
    assert cap.read_pending_wash() is None


def test_assign_columns_keeps_all_rows():
    base = {"name": "t", "columns": ["列1", "列2"],
            "rows": [{"列1": "a", "列2": "b"}]}
    out = cap._assign_columns(base, ["甲", "乙"])
    assert out["columns"] == ["甲", "乙"]
    assert out["rows"] == [{"甲": "a", "乙": "b"}]
    assert out["recognized"] is True


def test_header_reply_writes(monkeypatch):
    _, pending = _ask(monkeypatch)
    monkeypatch.setattr(cap.table_normalizer, "normalize", lambda t: {
        "name": "t", "columns": ["列1", "列2"],
        "rows": [{"列1": "a", "列2": "b"}], "recognized": False})
    calls = []
    monkeypatch.setattr(cap, "_finish_table", lambda c, p: calls.append(p))
    out = IntentDispatcher(None)._handle_pending_wash("表头是 甲、乙", pending)
    assert "已按你给的表头更新" in out["message"]
    assert calls and calls[0]["table"]["columns"] == ["甲", "乙"]
    assert cap.read_pending_wash() is None


def test_recover_reply_writes(monkeypatch):
    _, pending = _ask(monkeypatch)
    monkeypatch.setattr(cap.config, "get_api_key", lambda: "k")
    monkeypatch.setattr(cap.table_recover, "recover", lambda *a, **k: {
        "status": "recovered",
        "table": {"name": "t", "columns": ["甲"],
                  "rows": [{"甲": 1}], "recognized": True},
    })
    calls = []
    monkeypatch.setattr(cap, "_finish_table", lambda c, p: calls.append(p))
    out = IntentDispatcher(None)._handle_pending_wash("让模型恢复", pending)
    assert "模型已恢复表头" in out["message"]
    assert calls
    assert cap.read_pending_wash() is None


def test_no_reply_clears(monkeypatch):
    _, pending = _ask(monkeypatch)
    out = IntentDispatcher(None)._handle_pending_wash("不用", pending)
    assert "已保留原表" in out["message"]
    assert cap.read_pending_wash() is None


def test_rows_shrunk_rejected(monkeypatch):
    monkeypatch.setattr(cap.table_normalizer, "normalize",
                        lambda t: {"recognized": False})
    monkeypatch.setattr(cap.table_normalizer, "normalize_second_pass", lambda t: {
        "name": "t", "columns": ["甲"],
        "rows": [{"甲": 1}, {"甲": 2}], "recognized": True})
    parsed = _parsed()
    parsed["table"]["rows"] = [{"1": str(i), "2": str(i)} for i in range(10)]
    ctx = {"data": {"wash_confirm": {"t": True}}, "operations": [],
           "api_key": "", "user_input": "洗"}
    cap._finish_table(ctx, parsed)
    reasons = [r.get("reason") for r in (ctx["data"].get("wash_rejected") or [])]
    assert "rows_shrunk" in reasons


def test_headerless_wash_rejected(monkeypatch):
    monkeypatch.setattr(cap.table_normalizer, "normalize",
                        lambda t: {"recognized": False, "shape": "headerless"})
    monkeypatch.setattr(cap.table_normalizer, "normalize_second_pass", lambda t: {
        "name": "t", "columns": ["甲"], "rows": [{"甲": 1}], "recognized": True})
    ctx = {"data": {"wash_confirm": {"t": True}}, "operations": [],
           "api_key": "", "user_input": "洗"}
    cap._finish_table(ctx, _parsed())
    reasons = [r.get("reason") for r in (ctx["data"].get("wash_rejected") or [])]
    assert "still_unrecognized" in reasons
