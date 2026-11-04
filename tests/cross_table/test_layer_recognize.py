# -*- coding: utf-8 -*-
"""A' 多维结构识别：本地投影、包含一致性、复核缩范围、无草案不编造。"""

from __future__ import annotations

import inspect
import json

import pytest

from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.layer_recognize import (
    assert_review_within_capacity,
    attach_texts_for_model,
    build_review_payload,
    compute_containment_facts,
    portrait_metrics_absent,
    project_subtable_boundaries,
    project_upstream_rectangles,
    run_layer_recognize_block,
    shrink_review_locator_ids,
    validate_layer_proposal,
)


def _block_with_merge():
    return {
        "block_id": 0,
        "layout": "matrix",
        "parent_table_ids": ["t0"],
        "context": {
            "merges": [
                {"r0": 0, "c0": 0, "r1": 2, "c1": 4},
                {"r0": 1, "c0": 0, "r1": 2, "c1": 2},
                {"r0": 1, "c0": 2, "r1": 2, "c1": 4},
            ]
        },
    }


def _draft_group():
    return {
        "block_id": 0,
        "subtable_ids": [0],
        "fields": [
            {
                "field_id": "f0",
                "role": "group",
                "subtable_id": 0,
                "segments": [
                    {"group_index": 0, "col_range": [0, 2], "value_rows": [1]},
                    {"group_index": 1, "col_range": [2, 4], "value_rows": [1]},
                ],
            },
            {
                "field_id": "f1",
                "role": "list",
                "subtable_id": 0,
                "segments": [
                    {"group_index": 0, "col_range": [0, 2], "value_rows": [2, 3]},
                ],
            },
        ],
    }


def _map_outer_inner(rects):
    return {
        r["rect_id"]: ("L0" if r["rect_id"] == "merge:0" else "L1") for r in rects
    }


def test_project_does_not_require_grid_scan():
    rects = project_upstream_rectangles(_block_with_merge(), _draft_group())
    kinds = {r["kind"] for r in rects}
    assert "merge" in kinds
    assert "group_label" in kinds
    assert all("locator" in r and "back_ref" in r for r in rects)


def test_containment_outer_before_inner_in_order():
    rects = project_upstream_rectangles(_block_with_merge(), _draft_group())
    facts = compute_containment_facts(rects)
    assert any(f["relation"] == "contains" for f in facts)
    bad = validate_layer_proposal(
        rects,
        facts,
        {
            "rectangle_layer_map": {r["rect_id"]: "L0" for r in rects},
            "layer_order": ["L0"],
            "group_label_decls": [{"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}],
            "dimension_membership": [],
            "complete_header": True,
        },
        _draft_group(),
        {},
    )
    assert bad["ok"] is False
    assert any("containment_order_conflict" in x for x in bad["reasons"])

    good = {
        "rectangle_layer_map": _map_outer_inner(rects),
        "layer_order": ["L0", "L1"],
        "group_label_decls": [{"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}],
        "dimension_membership": [],
        "complete_header": True,
    }
    out = validate_layer_proposal(rects, facts, good, _draft_group(), {})
    assert out["ok"] is True
    assert out["rectangle_layer_map"]["merge:1"] == out["rectangle_layer_map"]["merge:2"] == "L1"


def test_ordinal_conflicts_with_order_list():
    rects = [
        {"rect_id": "a", "kind": "merge", "locator": {"r0": 0, "c0": 0, "r1": 2, "c1": 2}, "back_ref": {}},
        {"rect_id": "b", "kind": "merge", "locator": {"r0": 0, "c0": 0, "r1": 1, "c1": 1}, "back_ref": {}},
    ]
    facts = compute_containment_facts(rects)
    out = validate_layer_proposal(
        rects,
        facts,
        {
            "rectangle_layer_map": {"a": "L0", "b": "L1"},
            "layer_order": ["L0", "L1"],
            "layer_ordinals": {"L0": 1, "L1": 0},
            "group_label_decls": [],
            "dimension_membership": [],
        },
        {"fields": []},
        {},
    )
    assert out["ok"] is False
    assert any("ordinal_conflicts_order" in x for x in out["reasons"])


def test_shrink_review_is_subset_not_redefine():
    recognized = {
        "rectangle_layer_map": {"merge:0": "L0", "merge:1": "L1"},
        "layer_order": ["L0", "L1"],
        "layers": [
            {"layer_id": "L0", "rect_ids": ["merge:0"]},
            {"layer_id": "L1", "rect_ids": ["merge:1"]},
        ],
    }
    subset = shrink_review_locator_ids(recognized, ["merge:1"])
    assert subset == {"merge:1"}
    assert recognized["rectangle_layer_map"]["merge:0"] == "L0"
    assert recognized["layer_order"] == ["L0", "L1"]
    with pytest.raises(CrossTableError) as ei:
        shrink_review_locator_ids(recognized, ["unknown"])
    assert ei.value.code == "cross_table_proposal_invalid"


def test_attach_texts_all_cells_in_locator_not_only_corner():
    """方案 B：矩形定位内全部已载入单元格，不只左上角。"""
    rects = [
        {"rect_id": "a", "locator": {"r0": 0, "c0": 0, "r1": 1, "c1": 2}},
        {"rect_id": "b", "locator": {"r0": 1, "c0": 0, "r1": 2, "c1": 2}},
    ]
    grid = [["X", "Y"], ["P", "Q"]]
    all_t = attach_texts_for_model(rects, grid, None)
    assert [c["text"] for c in all_t["a"]] == ["X", "Y"]
    assert [c["text"] for c in all_t["b"]] == ["P", "Q"]
    sub = attach_texts_for_model(rects, grid, {"b"})
    assert list(sub.keys()) == ["b"]
    assert len(sub["b"]) == 2


def test_capacity_missing_is_contract_error():
    with pytest.raises(CrossTableError) as ei:
        assert_review_within_capacity({"x": 1}, None)
    assert ei.value.code == "cross_table_contract_invalid"


def test_capacity_over_does_not_truncate():
    texts = {"a": [{"r": 0, "c": 0, "text": "hello-world-text"}]}
    payload = build_review_payload(
        {"rectangles": [{"rect_id": "a"}]},
        {"rectangle_layer_map": {"a": "L0"}, "layer_order": ["L0"], "layers": []},
        texts,
    )
    size = len(json.dumps(payload, ensure_ascii=False))
    with pytest.raises(CrossTableError) as ei:
        assert_review_within_capacity(payload, max(1, size - 1))
    assert ei.value.message == "review input over capacity"
    assert payload["cell_texts"]["a"][0]["text"] == "hello-world-text"


def test_ordered_layout_absent_no_invented_layers():
    block = {"block_id": 1, "layout": "ordered", "parent_table_ids": ["t0"], "context": {}}
    out = run_layer_recognize_block(
        block,
        None,
        {"t0": {"grid": [["a"]]}},
        None,
        "u",
        review_input_capacity=10_000,
        chat=lambda _p: {},
    )
    assert out["status"] == "no_structure"
    assert out["layer_result"] is None
    assert out["metrics"]["upstream_structure"] == "absent"
    assert out["metrics"]["layer_count"] == 0
    assert portrait_metrics_absent(1)["upstream_structure"] == "absent"


def test_run_ok_many_to_one_and_review_pass(monkeypatch):
    block = _block_with_merge()
    draft = _draft_group()
    tables = {
        "t0": {
            "grid": [
                ["TOP", "TOP", "TOP", "TOP"],
                ["G0", "G0", "G1", "G1"],
                ["1", "2", "3", "4"],
                ["5", "6", "7", "8"],
            ]
        }
    }
    rects = project_upstream_rectangles(block, draft)
    facts = compute_containment_facts(rects)
    proposal = {
        "rectangle_layer_map": _map_outer_inner(rects),
        "layer_order": ["L0", "L1"],
        "group_label_decls": [{"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}],
        "dimension_membership": [],
        "complete_header": True,
    }
    assert validate_layer_proposal(rects, facts, proposal, draft, {}).get("ok")

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return proposal
        return {"passed": True, "reason": "ok"}

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds",
        lambda: 8,
    )
    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", review_input_capacity=100_000, chat=chat
    )
    assert out["status"] == "ok"
    assert out["rolled_back"] is False
    assert out["layer_result"]["layer_order"] == ["L0", "L1"]
    assert out["layer_result"]["subtable_boundaries"]
    m = out["layer_result"]["rect_to_layer"]
    non_outer = [rid for rid in m if rid != "merge:0"]
    assert non_outer and all(m[rid] == "L1" for rid in non_outer)
    assert out["ask_usage"]["total"] == 2
    assert out["ask_usage"]["proposal"] == 1
    assert out["ask_usage"]["review"] == 1


def test_review_fail_rolls_back_keeps_draft(monkeypatch):
    block = _block_with_merge()
    draft = _draft_group()
    tables = {
        "t0": {
            "grid": [
                ["A", "B", "C", "D"],
                ["a", "b", "c", "d"],
                ["1", "2", "3", "4"],
                ["5", "6", "7", "8"],
            ]
        }
    }
    rects = project_upstream_rectangles(block, draft)
    mapping = _map_outer_inner(rects)

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return {
                "rectangle_layer_map": mapping,
                "layer_order": ["L0", "L1"],
                "group_label_decls": [{"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}],
                "dimension_membership": [],
                "complete_header": False,
            }
        return {"passed": False, "reason": "header incomplete"}

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds",
        lambda: 8,
    )
    out = run_layer_recognize_block(block, draft, tables, {}, "u", 100_000, chat=chat)
    assert out["status"] == "review_failed"
    assert out["rolled_back"] is True
    assert out["layer_result"] is None
    assert out["layer_result_empty"] is True
    assert out["draft_retained"] is True
    assert "header_completeness=incomplete" in out["rejection_reason"]
    assert out["stage_snapshot_kept_draft"] is True


def test_group_label_not_a_layer():
    draft = {
        "subtable_ids": [0],
        "fields": [
            {
                "field_id": "g0",
                "role": "group",
                "subtable_id": 0,
                "segments": [{"col_range": [0, 2], "value_rows": [0]}],
            }
        ],
    }
    rects = project_upstream_rectangles(
        {"context": {"merges": [{"r0": 0, "c0": 0, "r1": 1, "c1": 2}]}}, draft
    )
    facts = compute_containment_facts(rects)
    mapping = {r["rect_id"]: "L0" for r in rects}
    ok = validate_layer_proposal(
        rects,
        facts,
        {
            "rectangle_layer_map": mapping,
            "layer_order": ["L0"],
            "group_label_decls": [{"field_id": "g0", "kind": "not_a_layer", "layer_ids": []}],
            "dimension_membership": [],
        },
        draft,
        {},
    )
    assert ok["ok"] is True
    bad = validate_layer_proposal(
        rects,
        facts,
        {
            "rectangle_layer_map": mapping,
            "layer_order": ["L0"],
            "group_label_decls": [{"field_id": "g0", "kind": "not_a_layer", "layer_ids": ["L0"]}],
            "dimension_membership": [],
        },
        draft,
        {},
    )
    assert bad["ok"] is False
    assert any("group_none_has_layers" in x for x in bad["reasons"])


def test_group_label_exact_one_layer():
    draft = {
        "subtable_ids": [0],
        "fields": [
            {
                "field_id": "g0",
                "role": "group",
                "subtable_id": 0,
                "segments": [{"col_range": [0, 2], "value_rows": [0]}],
            }
        ],
    }
    # 无额外 merge，只有分组标签矩形
    rects = project_upstream_rectangles({"context": {"merges": []}}, draft)
    facts = compute_containment_facts(rects)
    rid = rects[0]["rect_id"]
    out = validate_layer_proposal(
        rects,
        facts,
        {
            "rectangle_layer_map": {rid: "L0"},
            "layer_order": ["L0"],
            "group_label_decls": [{"field_id": "g0", "kind": "exact_one_layer", "layer_ids": ["L0"]}],
            "dimension_membership": [],
        },
        draft,
        {},
    )
    assert out["ok"] is True


def test_group_label_spans_multiple_layers():
    draft = {
        "subtable_ids": [0],
        "fields": [
            {
                "field_id": "g0",
                "role": "group",
                "subtable_id": 0,
                "segments": [
                    {"col_range": [0, 2], "value_rows": [0]},
                    {"col_range": [2, 4], "value_rows": [0]},
                ],
            }
        ],
    }
    rects = project_upstream_rectangles({"context": {"merges": []}}, draft)
    facts = compute_containment_facts(rects)
    assert len(rects) == 2
    out = validate_layer_proposal(
        rects,
        facts,
        {
            "rectangle_layer_map": {
                rects[0]["rect_id"]: "L0",
                rects[1]["rect_id"]: "L1",
            },
            "layer_order": ["L0", "L1"],
            "group_label_decls": [
                {"field_id": "g0", "kind": "spans_multiple_layers", "layer_ids": ["L0", "L1"]}
            ],
            "dimension_membership": [],
        },
        draft,
        {},
    )
    assert out["ok"] is True


def test_subtable_boundaries_delivered():
    draft = {
        "subtable_ids": [0, 1],
        "fields": [
            {
                "field_id": "f0",
                "role": "list",
                "subtable_id": 0,
                "segments": [{"col_range": [0, 2], "value_rows": [1, 2]}],
            },
            {
                "field_id": "f1",
                "role": "column",
                "subtable_id": 1,
                "segments": [{"col_range": [2, 3], "value_rows": [1, 2, 3]}],
            },
        ],
    }
    bounds = project_subtable_boundaries(draft)
    by = {b["subtable_id"]: b for b in bounds}
    assert set(by) == {"0", "1"}
    assert by["0"]["locator_envelope"] == {"r0": 1, "c0": 0, "r1": 3, "c1": 2}
    assert by["1"]["locator_envelope"] == {"r0": 1, "c0": 2, "r1": 4, "c1": 3}
    assert by["0"]["back_ref"]["subtable_id"] == "0"


def test_dimension_membership_required_and_output():
    draft = _draft_group()
    rects = project_upstream_rectangles(_block_with_merge(), draft)
    facts = compute_containment_facts(rects)
    mapping = _map_outer_inner(rects)
    dims = {0: ["f1"]}
    missing = validate_layer_proposal(
        rects,
        facts,
        {
            "rectangle_layer_map": mapping,
            "layer_order": ["L0", "L1"],
            "group_label_decls": [{"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}],
            "dimension_membership": [],
            "complete_header": True,
        },
        draft,
        dims,
    )
    assert missing["ok"] is False
    assert any("dim_unassigned" in x for x in missing["reasons"])

    ok = validate_layer_proposal(
        rects,
        facts,
        {
            "rectangle_layer_map": mapping,
            "layer_order": ["L0", "L1"],
            "group_label_decls": [{"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}],
            "dimension_membership": [
                {"dimension_field_id": "f1", "subtable_id": "0", "layer_ids": ["L1"]}
            ],
            "complete_header": True,
        },
        draft,
        dims,
    )
    assert ok["ok"] is True
    assert ok["dimension_membership"][0]["dimension_field_id"] == "f1"
    assert ok["subtable_boundaries"]
    assert isinstance(ok["object_nesting_facts"], list)


def test_retry_exhausted_status_conflict(monkeypatch):
    block = _block_with_merge()
    draft = _draft_group()
    tables = {"t0": {"grid": [["a", "b", "c", "d"], ["e", "f", "g", "h"], ["1", "2", "3", "4"]]}}

    def chat(_prompt: str):
        return {"rectangle_layer_map": {}, "layer_order": []}

    # ask=2：一次失败提案后剩余不足以再提案+预留复核 → 冲突（非 review_failed）
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds",
        lambda: 2,
    )
    out = run_layer_recognize_block(block, draft, tables, {}, "u", 100_000, chat=chat)
    assert out["status"] == "conflict"
    assert out["rolled_back"] is True
    assert out["layer_result"] is None
    assert "ask_budget_exhausted_proposal" in out["reason_codes"]
    assert out["ask_rounds_used"] == 1


def test_validate_does_not_read_cell_texts_for_layers():
    """本地层划分不读附件原文：validate_layer_proposal 签名无 cell_texts 参数。"""
    sig = inspect.signature(validate_layer_proposal)
    assert "cell_texts" not in sig.parameters
    src = inspect.getsource(validate_layer_proposal)
    assert "cell_texts" not in src
    assert "_cell" not in src
