# -*- coding: utf-8 -*-
"""A' 收尾 9 项：分路径 / 后患1 / 回调 / 【16】【18】【19】/ 预留 / 空层 / limits。"""
from __future__ import annotations

import copy

import pytest

from pipeline.block_cross_table.api import bind_chat_json
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.layer_recognize import (
    assert_ok_layer_fields_complete,
    build_ok_layer_result,
    compute_containment_facts,
    project_upstream_rectangles,
    run_layer_recognize_block,
    validate_layer_proposal,
)
from pipeline.block_cross_table.layer_recognize_limits import (
    ASK_MAX_ROUNDS_KEY,
    read_layer_recognize_ask_max_rounds,
)


def _block():
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


def _draft():
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


def _tables():
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


def _good_proposal(header="complete"):
    rects = project_upstream_rectangles(_block(), _draft())
    mapping = {
        r["rect_id"]: ("L0" if r["rect_id"] == "merge:0" else "L1") for r in rects
    }
    prop = {
        "rectangle_layer_map": mapping,
        "layer_order": ["L0", "L1"],
        "group_label_decls": [
            {"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}
        ],
        "dimension_membership": [],
        "header_completeness": header,
    }
    return prop


def _chat_ok(header="complete"):
    prop = _good_proposal(header)

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": True, "reason": "ok"}

    return chat


# --- 1. 后续-9：分路径 ---


def test_wrap_review_failed_vs_conflict_paths(monkeypatch):
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    block, draft, tables = _block(), _draft(), _tables()

    def chat_review_fail(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal("complete")
        return {"passed": False, "reason": "model_rejects_layers"}

    rf = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat_review_fail
    )
    assert rf["status"] == "review_failed"
    assert rf["rejection_reason"] == "model_rejects_layers"
    assert rf["layer_result_empty"] is True
    assert rf["draft_retained"] is True
    assert isinstance(rf["ask_rounds_used"], int)

    def chat_conflict(_prompt: str):
        return {"rectangle_layer_map": {}, "layer_order": []}

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 2
    )
    cf = run_layer_recognize_block(
        block, draft, tables, {}, "u", 100_000, chat=chat_conflict
    )
    assert cf["status"] == "conflict"
    assert cf["status"] != rf["status"]
    assert "ask_budget_exhausted_proposal" in cf["reason_codes"]
    assert cf["layer_result_empty"] is True
    assert cf["draft_retained"] is True
    assert cf["layer_result"] is None


# --- 2. 后患 1：表头判断为否 → review_failed ---


@pytest.mark.parametrize("header", ["incomplete", "not_header"])
def test_wrap_header_incomplete_maps_review_failed(monkeypatch, header):
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal(header)
        # 正式复核通过，仍不得成功
        return {"passed": True, "reason": "structure_ok"}

    out = run_layer_recognize_block(
        _block(), _draft(), _tables(), {}, "u", 100_000, chat=chat
    )
    assert out["status"] == "review_failed"
    assert out["status"] != "ok"
    assert ("header_completeness=%s" % header) in out["rejection_reason"]
    assert "structure_ok" in out["rejection_reason"]


# --- 3. 【11】(b) 回调缺省 ---


def test_wrap_callback_missing_matrix_input_invalid(monkeypatch):
    bind_chat_json(None)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    with pytest.raises(CrossTableError) as ei:
        run_layer_recognize_block(
            _block(), _draft(), _tables(), {}, "u", 100_000, chat=None
        )
    assert ei.value.code == "cross_table_input_invalid"
    assert "chat client is not bound" in ei.value.message


def test_wrap_callback_missing_no_structure_ok():
    bind_chat_json(None)
    out = run_layer_recognize_block(
        {"block_id": 9, "layout": "ordered", "parent_table_ids": ["t0"], "context": {}},
        None,
        {"t0": {"grid": [["a"]]}},
        None,
        "u",
        100_000,
        chat=None,
    )
    assert out["status"] == "no_structure"


# --- 4. 【16】三分类耗尽 ---


def test_wrap_ask_budget_exhausted_proposal(monkeypatch):
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 2
    )

    def chat(_p: str):
        return {"rectangle_layer_map": {}, "layer_order": []}

    out = run_layer_recognize_block(
        _block(), _draft(), _tables(), {}, "u", 100_000, chat=chat
    )
    assert out["status"] == "conflict"
    assert out["reason_codes"] == ["ask_budget_exhausted_proposal"]
    assert "内部追问用尽" not in "".join(out["reason_codes"])
    assert out["ask_usage"]["proposal"] == 1
    assert out["ask_usage"]["review"] == 0


def test_wrap_ask_budget_exhausted_subset(monkeypatch):
    """提案成功后进入要子集，预留正式复核失败 → ask_budget_exhausted_subset。"""
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 2
    )

    def always_over(payload, capacity):
        raise CrossTableError(
            "cross_table_proposal_invalid",
            "review input over capacity",
            {"size": 9999, "capacity": capacity},
        )

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize.assert_review_within_capacity",
        always_over,
    )
    prop = _good_proposal()

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"review_locator_ids": ["merge:0"]}

    # capacity 足够装 shrink 请求体；正式复核被 always_over 挡住
    out = run_layer_recognize_block(
        _block(), _draft(), _tables(), {}, "u", review_input_capacity=100_000, chat=chat
    )
    assert out["status"] == "conflict"
    assert "ask_budget_exhausted_subset" in out["reason_codes"]
    assert out["ask_usage"]["proposal"] == 1
    assert out["ask_usage"]["review"] == 0


def test_wrap_ask_budget_exhausted_review(monkeypatch):
    """提案成功后下调上限，正式复核前预算不足。"""
    state = {"max": 8}

    def ask():
        return state["max"]

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", ask
    )
    prop = _good_proposal()

    def chat(prompt: str):
        if "layer recognition" in prompt:
            state["max"] = 1  # 提案已用 1；复核前 1+1 > 1
            return prop
        return {"passed": True, "reason": "ok"}

    out = run_layer_recognize_block(
        _block(), _draft(), _tables(), {}, "u", 100_000, chat=chat
    )
    assert out["status"] == "conflict"
    assert out["reason_codes"] == ["ask_budget_exhausted_review"]
    assert out["ask_usage"]["proposal"] == 1
    assert out["ask_usage"]["review"] == 0


# --- 5. 【18】草案非空网格缺 ---


def test_wrap_draft_present_grid_missing_input_invalid():
    bind_chat_json(lambda _p: {})
    with pytest.raises(CrossTableError) as ei:
        run_layer_recognize_block(
            _block(),
            _draft(),
            {"t0": {}},  # 无 grid
            {},
            "u",
            100_000,
            chat=lambda _p: {},
        )
    assert ei.value.code == "cross_table_input_invalid"
    assert "grid missing" in ei.value.message


# --- 6. 【19】成功 8 字段 ---


def test_wrap_success_payload_complete_ok(monkeypatch):
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    out = run_layer_recognize_block(
        _block(), _draft(), _tables(), {}, "u", 100_000, chat=_chat_ok()
    )
    assert out["status"] == "ok"
    lr = out["layer_result"]
    assert assert_ok_layer_fields_complete(lr) == []
    for key in (
        "rect_to_layer",
        "layer_order",
        "layers",
        "subtable_boundaries",
        "nesting_facts",
        "group_label_declaration",
        "dimension_membership",
        "header_completeness",
    ):
        assert key in lr


@pytest.mark.parametrize(
    "drop_key",
    ["rect_to_layer", "layer_order", "header_completeness"],
)
def test_wrap_success_payload_incomplete_conflict(monkeypatch, drop_key):
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    prop = _good_proposal()

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": True, "reason": "ok"}

    real_build = build_ok_layer_result

    def broken_build(validated):
        lr = real_build(validated)
        lr[drop_key] = None
        if drop_key == "rect_to_layer":
            lr["rectangle_layer_map"] = None
        if drop_key == "header_completeness":
            lr.pop("header_completeness", None)
            lr["header_completeness"] = None
        return lr

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize.build_ok_layer_result",
        broken_build,
    )
    out = run_layer_recognize_block(
        _block(), _draft(), _tables(), {}, "u", 100_000, chat=chat
    )
    assert out["status"] == "conflict"
    assert "success_payload_incomplete" in out["reason_codes"]


def test_wrap_dimension_membership_missing_vs_empty_list_at_ok_check(monkeypatch):
    """【19】边界：dimension_membership 键缺失/None → incomplete；空列表 → 不算缺失。

    与任务 8（validate 内 layer_ids=[] 合法）不同：本测专打 assert_ok_layer_fields_complete。
    """
    base = {
        "rect_to_layer": {"a": "L0"},
        "layer_order": ["L0"],
        "layers": [],
        "subtable_boundaries": [],
        "nesting_facts": [],
        "group_label_declaration": [],
        "header_completeness": "complete",
    }
    empty_ok = dict(base)
    empty_ok["dimension_membership"] = []
    assert assert_ok_layer_fields_complete(empty_ok) == []

    missing_key = dict(base)
    assert "dimension_membership" in assert_ok_layer_fields_complete(missing_key)

    none_val = dict(base)
    none_val["dimension_membership"] = None
    assert "dimension_membership" in assert_ok_layer_fields_complete(none_val)

    # 经编排入口：键缺失 → conflict / success_payload_incomplete
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    prop = _good_proposal()

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return prop
        return {"passed": True, "reason": "ok"}

    real_build = build_ok_layer_result

    def drop_dim(validated):
        lr = real_build(validated)
        del lr["dimension_membership"]
        return lr

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize.build_ok_layer_result",
        drop_dim,
    )
    out = run_layer_recognize_block(
        _block(), _draft(), _tables(), {}, "u", 100_000, chat=chat
    )
    assert out["status"] == "conflict"
    assert "success_payload_incomplete" in out["reason_codes"]


# --- 7. 预留策略 ---


def test_wrap_reservation_stops_before_eating_review_slot(monkeypatch):
    """ask=1：剩余不足以提案+预留复核 → 立即冲突，review 次数为 0。"""
    calls = {"n": 0}

    def chat(_p: str):
        calls["n"] += 1
        return _good_proposal()

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 1
    )
    out = run_layer_recognize_block(
        _block(), _draft(), _tables(), {}, "u", 100_000, chat=chat
    )
    assert out["status"] == "conflict"
    assert out["reason_codes"] == ["ask_budget_exhausted_proposal"]
    assert calls["n"] == 0
    assert out["ask_usage"]["review"] == 0
    assert out["ask_usage"]["total"] == 0


# --- 8. 后续-7 空层列表 ---


def test_wrap_empty_dim_layer_ids_legal():
    draft = _draft()
    rects = project_upstream_rectangles(_block(), draft)
    facts = compute_containment_facts(rects)
    mapping = {
        r["rect_id"]: ("L0" if r["rect_id"] == "merge:0" else "L1") for r in rects
    }
    out = validate_layer_proposal(
        rects,
        facts,
        {
            "rectangle_layer_map": mapping,
            "layer_order": ["L0", "L1"],
            "group_label_decls": [
                {"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}
            ],
            "dimension_membership": [
                {
                    "dimension_field_id": "f1",
                    "subtable_id": "0",
                    "layer_ids": [],
                }
            ],
            "header_completeness": "complete",
        },
        draft,
        {0: ["f1"]},
    )
    assert out["ok"] is True
    assert out["dimension_membership"][0]["layer_ids"] == []
    assert not any("dim_layers_missing" in r for r in (out.get("reasons") or []))


# --- 9. limits 方案甲 ---


def test_wrap_limits_reader_used_not_old_key(monkeypatch):
    """业务走真实 _ask_max_rounds → limits 读取方；旧键不被读。"""
    seen = {"old": 0, "new": 0}

    def fake_get_param(key, default=None):
        if key == "cross_table.user_retry_max_rounds":
            seen["old"] += 1
            return 99
        return default

    monkeypatch.setattr("config.get_param", fake_get_param)

    def tracking_read(contract_data=None):
        seen["new"] += 1
        return 8

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize_limits.read_layer_recognize_ask_max_rounds",
        tracking_read,
    )
    out = run_layer_recognize_block(
        _block(), _draft(), _tables(), {}, "u", 100_000, chat=_chat_ok()
    )
    assert out["status"] == "ok"
    assert seen["old"] == 0
    assert seen["new"] >= 1


def test_wrap_limits_missing_key_contract_invalid():
    data = copy.deepcopy(
        __import__(
            "pipeline.synth_contract.registry", fromlist=["load_contract"]
        ).load_contract().data
    )
    del data["cross_table"][ASK_MAX_ROUNDS_KEY]
    with pytest.raises(CrossTableError) as ei:
        read_layer_recognize_ask_max_rounds(data)
    assert ei.value.code == "cross_table_contract_invalid"


def test_wrap_limits_zero_contract_invalid():
    data = copy.deepcopy(
        __import__(
            "pipeline.synth_contract.registry", fromlist=["load_contract"]
        ).load_contract().data
    )
    data["cross_table"][ASK_MAX_ROUNDS_KEY] = 0
    with pytest.raises(CrossTableError) as ei:
        read_layer_recognize_ask_max_rounds(data)
    assert ei.value.code == "cross_table_contract_invalid"
