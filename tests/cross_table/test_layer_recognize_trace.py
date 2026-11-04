# -*- coding: utf-8 -*-
"""后续-8：A' 三类轨迹落点 + 五元组 meta + 拒写路径。"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from pipeline.block_cross_table.errors import CrossTableError
from pipeline.block_cross_table.join_hypothesis import reset_trace_seq
from pipeline.block_cross_table.layer_recognize import (
    project_upstream_rectangles,
    run_layer_recognize_block,
)
from pipeline.block_cross_table.layer_recognize_trace import (
    KIND_ASK_SUBSET,
    KIND_FORMAL_REVIEW,
    KIND_PROPOSE,
    build_trace_meta,
    list_traces_for_block,
    rebuild_meta_from_trace_file,
    reset_block_id_registry,
    write_layer_recognize_trace,
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
    return {
        "rectangle_layer_map": mapping,
        "layer_order": ["L0", "L1"],
        "group_label_decls": [
            {"field_id": "f0", "kind": "not_a_layer", "layer_ids": []}
        ],
        "dimension_membership": [],
        "header_completeness": header,
    }


def _repo_trace_dir(tmp_path: Path) -> Path:
    """write_trace 对落盘路径做 relative_to(仓库根)；测目录必须在仓库内。"""
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


def _assert_quintuple(meta: dict, *, run_id: str, call_kind: str, entry_seq: int):
    for key in (
        "run_id",
        "block_id",
        "entry_seq",
        "call_kind",
        "kind_seq",
        "counts_toward_ask",
    ):
        assert key in meta, "missing meta key %s" % key
    assert meta["run_id"] == run_id
    assert meta["call_kind"] == call_kind
    assert meta["entry_seq"] == entry_seq
    assert isinstance(meta["kind_seq"], int) and meta["kind_seq"] >= 1
    assert meta["counts_toward_ask"] is True
    assert meta["block_id"] == "0"


# --- 1–3 + 5：三类落点 + 聚合检索 ---


def test_trace_three_kinds_land_and_aggregate_by_run_block(tmp_path, monkeypatch):
    """propose / ask_subset / formal_review 各至少一落点；按 (run_id, block_id) 聚合齐三类。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    assert_calls = {"n": 0}

    def capacity_once(payload, capacity):
        assert_calls["n"] += 1
        if assert_calls["n"] == 1:
            raise CrossTableError(
                "cross_table_proposal_invalid",
                "review input over capacity",
                {"size": 9999, "capacity": capacity},
            )
        return None

    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize.assert_review_within_capacity",
        capacity_once,
    )
    prop = _good_proposal()
    calls = {"review": 0}

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return prop
        calls["review"] += 1
        if calls["review"] == 1:
            return {"review_locator_ids": ["merge:0"]}
        return {"passed": True, "reason": "ok"}

    run_id = "run-trace-3kinds"
    out = run_layer_recognize_block(
        _block(),
        _draft(),
        _tables(),
        {},
        "u",
        100_000,
        chat=chat,
        run_id=run_id,
        entry_seq=1,
        clock=lambda: "20261020T120000",
    )
    assert out["status"] == "ok"
    assert out["ask_usage"]["proposal"] == 1
    assert out["ask_usage"]["subset"] == 1
    assert out["ask_usage"]["review"] == 1

    found = list_traces_for_block(run_id, 0, trace_dir=trace_dir)
    kinds = {item["meta"]["call_kind"] for item in found}
    assert kinds == {KIND_PROPOSE, KIND_ASK_SUBSET, KIND_FORMAL_REVIEW}

    for item in found:
        meta = rebuild_meta_from_trace_file(item["path"])
        _assert_quintuple(
            meta, run_id=run_id, call_kind=meta["call_kind"], entry_seq=1
        )
        assert meta["kind_seq"] == 1
        path = Path(item["path"])
        assert path.parent.resolve() == trace_dir.resolve()
        assert path.name.startswith(meta["call_kind"] + "_")
        assert path.stem.endswith("_20261020T120000")


def test_trace_propose_land_meta_quintuple(tmp_path, monkeypatch):
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        return {"passed": True, "reason": "ok"}

    run_id = "run-propose"
    run_layer_recognize_block(
        _block(),
        _draft(),
        _tables(),
        {},
        "u",
        100_000,
        chat=chat,
        run_id=run_id,
        entry_seq=1,
        clock=lambda: "TS1",
    )
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_PROPOSE
    ]
    assert len(items) == 1
    meta = rebuild_meta_from_trace_file(items[0]["path"])
    _assert_quintuple(meta, run_id=run_id, call_kind=KIND_PROPOSE, entry_seq=1)
    assert Path(items[0]["path"]).name.startswith("layer_propose_")


def test_trace_ask_subset_land_meta_quintuple(tmp_path, monkeypatch):
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
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

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        return {"review_locator_ids": ["merge:0"]}

    run_id = "run-subset"
    out = run_layer_recognize_block(
        _block(),
        _draft(),
        _tables(),
        {},
        "u",
        100_000,
        chat=chat,
        run_id=run_id,
        entry_seq=1,
        clock=lambda: "TS2",
    )
    assert out["status"] == "conflict"
    assert out["ask_usage"]["subset"] >= 1
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_ASK_SUBSET
    ]
    assert len(items) >= 1
    meta = rebuild_meta_from_trace_file(items[0]["path"])
    _assert_quintuple(meta, run_id=run_id, call_kind=KIND_ASK_SUBSET, entry_seq=1)
    assert meta["kind_seq"] == 1


def test_trace_formal_review_land_meta_quintuple(tmp_path, monkeypatch):
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        return {"passed": True, "reason": "ok"}

    run_id = "run-review"
    out = run_layer_recognize_block(
        _block(),
        _draft(),
        _tables(),
        {},
        "u",
        100_000,
        chat=chat,
        run_id=run_id,
        entry_seq=1,
        clock=lambda: "TS3",
    )
    assert out["status"] == "ok"
    items = [
        x
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_FORMAL_REVIEW
    ]
    assert len(items) == 1
    meta = rebuild_meta_from_trace_file(items[0]["path"])
    _assert_quintuple(meta, run_id=run_id, call_kind=KIND_FORMAL_REVIEW, entry_seq=1)


# --- 6：kind_seq 从 1；新 entry_seq 重置 ---


def test_kind_seq_starts_at_1_and_resets_on_new_entry_seq(tmp_path, monkeypatch):
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        return {"passed": True, "reason": "ok"}

    run_id = "run-kind-seq"
    for entry in (1, 2):
        out = run_layer_recognize_block(
            _block(),
            _draft(),
            _tables(),
            {},
            "u",
            100_000,
            chat=chat,
            run_id=run_id,
            entry_seq=entry,
            clock=lambda: "TSE%s" % entry,
        )
        assert out["status"] == "ok"

    found = list_traces_for_block(run_id, 0, trace_dir=trace_dir)
    by_entry_kind = {}
    for item in found:
        meta = rebuild_meta_from_trace_file(item["path"])
        key = (meta["entry_seq"], meta["call_kind"])
        by_entry_kind.setdefault(key, []).append(meta["kind_seq"])

    assert by_entry_kind[(1, KIND_PROPOSE)] == [1]
    assert by_entry_kind[(2, KIND_PROPOSE)] == [1]
    assert by_entry_kind[(1, KIND_FORMAL_REVIEW)] == [1]
    assert by_entry_kind[(2, KIND_FORMAL_REVIEW)] == [1]


def test_kind_seq_increments_within_same_entry(tmp_path, monkeypatch):
    """同一 entry 内提案重试：kind_seq 1,2。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )
    prop_bad = _good_proposal()
    prop_bad["layer_order"] = []  # 校验失败
    prop_ok = _good_proposal()
    state = {"n": 0}

    def chat(prompt: str):
        if "layer recognition" in prompt:
            state["n"] += 1
            return prop_bad if state["n"] == 1 else prop_ok
        return {"passed": True, "reason": "ok"}

    run_id = "run-kind-inc"
    out = run_layer_recognize_block(
        _block(),
        _draft(),
        _tables(),
        {},
        "u",
        100_000,
        chat=chat,
        run_id=run_id,
        entry_seq=1,
        clock=lambda: "INC",
    )
    assert out["status"] == "ok"
    proposes = [
        rebuild_meta_from_trace_file(x["path"])
        for x in list_traces_for_block(run_id, 0, trace_dir=trace_dir)
        if x["meta"]["call_kind"] == KIND_PROPOSE
    ]
    assert [m["kind_seq"] for m in proposes] == [1, 2]


# --- 4：拒写路径 ---


def test_reject_missing_run_id_no_write(tmp_path, monkeypatch):
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    before = list(trace_dir.glob("*.json"))
    with pytest.raises(CrossTableError) as ei:
        write_layer_recognize_trace(
            call_kind=KIND_PROPOSE,
            run_id="",
            block_id=0,
            entry_seq=1,
            kind_seq=1,
            clock=lambda: "T",
        )
    assert ei.value.code == "cross_table_audit_structure_invalid"
    assert list(trace_dir.glob("*.json")) == before


def test_reject_missing_call_kind_no_write(tmp_path, monkeypatch):
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    before = list(trace_dir.glob("*.json"))
    with pytest.raises(CrossTableError) as ei:
        write_layer_recognize_trace(
            call_kind="",
            run_id="r",
            block_id=0,
            entry_seq=1,
            kind_seq=1,
            clock=lambda: "T",
        )
    assert ei.value.code == "cross_table_audit_structure_invalid"
    assert list(trace_dir.glob("*.json")) == before


def test_reject_call_kind_not_in_enum_no_write(tmp_path, monkeypatch):
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    before = list(trace_dir.glob("*.json"))
    with pytest.raises(CrossTableError) as ei:
        write_layer_recognize_trace(
            call_kind="not_a_layer_kind",
            run_id="r",
            block_id=0,
            entry_seq=1,
            kind_seq=1,
            clock=lambda: "T",
        )
    assert ei.value.code == "cross_table_audit_structure_invalid"
    assert "not in layer_recognize_kinds" in ei.value.message
    assert list(trace_dir.glob("*.json")) == before


def test_reject_invalid_meta_shape_no_write(tmp_path, monkeypatch):
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    before = list(trace_dir.glob("*.json"))
    with pytest.raises(CrossTableError) as ei:
        build_trace_meta(
            run_id="r",
            block_id=0,
            entry_seq=0,
            call_kind=KIND_PROPOSE,
            kind_seq=1,
        )
    assert ei.value.code == "cross_table_audit_structure_invalid"
    with pytest.raises(CrossTableError) as ei2:
        build_trace_meta(
            run_id="r",
            block_id=True,
            entry_seq=1,
            call_kind=KIND_PROPOSE,
            kind_seq=1,
        )
    assert ei2.value.code == "cross_table_audit_structure_invalid"
    assert list(trace_dir.glob("*.json")) == before


def test_reject_block_id_collision_no_write(tmp_path, monkeypatch):
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    write_layer_recognize_trace(
        call_kind=KIND_PROPOSE,
        run_id="collide",
        block_id=1,
        entry_seq=1,
        kind_seq=1,
        clock=lambda: "C1",
    )
    before = list(trace_dir.glob("*.json"))
    with pytest.raises(CrossTableError) as ei:
        write_layer_recognize_trace(
            call_kind=KIND_FORMAL_REVIEW,
            run_id="collide",
            block_id="1",  # 不同原始值，归一相同
            entry_seq=1,
            kind_seq=1,
            clock=lambda: "C2",
        )
    assert ei.value.code == "cross_table_audit_structure_invalid"
    assert "collision" in ei.value.message
    assert list(trace_dir.glob("*.json")) == before


def test_no_run_id_skips_trace_legacy_compat(tmp_path, monkeypatch):
    """未传 run_id：不落点，保持旧测行为。"""
    trace_dir = _patch_trace_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pipeline.block_cross_table.layer_recognize._ask_max_rounds", lambda: 8
    )

    def chat(prompt: str):
        if "layer recognition" in prompt:
            return _good_proposal()
        return {"passed": True, "reason": "ok"}

    out = run_layer_recognize_block(
        _block(), _draft(), _tables(), {}, "u", 100_000, chat=chat
    )
    assert out["status"] == "ok"
    assert list(trace_dir.glob("*.json")) == []
