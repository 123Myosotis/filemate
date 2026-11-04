# -*- coding: utf-8 -*-
"""Prompt 输出规范增强：信封检测（不剥离）+ 裸 JSON 通过 + payload 含 contract。"""
from __future__ import annotations

import json

from pipeline.block_cross_table.layer_recognize import (
    project_upstream_rectangles,
    propose_layer_recognition,
    validate_layer_proposal,
)
from pipeline.block_cross_table.layer_recognize_slice import (
    LAYER_RECOGNIZE_JSON_SYSTEM,
    PROPOSE_PREFIX,
    PROPOSE_TOP_LEVEL_KEYS,
    build_merge_input,
    build_propose_payload,
    is_illicit_merge_envelope,
    is_illicit_propose_envelope,
    merge_output_contract,
    propose_output_contract,
    validate_merge_result,
)
from pipeline.block_cross_table.api import bind_chat_json


def _rects_minimal():
    return [
        {
            "rect_id": "rect_1",
            "kind": "merge",
            "locator": {"r0": 0, "c0": 0, "r1": 1, "c1": 2},
            "back_ref": {},
        },
        {
            "rect_id": "rect_2",
            "kind": "group_label",
            "locator": {"r0": 0, "c0": 0, "r1": 1, "c1": 2},
            "back_ref": {"field_id": "f0"},
        },
    ]


def _draft_minimal():
    return {
        "block_id": 0,
        "subtable_ids": [0],
        "fields": [
            {
                "field_id": "f0",
                "role": "group",
                "subtable_id": 0,
                "segments": [
                    {"group_index": 0, "col_range": [0, 2], "value_rows": [0]},
                ],
            }
        ],
    }


def _bare_ok(rects):
    return {
        "rectangle_layer_map": {r["rect_id"]: "L1" for r in rects},
        "layer_order": ["L1"],
        "group_label_decls": [
            {"field_id": "f0", "kind": "exact_one_layer", "layer_ids": ["L1"]}
        ],
        "dimension_membership": [],
        "header_completeness": "complete",
    }


def test_illicit_envelope_answer_dict_detected():
    inner = _bare_ok(_rects_minimal())
    wrapped = {"answer": inner}
    assert is_illicit_propose_envelope(wrapped) is True
    out = validate_layer_proposal(
        _rects_minimal(), [], wrapped, _draft_minimal(), {}
    )
    assert out["ok"] is False
    assert out["reasons"] == ["envelope_wrapper_forbidden"]
    # Policy A: still wrapped; validate must not unwrap into success
    assert "rectangle_layer_map" not in wrapped or wrapped.get("rectangle_layer_map") is None


def test_illicit_envelope_placeholder_string_detected():
    wrapped = {"answer": "Your answer here"}
    assert is_illicit_propose_envelope(wrapped) is True
    out = validate_layer_proposal(
        _rects_minimal(), [], wrapped, _draft_minimal(), {}
    )
    assert out["ok"] is False
    assert out["reasons"] == ["envelope_wrapper_forbidden"]


def test_bare_json_validate_passes():
    rects = _rects_minimal()
    bare = _bare_ok(rects)
    assert is_illicit_propose_envelope(bare) is False
    out = validate_layer_proposal(rects, [], bare, _draft_minimal(), {})
    assert out["ok"] is True


def test_propose_payload_contains_output_contract_and_example():
    payload = build_propose_payload(
        user_utterance="u",
        rectangles=_rects_minimal(),
        containment_facts=[],
        cell_texts={},
        draft=_draft_minimal(),
        dimensions_for_block={},
        subtable_boundaries=[],
    )
    oc = payload["output_contract"]
    assert set(oc["top_level_keys_exactly"]) == set(PROPOSE_TOP_LEVEL_KEYS)
    assert "answer" in oc["forbid_wrappers"]
    assert "rectangle_layer_map" in oc["example_legal_output"]
    assert "Do NOT wrap" in oc["system_rules"]
    assert "ILLUSTRATIVE" in oc["system_rules"]
    assert oc.get("example_is_illustrative_only") is True
    assert "mapping_rule" in oc
    assert "CRITICAL — Coverage requirement" in oc["system_rules"]
    assert "CRITICAL — Dimension coverage" in oc["system_rules"]
    assert "CRITICAL — Layer order rule" in oc["system_rules"]
    assert "CRITICAL — Group label semantic" in oc["system_rules"]
    assert "dimension_rule" in oc
    assert "contains_order_rule" in oc
    assert "group_label_rule" in oc
    assert "example_input_sketch" in oc
    assert "group_label_contrast" in oc
    assert "Do NOT nest" in payload["instructions"]
    assert "ILLUSTRATIVE ONLY" in payload["instructions"]
    prompt = PROPOSE_PREFIX + json.dumps(payload, ensure_ascii=False)
    assert '"output_contract"' in prompt
    assert "example_legal_output" in prompt
    assert "mapping_rule" in prompt
    assert "CRITICAL — Coverage requirement" in prompt


def test_propose_layer_recognition_prompt_has_contract(monkeypatch):
    captured = {}

    def chat(prompt: str):
        captured["prompt"] = prompt
        return _bare_ok(_rects_minimal())

    bind_chat_json(chat)
    prompt_out = {}
    propose_layer_recognition(
        "u",
        _rects_minimal(),
        [],
        {},
        _draft_minimal(),
        {},
        subtable_boundaries=[],
        prompt_out=prompt_out,
    )
    assert "output_contract" in prompt_out["prompt"]
    assert "example_legal_output" in prompt_out["prompt"]
    assert "Do NOT wrap" in prompt_out["prompt"]
    bind_chat_json(None)


def test_merge_output_contract_and_envelope():
    oc = merge_output_contract()
    assert "global_rectangle_layer_map" in oc["top_level_keys_exactly"]
    assert "Do NOT wrap" in oc["system_rules"]
    wrapped = {
        "answer": {
            "passed": True,
            "reason": "ok",
            "global_rectangle_layer_map": {},
            "global_layer_order": [],
            "group_label_decls": [],
            "dimension_membership": [],
            "header_completeness": "complete",
            "overall_sanity": {"passed": True, "reason": "x"},
        }
    }
    assert is_illicit_merge_envelope(wrapped) is True
    out = validate_merge_result(
        raw=wrapped,
        rectangles=_rects_minimal(),
        facts_all=[],
        draft=_draft_minimal(),
        dimensions_for_block={},
    )
    assert out["ok_status"] == "conflict"
    assert out["reason_codes"] == ["envelope_wrapper_forbidden"]


def test_json_system_constant_forbids_answer_wrapper():
    assert "answer" in LAYER_RECOGNIZE_JSON_SYSTEM
    assert "ONE JSON object" in LAYER_RECOGNIZE_JSON_SYSTEM
    assert "CRITICAL — Coverage requirement" in LAYER_RECOGNIZE_JSON_SYSTEM
    assert "CRITICAL — Dimension coverage" in LAYER_RECOGNIZE_JSON_SYSTEM
    assert "CRITICAL — Layer order rule" in LAYER_RECOGNIZE_JSON_SYSTEM
    assert "CRITICAL — Group label semantic" in LAYER_RECOGNIZE_JSON_SYSTEM
    assert "example_legal_output" in LAYER_RECOGNIZE_JSON_SYSTEM
    assert "group_label_contrast" in LAYER_RECOGNIZE_JSON_SYSTEM


def _rects_seventeen():
    """Abstract 17-rect set (mendian-scale coverage) — no business words."""
    rects = []
    for i in range(3):
        rects.append(
            {
                "rect_id": "merge:%d" % i,
                "kind": "merge",
                "locator": {"r0": 0, "c0": i * 3, "r1": 1, "c1": i * 3 + 3},
                "back_ref": {"merge_index": i},
            }
        )
    for i in range(3):
        rects.append(
            {
                "rect_id": "field:f0:%d" % i,
                "kind": "group_label",
                "locator": {"r0": 0, "c0": i * 3, "r1": 1, "c1": i * 3 + 3},
                "back_ref": {"field_id": "f0", "segment_index": i},
            }
        )
    for fid, role, nseg in (("f1", "attribute", 3), ("f2", "attribute", 3), ("f3", "list", 3)):
        for i in range(nseg):
            rects.append(
                {
                    "rect_id": "field:%s:%d" % (fid, i),
                    "kind": "field_segment",
                    "locator": {
                        "r0": 1 if fid != "f3" else 3,
                        "c0": i * 3,
                        "r1": 2 if fid != "f3" else 5,
                        "c1": i * 3 + 3,
                    },
                    "back_ref": {"field_id": fid, "segment_index": i},
                }
            )
    rects.append(
        {
            "rect_id": "field:f4:0",
            "kind": "field_segment",
            "locator": {"r0": 1, "c0": 12, "r1": 4, "c1": 13},
            "back_ref": {"field_id": "f4"},
        }
    )
    rects.append(
        {
            "rect_id": "field:f5:0",
            "kind": "field_segment",
            "locator": {"r0": 1, "c0": 13, "r1": 4, "c1": 14},
            "back_ref": {"field_id": "f5"},
        }
    )
    assert len(rects) == 17
    return rects


def _draft_seventeen():
    return {
        "block_id": 0,
        "subtable_ids": [0, 1],
        "fields": [
            {
                "field_id": "f0",
                "role": "group",
                "subtable_id": 0,
                "segments": [
                    {"group_index": i, "col_range": [i * 3, i * 3 + 3], "value_rows": [0]}
                    for i in range(3)
                ],
            },
            {
                "field_id": "f3",
                "role": "list",
                "subtable_id": 0,
                "segments": [
                    {"group_index": i, "col_range": [i * 3, i * 3 + 3], "value_rows": [3, 4]}
                    for i in range(3)
                ],
            },
            {
                "field_id": "f4",
                "role": "column",
                "subtable_id": 1,
                "segments": [{"group_index": 0, "col_range": [12, 13], "value_rows": [1, 2, 3]}],
            },
        ],
    }


def test_full_coverage_and_dim_membership_validate_passes():
    """Mock: all 17 rect_ids mapped + every dimension entry present → validate ok."""
    rects = _rects_seventeen()
    draft = _draft_seventeen()
    dims = {"0": ["f3"], "1": ["f4"]}
    proposal = {
        "rectangle_layer_map": {
            r["rect_id"]: ("L2" if r["rect_id"].startswith("field:f3:") else "L1")
            for r in rects
        },
        "layer_order": ["L1", "L2"],
        "group_label_decls": [
            {"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}
        ],
        "dimension_membership": [
            {"dimension_field_id": "f3", "subtable_id": "0", "layer_ids": ["L2"]},
            {"dimension_field_id": "f4", "subtable_id": "1", "layer_ids": []},
        ],
        "header_completeness": "complete",
    }
    out = validate_layer_proposal(rects, [], proposal, draft, dims)
    assert out["ok"] is True, out.get("reasons")
    assert not any(str(r).startswith("rect_unmapped") for r in (out.get("reasons") or []))
    assert not any(str(r).startswith("dim_unassigned") for r in (out.get("reasons") or []))


def test_missing_one_rect_still_fails_validate():
    """Mock: omit one field_segment → still rect_unmapped (validate rule unchanged)."""
    rects = _rects_seventeen()
    draft = _draft_seventeen()
    dims = {"0": ["f3"], "1": ["f4"]}
    mapping = {r["rect_id"]: "L1" for r in rects if r["rect_id"] != "field:f2:1"}
    proposal = {
        "rectangle_layer_map": mapping,
        "layer_order": ["L1"],
        "group_label_decls": [
            {"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}
        ],
        "dimension_membership": [
            {"dimension_field_id": "f3", "subtable_id": "0", "layer_ids": []},
            {"dimension_field_id": "f4", "subtable_id": "1", "layer_ids": []},
        ],
        "header_completeness": "complete",
    }
    out = validate_layer_proposal(rects, [], proposal, draft, dims)
    assert out["ok"] is False
    assert "rect_unmapped:field:f2:1" in (out.get("reasons") or [])
