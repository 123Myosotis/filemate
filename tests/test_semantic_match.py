# -*- coding: utf-8 -*-
"""P1：semantic_match（列名对齐 / 条件解析）契约回归。"""

from __future__ import annotations

from unittest.mock import patch

from pipeline.stage3_processor.semantic_match import (
    align_columns,
    match_column,
    normalize_text,
    parse_condition,
    parse_conditions,
    strip_unit,
)

import pipeline.stage3_processor.semantic_match as sm


def test_normalize_text_width_and_case():
    assert normalize_text("  ＡbＣ　") == "abc"  # 全角→半角 + casefold + 去空白
    assert normalize_text(None) == ""
    assert normalize_text(123) == "123"


def test_match_column_exact():
    col, score = match_column(["设备", "负责人"], "设备")
    assert col == "设备"
    assert score == 1.0


def test_match_column_contains():
    col, score = match_column(["设备型号", "负责人"], "设备")
    assert col == "设备型号"
    assert score >= 0.5


def test_match_column_normalized():
    # 全角/大小写差异也能命中
    col, _ = match_column(["Name", "负责人"], "ｎａｍｅ")
    assert col == "Name"


def test_match_column_miss():
    col, score = match_column(["设备", "负责人"], "完全不存在的列")
    assert col is None
    assert score == 0.0


def test_parse_condition_equals():
    p = parse_condition("设备=A", ["设备", "负责人"])
    assert p == {"column": "设备", "op": "==", "value": "A", "source": "设备=A"}


def test_parse_condition_contains():
    p = parse_condition("设备 包含 A", ["设备"])
    assert p["op"] == "contains"
    assert p["value"] == "A"


def test_parse_condition_strips_unit():
    p = parse_condition("数量>=3台", ["数量"])
    assert p == {"column": "数量", "op": ">=", "value": "3", "source": "数量>=3台"}


def test_parse_condition_unknown_column():
    assert parse_condition("未知列=1", ["设备"]) is None


def test_strip_unit_various():
    assert strip_unit("2024年") == "2024"
    assert strip_unit("3台") == "3"
    assert strip_unit("A型号") == "A型号"
    assert strip_unit("") == ""


def test_parse_conditions_local_only_no_llm():
    parsed, notes = parse_conditions(
        ["设备=A", "不存在列=1"], ["设备"], api_key="", allow_llm=False)
    assert [p["column"] for p in parsed] == ["设备"]
    assert notes == ["筛选条件「不存在列=1」的列在表中不存在，已跳过"]


def test_parse_conditions_empty():
    parsed, notes = parse_conditions([], ["设备"])
    assert parsed == []
    assert notes == []


# ------------------------------------------------------------------
# 列名对齐器（LLM 严格契约）
# ------------------------------------------------------------------

def test_align_columns_no_api_key():
    out = align_columns(["变电站名称"], ["站点"], api_key="")
    assert out == [{"query": "站点", "column": None, "confidence": 0.0}]


def test_align_columns_valid():
    with patch(
        "pipeline.stage3_processor.semantic_match._chat_json",
        return_value={"matches": [
            {"query": "站点", "column": "变电站名称", "confidence": 0.9},
        ]},
    ):
        out = align_columns(["变电站名称", "设备"], ["站点"], api_key="k")
    assert out == [{"query": "站点", "column": "变电站名称", "confidence": 0.9}]


def test_align_columns_low_confidence_becomes_empty():
    with patch(
        "pipeline.stage3_processor.semantic_match._chat_json",
        return_value={"matches": [
            {"query": "站点", "column": "变电站名称", "confidence": 0.3},
        ]},
    ):
        out = align_columns(["变电站名称"], ["站点"], api_key="k")
    assert out == [{"query": "站点", "column": None, "confidence": 0.0}]


def test_align_columns_rejects_invented_column():
    with patch(
        "pipeline.stage3_processor.semantic_match._chat_json",
        return_value={"matches": [
            {"query": "站点", "column": "编造的列", "confidence": 0.99},
        ]},
    ):
        out = align_columns(["变电站名称"], ["站点"], api_key="k")
    assert out == [{"query": "站点", "column": None, "confidence": 0.0}]


def test_align_columns_missing_entries_are_empty():
    with patch(
        "pipeline.stage3_processor.semantic_match._chat_json",
        return_value={"matches": []},
    ):
        out = align_columns(["变电站名称"], ["站点"], api_key="k")
    assert out == [{"query": "站点", "column": None, "confidence": 0.0}]


def test_parse_conditions_uses_align_columns():
    with patch(
        "pipeline.stage3_processor.semantic_match.align_columns",
        return_value=[{"query": "站点", "column": "变电站名称", "confidence": 0.9}],
    ):
        parsed, notes = parse_conditions(
            ["站点=A"], ["变电站名称", "设备"], api_key="k", allow_llm=True)
    assert parsed == [{"column": "变电站名称", "op": "==", "value": "A", "source": "站点=A"}]
    assert notes == []


def test_vector_match_uses_embedding(monkeypatch):
    sm._EMB_CACHE.clear()
    vecs = {"站点": [1.0, 0.0], "变电站名称": [0.9, 0.1], "负责人": [0.0, 1.0]}

    def fake_embed(texts):
        return [vecs.get(t, [0.0, 0.0]) for t in texts]

    monkeypatch.setattr(sm, "_embed_texts", fake_embed)
    col, score = sm.vector_match(["变电站名称", "负责人"], "站点")
    assert col == "变电站名称"
    assert score > 0.9


def test_vector_match_unavailable(monkeypatch):
    sm._EMB_CACHE.clear()
    monkeypatch.setattr(sm, "_embed_texts", lambda texts: None)
    assert sm.vector_match(["变电站名称"], "站点") == (None, 0.0)


def test_parse_conditions_uses_vector(monkeypatch):
    sm._EMB_CACHE.clear()
    monkeypatch.setattr(sm, "vector_match",
                        lambda columns, label, **k: ("变电站名称", 0.92))
    parsed, notes = parse_conditions(
        ["站点=A"], ["变电站名称", "设备"], api_key="", allow_llm=False)
    assert parsed == [{"column": "变电站名称", "op": "==", "value": "A", "source": "站点=A"}]
    assert notes == []


def test_embed_texts_off_when_dense_off(monkeypatch):
    from pipeline.block_cross_table.memory_rag import dense_provider as dp
    monkeypatch.setattr(
        dp, "resolve_dense_provider",
        lambda **k: type("R", (), {"dense_provider": "off"})())
    assert sm._embed_texts(["设备"]) is None
