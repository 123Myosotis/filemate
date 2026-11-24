# -*- coding: utf-8 -*-
"""P2：用户纠正才写入；指纹变了不套用；不碰文件。"""
from __future__ import annotations

import config
import agent_core.capabilities as cap
from db import choice_memory
from engine.dispatcher import IntentDispatcher

SCOPE = "/tmp/filemate-p2-choice.xlsx"


def _parsed(hash_="h1"):
    return {
        "table": {"name": "sheet1", "columns": ["1", "2"],
                  "rows": [{"1": "a", "2": "b"}, {"1": "c", "2": "d"}]},
        "filename": "filemate-p2-choice.xlsx",
        "path": SCOPE,
        "mtime": 0,
        "hash": hash_,
    }


def setup_function(_fn):
    choice_memory.forget_scope("table_wash", SCOPE)
    cap.clear_pending_wash()


def teardown_function(_fn):
    choice_memory.forget_scope("table_wash", SCOPE)
    cap.clear_pending_wash()


def _stub_finish(monkeypatch):
    monkeypatch.setattr(cap.cache_db, "init_cache_tables", lambda: None)
    monkeypatch.setattr(cap.cache_db, "get_table_profile", lambda *a: {"x": 1})
    monkeypatch.setattr(cap.db_writer, "write_document", lambda u: None)
    monkeypatch.setattr(cap.normalizer, "normalize", lambda *a, **k: {
        "document_info": {"failed_chunks": 0, "type": "table"},
        "structured_content": {"tables": [], "global_entities": [], "facts": []},
    })
    orig = config.get_param

    def _param(k, d=None):
        if k == "table_normalizer.enabled":
            return True
        return orig(k, d)

    monkeypatch.setattr(config, "get_param", _param)


def test_record_lookup_and_hash_mismatch():
    cid = choice_memory.record(
        "table_wash", SCOPE, "sheet1",
        {"action": "skip", "file_hash": "h1", "columns": []},
    )
    assert cid
    hit = choice_memory.lookup("table_wash", SCOPE, "sheet1", file_hash="h1")
    assert hit and (hit["value"] or {}).get("action") == "skip"
    assert choice_memory.lookup("table_wash", SCOPE, "sheet1", file_hash="h2") is None


def test_unknown_kind_not_written():
    assert choice_memory.record("word_delete", SCOPE, "x", {"action": "yes"}) == ""
    assert choice_memory.lookup("word_delete", SCOPE, "x") is None


def test_header_correction_reused(monkeypatch):
    _stub_finish(monkeypatch)
    monkeypatch.setattr(
        cap.table_normalizer, "normalize",
        lambda t: {"recognized": False, "columns": list(t.get("columns") or []),
                   "rows": t.get("rows"), "name": t.get("name")})
    parsed = _parsed()
    monkeypatch.setattr(
        "pipeline.stage1_parser.file_parser.parse_file", lambda p: dict(parsed))
    cap.write_pending_wash(SCOPE, "sheet1", "h1")
    IntentDispatcher(None)._handle_pending_wash("表头是 甲、乙", {
        "file": SCOPE, "table": "sheet1", "hash": "h1",
    })
    ctx = {"data": {}, "operations": [], "api_key": "", "user_input": "整理"}
    cap._finish_table(ctx, _parsed())
    assert not ctx["data"].get("table_needs_user")
    table = ctx["data"]["tables"]["sheet1"]
    assert table["columns"] == ["甲", "乙"]


def test_skip_correction_reused(monkeypatch):
    _stub_finish(monkeypatch)
    monkeypatch.setattr(cap.table_normalizer, "normalize",
                        lambda t: {"recognized": False})
    parsed = _parsed()
    monkeypatch.setattr(
        "pipeline.stage1_parser.file_parser.parse_file", lambda p: dict(parsed))
    cap.write_pending_wash(SCOPE, "sheet1", "h1")
    IntentDispatcher(None)._handle_pending_wash("不洗", {
        "file": SCOPE, "table": "sheet1",
    })
    ctx = {"data": {}, "operations": [], "api_key": "", "user_input": "整理"}
    cap._finish_table(ctx, _parsed())
    assert not ctx["data"].get("table_needs_user")
    assert ctx["data"]["tables"]["sheet1"]["columns"] == ["1", "2"]


def test_hash_change_asks_again(monkeypatch):
    _stub_finish(monkeypatch)
    monkeypatch.setattr(cap.table_normalizer, "normalize",
                        lambda t: {"recognized": False})
    choice_memory.record(
        "table_wash", SCOPE, "sheet1",
        {"action": "skip", "file_hash": "h1", "columns": []},
    )
    ctx = {"data": {}, "operations": [], "api_key": "", "user_input": "整理"}
    cap._finish_table(ctx, _parsed("h-new"))
    assert ctx["data"].get("table_needs_user")
