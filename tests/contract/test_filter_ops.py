# -*- coding: utf-8 -*-
"""P3-B：算子词汇收编——filter_ops / cmp_aliases / query_ops 与代码一致。"""
from __future__ import annotations

from pathlib import Path

from pipeline.synth_contract.authority_scan import (
    _cmp_aliases_hits,
    _filter_ops_hits,
    _module_dict_str_keys,
    _module_str_dict,
    _prompt_enum_hits,
    _query_ops_hits,
)
from pipeline.synth_contract.registry import load_contract

ROOT = Path(__file__).resolve().parent.parent.parent


def test_filter_ops_hits_clean():
    """当前代码与契约 filter_ops 一致，扫描应无告警。"""
    assert _filter_ops_hits() == []


def test_cmp_aliases_hits_clean():
    """executor._CMP_ALIASES 与契约 cmp_aliases 一致。"""
    assert _cmp_aliases_hits() == []


def test_query_ops_hits_clean():
    """qe.py::_cmp_cell 用的算子 token 都在契约 query_ops 里。"""
    assert _query_ops_hits() == []


def test_prompt_enum_hits_clean():
    """prompt 里的枚举清单（fail_locus/tool/header_completeness）都覆盖契约枚举。"""
    assert _prompt_enum_hits() == []


def test_semantic_ops_matches_contract():
    """semantic_match._OPS 必须原样等于契约 filter_ops.aliases（权威在契约）。"""
    from pipeline.stage3_processor.semantic_match import _OPS

    aliases = load_contract().data["filter_ops"]["aliases"]
    assert _OPS == {str(k): str(v) for k, v in aliases.items()}


def test_table_engine_keys_cover_canonical():
    """table_engine._OPS 的 key 集合必须等于 aliases 的值集合（规范化算子全集）。"""
    te = _module_dict_str_keys(
        ROOT / "pipeline" / "stage3_processor" / "table_engine.py", "_OPS")
    canonical = {str(v) for v in load_contract().data["filter_ops"]["aliases"].values()}
    assert te == canonical


def test_module_str_dict_extracts():
    """AST 抽取助手能正确读出 semantic_match._OPS 字面量。"""
    sm = _module_str_dict(
        ROOT / "pipeline" / "stage3_processor" / "semantic_match.py", "_OPS")
    assert isinstance(sm, dict)
    assert sm.get("==") == "=="
    assert sm.get("包含") == "contains"


def test_unit_suffixes_present():
    """单位后缀清单非空，且语义模块的 _UNIT_RE 覆盖契约里的每个后缀。"""
    from pipeline.stage3_processor.semantic_match import _UNIT_RE

    units = load_contract().data["filter_ops"]["unit_suffixes"]
    assert isinstance(units, list) and units
    for u in units:
        assert str(u) in _UNIT_RE.pattern


def test_apply_filter_contains_any():
    """补齐：apply_filter 现在真的实现 contains_any（此前落到 else 永不匹配）。"""
    from pipeline.synth_contract.executor import apply_filter

    cv = load_contract()
    working = {"name": "t", "columns": ["设备"], "rows": [
        {"设备": "A型机"}, {"设备": "B型机"}, {"设备": "C型机"}]}
    notes = []
    err = apply_filter(
        working,
        [{"column": "设备", "op": "contains_any", "values": ["A型", "C型"]}],
        notes, cv=cv)
    assert err is None
    assert [r["设备"] for r in working["rows"]] == ["A型机", "C型机"]


def test_apply_filter_contains_all():
    from pipeline.synth_contract.executor import apply_filter

    cv = load_contract()
    working = {"name": "t", "columns": ["备注"], "rows": [
        {"备注": "A型B型"}, {"备注": "A型C型"}, {"备注": "B型C型"}]}
    notes = []
    err = apply_filter(
        working,
        [{"column": "备注", "op": "contains_all", "values": ["A型", "B型"]}],
        notes, cv=cv)
    assert err is None
    assert [r["备注"] for r in working["rows"]] == ["A型B型"]
