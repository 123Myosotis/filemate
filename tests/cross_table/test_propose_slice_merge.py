# -*- coding: utf-8 -*-
"""v1.8.0 提案分片 / 跨片合并：T1–T15 + T13b。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import reset_trace_seq
from pipeline.block_cross_table.layer_recognize import (
    project_upstream_rectangles,
    run_layer_recognize_block,
)
from pipeline.block_cross_table.layer_recognize_slice import (
    SlicePlan,
    build_layer_summaries_for_merge,
    filter_containment_facts_for_propose,
    sort_rectangles_geometric,
    trim_merge_input_to_capacity,
)
from pipeline.block_cross_table.layer_recognize_trace import (
    KIND_MERGE_LAYERS,
    KIND_PROPOSE,
    KIND_PROPOSE_SLICE,
    list_traces_for_block,
    load_trace_file,
    reset_block_id_registry,
)


def _block_small():
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


def _draft_small():
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


def _tables_small():
    return {
        "t0": {
            "grid": [
                ["TOP", "TOP", "TOP", "TOP"],
                ["G0", "G0", "G1", "G1"],
                ["1", "2", "3", "4"],
                ["5", "6", "7", "8"],
            ]
        }
    }


def _good_proposal(block=None, draft=None, header="complete", layer_prefix="L"):
    block = block or _block_small()
    draft = draft or _draft_small()
    rects = project_upstream_rectangles(block, draft)
    mapping = {}
    for i, r in enumerate(rects):
        mapping[r["rect_id"]] = "%s%s" % (layer_prefix, 0 if i == 0 else 1)
    order = sorted(set(mapping.values()))
    return {
        "rectangle_layer_map": mapping,
        "layer_order": order,
        "group_label_decls": [
            {"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}
        ],
        "dimension_membership": [],
        "header_completeness": header,
    }


def _many_rects_block(n_merges: int = 12):
    merges = []
    for i in range(n_merges):
        merges.append({"r0": i, "c0": 0, "r1": i + 1, "c1": 4})
    return {
        "block_id": 0,
        "layout": "matrix",
        "parent_table_ids": ["t0"],
        "context": {"merges": merges},
    }


def _many_rects_draft():
    return {
        "block_id": 0,
        "subtable_ids": [0],
        "fields": [
            {
                "field_id": "f0",
                "role": "group",
                "subtable_id": 0,
                "segments": [
                    {"group_index": 0, "col_range": [0, 4], "value_rows": [0]},
                ],
            }
        ],
    }


def _many_rects_tables(n_rows: int = 14):
    grid = [["C%d" % c for c in range(4)] for _ in range(n_rows)]
    return {"t0": {"grid": grid}}


def _repo_trace_dir(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[2]
    local = root / ".pytest_trace_tmp" / tmp_path.name
    local.mkdir(parents=True, exist_ok=True)
    return local


def _patch_trace_dir(monkeypatch, tmp_path: Path) -> Path:
    reset_trace_seq()
    reset_block_id_registry()
    local = _repo_trace_dir(tmp_path)
    orig_get = config.get_param
    pattern = orig_get("cross_table.trace_filename_pattern")

    def params(key, default=None):
        if key == "cross_table.trace_dir":
            return str(local)
        if key == "cross_table.trace_filename_pattern":
            return pattern
        return orig_get(key, default)

    monkeypatch.setattr("config.get_param", params)
    return local


def _force_n_slices(monkeypatch, n_slices: int = 2):
    """测试用：按几何序切成固定片数（不依赖估长阈值）。"""

    def fake_plan(**kwargs):
        rects = sort_rectangles_geometric(kwargs["rectangles"])
        if not rects:
            return []
        n = max(1, min(int(n_slices), len(rects)))
        # evenly chunk
        size = (len(rects) + n - 1) // n
        plans = []
        for i in range(n):
            chunk = rects[i * size : (i + 1) * size]
            if chunk:
                plans.append(SlicePlan(i + 1, chunk))
        return plans

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize_slice.plan_propose_slices",
        fake_plan,
    )
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize_limits."
        "read_layer_recognize_propose_input_max_chars",
        lambda contract_data=None: 65536,
    )


def test_t1_small_table_no_slice(tmp_path, monkeypatch):
    """T1 小表不分片。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    prop = _good_proposal()

    def chat(prompt: str):
        if "layer recognition" in prompt:
            # no disjoint in prompt facts
            assert '"relation": "disjoint"' not in prompt
            return prop
        return {"passed": True, "reason": "ok"}

    out = run_layer_recognize_block(
        _block_small(),
        _draft_small(),
        _tables_small(),
        {},
        "u",
        100_000,
        chat=chat,
        run_id="t1",
        entry_seq=1,
        clock=lambda: "T1",
    )
    assert out["status"] == "ok"
    kinds = {
        x["meta"]["call_kind"]
        for x in list_traces_for_block("t1", 0, trace_dir=trace_dir)
    }
    assert KIND_PROPOSE in kinds
    assert KIND_PROPOSE_SLICE not in kinds


def test_t2_two_slices_merge_ok(tmp_path, monkeypatch):
    """T2 两片通过 + 合并 ok + overall_sanity.passed。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()
    calls = {"propose": 0, "merge": 0}

    def chat2(prompt: str):
        if "layer merge" in prompt:
            calls["merge"] += 1
            rects = project_upstream_rectangles(block, draft)
            mapping = {r["rect_id"]: "G0" for r in rects}
            return {
                "passed": True,
                "reason": "bound",
                "global_rectangle_layer_map": mapping,
                "global_layer_order": ["G0"],
                "group_label_decls": [
                    {"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}
                ],
                "dimension_membership": [],
                "header_completeness": "complete",
                "overall_sanity": {"passed": True, "reason": "structure ok"},
            }
        if "layer recognition" in prompt:
            calls["propose"] += 1
            payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
            rids = [r["rect_id"] for r in payload.get("rectangles") or []]
            mapping = {rid: "L0" for rid in rids}
            return {
                "rectangle_layer_map": mapping,
                "layer_order": ["L0"],
                "group_label_decls": [
                    {"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}
                ],
                "dimension_membership": [],
                "header_completeness": "complete",
            }
        return {"passed": True, "reason": "ok"}

    out = run_layer_recognize_block(
        block,
        draft,
        tables,
        {},
        "u",
        100_000,
        chat=chat2,
        run_id="t2",
        entry_seq=1,
        clock=lambda: "T2",
    )
    assert out["status"] == "ok"
    assert out["ask_usage"]["proposal"] >= 2
    assert out["ask_usage"]["review"] == 1
    assert calls["merge"] == 1
    found = list_traces_for_block("t2", 0, trace_dir=trace_dir)
    assert any(x["meta"]["call_kind"] == KIND_PROPOSE_SLICE for x in found)
    assert any(x["meta"]["call_kind"] == KIND_MERGE_LAYERS for x in found)
    merge_body = load_trace_file(
        [x for x in found if x["meta"]["call_kind"] == KIND_MERGE_LAYERS][0]["path"]
    )
    assert merge_body.get("merge_phase") is True


def test_t3_second_slice_fails_no_retry(tmp_path, monkeypatch):
    """T3 片2失败 → 整块冲突，不 merge，片2不重试。"""
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()
    calls = {"propose": 0, "merge": 0}

    def chat(prompt: str):
        if "layer merge" in prompt:
            calls["merge"] += 1
            return {}
        if "layer recognition" in prompt:
            calls["propose"] += 1
            payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
            rids = [r["rect_id"] for r in payload.get("rectangles") or []]
            if calls["propose"] == 1:
                return {
                    "rectangle_layer_map": {rid: "L0" for rid in rids},
                    "layer_order": ["L0"],
                    "group_label_decls": [],
                    "dimension_membership": [],
                    "header_completeness": "complete",
                }
            return {"answer": "bad"}
        return {}

    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat, run_id="t3", clock=lambda: "T3"
    )
    assert out["status"] == "conflict"
    assert "propose_slice_validate_failed" in out["reason_codes"]
    assert calls["merge"] == 0
    assert calls["propose"] == 2  # one fail, no third


def test_t4_merge_same_local_layers(tmp_path, monkeypatch):
    """T4 模型将两片同名层并成全局一层。"""
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()

    def chat(prompt: str):
        if "layer merge" in prompt:
            rects = project_upstream_rectangles(block, draft)
            return {
                "passed": True,
                "reason": "merge same",
                "global_rectangle_layer_map": {r["rect_id"]: "GLOBAL" for r in rects},
                "global_layer_order": ["GLOBAL"],
                "group_label_decls": [],
                "dimension_membership": [],
                "header_completeness": "complete",
                "overall_sanity": {"passed": True, "reason": "ok"},
            }
        payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
        rids = [r["rect_id"] for r in payload.get("rectangles") or []]
        return {
            "rectangle_layer_map": {rid: "L1" for rid in rids},
            "layer_order": ["L1"],
            "group_label_decls": [],
            "dimension_membership": [],
            "header_completeness": "complete",
        }

    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat, run_id="t4", clock=lambda: "T4"
    )
    assert out["status"] == "ok"
    assert out["layer_result"]["layer_order"] == ["GLOBAL"]


def test_t5_merge_keeps_distinct_layers(tmp_path, monkeypatch):
    """T5 同名局部层不自动合并。"""
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()
    state = {"n": 0}

    def chat(prompt: str):
        if "layer merge" in prompt:
            rects = project_upstream_rectangles(block, draft)
            mid = len(rects) // 2
            mapping = {}
            for i, r in enumerate(rects):
                mapping[r["rect_id"]] = "GA" if i < mid else "GB"
            return {
                "passed": True,
                "reason": "distinct",
                "global_rectangle_layer_map": mapping,
                "global_layer_order": ["GA", "GB"],
                "group_label_decls": [],
                "dimension_membership": [],
                "header_completeness": "complete",
                "overall_sanity": {"passed": True, "reason": "ok"},
            }
        state["n"] += 1
        payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
        rids = [r["rect_id"] for r in payload.get("rectangles") or []]
        return {
            "rectangle_layer_map": {rid: "L1" for rid in rids},
            "layer_order": ["L1"],
            "group_label_decls": [],
            "dimension_membership": [],
            "header_completeness": "complete",
        }

    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat, run_id="t5", clock=lambda: "T5"
    )
    assert out["status"] == "ok"
    assert set(out["layer_result"]["layer_order"]) == {"GA", "GB"}


def test_t6_merge_passed_false(tmp_path, monkeypatch):
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()
    merges = {"n": 0}

    def chat(prompt: str):
        if "layer merge" in prompt:
            merges["n"] += 1
            return {"passed": False, "reason": "cannot bind", "overall_sanity": {"passed": True, "reason": "n/a"}}
        payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
        rids = [r["rect_id"] for r in payload.get("rectangles") or []]
        return {
            "rectangle_layer_map": {rid: "L0" for rid in rids},
            "layer_order": ["L0"],
            "group_label_decls": [],
            "dimension_membership": [],
            "header_completeness": "complete",
        }

    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat, run_id="t6", clock=lambda: "T6"
    )
    assert out["status"] == "conflict"
    assert "cross_slice_layer_unresolved" in out["reason_codes"]
    assert merges["n"] == 1


def test_t7_header_rejudge_incomplete(tmp_path, monkeypatch):
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()

    def chat(prompt: str):
        if "layer merge" in prompt:
            rects = project_upstream_rectangles(block, draft)
            return {
                "passed": True,
                "reason": "ok",
                "global_rectangle_layer_map": {r["rect_id"]: "G0" for r in rects},
                "global_layer_order": ["G0"],
                "group_label_decls": [],
                "dimension_membership": [],
                "header_completeness": "incomplete",
                "overall_sanity": {"passed": True, "reason": "ok"},
            }
        payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
        rids = [r["rect_id"] for r in payload.get("rectangles") or []]
        return {
            "rectangle_layer_map": {rid: "L0" for rid in rids},
            "layer_order": ["L0"],
            "group_label_decls": [],
            "dimension_membership": [],
            "header_completeness": "complete",
        }

    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat, run_id="t7", clock=lambda: "T7"
    )
    assert out["status"] == "review_failed"


def test_t8_slice_count_exceeds_budget(tmp_path, monkeypatch):
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 2
    )
    _force_n_slices(monkeypatch, 3)
    block, draft, tables = _many_rects_block(20), _many_rects_draft(), _many_rects_tables(22)
    calls = {"n": 0}

    def chat(prompt: str):
        calls["n"] += 1
        return {}

    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat, run_id="t8", clock=lambda: "T8"
    )
    assert out["status"] == "conflict"
    assert "propose_slice_count_exceeds_ask_budget" in out["reason_codes"]
    assert calls["n"] == 0


def test_t9_trace_per_slice_and_merge(tmp_path, monkeypatch):
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()

    def chat(prompt: str):
        if "layer merge" in prompt:
            rects = project_upstream_rectangles(block, draft)
            return {
                "passed": True,
                "reason": "ok",
                "global_rectangle_layer_map": {r["rect_id"]: "G0" for r in rects},
                "global_layer_order": ["G0"],
                "group_label_decls": [],
                "dimension_membership": [],
                "header_completeness": "complete",
                "overall_sanity": {"passed": True, "reason": "ok"},
            }
        payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
        rids = [r["rect_id"] for r in payload.get("rectangles") or []]
        return {
            "rectangle_layer_map": {rid: "L0" for rid in rids},
            "layer_order": ["L0"],
            "group_label_decls": [],
            "dimension_membership": [],
            "header_completeness": "complete",
        }

    run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat, run_id="t9", clock=lambda: "T9"
    )
    found = list_traces_for_block("t9", 0, trace_dir=trace_dir)
    slices = [x for x in found if x["meta"]["call_kind"] == KIND_PROPOSE_SLICE]
    merges = [x for x in found if x["meta"]["call_kind"] == KIND_MERGE_LAYERS]
    assert len(slices) >= 2
    assert len(merges) == 1
    body0 = load_trace_file(slices[0]["path"])
    assert body0.get("slice_index") == 1
    assert "prompt" in str(body0.get("prompt_full") or "") or body0.get("prompt_chars", 0) > 0


def test_t10_no_disjoint_in_prompt(tmp_path, monkeypatch):
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        if "layer recognition" in prompt:
            assert '"relation": "disjoint"' not in prompt
            return _good_proposal()
        return {"passed": True, "reason": "ok"}

    out = run_layer_recognize_block(
        _block_small(),
        _draft_small(),
        _tables_small(),
        {},
        "u",
        100_000,
        chat=chat,
        run_id="t10",
        clock=lambda: "T10",
    )
    assert out["status"] == "ok"


def test_t11_first_slice_fail_stops(tmp_path, monkeypatch):
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()
    calls = {"n": 0}

    def chat(prompt: str):
        if "layer recognition" in prompt:
            calls["n"] += 1
            return {"answer": "x"}
        calls["n"] += 1
        return {}

    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat, run_id="t11", clock=lambda: "T11"
    )
    assert out["status"] == "conflict"
    assert calls["n"] == 1


def test_t12_single_rect_over_capacity(tmp_path, monkeypatch):
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize_limits."
        "read_layer_recognize_propose_input_max_chars",
        lambda contract_data=None: 50,
    )
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize_slice."
        "read_layer_recognize_propose_input_max_chars",
        lambda contract_data=None: 50,
    )
    block, draft, tables = _many_rects_block(3), _many_rects_draft(), _many_rects_tables(5)
    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=lambda p: {}, run_id="t12", clock=lambda: "T12"
    )
    assert out["status"] == "conflict"
    assert "propose_single_rect_over_capacity" in out["reason_codes"]


def test_t13_overall_sanity_false(tmp_path, monkeypatch):
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()

    def chat(prompt: str):
        if "layer merge" in prompt:
            rects = project_upstream_rectangles(block, draft)
            return {
                "passed": True,
                "reason": "ok",
                "global_rectangle_layer_map": {r["rect_id"]: "G0" for r in rects},
                "global_layer_order": ["G0"],
                "group_label_decls": [],
                "dimension_membership": [],
                "header_completeness": "complete",
                "overall_sanity": {"passed": False, "reason": "order weird"},
            }
        payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
        rids = [r["rect_id"] for r in payload.get("rectangles") or []]
        return {
            "rectangle_layer_map": {rid: "L0" for rid in rids},
            "layer_order": ["L0"],
            "group_label_decls": [],
            "dimension_membership": [],
            "header_completeness": "complete",
        }

    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat, run_id="t13", clock=lambda: "T13"
    )
    assert out["status"] == "review_failed"
    assert "order weird" in out["rejection_reason"]


def test_t13b_overall_sanity_reason_empty(tmp_path, monkeypatch):
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()

    def chat(prompt: str):
        if "layer merge" in prompt:
            rects = project_upstream_rectangles(block, draft)
            return {
                "passed": True,
                "reason": "ok",
                "global_rectangle_layer_map": {r["rect_id"]: "G0" for r in rects},
                "global_layer_order": ["G0"],
                "group_label_decls": [],
                "dimension_membership": [],
                "header_completeness": "complete",
                "overall_sanity": {"passed": True, "reason": ""},
            }
        payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
        rids = [r["rect_id"] for r in payload.get("rectangles") or []]
        return {
            "rectangle_layer_map": {rid: "L0" for rid in rids},
            "layer_order": ["L0"],
            "group_label_decls": [],
            "dimension_membership": [],
            "header_completeness": "complete",
        }

    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat, run_id="t13b", clock=lambda: "T13b"
    )
    assert out["status"] == "conflict"
    assert "cross_slice_merge_invalid" in out["reason_codes"]


def test_t14_layer_summaries_cover_layers():
    rects = [
        {
            "rect_id": "a",
            "kind": "merge",
            "locator": {"r0": 0, "c0": 0, "r1": 1, "c1": 1},
            "back_ref": {},
        },
        {
            "rect_id": "b",
            "kind": "merge",
            "locator": {"r0": 1, "c0": 0, "r1": 2, "c1": 1},
            "back_ref": {},
        },
    ]
    texts = {
        "a": [{"r": 0, "c": 0, "text": "账号"}],
        "b": [{"r": 1, "c": 0, "text": ""}],
    }
    summaries = build_layer_summaries_for_merge(
        layer_order=["L0", "L1"],
        rectangle_layer_map={"a": "L0", "b": "L1"},
        window_rects=rects,
        texts_all=texts,
    )
    assert {s["layer_id"] for s in summaries} == {"L0", "L1"}
    l0 = [s for s in summaries if s["layer_id"] == "L0"][0]
    assert 1 <= len(l0["sample_texts"]) <= 3
    l1 = [s for s in summaries if s["layer_id"] == "L1"][0]
    # empty after fallback
    if not l1["sample_texts"]:
        assert l1.get("empty_text_ratio") == 1.0


def test_t15a_trim_level_then_merge(tmp_path, monkeypatch):
    """T15a：裁一级后可合并。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()

    # force trim by tiny review capacity that still allows after level1
    # Use monkeypatch on trim to simulate level>=1 then success
    real_trim = trim_merge_input_to_capacity

    def trim_once(merge_input, capacity):
        cur, level = real_trim(merge_input, max(capacity, 10**9))
        cur2 = json.loads(json.dumps(cur, ensure_ascii=False, default=str))
        # fake that we applied level 1
        return cur2, max(level, 1)

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize_slice.trim_merge_input_to_capacity",
        trim_once,
    )
    merges = {"n": 0}

    def chat(prompt: str):
        if "layer merge" in prompt:
            merges["n"] += 1
            rects = project_upstream_rectangles(block, draft)
            return {
                "passed": True,
                "reason": "ok",
                "global_rectangle_layer_map": {r["rect_id"]: "G0" for r in rects},
                "global_layer_order": ["G0"],
                "group_label_decls": [],
                "dimension_membership": [],
                "header_completeness": "complete",
                "overall_sanity": {"passed": True, "reason": "ok"},
            }
        payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
        rids = [r["rect_id"] for r in payload.get("rectangles") or []]
        return {
            "rectangle_layer_map": {rid: "L0" for rid in rids},
            "layer_order": ["L0"],
            "group_label_decls": [],
            "dimension_membership": [],
            "header_completeness": "complete",
        }

    out = run_layer_recognize_block(
        block,
        draft,
        tables,
        {},
        "u",
        100_000,
        chat=chat,
        run_id="t15a",
        clock=lambda: "T15a",
    )
    assert out["status"] == "ok"
    assert merges["n"] == 1
    merge_body = load_trace_file(
        [
            x
            for x in list_traces_for_block("t15a", 0, trace_dir=trace_dir)
            if x["meta"]["call_kind"] == KIND_MERGE_LAYERS
        ][0]["path"]
    )
    assert int(merge_body.get("merge_input_trim_level") or 0) >= 1


def test_t15b_trim_exhausted_no_merge(tmp_path, monkeypatch):
    _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    _force_n_slices(monkeypatch, 2)
    block, draft, tables = _many_rects_block(), _many_rects_draft(), _many_rects_tables()

    def boom_trim(merge_input, capacity):
        raise CrossTableError(
            "cross_table_proposal_invalid",
            "cross_slice_merge_input_over_capacity",
            {"reason_codes": ["cross_slice_merge_input_over_capacity"]},
        )

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize_slice.trim_merge_input_to_capacity",
        boom_trim,
    )
    merges = {"n": 0}

    def chat(prompt: str):
        if "layer merge" in prompt:
            merges["n"] += 1
            return {}
        payload = json.loads(prompt.split("A' layer recognition. ", 1)[1])
        rids = [r["rect_id"] for r in payload.get("rectangles") or []]
        return {
            "rectangle_layer_map": {rid: "L0" for rid in rids},
            "layer_order": ["L0"],
            "group_label_decls": [],
            "dimension_membership": [],
            "header_completeness": "complete",
        }

    out = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100, chat=chat, run_id="t15b", clock=lambda: "T15b"
    )
    assert out["status"] == "conflict"
    assert "cross_slice_merge_input_over_capacity" in out["reason_codes"]
    assert merges["n"] == 0


def test_filter_drops_disjoint():
    facts = [
        {"a": "1", "b": "2", "relation": "contains"},
        {"a": "1", "b": "3", "relation": "disjoint"},
        {"a": "2", "b": "3", "relation": "overlap"},
    ]
    out = filter_containment_facts_for_propose(
        facts, keep=frozenset({"contains", "overlap", "equal"})
    )
    assert all(f["relation"] != "disjoint" for f in out)
    assert len(out) == 2
